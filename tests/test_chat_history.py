"""Durable /chat history (Part 1).

The Assistant panel's /chat used to persist to JSON files while /chat/history
served an in-memory cache that emptied on restart or after a 2h idle TTL — so a
conversation opened on another browser, or after a deploy, came back blank. It
also had no server-side message id, which per-message extras must key off.

These tests pin the new contract. The Postgres layer is stubbed so the suite
stays offline; the round trip against the real database is exercised manually.
"""

import asyncio

import pytest

import backend.main as main


def _hist(**kw):
    return asyncio.run(main.chat_history_endpoint(**kw))


# ── read precedence ──────────────────────────────────────────────────────────

def test_postgres_is_preferred_and_carries_ids(monkeypatch):
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "reads_pg", lambda: True)
    monkeypatch.setattr(chat_store, "load_full", lambda uid, sid: [
        {"id": "srv-1", "role": "user", "content": "hi", "ts": "2026-08-09T10:00:00"},
        {"id": "srv-2", "role": "assistant", "content": "hello", "ts": "2026-08-09T10:00:01"},
    ])
    out = _hist(session_id="s1", user_id="user_1")
    assert out["source"] == "postgres"
    assert [m["id"] for m in out["messages"]] == ["srv-1", "srv-2"]
    assert all("embeds" in m for m in out["messages"]), "shape must be stable for Part 2"


def test_falls_back_to_json_when_postgres_is_empty(monkeypatch):
    """A thread predating the pg write must not come back blank."""
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "reads_pg", lambda: True)
    monkeypatch.setattr(chat_store, "load_full", lambda uid, sid: [])
    monkeypatch.setattr(main, "load_history",
                        lambda sid: [{"role": "user", "content": "old turn"}])
    out = _hist(session_id="s1", user_id="user_1")
    assert out["source"] == "json"
    assert out["messages"][0]["content"] == "old turn"
    assert out["messages"][0]["id"] is None, "legacy rows have no server id"


def test_falls_back_to_session_cache_when_both_are_empty(monkeypatch):
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "reads_pg", lambda: False)
    monkeypatch.setattr(main, "load_history", lambda sid: [])
    monkeypatch.setitem(main.session_store, "s9",
                        [{"role": "user", "content": "cached"}])
    out = _hist(session_id="s9", user_id="user_1")
    assert out["source"] == "session_cache"
    assert out["messages"][0]["content"] == "cached"


def test_postgres_read_failure_degrades_instead_of_500(monkeypatch):
    from backend.chat import store as chat_store
    def boom(uid, sid):
        raise RuntimeError("db down")
    monkeypatch.setattr(chat_store, "reads_pg", lambda: True)
    monkeypatch.setattr(chat_store, "load_full", boom)
    monkeypatch.setattr(main, "load_history",
                        lambda sid: [{"role": "user", "content": "still here"}])
    out = _hist(session_id="s1", user_id="user_1")
    assert out["source"] == "json" and out["messages"][0]["content"] == "still here"


def test_without_user_id_postgres_is_skipped(monkeypatch):
    """Rows are per-user; no identity means the pg path cannot be queried."""
    from backend.chat import store as chat_store
    called = []
    monkeypatch.setattr(chat_store, "reads_pg", lambda: True)
    monkeypatch.setattr(chat_store, "load_full",
                        lambda uid, sid: called.append(uid) or [])
    monkeypatch.setattr(main, "load_history", lambda sid: [])
    _hist(session_id="s1")
    assert called == []


def test_limit_keeps_the_newest(monkeypatch):
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "reads_pg", lambda: True)
    monkeypatch.setattr(chat_store, "load_full", lambda uid, sid: [
        {"id": f"m{i}", "role": "user", "content": str(i)} for i in range(10)])
    out = _hist(session_id="s1", user_id="user_1", limit=3)
    assert [m["content"] for m in out["messages"]] == ["7", "8", "9"]


# ── writes: two stores, and a visible failure ────────────────────────────────

def test_persist_turn_writes_both_stores(monkeypatch):
    from backend.chat import store as chat_store
    seen = {}
    monkeypatch.setattr(main, "save_message",
                        lambda sid, role, content: seen.setdefault("json", (sid, role)))
    def fake_append(uid, sid, role, content, **kw):
        seen["pg"] = (uid, sid, role, kw.get("source"))
        return "mid-1"
    monkeypatch.setattr(chat_store, "append", fake_append)
    mid = main._persist_turn("sess", "user_1", "user", "hello")
    assert mid == "mid-1"
    assert seen["json"] == ("sess", "user")
    assert seen["pg"] == ("user_1", "sess", "user", "chat"), "must tag source=chat"


def test_silent_postgres_drop_is_logged(monkeypatch, caplog):
    """append() swallows its own failures and returns None. While JSON is still
    the safety net that is survivable — but it must never be invisible."""
    from backend.chat import store as chat_store
    monkeypatch.setattr(main, "save_message", lambda *a, **k: None)
    monkeypatch.setattr(chat_store, "append", lambda *a, **k: None)
    monkeypatch.setattr(chat_store, "enabled", lambda: True)
    with caplog.at_level("WARNING"):
        assert main._persist_turn("sess", "user_1", "user", "hello") is None
    assert any("NOT persisted to postgres" in r.message for r in caplog.records)


def test_json_write_still_happens_if_postgres_raises(monkeypatch):
    """The safety net is only a safety net if it is written first."""
    from backend.chat import store as chat_store
    wrote = []
    monkeypatch.setattr(main, "save_message", lambda *a, **k: wrote.append(a))
    def boom(*a, **k):
        raise RuntimeError("pg exploded")
    monkeypatch.setattr(chat_store, "append", boom)
    assert main._persist_turn("sess", "user_1", "user", "hello") is None
    assert wrote, "JSON write must not be skipped when postgres fails"
