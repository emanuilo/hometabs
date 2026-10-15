"""Images the user sends: size checks and normalization for Claude.

Claude accepts JPEG, PNG, GIF and WebP; a whole API request is capped at 32 MB
and earlier images are resent on every turn, so images are kept small. Everything else
(HEIC, BMP, TIFF, ...) is converted to JPEG, large images are downscaled, and
the result is kept well under the size limit.
"""

from __future__ import annotations

import io

from loguru import logger
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from .agent import Image

try:  # HEIC/HEIF (iPhone photos sent as files); optional.
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on the install
    HEIF_SUPPORTED = False

# Telegram's Bot API only lets bots download files up to 20 MB.
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
MAX_IMAGES_PER_TURN = 10
MAX_EDGE = 1568
# Per-image cap (raw bytes; base64 adds a third). The API limits a whole request
# to 32 MB and resends every earlier image of the conversation on each turn, so
# images are kept small: 1 MB each and 6 MB per turn in total.
MAX_ENCODED_BYTES = 1_000_000
TURN_IMAGE_BUDGET = 6_000_000


def per_image_budget(n_images: int) -> int:
    """Byte cap for each of *n_images* sent in one turn."""
    return min(MAX_ENCODED_BYTES, TURN_IMAGE_BUDGET // max(1, n_images))


SUPPORTED_MEDIA_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
_JPEG_QUALITIES = (85, 75, 60, 45)
_EXIF_ORIENTATION = 0x0112


class ImageError(ValueError):
    """An image we can't use; the message is shown to the user."""


def too_large_message(size: int, name: str = "That file") -> str:
    return (
        f"{name} is too large ({size / 1024 / 1024:.1f} MB). Telegram lets bots "
        "download files up to 20 MB; send a smaller image or a compressed photo."
    )


def normalize_image(
    data: bytes, *, max_edge: int = MAX_EDGE, max_bytes: int = MAX_ENCODED_BYTES
) -> Image:
    """Return *data* as an image Claude accepts.

    Supported formats within the limits are passed through unchanged; the
    rest are re-encoded (PNG for PNG sources and images with transparency,
    JPEG otherwise). Raises ImageError for unreadable or unsupported input.
    """
    try:
        img = PILImage.open(io.BytesIO(data))
        fmt = (img.format or "").upper()
        if fmt == "JPEG":
            img.draft("RGB", (max_edge, max_edge))  # fast DCT downscale on decode
        img.load()
    except PILImage.DecompressionBombError as exc:
        raise ImageError("That image has too many pixels to process.") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        hint = "" if HEIF_SUPPORTED else " (HEIC needs the optional pillow-heif package)"
        raise ImageError(f"I couldn't read that image; its format isn't supported{hint}.") from exc

    media_type = SUPPORTED_MEDIA_TYPES.get(fmt)
    oriented = _orientation(img) in (None, 1)
    if media_type is not None and oriented and max(img.size) <= max_edge and len(data) <= max_bytes:
        return Image(bytes(data), media_type)

    img = ImageOps.exif_transpose(img)  # also takes the first frame of animations
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), PILImage.Resampling.LANCZOS)
    out = _encode(img, prefer_png=fmt == "PNG", max_bytes=max_bytes)
    logger.debug("Normalized {} image ({} bytes) -> {}", fmt or "?", len(data), out.media_type)
    return out


def _orientation(img: PILImage.Image) -> int | None:
    try:
        return img.getexif().get(_EXIF_ORIENTATION)
    except Exception:  # noqa: BLE001 - broken EXIF shouldn't block the image
        return None


def _has_alpha(img: PILImage.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)


def _encode(img: PILImage.Image, *, prefer_png: bool, max_bytes: int) -> Image:
    if prefer_png or _has_alpha(img):
        png = img if img.mode in ("RGB", "RGBA", "L", "LA", "P") else img.convert("RGBA")
        buf = io.BytesIO()
        png.save(buf, "PNG", optimize=True)
        if buf.tell() <= max_bytes:
            return Image(buf.getvalue(), "image/png")
    rgb = _flatten(img)
    while True:
        for quality in _JPEG_QUALITIES:
            buf = io.BytesIO()
            rgb.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                return Image(buf.getvalue(), "image/jpeg")
        if max(rgb.size) <= 64:
            raise ImageError("I couldn't shrink that image enough to send it to the model.")
        rgb = rgb.resize((max(1, rgb.width * 3 // 4), max(1, rgb.height * 3 // 4)))


def _flatten(img: PILImage.Image) -> PILImage.Image:
    """RGB for JPEG, with any transparency composited onto white."""
    if _has_alpha(img):
        rgba = img.convert("RGBA")
        bg = PILImage.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img.convert("RGB") if img.mode != "RGB" else img
