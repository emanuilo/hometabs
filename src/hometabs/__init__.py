"""Hometabs: a personal agent in Telegram that uses your real browser (via the Aside MCP)."""

from __future__ import annotations

import sys

from loguru import logger

__all__ = ["main"]


def main() -> None:
    """Console entry point: ``uv run hometabs``."""
    from .auth import SCRUBBED_ENV_VARS, log_auth_choice
    from .config import ConfigError, load_settings, log_level, unset_env_vars

    logger.remove()
    # diagnose=False: loguru would otherwise print local variable values in
    # tracebacks, and python-telegram-bot's Bot repr contains the bot token.
    logger.add(sys.stderr, level=log_level(), diagnose=False)

    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("Configuration error: {}", exc)
        raise SystemExit(2) from exc

    # The SDK merges options.env over os.environ, so credentials must be
    # removed from this process too (the chosen credential was already
    # captured into settings and is re-injected explicitly for the CLI).
    unset_env_vars(SCRUBBED_ENV_VARS)
    log_auth_choice(settings.agent.api_key, settings.agent.oauth_token)
    if settings.anthropic_base_url:
        logger.warning("ANTHROPIC_BASE_URL is set; the Claude CLI will use it")
    logger.info(
        "Starting Hometabs (default model={}, default effort={}, aside={}, allowed users={})",
        settings.agent.model,
        settings.agent.effort,
        settings.agent.aside_command,
        sorted(settings.allowed_user_ids),
    )

    from .bot import HometabsBot

    try:
        HometabsBot(settings).run()
    except ConfigError as exc:
        logger.error("{}", exc)
        raise SystemExit(2) from None
