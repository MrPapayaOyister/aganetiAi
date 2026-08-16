"""Where the model's conversation context comes from.

Moved off the JSON files onto Postgres, which is the system of record since
/chat/history moved over and the only store carrying per-message provenance.
The JSON files stay behind it for threads that predate the Postgres write.

Same source ordering as /chat/history, deliberately: if the context the model
sees and the transcript the user sees disagreed about which store won, a thread
could be answered from one history and displayed from another.
"""

import backend.main as main


def _rows(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


def test_postgres_is_preferred(monkeypatch):
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "reads_pg", lambda: True)
    monkeypatch.setattr(chat_store, "load",
                        lambda uid, sid, limit: _rows(("user", "hi"), ("assistant", "hello")))
    monkeypatch.setattr(main, "load_history", lambda sid: _rows(("user", "STALE JSON")))
    rows, source = main._context_history("s1", "user_1")
    assert source == "postgres"
    assert [r["content"] for r in rows] == ["hi", "hello"]


def test_falls_back_to_json_for_pre_migration_threads(monkeypatch):
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "reads_pg", lambda: True)
    monkeypatch.setattr(chat_store, "load", lambda uid, sid, limit: [])
    monkeypatch.setattr(main, "load_history", lambda sid: _rows(("user", "old thread")))
    rows, source = main._context_history("s1", "user_1")
    assert source == "json" and rows[0]["content"] == "old thread"


def test_postgres_failure_degrades_to_json(monkeypatch):
    from backend.chat import store as chat_store
    def boom(uid, sid, limit): raise RuntimeError("db down")
    monkeypatch.setattr(chat_store, "reads_pg", lambda: True)
    monkeypatch.setattr(chat_store, "load", boom)
    monkeypatch.setattr(main, "load_history", lambda sid: _rows(("user", "still here")))
    rows, source = main._context_history("s1", "user_1")
    assert source == "json" and rows[0]["content"] == "still here"


def test_json_failure_degrades_to_empty_not_an_exception(monkeypatch):
    """Empty is the caller's signal to use the in-memory cache — a raise here
    would take the whole turn down."""
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "reads_pg", lambda: False)
    def boom(sid): raise RuntimeError("disk gone")
    monkeypatch.setattr(main, "load_history", boom)
    assert main._context_history("s1", "user_1") == ([], "none")


def test_pg_disabled_uses_json(monkeypatch):
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "reads_pg", lambda: False)
    monkeypatch.setattr(main, "load_history", lambda sid: _rows(("user", "json only")))
    rows, source = main._context_history("s1", "user_1")
    assert source == "json"


def test_context_is_trimmed_and_keeps_the_thread_anchor():
    """A long thread must stay bounded without losing what it is about. The JSON
    path bounded itself by summarising past 20 entries; this bounds by turn count
    and pins the first user message."""
    from backend.chat import store as chat_store
    msgs = _rows(("user", "ANCHOR: plan my trip"),
                 *[("assistant", f"a{i}") if i % 2 else ("user", f"u{i}") for i in range(40)])
    trimmed = chat_store.load.__wrapped__ if hasattr(chat_store.load, "__wrapped__") else None
    # exercise the real trimming logic through load_full
    import backend.chat.store as st
    orig = st.load_full
    st.load_full = lambda uid, sid: msgs
    try:
        out = st.load("u", "s", 8)
    finally:
        st.load_full = orig
    assert len(out) <= 8
    assert out[0]["content"].startswith("ANCHOR"), "first user message must survive trimming"
