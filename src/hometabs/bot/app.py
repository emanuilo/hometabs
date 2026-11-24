"""The Telegram application: handlers, per-chat agents and the turn queue."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass

from loguru import logger
from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction
from telegram.error import InvalidToken, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..agent import Image
from ..config import ConfigError, Settings
from ..models import CACHE_FILENAME, ModelCatalog
from .chats import Chats, ChatState
from .media import Albums, download_images, media_prompt
from .model_menu import ModelMenu
from .replies import ToolProgress, deliver, keep_typing, send_text
from .stores import PreferenceStore, SessionStore

UNSUPPORTED_TEXT = "I can handle text and images (photos or image files) for now."

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


@dataclass(frozen=True)
class TurnRequest:
    """One user message to run as a turn."""

    chat_id: int
    msg: Message  # what the reply goes to
    epoch: int  # the chat's epoch when the message arrived
    prompt: str
    images: Sequence[Image] = ()


def _message_and_chat(update: Update) -> tuple[Message, int] | None:
    msg, chat = update.effective_message, update.effective_chat
    if msg is None or chat is None:
        return None
    return msg, chat.id


def _args(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args or []).strip()


class HometabsBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_dir = settings.state_file.parent.resolve()
        self.prefs = PreferenceStore(
            self.state_dir / "settings.json", settings.agent.model, settings.agent.effort
        )
        self.catalog = ModelCatalog(
            settings.agent.api_key, settings.agent.oauth_token, self.state_dir / CACHE_FILENAME
        )
        self.menu = ModelMenu(self.prefs, self.catalog)
        self.chats = Chats(
            settings.agent, self.state_dir, SessionStore(settings.state_file), self.menu
        )
        self.albums = Albums()
        self._bg_tasks: set[asyncio.Task] = set()

    def _allowed(self, update: Update, what: str) -> bool:
        """Whether the update's sender is allowlisted; logs a rejection."""
        user = update.effective_user
        if user is not None and user.id in self.settings.allowed_user_ids:
            return True
        logger.warning(
            "Rejected {} from user id={} username={}",
            what,
            user.id if user else None,
            user.username if user else None,
        )
        return False

    # -- application -------------------------------------------------------

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

    def run(self) -> None:
        """Poll Telegram until stopped. Raises ConfigError if the token is rejected."""
        app = self.build_application()
        try:
            app.run_polling(
                allowed_updates=["message", "callback_query"], drop_pending_updates=True
            )
        except InvalidToken as exc:
            raise ConfigError(
                "Telegram rejected TELEGRAM_BOT_TOKEN; check the token from @BotFather."
            ) from exc

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
        await self.chats.close_all()

    # -- commands ----------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message is not None:
            await update.effective_message.reply_text(HELP_TEXT)

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        target = _message_and_chat(update)
        if target is None:
            return
        msg, chat_id = target
        await self.chats.reset(chat_id)
        await msg.reply_text("Started a new conversation.")

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        target = _message_and_chat(update)
        if target is None:
            return
        msg, chat_id = target
        state = self.chats.chat(chat_id)
        state.epoch += 1
        if not state.lock.locked() or state.agent is None:
            await msg.reply_text("Nothing is running.")
            return
        try:
            await state.agent.interrupt()
            await msg.reply_text("Stopping the current task…")
        except Exception as exc:
            logger.exception("interrupt failed")
            await msg.reply_text(f"Couldn't interrupt: {exc}")

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        target = _message_and_chat(update)
        if target is None:
            return
        msg, chat_id = target
        await self.catalog.refresh()
        text, markup = self.menu.model_reply(_args(context), busy=self.chats.is_busy(chat_id))
        await msg.reply_text(text, reply_markup=markup)

    async def cmd_effort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        target = _message_and_chat(update)
        if target is None:
            return
        msg, chat_id = target
        await self.catalog.refresh()
        text, markup = self.menu.effort_reply(_args(context), busy=self.chats.is_busy(chat_id))
        await msg.reply_text(text, reply_markup=markup)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._allowed(update, "button press"):
            with contextlib.suppress(TelegramError):
                await query.answer("You're not allowed to use this bot.")
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        await self.menu.on_button(query, busy=self.chats.is_busy(chat_id))

    # -- messages ----------------------------------------------------------

    async def on_other(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not self._allowed(update, "update"):
            chat = update.effective_chat
            if msg is not None and chat is not None and chat.type == "private":
                user_id = update.effective_user.id if update.effective_user else "?"
                await msg.reply_text(
                    f"Sorry, you're not allowed to use this bot. (Your user id: {user_id})"
                )
            return
        if msg is not None:
            await msg.reply_text(UNSUPPORTED_TEXT)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        target = _message_and_chat(update)
        if target is None:
            return
        msg, chat_id = target
        if msg.text:
            await self._enqueue(
                context, TurnRequest(chat_id, msg, self.chats.chat(chat_id).epoch, msg.text)
            )

    async def on_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A photo or image file. Album items are collected into one turn."""
        target = _message_and_chat(update)
        if target is None:
            return
        msg, chat_id = target
        epoch = self.chats.chat(chat_id).epoch
        if not msg.media_group_id:
            await self._media_turn(context, chat_id, [msg], epoch)
            return
        album = await self.albums.collect(chat_id, msg.media_group_id, msg, epoch)
        if album is not None:  # the first item's handler sends the turn
            await self._media_turn(context, chat_id, album.messages, album.epoch)

    async def _media_turn(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, messages: list[Message], epoch: int
    ) -> None:
        """Download and normalize the images of *messages*, then queue one turn."""
        bot = context.bot
        with contextlib.suppress(TelegramError):
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        got = await download_images(bot, messages, lambda: self.chats.chat(chat_id).epoch != epoch)
        if got is None:
            return  # /stop or /new while downloading
        if got.notes:
            await send_text(bot, chat_id, "\n".join(got.notes), reply_to=messages[0])
        if got.images:
            prompt = media_prompt(messages, len(got.images))
            await self._enqueue(
                context, TurnRequest(chat_id, messages[0], epoch, prompt, got.images)
            )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.opt(exception=context.error).error("Unhandled error while processing an update")

    # -- turns -------------------------------------------------------------

    async def _enqueue(self, context: ContextTypes.DEFAULT_TYPE, request: TurnRequest) -> None:
        """Run one turn under the chat's lock, queueing behind a running one.
        Messages from before a /stop or /new (an older epoch) are dropped."""
        state = self.chats.chat(request.chat_id)
        if state.epoch != request.epoch:
            logger.info("Dropping message sent before /stop or /new in chat {}", request.chat_id)
            return
        if state.lock.locked():
            await request.msg.reply_text(
                "Queued; I'll get to it after the current task. (/stop to cancel)"
            )
        async with state.lock:
            if state.epoch != request.epoch:
                logger.info(
                    "Dropping message queued before /stop or /new in chat {}", request.chat_id
                )
                return
            await self._run_turn(context, state, request)

    async def _run_turn(
        self, context: ContextTypes.DEFAULT_TYPE, state: ChatState, request: TurnRequest
    ) -> None:
        bot, chat_id = context.bot, request.chat_id
        agent, stale = await self.chats.ready_agent(state, chat_id)
        if stale:
            await send_text(
                bot, chat_id, "My instructions were updated, so this is a fresh conversation."
            )
        typing = asyncio.create_task(keep_typing(bot, chat_id))
        progress = ToolProgress(bot, chat_id)
        try:
            if request.images:
                logger.info("Turn with {} image(s) in chat {}", len(request.images), chat_id)
            result = await agent.ask(
                request.prompt, images=list(request.images) or None, on_tool=progress
            )
        except Exception as exc:
            logger.exception("Agent turn failed in chat {}", chat_id)
            # The CLI subprocess may be dead; reconnect (resuming) next time.
            await self.chats.retire_agent(state, chat_id)
            await send_text(
                bot, chat_id, f"Sorry, something went wrong: {exc}", reply_to=request.msg
            )
            return
        finally:
            typing.cancel()
            await progress.clear()

        self.chats.save_session(chat_id, result.session_id)
        await deliver(bot, chat_id, request.msg, result)
