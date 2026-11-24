"""What the bot keeps across restarts: chat sessions and the model choice."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from ..models import SDK_EFFORT_LEVELS


def _read_json(path: Path, what: str) -> object:
    """The JSON in *path*, or ``{}`` if it is missing or unreadable."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable {} {}: {}", what, path, exc)
        return {}


def _write_json(path: Path, data: object) -> None:
    """Replace *path* atomically (write a temp file, then rename it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


class PreferenceStore:
    """Bot-wide model and effort choice, persisted so restarts keep it.

    ``effort`` is the preferred level; the level actually used may be lower
    (or omitted) when the current model doesn't support it.
    """

    def __init__(self, path: Path, model: str, effort: str | None) -> None:
        self.path = path
        self.model = model
        self.effort = effort
        raw = _read_json(path, "settings file")
        if not isinstance(raw, dict):
            raw = {}
        if isinstance(raw.get("model"), str) and raw["model"]:
            self.model = raw["model"]
        if "effort" in raw and (raw["effort"] is None or raw["effort"] in SDK_EFFORT_LEVELS):
            self.effort = raw["effort"]

    def update(self, **changes: str | None) -> None:
        for key, value in changes.items():
            if key not in ("model", "effort"):
                raise KeyError(key)
            setattr(self, key, value)
        _write_json(self.path, {"model": self.model, "effort": self.effort})


class SessionStore:
    """chat_id -> Claude session id, persisted so restarts keep context.

    Each entry also records the fingerprint of the agent instructions the
    session was started with; Claude Code keeps a session's original system
    prompt on resume, so entries from other instructions aren't resumed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, str]] = {}
        raw = _read_json(path, "session store")
        for key, value in (raw if isinstance(raw, dict) else {}).items():
            # Legacy entries were bare session ids with no fingerprint.
            self._data[key] = value if isinstance(value, dict) else {"session_id": value}

    def get(self, chat_id: int, fingerprint: str) -> str | None:
        """The session to resume, or None if there is none or it is stale."""
        entry = self._data.get(str(chat_id))
        if entry and entry.get("fingerprint") == fingerprint:
            return entry.get("session_id")
        return None

    def has(self, chat_id: int) -> bool:
        return str(chat_id) in self._data

    def set(self, chat_id: int, session_id: str | None, fingerprint: str = "") -> None:
        key = str(chat_id)
        if session_id:
            entry = {"session_id": session_id, "fingerprint": fingerprint}
            if self._data.get(key) == entry:
                return
            self._data[key] = entry
        elif self._data.pop(key, None) is None:
            return
        _write_json(self.path, self._data)
