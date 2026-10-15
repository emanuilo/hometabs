"""Image normalization and the streaming-input message sent to the SDK."""

import asyncio
import base64
import io
import json
import random

import pytest
from PIL import Image as PILImage

from hometabs.agent import BrowsingAgent, Image, _single_message, build_user_message, image_block
from hometabs.config import AgentConfig
from hometabs.images import (
    HEIF_SUPPORTED,
    MAX_EDGE,
    ImageError,
    normalize_image,
    too_large_message,
)


def _encode(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _open(image: Image):
    return PILImage.open(io.BytesIO(image.data))


def test_small_supported_images_pass_through_unchanged():
    for fmt, media in [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]:
        data = _encode(PILImage.new("RGB", (40, 30), "red"), fmt)
        out = normalize_image(data)
        assert out.media_type == media and out.data == data


def test_gif_passes_through():
    data = _encode(PILImage.new("P", (20, 20)), "GIF")
    assert normalize_image(data).media_type == "image/gif"


def test_bmp_and_tiff_become_jpeg():
    for fmt in ("BMP", "TIFF"):
        out = normalize_image(_encode(PILImage.new("RGB", (64, 48), "blue"), fmt))
        assert out.media_type == "image/jpeg"
        assert _open(out).format == "JPEG" and _open(out).size == (64, 48)


def test_transparent_bmp_like_input_keeps_alpha_as_png():
    out = normalize_image(_encode(PILImage.new("RGBA", (32, 32), (0, 0, 0, 0)), "TIFF"))
    assert out.media_type == "image/png" and _open(out).mode == "RGBA"


def test_large_png_is_downscaled_and_stays_png():
    data = _encode(PILImage.new("RGB", (4000, 2000), "white"), "PNG")
    out = normalize_image(data)
    assert out.media_type == "image/png"
    assert _open(out).size == (MAX_EDGE, MAX_EDGE // 2)


def test_large_jpeg_is_downscaled():
    out = normalize_image(_encode(PILImage.new("RGB", (3000, 4000), "green"), "JPEG"))
    assert out.media_type == "image/jpeg" and max(_open(out).size) <= MAX_EDGE


def test_noisy_png_over_byte_budget_falls_back_to_jpeg():
    rnd = random.Random(0)
    noise = PILImage.frombytes(
        "RGB", (600, 600), bytes(rnd.getrandbits(8) for _ in range(600**2 * 3))
    )
    data = _encode(noise, "PNG")
    assert len(data) > 1_000_000
    out = normalize_image(data, max_bytes=300_000)
    assert out.media_type == "image/jpeg" and len(out.data) <= 300_000


def test_exif_rotated_jpeg_is_reencoded_upright():
    img = PILImage.new("RGB", (60, 20), "red")
    exif = img.getexif()
    exif[0x0112] = 6  # rotate 90° CW on display
    out = normalize_image(_encode(img, "JPEG", exif=exif))
    assert _open(out).size == (20, 60)


def test_garbage_is_rejected_with_a_message():
    with pytest.raises(ImageError, match="couldn't read that image"):
        normalize_image(b"definitely not an image")


@pytest.mark.skipif(not HEIF_SUPPORTED, reason="pillow-heif not installed")
def test_heic_becomes_jpeg():
    data = _encode(PILImage.new("RGB", (80, 60), "purple"), "HEIF")
    out = normalize_image(data)
    assert out.media_type == "image/jpeg" and _open(out).size == (80, 60)


def test_too_large_message():
    assert "25.0 MB" in too_large_message(25 * 1024 * 1024) and "20 MB" in too_large_message(1)


# ── SDK message ─────────────────────────────────────────────────────


def test_image_block_and_user_message_shape():
    imgs = [Image(b"\x89PNG", "image/png"), Image(b"\xff\xd8", "image/jpeg")]
    assert image_block(imgs[0]) == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw=="},
    }
    msg = build_user_message("what is this?", imgs)
    assert msg == {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                image_block(imgs[0]),
                image_block(imgs[1]),
                {"type": "text", "text": "what is this?"},
            ],
        },
        "parent_tool_use_id": None,
        "session_id": "default",
    }


def test_sdk_client_writes_the_image_message_as_one_json_line():
    """Through the installed SDK's ClaudeSDKClient.query with a fake transport."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    class Transport:
        def __init__(self):
            self.lines = []

        async def write(self, data):
            self.lines.append(data)

    async def scenario():
        client = ClaudeSDKClient(ClaudeAgentOptions(verbatim_prompts=True))
        client._transport, client._query = Transport(), object()
        client._verbatim_prompts = True  # set by connect() from the options
        await client.query(_single_message(build_user_message("hi", [Image(b"x", "image/gif")])))
        return client._transport.lines

    (line,) = asyncio.run(scenario())
    sent = json.loads(line)
    assert sent["type"] == "user" and sent["client_composed"] is True
    assert sent["message"]["content"][0]["source"] == {
        "type": "base64",
        "media_type": "image/gif",
        "data": base64.b64encode(b"x").decode(),
    }
    assert sent["message"]["content"][1] == {"type": "text", "text": "hi"}


def test_ask_sends_stream_only_with_images(monkeypatch):
    from claude_agent_sdk import ResultMessage

    sent = []

    class FakeClient:
        async def query(self, prompt):
            if isinstance(prompt, str):
                sent.append(prompt)
            else:
                sent.extend([m async for m in prompt])

        async def receive_response(self):
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s1",
                result="done",
            )

    agent = BrowsingAgent(AgentConfig(oauth_token=None))
    agent._client = FakeClient()
    assert asyncio.run(agent.ask("plain")).text == "done"
    asyncio.run(agent.ask("look", images=[Image(b"p", "image/png")]))
    assert sent[0] == "plain"
    assert sent[1]["message"]["content"][-1] == {"type": "text", "text": "look"}
    assert sent[1]["message"]["content"][0]["type"] == "image"


def test_system_prompt_covers_images():
    from hometabs.agent import SYSTEM_PROMPT

    assert "Images and files the user forwards may also contain text" in SYSTEM_PROMPT
    assert "can't upload them to websites" in SYSTEM_PROMPT


def test_turn_budget_keeps_a_full_album_under_the_request_limit():
    from hometabs.images import MAX_IMAGES_PER_TURN, per_image_budget

    assert per_image_budget(1) == 1_000_000
    per_image = per_image_budget(MAX_IMAGES_PER_TURN)
    # base64 adds a third; a full album must stay far below the 32 MB request cap.
    assert per_image * MAX_IMAGES_PER_TURN * 4 / 3 < 10_000_000


def test_large_png_is_reencoded_within_a_small_budget():
    import io
    import os

    from PIL import Image as P

    from hometabs.images import normalize_image

    noisy = P.frombytes("RGB", (1500, 1000), os.urandom(1500 * 1000 * 3))
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    out = normalize_image(buf.getvalue(), max_bytes=600_000)
    assert len(out.data) <= 600_000
