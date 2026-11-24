"""Discover the Claude models the configured credential can reach.

Anthropic's ``GET /v1/models`` lists every model available to a credential,
each with a capability tree that says which effort levels it accepts. The
``/model`` and ``/effort`` commands are built from it, so a newly released
model shows up without a code change.

Auth follows :func:`hometabs.auth.select_credential`: an API key is sent as
``x-api-key``, an OAuth token (``CLAUDE_CODE_OAUTH_TOKEN``) as a bearer token,
both to api.anthropic.com only. With neither (local Claude Code login) there
is no credential to send, so no request is made. The list is cached in the
state dir for a day; a network or response error falls back to the last
cached list (even a stale one), or to an empty list, never to an exception.

Ported from memclaw's ``anthropic_models.py`` (PR #11).
"""

from __future__ import annotations

import json
import time
import typing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from claude_agent_sdk import EffortLevel
from loguru import logger

from .auth import AUTH_MODE_API_KEY, AUTH_MODE_OAUTH, Credential, select_credential

MODELS_URL = "https://api.anthropic.com/v1/models"
API_VERSION = "2023-06-01"
# /v1/models accepts the OAuth token without it, but other OAuth endpoints
# require it; sent so this auth path stays correct if reused.
OAUTH_BETA = "oauth-2025-04-20"
REQUEST_TIMEOUT = 10.0
PAGE_LIMIT = 100  # the endpoint pages at 20 by default
MAX_PAGES = 10

CACHE_FILENAME = "models_cache.json"
CACHE_TTL_SECONDS = 24 * 60 * 60

# The effort levels the installed SDK accepts, lowest first.
SDK_EFFORT_LEVELS: tuple[str, ...] = typing.get_args(EffortLevel)


@dataclass
class ModelInfo:
    id: str
    display_name: str
    created_at: str = ""
    effort_levels: list[str] = field(default_factory=list)  # empty: no effort support


# ── Request / response ──────────────────────────────────────────────


def build_headers(credential: Credential) -> dict[str, str]:
    """Headers for /v1/models matching the credential type.

    The two are not interchangeable: an OAuth token must go in
    ``Authorization: Bearer`` (sent as ``x-api-key`` it is rejected with 401),
    and an API key goes in ``x-api-key`` with no OAuth beta header.
    """
    headers = {"anthropic-version": API_VERSION}
    if credential.mode == AUTH_MODE_API_KEY and credential.secret:
        headers["x-api-key"] = credential.secret
    elif credential.mode == AUTH_MODE_OAUTH and credential.secret:
        headers["Authorization"] = f"Bearer {credential.secret}"
        headers["anthropic-beta"] = OAUTH_BETA
    else:
        raise ValueError("No Claude credential to call /v1/models with")
    return headers


def effort_levels(capabilities: dict[str, Any]) -> list[str]:
    """Supported effort levels from one model's capability tree, in SDK order.

    Iterating the SDK's levels (not the API's keys) drops levels the
    installed SDK doesn't know yet.
    """
    effort = capabilities.get("effort") if isinstance(capabilities, dict) else None
    if not isinstance(effort, dict) or not effort.get("supported"):
        return []
    return [
        level
        for level in SDK_EFFORT_LEVELS
        if isinstance(effort.get(level), dict) and effort[level].get("supported")
    ]


