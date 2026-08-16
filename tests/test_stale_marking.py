"""Stale-data marking: the model must re-call, not quote the transcript.

Pre-existing correctness bug, independent of tool toggles — "any new emails?"
asked twice was answered from the first reply. Prompting rescued weather and
never mail or calendar; marking is 4/4 where a prompt was 0/4.
"""

import pathlib

import pytest

from backend.chat.stale import MARKER, mark_stale


def _rows(*triples):
    return [{"role": r, "content": c, "tools": t} for r, c, t in triples]


# ── what gets marked ─────────────────────────────────────────────────────────

def test_volatile_read_is_marked():
    out = mark_stale(_rows(("assistant", "You have 2 unread.", ["get_emails"])))
    assert MARKER in out[0]["content"]


def test_action_confirmations_are_never_marked():
    """NON-NEGOTIABLE. Marking 'task created' invites the model to re-run it —
    a duplicate side effect, not a wasted lookup."""
    import backend.tools as tools
    for name in sorted(tools.ACTION_TOOLS):
        out = mark_stale(_rows(("assistant", "Done.", [name])))
        assert MARKER not in out[0]["content"], f"{name} is an ACTION tool"


def test_action_and_volatile_sets_are_disjoint():
    """Structural, not a habit — tools.py asserts this at import."""
    import backend.tools as tools
    assert not (tools.VOLATILE_TOOLS & tools.ACTION_TOOLS)


def test_a_turn_mixing_an_action_with_a_read_is_not_marked():
    """If any action ran in the turn, re-calling is unsafe — the read is not
    worth risking a repeated side effect."""
    out = mark_stale(_rows(("assistant", "Emailed and checked.",
                            ["get_emails", "draft_email"])))
    assert MARKER not in out[0]["content"]


def test_search_tools_are_not_marked():
    """Their reply is a pointer, not data; they already re-call."""
    for name in ("search_youtube", "web_search", "play_youtube_video"):
        out = mark_stale(_rows(("assistant", "Here are results.", [name])))
        assert MARKER not in out[0]["content"]


def test_user_turns_are_never_marked():
    out = mark_stale(_rows(("user", "any new emails?", ["get_emails"])))
    assert MARKER not in out[0]["content"]


def test_rows_without_provenance_pass_through():
    """The JSON fallback store has no provenance. Guessing from content would
    reintroduce the heuristic this design avoided."""
    out = mark_stale([{"role": "assistant", "content": "You have 2 unread."}])
    assert MARKER not in out[0]["content"]


def test_marking_is_idempotent():
    once = mark_stale(_rows(("assistant", "2 unread.", ["get_emails"])))
    twice = mark_stale([{**once[0], "tools": ["get_emails"]}])
    assert twice[0]["content"].count("[STALE") == 1


def test_output_carries_no_extra_keys():
    """`tools` is provenance, not part of the provider's message schema."""
    for m in mark_stale(_rows(("assistant", "x", ["get_emails"]))):
        assert set(m) == {"role", "content"}


# ── NON-NEGOTIABLE: the marker is never persisted ────────────────────────────

def test_marker_never_reaches_a_write_path():
    """The one failure here that is not recoverable: a persisted marker compounds
    every turn and corrupts stored history. Marking happens on the in-memory copy
    at injection time only."""
    root = pathlib.Path(__file__).resolve().parent.parent
    writers = [
        root / "memory" / "store.py",
        root / "backend" / "chat" / "store.py",
    ]
    for f in writers:
        src = f.read_text()
        assert "mark_stale" not in src and "STALE —" not in src, \
            f"{f.name} is a WRITE path and must never see the marker"



def test_persist_turn_stores_unmarked_content(monkeypatch):
    import backend.main as main
    from backend.chat import store as chat_store
    seen = {}
    monkeypatch.setattr(main, "save_message",
                        lambda sid, role, content: seen.setdefault("json", content))
    def fake_append(uid, sid, role, content, **kw):
        seen["pg"] = content
        seen["tools"] = kw.get("tool_calls")
        return "m1"
    monkeypatch.setattr(chat_store, "append", fake_append)
    main._persist_turn("s", "u", "assistant", "You have 2 unread.", ["get_emails"])
    assert MARKER not in seen["json"] and MARKER not in seen["pg"]
    assert seen["tools"] == [{"type": "function", "function": {"name": "get_emails"}}]



