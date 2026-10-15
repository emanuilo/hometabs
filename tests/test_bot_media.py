"""Photo / image-file handling, driven with fake Telegram objects."""

import asyncio
import datetime as dt
import io
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage
from telegram import Chat, Document, Message, PhotoSize, Update, User
from telegram.error import BadRequest

import hometabs.bot as botmod
from hometabs.agent import TurnResult
from hometabs.bot import DEFAULT_IMAGE_PROMPT, UNSUPPORTED_TEXT, HometabsBot, media_ref
from hometabs.config import AgentConfig, Settings

ALLOWED, STRANGER, CHAT = 1, 2, 100


def _png(color="red", size=(40, 30)):
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class FakeMessage:
    _next_id = 10

    def __init__(self, *, photo=None, document=None, caption=None, media_group_id=None):
        FakeMessage._next_id += 1
        self.message_id = FakeMessage._next_id
        self.text = None
        self.photo = photo or ()
        self.document = document
        self.caption = caption
        self.media_group_id = media_group_id
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _photo(file_id="big", size=None):
    return [
        SimpleNamespace(file_id="thumb", file_size=1_000, width=90, height=60),
        SimpleNamespace(file_id=file_id, file_size=size, width=1280, height=853),
        SimpleNamespace(file_id="mid", file_size=40_000, width=320, height=213),
    ]


class FakeFile:
    def __init__(self, data):
        self.data = data

    async def download_as_bytearray(self):
        return bytearray(self.data)


class FakeBot:
    def __init__(self, files=None):
        self.files = files or {}
        self.sent = []
        self.downloads = []

    async def get_file(self, file_id):
        self.downloads.append(file_id)
        if file_id not in self.files:
            raise BadRequest("File is too big")
        return FakeFile(self.files[file_id])

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

    async def send_chat_action(self, *a, **kw):
        pass


class FakeAgent:
    def __init__(self, config=None, gate=None):
        self.config = config
        self.session_id = "sess-1"
        self.calls = []
        self.gate = gate

    async def ask(self, prompt, images=None, on_tool=None):
        self.calls.append((prompt, list(images or [])))
        if self.gate is not None:
            await self.gate.wait()
        return TurnResult(text=f"saw {len(images or [])}", session_id=self.session_id)

    async def close(self):
        pass


def _update(msg, user_id=ALLOWED):
    return SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHAT, type="private"),
        effective_user=SimpleNamespace(id=user_id, username="u"),
    )


@pytest.fixture
def bot(tmp_path, aside_skills_dir, monkeypatch):
    settings = Settings(
        telegram_bot_token="123:fake",
        allowed_user_ids=frozenset({ALLOWED}),
        agent=AgentConfig(
            oauth_token=None, aside_command="/bin/aside", aside_skills_dir=aside_skills_dir
        ),
        state_file=tmp_path / "sessions.json",
    )
    b = HometabsBot(settings)
    b.agent = FakeAgent(b._agent_config())
    monkeypatch.setattr(b, "_new_agent", lambda chat_id: (b.agent, False))
    monkeypatch.setattr(botmod, "ALBUM_DEBOUNCE_S", 0.05)
    return b


def run(coro):
    return asyncio.run(coro)


# ── selection ───────────────────────────────────────────────────────


def test_largest_photo_size_is_selected():
    ref = media_ref(FakeMessage(photo=_photo("big", 200_000)))
    assert (ref.file_id, ref.file_size) == ("big", 200_000)


def test_image_document_and_non_image_document():
    doc = SimpleNamespace(file_id="d1", file_size=5, mime_type="image/heic", file_name="a.heic")
    assert media_ref(FakeMessage(document=doc)).label == "a.heic"
    pdf = SimpleNamespace(file_id="d2", file_size=5, mime_type="application/pdf", file_name="x")
    assert media_ref(FakeMessage(document=pdf)) is None


# ── single images ───────────────────────────────────────────────────


def test_photo_with_caption_runs_one_turn_with_the_image(bot):
    tg = FakeBot({"big": _png()})
    msg = FakeMessage(photo=_photo("big", 1000), caption="What colour is this?")
    run(bot.on_media(_update(msg), SimpleNamespace(bot=tg)))
    assert tg.downloads == ["big"]
    ((prompt, images),) = bot.agent.calls
    assert prompt == "What colour is this?"
    assert len(images) == 1 and images[0].media_type == "image/png"
    assert tg.sent == ["saw 1"]


def test_photo_without_caption_uses_default_prompt(bot):
    tg = FakeBot({"big": _png()})
    run(bot.on_media(_update(FakeMessage(photo=_photo())), SimpleNamespace(bot=tg)))
    assert bot.agent.calls[0][0] == DEFAULT_IMAGE_PROMPT


def test_bmp_document_is_converted(bot):
    buf = io.BytesIO()
    PILImage.new("RGB", (10, 10)).save(buf, "BMP")
    doc = SimpleNamespace(
        file_id="d", file_size=len(buf.getvalue()), mime_type="image/bmp", file_name="x.bmp"
    )
    tg = FakeBot({"d": buf.getvalue()})
    run(bot.on_media(_update(FakeMessage(document=doc, caption="hi")), SimpleNamespace(bot=tg)))
    assert bot.agent.calls[0][1][0].media_type == "image/jpeg"


def test_file_over_telegram_limit_is_rejected_without_download(bot):
    doc = SimpleNamespace(
        file_id="huge", file_size=25 * 1024 * 1024, mime_type="image/png", file_name="huge.png"
    )
    tg = FakeBot()
    msg = FakeMessage(document=doc)
    run(bot.on_media(_update(msg), SimpleNamespace(bot=tg)))
    assert tg.downloads == [] and bot.agent.calls == []
    assert "huge.png is too large (25.0 MB)" in tg.sent[0] and "20 MB" in tg.sent[0]


