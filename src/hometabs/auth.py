"""Auth for the Claude Code CLI subprocess.

The Agent SDK shells out to the Claude Code CLI and *merges* ``options.env``
over the parent process environment, so passing a scrubbed dict alone is not
enough to keep a stray credential out of the subprocess. We therefore
(1) remove every credential / provider var in :data:`SCRUBBED_ENV_VARS` from
the environment of this (dedicated) bot process at startup and (2) pass an
explicit env that contains exactly the one credential chosen here.

Precedence:
- ``api_key``      -> ANTHROPIC_API_KEY from the Claude Console (recommended)
- ``oauth_token``  -> CLAUDE_CODE_OAUTH_TOKEN from ``claude setup-token``
- ``local_login``  -> no credential; the CLI uses the local Claude Code login
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

# Every env var that could make the CLI authenticate with something other
# than the credential we inject (or route requests to another provider).
SCRUBBED_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

AUTH_MODE_API_KEY = "api_key"
AUTH_MODE_OAUTH = "oauth_token"
AUTH_MODE_LOCAL = "local_login"

_MODE_ENV_VAR = {
    AUTH_MODE_API_KEY: "ANTHROPIC_API_KEY",
    AUTH_MODE_OAUTH: "CLAUDE_CODE_OAUTH_TOKEN",
}


@dataclass(frozen=True)
class Credential:
    """The credential chosen for Claude. ``secret`` is None for local_login."""

    mode: str
    secret: str | None = None

    def __repr__(self) -> str:  # never show the secret
        return f"Credential(mode={self.mode!r})"

    __str__ = __repr__


def _clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def select_credential(api_key: str | None, oauth_token: str | None) -> Credential:
    """Pick the credential: API key > OAuth token > local Claude Code login."""
    key = _clean(api_key)
    if key:
        return Credential(AUTH_MODE_API_KEY, key)
    token = _clean(oauth_token)
    if token:
        return Credential(AUTH_MODE_OAUTH, token)
    return Credential(AUTH_MODE_LOCAL)


def build_cli_env(api_key: str | None, oauth_token: str | None) -> tuple[dict[str, str], str]:
    """Build ``options.env`` for the Claude CLI subprocess.

    It holds exactly the credential chosen by :func:`select_credential`
    (``ANTHROPIC_API_KEY`` or ``CLAUDE_CODE_OAUTH_TOKEN``), or nothing for the
    local Claude Code login. The SDK merges it over ``os.environ``, which
    ``main()`` scrubbed of :data:`SCRUBBED_ENV_VARS` at startup.

    Returns ``(env, auth_mode)``.
    """
    cred = select_credential(api_key, oauth_token)
    env = {_MODE_ENV_VAR[cred.mode]: cred.secret} if cred.secret is not None else {}
    return env, cred.mode


def log_auth_choice(api_key: str | None, oauth_token: str | None) -> str:
    """Log which credential is used (names only, never values). Returns the mode."""
    mode = select_credential(api_key, oauth_token).mode
    if mode == AUTH_MODE_API_KEY:
        logger.info("Claude auth: {} (ANTHROPIC_API_KEY)", mode)
        if _clean(oauth_token):
            logger.info(
                "Both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN are set; using the API key"
            )
    elif mode == AUTH_MODE_OAUTH:
        logger.info("Claude auth: {} (CLAUDE_CODE_OAUTH_TOKEN)", mode)
    else:
        logger.warning(
            "Claude auth: {} (neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set; "
            "the Claude CLI uses the local Claude Code login)",
            mode,
        )
    return mode