# ── NON-NEGOTIABLE: both chat paths, or neither ──────────────────────────────

def test_both_chat_paths_mark():
    root = pathlib.Path(__file__).resolve().parent.parent
    chat = (root / "backend" / "main.py").read_text()
    agent = (root / "backend" / "routes" / "agent_os.py").read_text()
    assert "mark_stale(rows)" in chat, "/chat must mark"
    assert "mark_stale=True" in agent, "/agent/chat must mark — :8000 serves it"


def test_dashboard_paths_are_left_alone():
    """Deliberate: chart/analytics threads are a separate question, and marking
    them was never asked for or verified."""
    root = pathlib.Path(__file__).resolve().parent.parent
    for f in ("stream.py", "ask.py"):
        src = (root / "backend" / "dashboard" / f).read_text()
        assert "mark_stale" not in src


def test_conversation_load_strips_provenance_even_unmarked():
    """The store's `tools` key must never reach a provider payload, marked or not."""
    import backend.orchestrator.conversation as convo
    src = __import__("inspect").getsource(convo.load)
    assert '"role": r.get("role"), "content": r.get("content", "")' in src


# ── NON-NEGOTIABLE: never reaches Telegram or the digests ────────────────────

def test_marker_cannot_reach_telegram_or_digests():
    """Those read no chat context at all — verified by grep, and pinned here so a
    future 'let's give the bot conversation memory' change has to look at this."""
    root = pathlib.Path(__file__).resolve().parent.parent
    for sub in ("integrations", "reports", "tasks", "scheduler"):
        d = root / sub
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            src = f.read_text(errors="replace")
            assert "mark_stale" not in src, f"{f} would expose the marker"



# ── widget turns are EXCLUDED from marking ───────────────────────────────────
#
# A player turn's reply carries no data, so nothing in it can go stale. That is
# the reason, and it is the only one that survived scrutiny: measurements that
# once justified this were taken with model thinking ENABLED while the server
# disables it. See the STATUS note on WIDGET_TOOLS in backend/tools.py. The
# repeat-request bug remains open; this exclusion is not its fix.

import backend.tools as _tools  # noqa: E402


def _mark(tools, content="x"):
    return mark_stale([{"role": "assistant", "content": content, "tools": tools}])[0]["content"]


@pytest.mark.parametrize("tool", sorted({"watch_live_tv", "uae_radio", "arabic_radio",
                                         "quran_radio", "radio_by_genre", "search_radio",
                                         "my_radio", "play_youtube_video",
                                         "generate_qr_code"}))
def test_no_widget_turn_is_ever_marked(tool):
    assert MARKER not in _mark([tool])


def test_no_widget_tool_sits_in_the_volatile_set():
    """The set difference in mark_stale protects against this, but leaving one
    here would still mislead the next reader into thinking it gets marked."""
    assert not (_tools.WIDGET_TOOLS & _tools.VOLATILE_TOOLS)


def test_the_subtraction_survives_someone_re_adding_a_widget_tool(monkeypatch):
    """Belt and braces: mark_stale subtracts WIDGET_TOOLS at use time, so putting
    watch_live_tv back into VOLATILE_TOOLS cannot silently re-break the repeat
    request — which is exactly the 'fix' a future reader is likely to attempt."""
    monkeypatch.setattr(_tools, "VOLATILE_TOOLS",
                        _tools.VOLATILE_TOOLS | {"watch_live_tv"})
    assert MARKER not in _mark(["watch_live_tv"])


def test_a_turn_that_is_both_widget_and_data_still_gets_marked():
    """get_weather renders a card AND carries figures. Its data must still
    refresh on a repeat; the card not re-rendering is the accepted trade."""
    assert MARKER in _mark(["get_weather"])
    assert MARKER in _mark(["get_weather", "watch_live_tv"])


def test_the_pickers_are_still_marked():
    """Their replies list what was found, so there is real data to go stale."""
    for tool in ("search_tv_channels", "search_radio_stations"):
        assert MARKER in _mark([tool]), tool


def test_membership_is_by_tool_never_by_content():
    text = "Opened the player."
    assert MARKER not in _mark(["watch_live_tv"], text)
    assert MARKER in _mark(["get_emails"], text)


def test_no_action_tool_is_a_widget_tool():
    assert not (_tools.WIDGET_TOOLS & _tools.ACTION_TOOLS)
