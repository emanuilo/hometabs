"""/model, /effort and their inline buttons, driven with fake Telegram objects."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from hometabs.agent import BrowsingAgent, TurnResult
from hometabs.bot import (
    BOT_COMMANDS,
    CALLBACK_DATA_MAX_BYTES,
    HometabsBot,
    PreferenceStore,
    model_callback_data,
)
from hometabs.config import AgentConfig, Settings
from hometabs.models import ModelInfo

ALLOWED, STRANGER, CHAT = 1, 2, 100

MODELS = [
    ModelInfo(
        "claude-sonnet-5-5",
        "Claude Sonnet 5.5",
        "2026-09-28",
        ["low", "medium", "high", "xhigh", "max"],
    ),
    ModelInfo(
        "claude-opus-4-5-20251101", "Claude Opus 4.5", "2025-11-24", ["low", "medium", "high"]
    ),
    ModelInfo("claude-haiku-4-5-20251001", "Claude Haiku 4.5", "2025-10-15", []),
]


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.message_id = 7
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.replies.append((text, reply_markup))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

    async def send_chat_action(self, *a, **kw):
        pass


class FakeAgent:
    def __init__(self, config, session_id="sess-1"):
        self.config = config
        self.session_id = session_id
        self.closed = False
        self.asked = []

    async def close(self):
        self.closed = True

    async def ask(self, prompt, images=None, on_tool=None):
        self.asked.append(prompt)
        return TurnResult(text="ok", session_id=self.session_id)


def _update(user_id=ALLOWED, text="", query=None):
    return SimpleNamespace(
        effective_message=FakeMessage(text),
        effective_chat=SimpleNamespace(id=CHAT, type="private"),
        effective_user=SimpleNamespace(id=user_id, username="u"),
        callback_query=query,
    )


def _ctx(*args):
    return SimpleNamespace(args=list(args), bot=FakeBot())


@pytest.fixture
def bot(tmp_path, aside_skills_dir):
    settings = Settings(
        telegram_bot_token="x",
        allowed_user_ids=frozenset({ALLOWED}),
        agent=AgentConfig(  # no token: no network
            oauth_token=None, aside_command="/bin/aside", aside_skills_dir=aside_skills_dir
        ),
        state_file=tmp_path / "sessions.json",
    )
    b = HometabsBot(settings)
    b.catalog.models = list(MODELS)
    return b


def _buttons(markup):
    return [btn for row in markup.inline_keyboard for btn in row]


def run(coro):
    return asyncio.run(coro)


# ── /model ──────────────────────────────────────────────────────────


def test_model_lists_models_and_marks_current(bot):
    upd = _update()
    run(bot.cmd_model(upd, _ctx()))
    text, markup = upd.effective_message.replies[-1]
    assert "Claude Sonnet 5.5 (claude-sonnet-5-5)" in text and "Effort: medium" in text
    labels = [b.text for b in _buttons(markup)]
    assert labels == ["✓ Claude Sonnet 5.5", "Claude Opus 4.5", "Claude Haiku 4.5"]
    assert [b.callback_data for b in _buttons(markup)][1] == "m:claude-opus-4-5-20251101"


def test_button_tap_by_allowed_user_switches_and_persists(bot, tmp_path):
    q = FakeQuery("m:claude-opus-4-5-20251101")
    run(bot.on_callback(_update(query=q), _ctx()))
    assert q.answers == ["Model: Claude Opus 4.5"]
    text, markup = q.edits[-1]
    assert "Switched to Claude Opus 4.5" in text
    assert _buttons(markup)[1].text == "✓ Claude Opus 4.5"
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved == {"model": "claude-opus-4-5-20251101", "effort": "medium"}
    # Survives a restart.
    assert (
        PreferenceStore(tmp_path / "settings.json", "d", "low").model == "claude-opus-4-5-20251101"
    )


def test_button_tap_by_stranger_is_rejected(bot, tmp_path):
    q = FakeQuery("m:claude-haiku-4-5-20251001")
    run(bot.on_callback(_update(user_id=STRANGER, query=q), _ctx()))
    assert q.answers == ["You're not allowed to use this bot."]
    assert q.edits == []
    assert bot.prefs.model == "claude-sonnet-5-5"
    assert not (tmp_path / "settings.json").exists()
    q = FakeQuery("e:max")
    run(bot.on_callback(_update(user_id=STRANGER, query=q), _ctx()))
    assert bot.prefs.effort == "medium" and q.edits == []


def test_switch_to_model_without_effort_omits_it(bot):
    upd = _update()
    run(bot.cmd_model(upd, _ctx("claude-haiku-4-5")))  # alias resolves to dated id
    text, _ = upd.effective_message.replies[-1]
    assert bot.prefs.model == "claude-haiku-4-5-20251001"
    assert "doesn't support effort" in text
    cfg = bot._agent_config()
    assert cfg.model == "claude-haiku-4-5-20251001" and cfg.effort is None
    assert "--effort" not in _cli_cmd(cfg)


def test_switch_drops_unsupported_level(bot):
    bot.prefs.update(effort="max")
    upd = _update()
    run(bot.cmd_model(upd, _ctx("claude-opus-4-5-20251101")))
    assert "doesn't support 'max' effort; using 'high'" in upd.effective_message.replies[-1][0]
    assert bot._agent_config().effort == "high"


def test_unknown_model_rejected_when_list_known(bot):
    upd = _update()
    run(bot.cmd_model(upd, _ctx("claude-typo")))
    assert "Unknown model" in upd.effective_message.replies[-1][0]
    assert bot.prefs.model == "claude-sonnet-5-5"


def test_model_without_list_accepts_loose_id(bot):
    bot.catalog.models = []
    upd = _update()
    run(bot.cmd_model(upd, _ctx()))
    text, markup = upd.effective_message.replies[-1]
    assert "Model: claude-sonnet-5-5" in text and "Couldn't load the model list" in text
    assert markup is None
    run(bot.cmd_model(upd, _ctx("gpt-4")))
    assert bot.prefs.model == "claude-sonnet-5-5"
    run(bot.cmd_model(upd, _ctx("claude-opus-9")))
    assert bot.prefs.model == "claude-opus-9" and bot._agent_config().effort == "medium"


# ── /effort ─────────────────────────────────────────────────────────


def test_effort_buttons_follow_model_capabilities(bot):
    bot.prefs.update(model="claude-opus-4-5-20251101")
    upd = _update()
    run(bot.cmd_effort(upd, _ctx()))
    _, markup = upd.effective_message.replies[-1]
    assert [b.callback_data for b in _buttons(markup)] == ["e:low", "e:medium", "e:high"]
    assert _buttons(markup)[1].text == "✓ medium"


def test_effort_unsupported_level_is_rejected(bot):
    bot.prefs.update(model="claude-opus-4-5-20251101")
    upd = _update()
    run(bot.cmd_effort(upd, _ctx("max")))
    assert "doesn't support 'max'" in upd.effective_message.replies[-1][0]
    assert bot.prefs.effort == "medium"
    run(bot.cmd_effort(upd, _ctx("bogus")))
    assert "Unknown effort" in upd.effective_message.replies[-1][0]
    q = FakeQuery("e:max")
    run(bot.on_callback(_update(query=q), _ctx()))
    assert "doesn't support 'max'" in q.answers[-1] and q.edits == []


def test_effort_on_model_without_support(bot):
    bot.prefs.update(model="claude-haiku-4-5-20251001")
    upd = _update()
    run(bot.cmd_effort(upd, _ctx()))
    text, markup = upd.effective_message.replies[-1]
    assert "doesn't support an effort setting" in text and markup is None
    run(bot.cmd_effort(upd, _ctx("low")))
    assert "doesn't support an effort setting" in upd.effective_message.replies[-1][0]


def test_effort_button_sets_level(bot):
    q = FakeQuery("e:high")
    run(bot.on_callback(_update(query=q), _ctx()))
    assert q.answers == ["Effort: high"] and bot.prefs.effort == "high"
    assert "Effort set to high" in q.edits[-1][0]
    assert bot._agent_config().effort == "high"


# ── applying changes to live agents ─────────────────────────────────


def test_idle_agent_switches_on_next_message_keeping_session(bot, monkeypatch):
    async def scenario():
        state = bot._chat(CHAT)
        old = FakeAgent(bot._agent_config(), session_id="sess-1")
        state.agent = old
        upd = _update()
        await bot.cmd_model(upd, _ctx("claude-opus-4-5-20251101"))
        assert "Applies from your next message" in upd.effective_message.replies[-1][0]
        assert not old.closed  # nothing is torn down by the command itself

        created = []

        def fake_new_agent(chat_id):
            cfg = bot._agent_config()
            created.append((cfg, bot.sessions.get(chat_id, bot.fingerprint)))
            return FakeAgent(cfg, session_id="sess-1"), False

        monkeypatch.setattr(bot, "_new_agent", fake_new_agent)
        await bot.on_text(_update(text="hi again"), _ctx())
        assert old.closed
        cfg, resume = created[0]
        assert cfg.model == "claude-opus-4-5-20251101" and resume == "sess-1"

    run(scenario())


def test_model_command_does_not_wait_for_queued_turn(bot):
    async def scenario():
        state = bot._chat(CHAT)
        state.agent = FakeAgent(bot._agent_config(), session_id="sess-1")
        await state.lock.acquire()  # running turn
        waiter = asyncio.ensure_future(state.lock.acquire())  # queued message
        await asyncio.sleep(0)
        upd = _update()
        await asyncio.wait_for(bot.cmd_model(upd, _ctx("claude-opus-4-5-20251101")), timeout=1)
        assert "A task is running" in upd.effective_message.replies[-1][0]
        waiter.cancel()

    run(scenario())


def test_change_during_running_turn_applies_after_it(bot, monkeypatch):
    async def scenario():
        state = bot._chat(CHAT)
        old = FakeAgent(bot._agent_config(), session_id="sess-1")
        state.agent = old
        await state.lock.acquire()  # a turn is running
        upd = _update()
        await bot.cmd_model(upd, _ctx("claude-opus-4-5-20251101"))
        assert "A task is running" in upd.effective_message.replies[-1][0]
        assert not old.closed and state.agent is old  # running turn untouched
        state.lock.release()

        created = []

        def fake_new_agent(chat_id):
            cfg = bot._agent_config()
            created.append((cfg, bot.sessions.get(chat_id, bot.fingerprint)))
            return FakeAgent(cfg, session_id="sess-1"), False

        monkeypatch.setattr(bot, "_new_agent", fake_new_agent)
        await bot.on_text(_update(text="what did I say?"), _ctx())
        assert old.closed
        cfg, resume = created[0]
        assert cfg.model == "claude-opus-4-5-20251101" and resume == "sess-1"
        assert state.agent.asked == ["what did I say?"]

    run(scenario())


def test_fingerprint_ignores_model_and_effort(bot):
    fp = bot.fingerprint
    bot.prefs.update(model="claude-haiku-4-5-20251001", effort="low")
    assert HometabsBot(bot.settings).fingerprint == fp


def _cli_cmd(cfg, resume=None):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    transport = SubprocessCLITransport(prompt="", options=BrowsingAgent(cfg).build_options(resume))
    transport._cli_path = "claude"
    return transport._build_command()


def test_new_agent_resumes_with_selected_options(bot):
    bot.sessions.set(CHAT, "sess-9", bot.fingerprint)
    bot.prefs.update(model="claude-opus-4-5-20251101", effort="low")
    agent, stale = bot._new_agent(CHAT)
    assert not stale
    cmd = _cli_cmd(agent.config, resume=agent._resume)
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-5-20251101"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert "--resume=sess-9" in cmd


# ── misc ────────────────────────────────────────────────────────────


def test_callback_data_fits_telegram_limit(bot):
    long_id = "claude-" + "x" * 80
    assert model_callback_data(3, long_id) == "mi:3"
    assert model_callback_data(0, "claude-opus-5") == "m:claude-opus-5"
    bot.catalog.models = [*MODELS, ModelInfo(long_id, "Long", "", ["low"])]
    upd = _update()
    run(bot.cmd_model(upd, _ctx()))
    for btn in _buttons(upd.effective_message.replies[-1][1]):
        assert len(btn.callback_data.encode()) <= CALLBACK_DATA_MAX_BYTES
    q = FakeQuery("mi:3")
    run(bot.on_callback(_update(query=q), _ctx()))
    assert bot.prefs.model == long_id
    q = FakeQuery("mi:99")
    run(bot.on_callback(_update(query=q), _ctx()))
    assert "no longer available" in q.answers[-1]


def test_preferences_defaults_and_bad_file(tmp_path):
    p = tmp_path / "settings.json"
    s = PreferenceStore(p, "claude-sonnet-5-5", "medium")
    assert (s.model, s.effort) == ("claude-sonnet-5-5", "medium")
    p.write_text('{"model": "claude-x", "effort": "turbo"}')
    s = PreferenceStore(p, "d", "medium")
    assert (s.model, s.effort) == ("claude-x", "medium")  # unknown effort ignored
    p.write_text("garbage")
    assert PreferenceStore(p, "d", "low").model == "d"


def test_command_menu_and_help():
    from hometabs.bot import HELP_TEXT

    assert [c.command for c in BOT_COMMANDS] == ["start", "new", "stop", "model", "effort"]
    assert "/model" in HELP_TEXT and "/effort" in HELP_TEXT


def test_bot_builds_plugin_in_state_dir(bot, tmp_path):
    assert bot.plugin.path == (tmp_path / "plugin").resolve()
    cfg = bot._agent_config()
    assert cfg.plugin_dir == bot.plugin.path
    assert cfg.skills == ("hometabs:sign-in", "hometabs:1password")
    assert "Skill(hometabs:1password)" in _cli_cmd(cfg)[_cli_cmd(cfg).index("--allowedTools") + 1]
