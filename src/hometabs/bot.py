"""Telegram layer: routes allowlisted chats to per-chat BrowsingAgent sessions."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from loguru import logger
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import BrowsingAgent, Image, ToolStep, TurnResult, instructions_fingerprint
from .config import Settings
from .formatting import SAFE_CHUNK_LEN, TELEGRAM_MAX_LEN, markdown_to_telegram_html, split_message
from .images import (
    MAX_IMAGES_PER_TURN,
    TELEGRAM_DOWNLOAD_LIMIT,
    ImageError,
    normalize_image,
    per_image_budget,
    too_large_message,
)
from .models import SDK_EFFORT_LEVELS, ModelCatalog, ModelInfo, resolve_effort
from .skills import build_plugin

MAX_PHOTOS_PER_TURN = 5
STATUS_EDIT_INTERVAL_S = 2.0
TYPING_INTERVAL_S = 4.0
# Album items arrive as separate updates; wait this long after the last one.
ALBUM_DEBOUNCE_S = 1.2
DEFAULT_IMAGE_PROMPT = (
    "The user sent this image without a caption. "
    "Describe what you see and ask what they want to do with it."
)
DEFAULT_ALBUM_PROMPT = (
    "The user sent these images without a caption. "
    "Describe what you see and ask what they want to do with them."
)
UNSUPPORTED_TEXT = "I can handle text and images (photos or image files) for now."
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

HELP_TEXT = (
    "I'm a browsing assistant driving your Aside browser.\n\n"
    'Just tell me what to do, e.g. "open example.com and tell me the heading". '
    "You can also send photos or screenshots, with a caption saying what to do.\n\n"
    "/new - start a fresh conversation\n"
    "/stop - interrupt the current task (and drop queued messages)\n"
    "/model - show or change the Claude model\n"
    "/effort - show or change the reasoning effort\n\n"
    "I'll ask before doing anything irreversible (purchases, sending messages, "
    "submitting forms, deleting)."
)

BOT_COMMANDS = [
    BotCommand("start", "Help"),
    BotCommand("new", "Start a fresh conversation"),
    BotCommand("stop", "Interrupt the current task"),
    BotCommand("model", "Show or change the Claude model"),
    BotCommand("effort", "Show or change the reasoning effort"),
]
CALLBACK_DATA_MAX_BYTES = 64  # Telegram's limit for inline button data
# Loose check for /model <id> when the model list is unavailable.
_MODEL_ID_RE = re.compile(r"^claude-[A-Za-z0-9._\[\]-]{1,100}$")


# ── Preferences (model / effort) ────────────────────────────────────


class PreferenceStore:
    """Bot-wide model and effort choice, persisted so restarts keep it.

    ``effort`` is the preferred level; the level actually used may be lower
    (or omitted) when the current model doesn't support it.
    """

    def __init__(self, path: Path, model: str, effort: str | None) -> None:
        self.path = path
        self.model = model
        self.effort = effort
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable settings file {}: {}", path, exc)
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if isinstance(raw.get("model"), str) and raw["model"]:
            self.model = raw["model"]
        if "effort" in raw and (raw["effort"] is None or raw["effort"] in SDK_EFFORT_LEVELS):
            self.effort = raw["effort"]

    def update(self, **changes: str | None) -> None:
        for key, value in changes.items():
            if key not in ("model", "effort"):
                raise KeyError(key)
            setattr(self, key, value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"model": self.model, "effort": self.effort}, indent=2))
        tmp.replace(self.path)


def model_callback_data(index: int, model_id: str) -> str:
    """``m:<id>``, or ``mi:<index>`` if the id would exceed Telegram's 64 bytes."""
    data = f"m:{model_id}"
    return data if len(data.encode()) <= CALLBACK_DATA_MAX_BYTES else f"mi:{index}"


# ── Session persistence ─────────────────────────────────────────────


