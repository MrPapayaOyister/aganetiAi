"""Tool-toggle preference resolution: session overrides user, absent means on."""

import pytest

from backend.services import tool_prefs


def test_none_and_empty_list_mean_different_things():
    """None is 'no preference at this scope' — fall through. [] is 'explicitly
    nothing disabled' — stop. Collapsing them would make a session that
    re-enables everything indistinguishable from one with no opinion."""
    assert tool_prefs._from_blob({}) is None
    assert tool_prefs._from_blob({"tools": {}}) is None
    assert tool_prefs._from_blob({"tools": {"disabled": []}}) == []
    assert tool_prefs._from_blob({"tools": {"disabled": ["email"]}}) == ["email"]


def test_unknown_groups_are_dropped_on_read():
    assert tool_prefs._from_blob({"tools": {"disabled": ["email", "nope"]}}) == ["email"]


def test_garbage_shapes_degrade_to_no_preference():
    for blob in (None, [], "email", {"tools": "email"}, {"tools": {"disabled": "email"}}):
        assert tool_prefs._from_blob(blob) is None


def test_session_overrides_user(monkeypatch):
    """An override, not a union: a thread that re-enables email must beat a user
    default that disables it, which a union could not express."""
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "enabled", lambda: True)

    class FakeSession:
        meta = {"tools": {"disabled": []}}          # explicitly nothing off
    class FakeUser:
        settings = {"tools": {"disabled": ["email"]}}  # user default: email off

    class FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, model, sid): return FakeSession()
    from backend.db import sync as dbsync
    monkeypatch.setattr(dbsync, "session", lambda: FakeDB())
    monkeypatch.setattr(dbsync, "resolve_user", lambda s, uid: FakeUser())

    assert tool_prefs.get_disabled("u", "sess") == [], "session override must win"
    assert tool_prefs.get_disabled("u", None) == ["email"], "user default applies alone"


def test_read_failure_leaves_every_tool_enabled(monkeypatch):
    """A preference lookup must never cost the user their tools."""
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "enabled", lambda: True)
    from backend.db import sync as dbsync
    def boom(): raise RuntimeError("db down")
    monkeypatch.setattr(dbsync, "session", boom)
    assert tool_prefs.get_disabled("u", "sess") == []
