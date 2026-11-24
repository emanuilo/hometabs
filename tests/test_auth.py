import pytest
from loguru import logger

from hometabs.auth import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_LOCAL,
    AUTH_MODE_OAUTH,
    SCRUBBED_ENV_VARS,
    build_cli_env,
    log_auth_choice,
    select_credential,
)

API_KEY = "sk-ant-api03-test-key-should-not-leak"
OAUTH = "sk-ant-oat01-test-token-should-not-leak"


@pytest.mark.parametrize(
    "api_key, oauth, mode, secret",
    [
        (API_KEY, OAUTH, AUTH_MODE_API_KEY, API_KEY),  # API key wins
        (API_KEY, None, AUTH_MODE_API_KEY, API_KEY),
        (f"  {API_KEY}\n", "", AUTH_MODE_API_KEY, API_KEY),  # stripped
        (None, OAUTH, AUTH_MODE_OAUTH, OAUTH),
        ("   ", OAUTH, AUTH_MODE_OAUTH, OAUTH),  # blank key is ignored
        (None, None, AUTH_MODE_LOCAL, None),
        ("", "  ", AUTH_MODE_LOCAL, None),
    ],
)
def test_select_credential_precedence(api_key, oauth, mode, secret):
    cred = select_credential(api_key, oauth)
    assert (cred.mode, cred.secret) == (mode, secret)


def test_credential_repr_hides_secret():
    cred = select_credential(API_KEY, None)
    assert API_KEY not in repr(cred) and API_KEY not in str(cred)
    assert "api_key" in repr(cred)


def test_api_key_is_only_credential():
    env, mode = build_cli_env(API_KEY, None)
    assert mode == AUTH_MODE_API_KEY
    assert env == {"ANTHROPIC_API_KEY": API_KEY}


def test_api_key_wins_over_oauth_token():
    env, mode = build_cli_env(API_KEY, OAUTH)
    assert mode == AUTH_MODE_API_KEY
    assert env == {"ANTHROPIC_API_KEY": API_KEY}


def test_oauth_token_is_only_credential():
    env, mode = build_cli_env(None, OAUTH)
    assert mode == AUTH_MODE_OAUTH
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH}


def test_nothing_set_falls_back_to_local_login_without_credential():
    for api_key, token in ((None, None), ("", ""), ("   ", "   ")):
        env, mode = build_cli_env(api_key, token)
        assert mode == AUTH_MODE_LOCAL
        assert env == {}


def test_only_scrubbed_vars_can_carry_the_credential():
    for api_key, token in ((API_KEY, None), (None, OAUTH)):
        env, _ = build_cli_env(api_key, token)
        assert set(env) <= set(SCRUBBED_ENV_VARS)


@pytest.fixture
def log_lines():
    lines: list[str] = []
    handler = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    yield lines
    logger.remove(handler)


@pytest.mark.parametrize(
    "api_key, oauth, mode, expect",
    [
        (API_KEY, OAUTH, AUTH_MODE_API_KEY, "using the API key"),
        (API_KEY, None, AUTH_MODE_API_KEY, "ANTHROPIC_API_KEY"),
        (None, OAUTH, AUTH_MODE_OAUTH, "CLAUDE_CODE_OAUTH_TOKEN"),
        (None, None, AUTH_MODE_LOCAL, "local Claude Code login"),
    ],
)
def test_log_auth_choice_logs_mode_never_values(log_lines, api_key, oauth, mode, expect):
    assert log_auth_choice(api_key, oauth) == mode
    text = "\n".join(log_lines)
    assert mode in text and expect in text
    assert API_KEY not in text and OAUTH not in text
    if not (api_key and oauth):
        assert "Both" not in text
