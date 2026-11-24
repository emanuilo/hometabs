"""Configuration loaded from environment / .env."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

DEFAULT_ASIDE_COMMAND = shutil.which("aside") or str(Path.home() / ".local/bin/aside")
# Where Aside keeps its builtin skills (profile 0); see skills.py.
DEFAULT_ASIDE_SKILLS_DIR = Path.home() / ".aside" / "u" / "0" / "skills" / "builtin"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "medium"


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def parse_allowed_user_ids(raw: str | None) -> frozenset[int]:
    """Parse a comma/whitespace separated list of Telegram user IDs.

    Raises ConfigError on any non-integer entry so a typo can't silently
    lock out (or worse, mis-configure) the allowlist.
    """
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"ALLOWED_USER_IDS contains a non-integer entry: {part!r}") from exc
    return frozenset(ids)


@dataclass(frozen=True)
class AgentConfig:
    """Everything the agent core needs (no Telegram)."""

    aside_command: str = DEFAULT_ASIDE_COMMAND
    aside_args: tuple[str, ...] = ("mcp",)
    model: str = DEFAULT_MODEL
    effort: str | None = DEFAULT_EFFORT  # None: let the CLI/model default apply
    # Claude credentials; see auth.select_credential for the precedence.
    # Kept out of repr so a logged config never shows them.
    api_key: str | None = field(default=None, repr=False)
    oauth_token: str | None = field(default=None, repr=False)
    max_turns: int | None = 60
    cwd: Path | None = None
    # Source of Aside's builtin skills, copied into the plugin at startup.
    aside_skills_dir: Path | None = DEFAULT_ASIDE_SKILLS_DIR
    # The built plugin (see skills.build_plugin) and its allowlisted skills.
    plugin_dir: Path | None = None
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    agent: AgentConfig = field(default_factory=AgentConfig)
    state_file: Path = Path(".state/sessions.json")
    # Set when the Claude CLI is routed to another endpoint (logged at startup).
    anthropic_base_url: str | None = None


def log_level() -> str:
    """The loguru level from LOG_LEVEL (read before .env is loaded)."""
    return os.environ.get("LOG_LEVEL", "INFO")


def unset_env_vars(names: Iterable[str]) -> list[str]:
    """Remove *names* from this process's environment.

    Returns the names that were set (values are never returned or logged).
    """
    removed = [name for name in names if name in os.environ]
    for name in removed:
        del os.environ[name]
    return removed


def _path_env(name: str, default: Path) -> Path:
    value = (os.environ.get(name) or "").strip()
    return Path(value).expanduser() if value else default


def load_agent_config(env_file: str | os.PathLike | None = None) -> AgentConfig:
    if env_file is None:
        # Look for .env from the working directory up. python-dotenv's default
        # searches from this module's directory instead, which would pick up
        # a source checkout's .env no matter where the bot was started.
        # (load_dotenv(None) would fall back to that search, so only call it
        # when a file was found.)
        env_file = find_dotenv(usecwd=True) or None
    if env_file is not None:
        load_dotenv(env_file, override=False)
    return AgentConfig(
        aside_command=os.environ.get("ASIDE_COMMAND") or DEFAULT_ASIDE_COMMAND,
        model=os.environ.get("HOMETABS_MODEL") or DEFAULT_MODEL,
        effort=os.environ.get("HOMETABS_EFFORT") or DEFAULT_EFFORT,
        api_key=(os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None,
        oauth_token=(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip() or None,
        aside_skills_dir=_path_env("ASIDE_SKILLS_DIR", DEFAULT_ASIDE_SKILLS_DIR),
    )


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    """Load and validate bot settings. Raises ConfigError if unusable."""
    agent = load_agent_config(env_file)
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set (see .env.example).")
    allowed = parse_allowed_user_ids(os.environ.get("ALLOWED_USER_IDS"))
    if not allowed:
        raise ConfigError(
            "ALLOWED_USER_IDS is empty. This bot controls a real browser, so an "
            "allowlist of Telegram user IDs is mandatory."
        )
    state_file = Path(os.environ.get("STATE_FILE") or ".state/sessions.json")
    return Settings(
        telegram_bot_token=token,
        allowed_user_ids=allowed,
        agent=agent,
        state_file=state_file,
        anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
    )
