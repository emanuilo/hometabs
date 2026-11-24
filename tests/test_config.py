import os
from pathlib import Path

import pytest

from hometabs.config import (
    DEFAULT_ASIDE_SKILLS_DIR,
    ConfigError,
    load_settings,
    parse_allowed_user_ids,
    unset_env_vars,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, set()),
        ("", set()),
        ("  ", set()),
        ("123", {123}),
        ("123,456", {123, 456}),
        (" 123 , 456 ,, ", {123, 456}),
        ("123 456;789", {123, 456, 789}),
        ("-100123", {-100123}),
    ],
)
def test_parse_allowed_user_ids(raw, expected):
    assert parse_allowed_user_ids(raw) == frozenset(expected)


def test_parse_allowed_user_ids_rejects_garbage():
    with pytest.raises(ConfigError):
        parse_allowed_user_ids("123,@someone")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in (
        "TELEGRAM_BOT_TOKEN",
        "ALLOWED_USER_IDS",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ASIDE_COMMAND",
        "ASIDE_SKILLS_DIR",
        "HOMETABS_MODEL",
        "HOMETABS_EFFORT",
        "STATE_FILE",
    ):
        monkeypatch.delenv(k, raising=False)
    empty = tmp_path / ".env"
    empty.write_text("")
    return empty


def test_load_settings_requires_allowlist(monkeypatch, clean_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        load_settings(clean_env)


def test_load_settings_requires_token(monkeypatch, clean_env):
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_settings(clean_env)


def test_load_settings_ok(monkeypatch, clean_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1,2")
    monkeypatch.setenv("ASIDE_COMMAND", "/opt/aside")
    s = load_settings(clean_env)
    assert s.allowed_user_ids == {1, 2}
    assert s.agent.aside_command == "/opt/aside"
    assert s.agent.model == "claude-sonnet-5-5"
    assert s.agent.effort == "medium"
    assert s.agent.api_key is None
    assert s.agent.oauth_token is None
    assert s.agent.aside_skills_dir == DEFAULT_ASIDE_SKILLS_DIR
    assert s.state_file == Path(".state/sessions.json")


def test_load_settings_paths_from_env(monkeypatch, clean_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("ASIDE_SKILLS_DIR", "~/aside-skills")
    monkeypatch.setenv("STATE_FILE", "/var/lib/bot/sessions.json")
    s = load_settings(clean_env)
    assert s.agent.aside_skills_dir == Path.home() / "aside-skills"
    assert s.state_file == Path("/var/lib/bot/sessions.json")


def test_load_settings_model_effort_and_credentials(monkeypatch, clean_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("HOMETABS_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("HOMETABS_EFFORT", "high")
    monkeypatch.setenv("ANTHROPIC_API_KEY", " sk-test-key-xyz ")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-test-xyz")
    s = load_settings(clean_env)
    assert (s.agent.model, s.agent.effort) == ("claude-opus-5-5", "high")
    assert (s.agent.api_key, s.agent.oauth_token) == ("sk-test-key-xyz", "oat-test-xyz")
    text = repr(s)
    assert "sk-test-key-xyz" not in text and "oat-test-xyz" not in text and "123:abc" not in text


def test_old_env_var_names_are_not_read(monkeypatch, clean_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("ASIDE_BOT_MODEL", "claude-old")
    monkeypatch.setenv("ASIDE_BOT_EFFORT", "max")
    s = load_settings(clean_env)
    assert (s.agent.model, s.agent.effort) == ("claude-sonnet-5-5", "medium")


def test_env_file_is_found_from_the_working_directory(monkeypatch, clean_env, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text("TELEGRAM_BOT_TOKEN=123:fromcwd\nALLOWED_USER_IDS=7\n")
    for name in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS"):
        # Record the vars so monkeypatch removes what load_dotenv sets.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.chdir(work)
    s = load_settings()
    assert s.telegram_bot_token == "123:fromcwd" and s.allowed_user_ids == {7}


def test_no_env_file_outside_the_working_directory_tree(monkeypatch, clean_env, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_settings()


def test_dotenv_default_search_is_never_used(monkeypatch, clean_env, tmp_path):
    # load_dotenv(None) searches from the module's directory (a source
    # checkout's .env), so it must not be called when the cwd has no .env.
    import hometabs.config as C

    calls = []
    monkeypatch.setattr(C, "load_dotenv", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(C, "find_dotenv", lambda **k: "")
    monkeypatch.chdir(tmp_path)
    C.load_agent_config()
    assert calls == []


def test_unset_env_vars_removes_only_the_given_names(monkeypatch):
    monkeypatch.setenv("HOMETABS_TEST_SECRET", "x")
    monkeypatch.setenv("HOMETABS_TEST_KEEP", "y")
    monkeypatch.delenv("HOMETABS_TEST_UNSET", raising=False)
    removed = unset_env_vars(["HOMETABS_TEST_SECRET", "HOMETABS_TEST_UNSET"])
    assert removed == ["HOMETABS_TEST_SECRET"]
    assert "HOMETABS_TEST_SECRET" not in os.environ
    assert os.environ["HOMETABS_TEST_KEEP"] == "y"


def test_base_url_is_captured(monkeypatch, clean_env):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert load_settings(clean_env).anthropic_base_url is None
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    assert load_settings(clean_env).anthropic_base_url == "https://proxy.example"
