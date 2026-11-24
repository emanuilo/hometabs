"""What the bot sends back: replies, images, typing and progress indicators."""

from __future__ import annotations

import asyncio
import contextlib
import io
import time

from loguru import logger
from telegram import Bot, LinkPreviewOptions, Message, ReplyParameters
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError

from ..agent import Image, ToolStep, TurnResult
from ..formatting import SAFE_CHUNK_LEN, TELEGRAM_MAX_LEN, markdown_to_telegram_html, split_message

MAX_PHOTOS_PER_TURN = 5
STATUS_EDIT_INTERVAL_S = 2.0
TYPING_INTERVAL_S = 4.0
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def reply_text(result: TurnResult) -> str:
    """The text to send for a finished turn ("" if it only produced images)."""
    text = result.text.strip()
    if result.interrupted:
        text = (text + "\n\n" if text else "") + "(stopped)"
    elif result.is_error:
        detail = "; ".join(result.errors) or "unknown error"
        text = (text + "\n\n" if text else "") + f"(The agent hit an error: {detail})"
    if not text and not result.images:
        text = "(no reply)"
    return text


async def deliver(bot: Bot, chat_id: int, msg: Message, result: TurnResult) -> None:
    """Send a finished turn: its text as a reply to *msg*, then its images."""
    text = reply_text(result)
    if text:
        await send_text(bot, chat_id, text, reply_to=msg)
    for image in result.images[-MAX_PHOTOS_PER_TURN:]:
        await send_image(bot, chat_id, image)


async def send_text(bot: Bot, chat_id: int, text: str, reply_to: Message | None = None) -> None:
    """Send Markdown *text* as Telegram HTML, split to fit, in plain text if rejected."""
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


async def send_image(bot: Bot, chat_id: int, image: Image) -> None:
    ext = image.media_type.split("/")[-1] or "png"
    try:
        await bot.send_photo(chat_id, io.BytesIO(image.data), filename=f"screenshot.{ext}")
    except TelegramError as exc:
        # Too large / odd dimensions for a photo: fall back to a document.
        logger.debug("send_photo failed ({}); sending as document", exc)
        with contextlib.suppress(TelegramError):
            await bot.send_document(chat_id, io.BytesIO(image.data), filename=f"screenshot.{ext}")


async def keep_typing(bot: Bot, chat_id: int) -> None:
    """Show "typing…" until cancelled."""
    while True:
        with contextlib.suppress(TelegramError):
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(TYPING_INTERVAL_S)


class ToolProgress:
    """A "Working… step N" message, edited as the agent calls tools.

    Call the instance with each :class:`ToolStep`; :meth:`clear` deletes the
    message when the turn ends.
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.status: Message | None = None
        self.last_edit = 0.0

    async def __call__(self, step: ToolStep) -> None:
        now = time.monotonic()
        if now - self.last_edit < STATUS_EDIT_INTERVAL_S:
            return
        self.last_edit = now
        text = f"Working… step {step.index}: {step.summary}"
        try:
            if self.status is None:
                self.status = await self.bot.send_message(
                    self.chat_id, text, disable_notification=True
                )
            else:
                await self.status.edit_text(text)
        except TelegramError as exc:
            logger.debug("status update failed: {}", exc)

    async def clear(self) -> None:
        if self.status is not None:
            with contextlib.suppress(TelegramError):
                await self.status.delete()