def test_telegram_too_big_error_is_reported(bot):
    tg = FakeBot()  # get_file raises "File is too big"
    run(bot.on_media(_update(FakeMessage(photo=_photo("x"))), SimpleNamespace(bot=tg)))
    assert bot.agent.calls == [] and "too large" in tg.sent[0]


def test_unreadable_image_gets_a_clear_reply(bot):
    doc = SimpleNamespace(file_id="d", file_size=3, mime_type="image/x-weird", file_name="w")
    tg = FakeBot({"d": b"???"})
    run(bot.on_media(_update(FakeMessage(document=doc)), SimpleNamespace(bot=tg)))
    assert bot.agent.calls == [] and "couldn't read that image" in tg.sent[0]


# ── albums ──────────────────────────────────────────────────────────


def test_album_items_become_one_turn(bot):
    tg = FakeBot({"a": _png("red"), "b": _png("blue")})
    first = FakeMessage(photo=_photo("a"), caption="Compare these", media_group_id="g1")
    second = FakeMessage(photo=_photo("b"), media_group_id="g1")

    async def scenario():
        ctx = SimpleNamespace(bot=tg)
        # Delivered as separate, concurrently handled updates (second one first).
        t2 = asyncio.create_task(bot.on_media(_update(second), ctx))
        await asyncio.sleep(0.01)
        t1 = asyncio.create_task(bot.on_media(_update(first), ctx))
        await asyncio.gather(t1, t2)

    run(scenario())
    ((prompt, images),) = bot.agent.calls
    assert prompt == "Compare these" and len(images) == 2
    assert tg.downloads == ["a", "b"]  # sorted by message id
    assert tg.sent == ["saw 2"] and not bot._albums


def test_different_albums_are_separate_turns(bot):
    tg = FakeBot({"a": _png(), "b": _png()})

    async def scenario():
        ctx = SimpleNamespace(bot=tg)
        await asyncio.gather(
            bot.on_media(_update(FakeMessage(photo=_photo("a"), media_group_id="g1")), ctx),
            bot.on_media(_update(FakeMessage(photo=_photo("b"), media_group_id="g2")), ctx),
        )

    run(scenario())
    assert [len(imgs) for _, imgs in bot.agent.calls] == [1, 1]


def test_stop_during_album_debounce_drops_it(bot):
    tg = FakeBot({"a": _png()})

    async def scenario():
        task = asyncio.create_task(
            bot.on_media(
                _update(FakeMessage(photo=_photo("a"), media_group_id="g")), SimpleNamespace(bot=tg)
            )
        )
        await asyncio.sleep(0.01)
        bot._chat(CHAT).epoch += 1  # what /stop does
        await task

    run(scenario())
    assert bot.agent.calls == [] and tg.downloads == []


# ── queueing ────────────────────────────────────────────────────────


def test_image_queues_behind_running_turn(bot):
    tg = FakeBot({"a": _png()})

    async def scenario():
        bot.agent.gate = asyncio.Event()
        ctx = SimpleNamespace(bot=tg)
        text_msg = FakeMessage()
        text_msg.text = "first task"
        running = asyncio.create_task(bot.on_text(_update(text_msg), ctx))
        await asyncio.sleep(0.01)
        photo_msg = FakeMessage(photo=_photo("a"), caption="then this")
        queued = asyncio.create_task(bot.on_media(_update(photo_msg), ctx))
        await asyncio.sleep(0.05)
        assert photo_msg.replies and photo_msg.replies[0].startswith("Queued")
        assert len(bot.agent.calls) == 1  # still waiting for the lock
        bot.agent.gate.set()
        await asyncio.gather(running, queued)

    run(scenario())
    assert [(p, len(i)) for p, i in bot.agent.calls] == [("first task", 0), ("then this", 1)]


# ── routing / allowlist ─────────────────────────────────────────────


def _real_update(user_id, **kw):
    msg = Message(
        message_id=1,
        date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=CHAT, type="private"),
        from_user=User(id=user_id, first_name="x", is_bot=False),
        **kw,
    )
    return Update(update_id=1, message=msg)


def _route(app, update):
    """The callback of the first handler that accepts *update* (PTB's order)."""
    for handler in app.handlers[0]:
        check = handler.check_update(update)
        if check is not None and check is not False:
            return handler.callback
    return None


def test_routing_and_allowlist(bot):
    app = bot.build_application()
    photo = [PhotoSize("f", "u", 90, 90)]
    image_doc = Document("d", "u", file_name="a.png", mime_type="image/png")
    pdf_doc = Document("d", "u", file_name="a.pdf", mime_type="application/pdf")
    assert _route(app, _real_update(ALLOWED, photo=photo)) == bot.on_media
    assert _route(app, _real_update(ALLOWED, document=image_doc)) == bot.on_media
    assert _route(app, _real_update(ALLOWED, document=pdf_doc)) == bot.on_other
    assert _route(app, _real_update(STRANGER, photo=photo)) == bot.on_other
    assert _route(app, _real_update(STRANGER, document=image_doc)) == bot.on_other


def test_stranger_photo_is_rejected_and_non_image_gets_hint(bot):
    stranger_msg = FakeMessage(photo=_photo())
    run(bot.on_other(_update(stranger_msg, user_id=STRANGER), SimpleNamespace(bot=FakeBot())))
    assert "not allowed" in stranger_msg.replies[0]
    voice = FakeMessage()
    run(bot.on_other(_update(voice), SimpleNamespace(bot=FakeBot())))
    assert voice.replies == [UNSUPPORTED_TEXT]
    assert bot.agent.calls == []