def _created_key(created_at: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_models(entries: list[dict[str, Any]]) -> list[ModelInfo]:
    """Parse /v1/models ``data`` entries, newest first."""
    models = [
        ModelInfo(
            id=entry["id"],
            display_name=entry.get("display_name") or entry["id"],
            created_at=entry.get("created_at") or "",
            effort_levels=effort_levels(entry.get("capabilities") or {}),
        )
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
    ]
    models.sort(key=lambda m: _created_key(m.created_at), reverse=True)
    return models


async def fetch_models(
    credential: Credential, client: httpx.AsyncClient | None = None
) -> list[ModelInfo]:
    """Fetch every model page from /v1/models. Raises on any failure."""
    headers = build_headers(credential)
    own = client is None
    client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False)
    entries: list[dict[str, Any]] = []
    params: dict[str, Any] = {"limit": PAGE_LIMIT}
    try:
        for _ in range(MAX_PAGES):
            response = await client.get(MODELS_URL, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
            entries.extend(payload.get("data") or [])
            last_id = payload.get("last_id")
            if not payload.get("has_more") or not last_id:
                break
            params = {"limit": PAGE_LIMIT, "after_id": last_id}
    finally:
        if own:
            await client.aclose()
    return parse_models(entries)


# ── Cache + catalog ─────────────────────────────────────────────────


def read_cache(path: Path, max_age: float | None = CACHE_TTL_SECONDS) -> list[ModelInfo] | None:
    """Cached models, or None if missing/malformed/older than *max_age*
    (``max_age=None`` accepts any age)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(raw["fetched_at"])
        if max_age is not None and not 0 <= age <= max_age:
            return None
        return [
            ModelInfo(
                id=str(e["id"]),
                display_name=str(e["display_name"]),
                created_at=str(e.get("created_at", "")),
                effort_levels=[str(lv) for lv in e["effort_levels"] if lv in SDK_EFFORT_LEVELS],
            )
            for e in raw["models"]
        ]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_cache(path: Path, models: list[ModelInfo]) -> None:
    payload = {"fetched_at": time.time(), "models": [asdict(m) for m in models]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("Could not write model cache: {}", exc)


class ModelCatalog:
    """The model list, fetched lazily and cached on disk for a day."""

    def __init__(self, api_key: str | None, oauth_token: str | None, cache_path: Path) -> None:
        cred = select_credential(api_key, oauth_token)
        # None in local_login mode: there is no credential to send.
        self._credential: Credential | None = cred if cred.secret else None
        self.cache_path = cache_path
        # Last known list (possibly stale) so lookups never need the network.
        self.models: list[ModelInfo] = read_cache(cache_path, max_age=None) or []

    async def refresh(self, *, force: bool = False) -> list[ModelInfo]:
        """Return a fresh list if possible, else the last known one (maybe [])."""
        if not force:
            cached = read_cache(self.cache_path)
            if cached:
                self.models = cached
                return cached
        if self._credential is None:
            return self.models
        try:
            models = await fetch_models(self._credential)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Only the type: the request (and its headers) must never be logged.
            logger.warning("Could not fetch the model list: {}", type(exc).__name__)
            return self.models
        if models:
            self.models = models
            write_cache(self.cache_path, models)
        return self.models

    def get(self, model_id: str) -> ModelInfo | None:
        for m in self.models:
            if m.id == model_id:
                return m
        return None

    def resolve(self, text: str) -> ModelInfo | None:
        """Match user input against the list: exact id, display name, or a
        unique id prefix (e.g. an alias like ``claude-haiku-4-5`` for the
        dated ``claude-haiku-4-5-20251001``)."""
        text = text.strip()
        low = text.lower()
        for m in self.models:
            if m.id.lower() == low or m.display_name.lower() == low:
                return m
        prefixed = [m for m in self.models if m.id.lower().startswith(low + "-")]
        return prefixed[0] if len(prefixed) == 1 else None


def resolve_effort(levels: list[str] | None, preferred: str | None) -> str | None:
    """The effort to actually use for a model.

    *levels* is the model's supported list (``[]``: no effort support,
    ``None``: unknown, so *preferred* is used as is). An unsupported level
    drops to the nearest supported level below it (else the lowest).
    """
    if levels is None or preferred is None:
        return preferred
    if not levels:
        return None
    if preferred in levels:
        return preferred
    order = list(SDK_EFFORT_LEVELS)
    rank = order.index(preferred) if preferred in order else len(order)
    below = [lv for lv in levels if lv in order and order.index(lv) < rank]
    return below[-1] if below else levels[0]