class SessionStore:
    """chat_id -> Claude session id, persisted so restarts keep context.

    Each entry also records the fingerprint of the agent instructions the
    session was started with; Claude Code keeps a session's original system
    prompt on resume, so entries from other instructions aren't resumed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, str]] = {}
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable session store {}: {}", path, exc)
            raw = {}
        for key, value in raw.items():
            # Legacy entries were bare session ids with no fingerprint.
            self._data[key] = value if isinstance(value, dict) else {"session_id": value}

    def get(self, chat_id: int, fingerprint: str) -> str | None:
        """The session to resume, or None if there is none or it is stale."""
        entry = self._data.get(str(chat_id))
        if entry and entry.get("fingerprint") == fingerprint:
            return entry.get("session_id")
        return None

    def has(self, chat_id: int) -> bool:
        return str(chat_id) in self._data

    def set(self, chat_id: int, session_id: str | None, fingerprint: str = "") -> None:
        key = str(chat_id)
        if session_id:
            entry = {"session_id": session_id, "fingerprint": fingerprint}
            if self._data.get(key) == entry:
                return
            self._data[key] = entry
        elif self._data.pop(key, None) is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)


# ── Per-chat state ──────────────────────────────────────────────────


@dataclass
class ChatState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    agent: BrowsingAgent | None = None
    # Bumped by /stop and /new so messages queued before them are dropped.
    epoch: int = 0


@dataclass
class PendingAlbum:
    """Items of one media group, collected until no new item arrives."""

    epoch: int
    messages: list[Message] = field(default_factory=list)


@dataclass(frozen=True)
class MediaRef:
    """The Telegram file to download for one image message."""

    file_id: str
    file_size: int | None
    label: str


def media_ref(msg: Message) -> MediaRef | None:
    """The image to download from *msg*: the largest photo size, or an image
    document (a file sent uncompressed). None for anything else."""
    if msg.photo:
        best = max(msg.photo, key=lambda p: (p.width * p.height, p.file_size or 0))
        return MediaRef(best.file_id, best.file_size, "That photo")
    doc = msg.document
    if doc is not None and (doc.mime_type or "").startswith("image/"):
        return MediaRef(doc.file_id, doc.file_size, doc.file_name or "That file")
    return None


def media_prompt(messages: Sequence[Message], n_images: int) -> str:
    """The captions of an album (usually only on the first item), or a default."""
    captions: list[str] = []
    for m in messages:
        caption = (m.caption or "").strip()
        if caption and caption not in captions:
            captions.append(caption)
    if captions:
        return "\n\n".join(captions)
    return DEFAULT_ALBUM_PROMPT if n_images > 1 else DEFAULT_IMAGE_PROMPT


class HometabsBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_dir = settings.state_file.parent.resolve()
        self.sessions = SessionStore(settings.state_file)
        self.chats: dict[int, ChatState] = {}
        # Our skills + Aside's builtin ones from the local install, rebuilt
        # on every start so an Aside update is picked up.
        self.plugin = build_plugin(self.state_dir / "plugin", settings.agent.aside_skills_dir)
        logger.info("Skills: {} (plugin {})", ", ".join(self.plugin.skills), self.plugin.path)
        # Model/effort are deliberately not part of the fingerprint: a session
        # resumes fine under a different --model/--effort.
        self.fingerprint = instructions_fingerprint(self.plugin)
        self.prefs = PreferenceStore(
            self.state_dir / "settings.json", settings.agent.model, settings.agent.effort
        )
        self.catalog = ModelCatalog(
            settings.agent.api_key, settings.agent.oauth_token, self.state_dir / "models_cache.json"
        )
        self._bg_tasks: set[asyncio.Task] = set()
        self._albums: dict[tuple[int, str], PendingAlbum] = {}

    def _chat(self, chat_id: int) -> ChatState:
        return self.chats.setdefault(chat_id, ChatState())

    # -- model / effort ----------------------------------------------------

    def _model_info(self) -> ModelInfo | None:
        return self.catalog.get(self.prefs.model)

    def _effective_effort(self) -> str | None:
        info = self._model_info()
        return resolve_effort(info.effort_levels if info else None, self.prefs.effort)

    def _agent_config(self):
        # Fixed cwd so Claude Code stores/resumes sessions in one place.
        return replace(
            self.settings.agent,
            model=self.prefs.model,
            effort=self._effective_effort(),
            cwd=self.state_dir,
            plugin_dir=self.plugin.path,
            skills=self.plugin.skills,
        )

    def _agent_is_current(self, agent: BrowsingAgent) -> bool:
        cfg = self._agent_config()
        return (agent.config.model, agent.config.effort) == (cfg.model, cfg.effort)

    async def _retire_agent(self, state: ChatState, chat_id: int) -> None:
        """Close the chat's agent but keep its session, so the next message
        resumes the same conversation with the current options. Caller must
        hold the chat lock (or know it is free)."""
        agent, state.agent = state.agent, None
        if agent is None:
            return
        if agent.session_id:
            self.sessions.set(chat_id, agent.session_id, self.fingerprint)
        await agent.close()

    def _is_busy(self, chat_id: int | None) -> bool:
        """Whether *chat_id* is mid-turn. Agents with outdated options are not
        closed here: _run_turn reconnects them (resuming the session) before
        the next turn, which never waits on a running or queued turn."""
        state = self.chats.get(chat_id) if chat_id is not None else None
        return state is not None and state.lock.locked()

    def _effort_label(self) -> str:
        info = self._model_info()
        effective = self._effective_effort()
        if info is not None and not info.effort_levels:
            return "not supported by this model"
        if effective is None:
            return "model default"
        if self.prefs.effort and effective != self.prefs.effort:
            return f"{effective} (preferred {self.prefs.effort}, not supported by this model)"
        return effective

    def _model_label(self) -> str:
        info = self._model_info()
        return f"{info.display_name} ({info.id})" if info else self.prefs.model

    def _model_text(self, note: str = "") -> str:
        lines = [f"Model: {self._model_label()}", f"Effort: {self._effort_label()}"]
        if note:
            lines += ["", note]
        lines.append("")
        if self.catalog.models:
            lines.append("Tap a model to switch, or send /model <id>.")
        else:
            lines.append("Couldn't load the model list right now; send /model <id> to switch.")
        return "\n".join(lines)

    def _model_keyboard(self) -> InlineKeyboardMarkup | None:
        if not self.catalog.models:
            return None
        rows = [
            [
                InlineKeyboardButton(
                    ("✓ " if m.id == self.prefs.model else "") + m.display_name,
                    callback_data=model_callback_data(i, m.id),
                )
            ]
            for i, m in enumerate(self.catalog.models)
        ]
        return InlineKeyboardMarkup(rows)

    def _effort_levels(self) -> list[str] | None:
        """Levels offered for the current model ([]: none, None: unknown model)."""
        info = self._model_info()
        return list(info.effort_levels) if info else None

    def _effort_text(self, note: str = "") -> str:
        lines = [f"Model: {self._model_label()}", f"Effort: {self._effort_label()}"]
        if note:
            lines += ["", note]
        levels = self._effort_levels()
        if levels == []:
            lines += ["", "This model doesn't support an effort setting."]
        else:
            lines += ["", "Tap a level, or send /effort <level>."]
        return "\n".join(lines)

    def _effort_keyboard(self) -> InlineKeyboardMarkup | None:
        levels = self._effort_levels()
        if levels is None:
            levels = list(SDK_EFFORT_LEVELS)
        if not levels:
            return None
        current = self._effective_effort()
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        ("✓ " if lv == current else "") + lv, callback_data=f"e:{lv}"
                    )
                    for lv in levels
                ]
            ]
        )

    def _applies_note(self, busy: bool) -> str:
        if busy:
            return (
                "A task is running; it finishes with the old setting and the change "
                "applies to your next message."
            )
        return "Applies from your next message; the conversation is kept."

    async def _select_model(self, model_id: str, chat_id: int | None) -> str:
        """Switch the bot-wide model. Returns a note for the user."""
        old = self.prefs.model
        self.prefs.update(model=model_id)
        info = self._model_info()
        name = info.display_name if info else model_id
        notes = []
        if model_id == old:
            notes.append(f"Already using {name}.")
        else:
            notes.append(f"Switched to {name}.")
            logger.info("Model changed: {} -> {}", old, model_id)
        effective = self._effective_effort()
        if info is not None and not info.effort_levels and self.prefs.effort:
            notes.append(f"{name} doesn't support effort, so it will be omitted.")
        elif self.prefs.effort and effective != self.prefs.effort:
            notes.append(
                f"{name} doesn't support '{self.prefs.effort}' effort; using '{effective}'."
            )
        if model_id != old:
            notes.append(self._applies_note(self._is_busy(chat_id)))
        return " ".join(notes)

    async def _select_effort(self, level: str, chat_id: int | None) -> tuple[bool, str]:
        """Set the preferred effort. Returns (ok, note)."""
        level = level.strip().lower()
        levels = self._effort_levels()
        if level not in SDK_EFFORT_LEVELS:
            return False, f"Unknown effort '{level}'. Levels: {', '.join(SDK_EFFORT_LEVELS)}."
        if levels == []:
            return False, "The current model doesn't support an effort setting."
        if levels is not None and level not in levels:
            return (
                False,
                f"The current model doesn't support '{level}'. Supported: {', '.join(levels)}.",
            )
        before = self._effective_effort()
        self.prefs.update(effort=level)
        if before == level:
            return True, f"Effort is already {level}."
        logger.info("Effort changed: {} -> {}", before, level)
        return True, f"Effort set to {level}. " + self._applies_note(self._is_busy(chat_id))

    def _new_agent(self, chat_id: int) -> tuple[BrowsingAgent, bool]:
        """Create the chat's agent. The flag is True if a saved session was
        dropped because the agent's instructions changed since it started."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cfg = self._agent_config()
        resume = self.sessions.get(chat_id, self.fingerprint)
        stale = resume is None and self.sessions.has(chat_id)
        if stale:
            logger.info("Instructions changed; not resuming the old session for chat {}", chat_id)
            self.sessions.set(chat_id, None)
        return BrowsingAgent(cfg, resume=resume), stale

    # -- application -----------------------------------------------------

    def build_application(self) -> Application:
        app = (
            ApplicationBuilder()
            .token(self.settings.telegram_bot_token)
            # Needed so /stop can run while a message handler is busy.
            .concurrent_updates(True)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        allowed = filters.User(user_id=list(self.settings.allowed_user_ids))
        app.add_handler(CommandHandler(["start", "help"], self.cmd_start, filters=allowed))
        app.add_handler(CommandHandler("new", self.cmd_new, filters=allowed))
        app.add_handler(CommandHandler("stop", self.cmd_stop, filters=allowed))
        app.add_handler(CommandHandler("model", self.cmd_model, filters=allowed))
        app.add_handler(CommandHandler("effort", self.cmd_effort, filters=allowed))
        # CallbackQueryHandler takes no user filter: on_callback checks the allowlist.
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(
            MessageHandler(allowed & (filters.PHOTO | filters.Document.IMAGE), self.on_media)
        )
        # Everyone else (and unsupported message types) lands here.
        app.add_handler(MessageHandler(filters.ALL, self.on_other))
        app.add_error_handler(self.on_error)
        return app

    async def _post_init(self, app: Application) -> None:
        try:
            await app.bot.set_my_commands(BOT_COMMANDS)
        except TelegramError as exc:
            logger.warning("Could not register the bot command menu: {}", exc)
        task = asyncio.create_task(self.catalog.refresh())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        logger.info(
            "Model={} effort={} (state {})", self.prefs.model, self.prefs.effort, self.state_dir
        )

    async def _post_shutdown(self, app: Application) -> None:
        for state in self.chats.values():
            if state.agent is not None:
                await state.agent.close()

    # -- handlers ----------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP_TEXT)

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        state = self._chat(chat_id)
        state.epoch += 1
        if state.lock.locked() and state.agent is not None:
            with contextlib.suppress(Exception):
                await state.agent.interrupt()
        async with state.lock:
            if state.agent is not None:
                await state.agent.close()
                state.agent = None
            self.sessions.set(chat_id, None)
        await update.effective_message.reply_text("Started a new conversation.")

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        state = self._chat(update.effective_chat.id)
        state.epoch += 1
        if not state.lock.locked() or state.agent is None:
            await update.effective_message.reply_text("Nothing is running.")
            return
        try:
            await state.agent.interrupt()
            await update.effective_message.reply_text("Stopping the current task…")
        except Exception as exc:  # noqa: BLE001
            logger.exception("interrupt failed")
            await update.effective_message.reply_text(f"Couldn't interrupt: {exc}")

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat_id = update.effective_chat.id if update.effective_chat else None
        await self.catalog.refresh()
        arg = " ".join(context.args or []).strip()
        note = ""
        if arg:
            info = self.catalog.resolve(arg)
            if info is not None:
                note = await self._select_model(info.id, chat_id)
            elif not self.catalog.models and _MODEL_ID_RE.match(arg):
                note = await self._select_model(arg, chat_id)
            else:
                await msg.reply_text(
                    f"Unknown model '{arg}'. Send /model to see the available models."
                )
                return
        await msg.reply_text(self._model_text(note), reply_markup=self._model_keyboard())

    async def cmd_effort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat_id = update.effective_chat.id if update.effective_chat else None
        await self.catalog.refresh()
        arg = " ".join(context.args or []).strip()
        note = ""
        if arg:
            ok, note = await self._select_effort(arg, chat_id)
            if not ok:
                await msg.reply_text(note, reply_markup=self._effort_keyboard())
                return
        await msg.reply_text(self._effort_text(note), reply_markup=self._effort_keyboard())

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        user = update.effective_user
        if user is None or user.id not in self.settings.allowed_user_ids:
            logger.warning(
                "Rejected button press from user id={} username={}",
                user.id if user else None,
                user.username if user else None,
            )
            with contextlib.suppress(TelegramError):
                await query.answer("You're not allowed to use this bot.")
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        data = query.data or ""
        kind, _, value = data.partition(":")
        if kind in ("m", "mi"):
            info = None
            if kind == "m":
                info = self.catalog.get(value)
            elif value.isdigit() and int(value) < len(self.catalog.models):
                info = self.catalog.models[int(value)]
            if info is None:
                await query.answer("That model is no longer available; send /model again.")
                return
            note = await self._select_model(info.id, chat_id)
            await query.answer(f"Model: {info.display_name}")
            await self._edit_query(query, self._model_text(note), self._model_keyboard())
        elif kind == "e":
            ok, note = await self._select_effort(value, chat_id)
            await query.answer(note[:200] if not ok else f"Effort: {value}")
            if ok:
                await self._edit_query(query, self._effort_text(note), self._effort_keyboard())
        else:
            await query.answer()

    @staticmethod
    async def _edit_query(query, text: str, markup: InlineKeyboardMarkup | None) -> None:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest as exc:  # e.g. "message is not modified"
            logger.debug("edit after button press failed: {}", exc)

    async def on_other(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        msg = update.effective_message
        if user is None or user.id not in self.settings.allowed_user_ids:
            logger.warning(
                "Rejected update from user id={} username={}",
                user.id if user else None,
                user.username if user else None,
            )
            if (
                msg is not None
                and update.effective_chat
                and update.effective_chat.type == "private"
            ):
                user_id = user.id if user else "?"
                await msg.reply_text(
                    f"Sorry, you're not allowed to use this bot. (Your user id: {user_id})"
                )
            return
        if msg is not None:
            await msg.reply_text(UNSUPPORTED_TEXT)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat_id = update.effective_chat.id
        await self._enqueue(context, msg, chat_id, self._chat(chat_id).epoch, msg.text)

    async def on_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A photo or image file. Album items are collected into one turn."""
        msg = update.effective_message
        chat_id = update.effective_chat.id
        epoch = self._chat(chat_id).epoch
        group = msg.media_group_id
        if not group:
            await self._media_turn(context, chat_id, [msg], epoch)
            return
        key = (chat_id, group)
        album = self._albums.get(key)
        if album is not None:
            album.messages.append(msg)  # the first item's handler sends the turn
            return
        # This handler owns the album: wait until no new item arrives for
        # ALBUM_DEBOUNCE_S, then send all of them as one turn. Everything
        # between awaits runs atomically on the event loop, so items can't be
        # lost: they either join before the album is popped or start a new one.
        album = self._albums[key] = PendingAlbum(epoch=epoch, messages=[msg])
        try:
            while True:
                seen = len(album.messages)
                await asyncio.sleep(ALBUM_DEBOUNCE_S)
                if len(album.messages) == seen:
                    break
        finally:
            self._albums.pop(key, None)
        messages = sorted(album.messages, key=lambda m: m.message_id)
        await self._media_turn(context, chat_id, messages, album.epoch)

    async def _media_turn(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, messages: list[Message], epoch: int
    ) -> None:
        """Download and normalize the images of *messages*, then queue one turn."""
        bot = context.bot
        first = messages[0]
        refs = [ref for ref in (media_ref(m) for m in messages) if ref is not None]
        notes: list[str] = []
        if len(refs) > MAX_IMAGES_PER_TURN:
            notes.append(f"Only the first {MAX_IMAGES_PER_TURN} images are used.")
            refs = refs[:MAX_IMAGES_PER_TURN]
        with contextlib.suppress(TelegramError):
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        images: list[Image] = []
        max_bytes = per_image_budget(len(refs))
        for ref in refs:
            if self._chat(chat_id).epoch != epoch:
                return  # /stop or /new while downloading
            try:
                images.append(await self._download_image(bot, ref, max_bytes))
            except ImageError as exc:
                notes.append(str(exc))
        if notes:
            skipped = len(refs) - len(images)
            if images and skipped:
                notes.append(f"Continuing with {len(images)} of {len(refs)} images.")
            await self._send_text(bot, chat_id, "\n".join(notes), reply_to=first)
        if not images:
            return
        prompt = media_prompt(messages, len(images))
        await self._enqueue(context, first, chat_id, epoch, prompt, images)

    @staticmethod
    async def _download_image(bot, ref: MediaRef, max_bytes: int) -> Image:
        """Download one image and normalize it for Claude. Raises ImageError
        with a user-facing message."""
        if ref.file_size and ref.file_size > TELEGRAM_DOWNLOAD_LIMIT:
            raise ImageError(too_large_message(ref.file_size, ref.label))
        try:
            tg_file = await bot.get_file(ref.file_id)
            data = await tg_file.download_as_bytearray()
        except BadRequest as exc:
            if "too big" in str(exc).lower():
                raise ImageError(too_large_message(ref.file_size or 0, ref.label)) from exc
            raise ImageError(f"I couldn't download {ref.label.lower()}: {exc}") from exc
        except TelegramError as exc:
            raise ImageError(f"I couldn't download {ref.label.lower()}: {exc}") from exc
        # Decoding and re-encoding is CPU-bound; keep the event loop free.
        return await asyncio.to_thread(normalize_image, bytes(data), max_bytes=max_bytes)

    async def _enqueue(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        msg: Message,
        chat_id: int,
        epoch: int,
        prompt: str,
        images: Sequence[Image] = (),
    ) -> None:
        """Run one turn under the chat's lock, queueing behind a running one.
        *epoch* is the chat's epoch when the message arrived; /stop and /new
        bump it so messages from before them are dropped."""
        state = self._chat(chat_id)
        if state.epoch != epoch:
            logger.info("Dropping message sent before /stop or /new in chat {}", chat_id)
            return
        if state.lock.locked():
            await msg.reply_text("Queued; I'll get to it after the current task. (/stop to cancel)")
        async with state.lock:
            if state.epoch != epoch:
                logger.info("Dropping message queued before /stop or /new in chat {}", chat_id)
                return
            await self._run_turn(context, msg, state, chat_id, prompt, images)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.opt(exception=context.error).error("Unhandled error while processing an update")

    # -- turn ------------------------------------------------------------

    async def _run_turn(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        msg: Message,
        state: ChatState,
        chat_id: int,
        prompt: str,
        images: Sequence[Image] = (),
    ) -> None:
        bot = context.bot
        if state.agent is not None and not self._agent_is_current(state.agent):
            # /model or /effort changed since this agent started: reconnect,
            # resuming the same session with the new options.
            await self._retire_agent(state, chat_id)
        if state.agent is None:
            state.agent, stale = self._new_agent(chat_id)
            if stale:
                await self._send_text(
                    bot, chat_id, "My instructions were updated, so this is a fresh conversation."
                )
        agent = state.agent

        typing = asyncio.create_task(self._keep_typing(bot, chat_id))
        status: Message | None = None
        last_edit = 0.0

        async def on_tool(step: ToolStep) -> None:
            nonlocal status, last_edit
            now = time.monotonic()
            if now - last_edit < STATUS_EDIT_INTERVAL_S:
                return
            last_edit = now
            text = f"Working… step {step.index}: {step.summary}"
            try:
                if status is None:
                    status = await bot.send_message(chat_id, text, disable_notification=True)
                else:
                    await status.edit_text(text)
            except TelegramError as exc:
                logger.debug("status update failed: {}", exc)

        try:
            if images:
                logger.info("Turn with {} image(s) in chat {}", len(images), chat_id)
            result = await agent.ask(prompt, images=list(images) or None, on_tool=on_tool)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent turn failed in chat {}", chat_id)
            # The CLI subprocess may be dead; reconnect (resuming) next time.
            if agent.session_id:
                self.sessions.set(chat_id, agent.session_id, self.fingerprint)
            await agent.close()
            state.agent = None
            await self._send_text(bot, chat_id, f"Sorry, something went wrong: {exc}", reply_to=msg)
            return
        finally:
            typing.cancel()
            if status is not None:
                with contextlib.suppress(TelegramError):
                    await status.delete()

        if result.session_id:
            self.sessions.set(chat_id, result.session_id, self.fingerprint)
        await self._deliver(bot, chat_id, msg, result)

    async def _deliver(self, bot, chat_id: int, msg: Message, result: TurnResult) -> None:
        text = result.text.strip()
        if result.interrupted:
            text = (text + "\n\n" if text else "") + "(stopped)"
        elif result.is_error:
            detail = "; ".join(result.errors) or "unknown error"
            text = (text + "\n\n" if text else "") + f"(The agent hit an error: {detail})"
        if not text and not result.images:
            text = "(no reply)"
        if text:
            await self._send_text(bot, chat_id, text, reply_to=msg)
        for image in result.images[-MAX_PHOTOS_PER_TURN:]:
            await self._send_image(bot, chat_id, image)

    async def _send_text(
        self, bot, chat_id: int, text: str, reply_to: Message | None = None
    ) -> None:
        for i, chunk in enumerate(split_message(text, SAFE_CHUNK_LEN)):
            reply = (
                ReplyParameters(reply_to.message_id, allow_sending_without_reply=True)
                if (reply_to is not None and i == 0)
                else None
            )
            html_text = markdown_to_telegram_html(chunk)
            if len(html_text) <= TELEGRAM_MAX_LEN:
                try:
                    await bot.send_message(
                        chat_id,
                        html_text,
                        parse_mode=ParseMode.HTML,
                        reply_parameters=reply,
                        link_preview_options=_NO_PREVIEW,
                    )
                    continue
                except BadRequest as exc:
                    logger.debug("HTML send rejected ({}); falling back to plain text", exc)
            await bot.send_message(
                chat_id, chunk, reply_parameters=reply, link_preview_options=_NO_PREVIEW
            )

    async def _send_image(self, bot, chat_id: int, image: Image) -> None:
        ext = image.media_type.split("/")[-1] or "png"
        try:
            await bot.send_photo(chat_id, io.BytesIO(image.data), filename=f"screenshot.{ext}")
        except TelegramError as exc:
            # Too large / odd dimensions for a photo: fall back to a document.
            logger.debug("send_photo failed ({}); sending as document", exc)
            with contextlib.suppress(TelegramError):
                await bot.send_document(
                    chat_id, io.BytesIO(image.data), filename=f"screenshot.{ext}"
                )

    @staticmethod
    async def _keep_typing(bot, chat_id: int) -> None:
        while True:
            with contextlib.suppress(TelegramError):
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(TYPING_INTERVAL_S)
