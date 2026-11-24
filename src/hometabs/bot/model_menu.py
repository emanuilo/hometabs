"""The /model and /effort settings: the bot-wide choice, its texts and its buttons."""

from __future__ import annotations

import re

from loguru import logger
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from ..models import SDK_EFFORT_LEVELS, ModelCatalog, ModelInfo, resolve_effort
from .stores import PreferenceStore

CALLBACK_DATA_MAX_BYTES = 64  # Telegram's limit for inline button data
# Loose check for /model <id> when the model list is unavailable.
_MODEL_ID_RE = re.compile(r"^claude-[A-Za-z0-9._\[\]-]{1,100}$")

Reply = tuple[str, InlineKeyboardMarkup | None]


def model_callback_data(index: int, model_id: str) -> str:
    """``m:<id>``, or ``mi:<index>`` if the id would exceed Telegram's 64 bytes."""
    data = f"m:{model_id}"
    return data if len(data.encode()) <= CALLBACK_DATA_MAX_BYTES else f"mi:{index}"


def applies_note(*, busy: bool) -> str:
    if busy:
        return (
            "A task is running; it finishes with the old setting and the change "
            "applies to your next message."
        )
    return "Applies from your next message; the conversation is kept."


async def edit_query(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:  # e.g. "message is not modified"
        logger.debug("edit after button press failed: {}", exc)


class ModelMenu:
    """The model and effort the bot uses, and the replies that show or change them."""

    def __init__(self, prefs: PreferenceStore, catalog: ModelCatalog) -> None:
        self.prefs = prefs
        self.catalog = catalog

    # -- current choice ----------------------------------------------------

    def model_info(self) -> ModelInfo | None:
        return self.catalog.get(self.prefs.model)

    def effective_effort(self) -> str | None:
        info = self.model_info()
        return resolve_effort(info.effort_levels if info else None, self.prefs.effort)

    def effort_levels(self) -> list[str] | None:
        """Levels offered for the current model ([]: none, None: unknown model)."""
        info = self.model_info()
        return list(info.effort_levels) if info else None

    # -- texts and buttons -------------------------------------------------

    def _effort_label(self) -> str:
        info = self.model_info()
        effective = self.effective_effort()
        if info is not None and not info.effort_levels:
            return "not supported by this model"
        if effective is None:
            return "model default"
        if self.prefs.effort and effective != self.prefs.effort:
            return f"{effective} (preferred {self.prefs.effort}, not supported by this model)"
        return effective

    def _model_label(self) -> str:
        info = self.model_info()
        return f"{info.display_name} ({info.id})" if info else self.prefs.model

    def _header(self, note: str) -> list[str]:
        lines = [f"Model: {self._model_label()}", f"Effort: {self._effort_label()}"]
        if note:
            lines += ["", note]
        return lines

    def model_text(self, note: str = "") -> str:
        lines = self._header(note)
        lines.append("")
        if self.catalog.models:
            lines.append("Tap a model to switch, or send /model <id>.")
        else:
            lines.append("Couldn't load the model list right now; send /model <id> to switch.")
        return "\n".join(lines)

    def model_keyboard(self) -> InlineKeyboardMarkup | None:
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

    def effort_text(self, note: str = "") -> str:
        lines = self._header(note)
        if self.effort_levels() == []:
            lines += ["", "This model doesn't support an effort setting."]
        else:
            lines += ["", "Tap a level, or send /effort <level>."]
        return "\n".join(lines)

    def effort_keyboard(self) -> InlineKeyboardMarkup | None:
        levels = self.effort_levels()
        if levels is None:
            levels = list(SDK_EFFORT_LEVELS)
        if not levels:
            return None
        current = self.effective_effort()
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

    # -- changes -----------------------------------------------------------

    def select_model(self, model_id: str, *, busy: bool) -> str:
        """Switch the bot-wide model. Returns a note for the user."""
        old = self.prefs.model
        self.prefs.update(model=model_id)
        info = self.model_info()
        name = info.display_name if info else model_id
        notes = []
        if model_id == old:
            notes.append(f"Already using {name}.")
        else:
            notes.append(f"Switched to {name}.")
            logger.info("Model changed: {} -> {}", old, model_id)
        effective = self.effective_effort()
        if info is not None and not info.effort_levels and self.prefs.effort:
            notes.append(f"{name} doesn't support effort, so it will be omitted.")
        elif self.prefs.effort and effective != self.prefs.effort:
            notes.append(
                f"{name} doesn't support '{self.prefs.effort}' effort; using '{effective}'."
            )
        if model_id != old:
            notes.append(applies_note(busy=busy))
        return " ".join(notes)

    def select_effort(self, level: str, *, busy: bool) -> tuple[bool, str]:
        """Set the preferred effort. Returns (ok, note)."""
        level = level.strip().lower()
        levels = self.effort_levels()
        if level not in SDK_EFFORT_LEVELS:
            return False, f"Unknown effort '{level}'. Levels: {', '.join(SDK_EFFORT_LEVELS)}."
        if levels == []:
            return False, "The current model doesn't support an effort setting."
        if levels is not None and level not in levels:
            return (
                False,
                f"The current model doesn't support '{level}'. Supported: {', '.join(levels)}.",
            )
        before = self.effective_effort()
        self.prefs.update(effort=level)
        if before == level:
            return True, f"Effort is already {level}."
        logger.info("Effort changed: {} -> {}", before, level)
        return True, f"Effort set to {level}. " + applies_note(busy=busy)

    # -- commands and buttons ----------------------------------------------

    def model_reply(self, arg: str, *, busy: bool) -> Reply:
        """The reply to ``/model [arg]``: show the model, or switch to *arg*."""
        note = ""
        if arg:
            info = self.catalog.resolve(arg)
            if info is not None:
                note = self.select_model(info.id, busy=busy)
            elif not self.catalog.models and _MODEL_ID_RE.match(arg):
                note = self.select_model(arg, busy=busy)
            else:
                return f"Unknown model '{arg}'. Send /model to see the available models.", None
        return self.model_text(note), self.model_keyboard()

    def effort_reply(self, arg: str, *, busy: bool) -> Reply:
        """The reply to ``/effort [arg]``: show the effort, or set it to *arg*."""
        note = ""
        if arg:
            ok, note = self.select_effort(arg, busy=busy)
            if not ok:
                return note, self.effort_keyboard()
        return self.effort_text(note), self.effort_keyboard()

    def _button_model(self, kind: str, value: str) -> ModelInfo | None:
        if kind == "m":
            return self.catalog.get(value)
        if value.isdigit() and int(value) < len(self.catalog.models):
            return self.catalog.models[int(value)]
        return None

    async def on_button(self, query: CallbackQuery, *, busy: bool) -> None:
        """Handle a tap on a model (``m:``/``mi:``) or effort (``e:``) button."""
        kind, _, value = (query.data or "").partition(":")
        if kind in ("m", "mi"):
            info = self._button_model(kind, value)
            if info is None:
                await query.answer("That model is no longer available; send /model again.")
                return
            note = self.select_model(info.id, busy=busy)
            await query.answer(f"Model: {info.display_name}")
            await edit_query(query, self.model_text(note), self.model_keyboard())
        elif kind == "e":
            ok, note = self.select_effort(value, busy=busy)
            await query.answer(note[:200] if not ok else f"Effort: {value}")
            if ok:
                await edit_query(query, self.effort_text(note), self.effort_keyboard())
        else:
            await query.answer()
