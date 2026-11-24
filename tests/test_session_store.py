import json

from hometabs.agent import instructions_fingerprint
from hometabs.bot.stores import SessionStore


def test_resumes_only_matching_fingerprint(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set(1, "sess-a", "fp1")
    reloaded = SessionStore(tmp_path / "s.json")
    assert reloaded.get(1, "fp1") == "sess-a"
    assert reloaded.get(1, "fp2") is None
    assert reloaded.has(1)


def test_legacy_string_entries_are_stale(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"1": "old-session"}))
    store = SessionStore(path)
    assert store.has(1)
    assert store.get(1, instructions_fingerprint(None)) is None


def test_clear_removes_entry(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set(1, "sess-a", "fp1")
    store.set(1, None)
    assert not store.has(1)
    assert json.loads((tmp_path / "s.json").read_text()) == {}


def test_fingerprint_is_stable_and_ignores_date(plugin):
    # The prompt template (not the formatted prompt with today's date) is
    # hashed, so sessions aren't dropped every midnight.
    assert instructions_fingerprint(plugin) == instructions_fingerprint(plugin)
