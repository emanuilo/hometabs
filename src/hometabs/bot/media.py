"""Photos and image files sent to the bot: which file to fetch, albums, downloads."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from telegram import Bot, Message
from telegram.error import BadRequest, TelegramError

from ..agent import Image
from ..images import (
    MAX_IMAGES_PER_TURN,
    TELEGRAM_DOWNLOAD_LIMIT,
    ImageError,
    normalize_image,
    per_image_budget,
    too_large_message,
)

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


# ── Albums ──────────────────────────────────────────────────────────


@dataclass
class PendingAlbum:
    """Items of one media group, collected until no new item arrives."""

    epoch: int
    messages: list[Message] = field(default_factory=list)


class Albums:
    """Collects the items of media groups (albums) into one turn each."""

    def __init__(self) -> None:
        self._pending: dict[tuple[int, str], PendingAlbum] = {}

    async def collect(
        self, chat_id: int, group: str, msg: Message, epoch: int
    ) -> PendingAlbum | None:
        """Add *msg* to its album.

        The handler of the album's first item waits until no new item arrives
        for ALBUM_DEBOUNCE_S and gets the whole album; the others get None.
        Everything between awaits runs atomically on the event loop, so items
        can't be lost: they either join before the album is popped or start a
        new one.
        """
        key = (chat_id, group)
        album = self._pending.get(key)
        if album is not None:
            album.messages.append(msg)
            return None
        album = self._pending[key] = PendingAlbum(epoch=epoch, messages=[msg])
        try:
            while True:
                seen = len(album.messages)
                await asyncio.sleep(ALBUM_DEBOUNCE_S)
                if len(album.messages) == seen:
                    break
        finally:
            self._pending.pop(key, None)
        album.messages.sort(key=lambda m: m.message_id)
        return album


# ── Downloads ───────────────────────────────────────────────────────


@dataclass
class Downloaded:
    """The usable images of a turn, and notes for the user about the rest."""

    images: list[Image]
    notes: list[str]


async def download_image(bot: Bot, ref: MediaRef, max_bytes: int) -> Image:
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


async def download_images(
    bot: Bot, messages: Sequence[Message], cancelled: Callable[[], bool]
) -> Downloaded | None:
    """Download and normalize the images of *messages*.

    Returns None if *cancelled* turns true (/stop or /new) while downloading.
    """
    refs = [ref for ref in (media_ref(m) for m in messages) if ref is not None]
    notes: list[str] = []
    if len(refs) > MAX_IMAGES_PER_TURN:
        notes.append(f"Only the first {MAX_IMAGES_PER_TURN} images are used.")
        refs = refs[:MAX_IMAGES_PER_TURN]
    images: list[Image] = []
    max_bytes = per_image_budget(len(refs))
    for ref in refs:
        if cancelled():
            return None
        try:
            images.append(await download_image(bot, ref, max_bytes))
        except ImageError as exc:
            notes.append(str(exc))
    if notes and images and len(images) < len(refs):
        notes.append(f"Continuing with {len(images)} of {len(refs)} images.")
    return Downloaded(images, notes)
