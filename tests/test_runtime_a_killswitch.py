"""Phase 1.2 — the temporary migration safety control for Runtime A (audit R1).

The problem, restated so this file is readable on its own:

  Runtime A's `schedule_meeting` creates a REAL calendar event on the user's Google
  or Microsoft account. `guardrails.decide("schedule_meeting")` returns "approval"
  at the default autonomy level, but Runtime A's dispatcher only ever acted on
  "deny" — so the action executed with no human sign-off.

Runtime A has no HITL mechanism and, by instruction, is not getting one. The only
honest lever is therefore to REFUSE the tool outright until traffic moves to
Runtime B, which does have an approval gate. `AGANETI_DENIED_TOOLS` is that lever.

These tests exist to prove the lever is real — that setting the variable actually
prevents the provider call, rather than merely being documented as doing so. They
are the evidence behind the claim in docs/p0-remediation.md §6 R1.

This control is TEMPORARY. It is removed when Runtime B serves the traffic; see
docs/runtime-migration.md.
"""
import importlib

import pytest


@pytest.fixture
def guardrails_with(monkeypatch):
    """Reload guardrails with a given AGANETI_DENIED_TOOLS, then restore it.

    A reload is required because DENIED_TOOLS is read at import time — which is
    itself worth asserting, since it means the control needs a process restart and
    an operator must know that.
    """
    def _load(denied: str = ""):
        import backend.guardrails as g
        if denied:
            monkeypatch.setenv("AGANETI_DENIED_TOOLS", denied)
        else:
            monkeypatch.delenv("AGANETI_DENIED_TOOLS", raising=False)
        return importlib.reload(g)

    yield _load
    monkeypatch.delenv("AGANETI_DENIED_TOOLS", raising=False)
    import backend.guardrails as g
    importlib.reload(g)


# ── the policy layer ──────────────────────────────────────────────────────────
def test_schedule_meeting_is_approval_without_the_switch(guardrails_with):
    """The state that motivates the control: policy says 'approval', and Runtime A
    has nothing that can honour it."""
    g = guardrails_with("")
    assert g.decide("schedule_meeting") == "approval"


def test_kill_switch_turns_it_into_deny(guardrails_with):
    g = guardrails_with("schedule_meeting")
    assert g.decide("schedule_meeting") == "deny"


def test_kill_switch_accepts_a_list_and_ignores_whitespace(guardrails_with):
    g = guardrails_with(" schedule_meeting , draft_email ")
    assert g.DENIED_TOOLS == {"schedule_meeting", "draft_email"}
    assert g.decide("schedule_meeting") == "deny"
    assert g.decide("draft_email") == "deny"


def test_kill_switch_does_not_disable_anything_else(guardrails_with):
    """Blast radius: naming one tool must not affect the rest of the catalogue."""
    g = guardrails_with("schedule_meeting")
    assert g.decide("create_task") == "auto"
    assert g.decide("get_emails") == "auto"
    assert g.decide("get_weather") == "auto"
    assert g.decide("draft_email") == "approval"


def test_switch_is_reported_in_the_policy_snapshot(guardrails_with):
    """`GET /guardrails` must show the control, or an operator cannot confirm it is
    on without reading the process environment."""
    g = guardrails_with("schedule_meeting")
    snap = g.policy_snapshot()
    assert snap["denied_tools"] == ["schedule_meeting"]
    assert snap["actions"]["schedule_meeting"]["verdict"] == "deny"


# ── the enforcement layer: Runtime A's dispatcher ─────────────────────────────
def test_runtime_a_dispatcher_refuses_the_tool(guardrails_with, monkeypatch):
    """THE test that matters. Not 'the policy says deny' but 'the dispatcher stops'.

    A stub is installed over the calendar path: if the dispatcher ever reaches it,
    the test fails loudly rather than silently making a provider call.
    """
    guardrails_with("schedule_meeting")
    from backend.services import mailbox

    def _boom(*a, **kw):
        raise AssertionError("schedule_meeting reached the provider despite the kill switch")

    monkeypatch.setattr(mailbox, "create_event", _boom, raising=False)

    from backend.tools import dispatch_tool_call
    result = dispatch_tool_call(
        "schedule_meeting",
        {"title": "Board review", "time": "tomorrow at 3pm", "with": "victim@example.com"},
        "user-under-test")

    assert isinstance(result, str)
    assert result.startswith("⚠️")
    assert "schedule_meeting" in result


def test_runtime_a_refusal_happens_before_time_parsing(guardrails_with, monkeypatch):
    """The refusal is the FIRST thing the dispatcher does, so a malformed time or an
    unreachable provider cannot change the outcome."""
    guardrails_with("schedule_meeting")
    import backend.services.timeparse as tp

    def _boom(*a, **kw):
        raise AssertionError("time parsing ran; the deny check is not first")

    monkeypatch.setattr(tp, "parse_meeting_time", _boom, raising=False)

    from backend.tools import dispatch_tool_call
    result = dispatch_tool_call("schedule_meeting", {"time": "tomorrow 3pm"}, "u")
    assert result.startswith("⚠️")


def test_without_the_switch_the_tool_is_reachable(guardrails_with, monkeypatch):
    """The control is doing the work — not some unrelated failure. With the switch
    OFF the dispatcher proceeds past the guardrail into the tool body (where it
    fails on our stub, which is exactly how we know it got there)."""
    guardrails_with("")
    import backend.services.timeparse as tp
    reached = []

    def _mark(*a, **kw):
        reached.append(True)
        raise RuntimeError("stop here — we only needed to prove we got this far")

    monkeypatch.setattr(tp, "parse_meeting_time", _mark, raising=False)

    from backend.tools import dispatch_tool_call
    result = dispatch_tool_call("schedule_meeting", {"time": "tomorrow 3pm"}, "u")
    assert reached, "dispatcher did not reach the tool body with the switch off"
    assert not str(result).startswith("⚠️ I'm not able to perform that action")


# ── Runtime B is unaffected: it gates rather than blocks ──────────────────────
def test_runtime_b_equivalent_is_gated_not_killed():
    """Runtime B's counterpart (`create_calendar_event`) does not need the kill
    switch: it is is_outbound, so the authorization boundary pauses it for approval.
    That is the permanent fix, and the reason this control is temporary."""
    import backend.orchestrator  # noqa: F401
    from backend.orchestrator import authz, registry
    from backend.orchestrator.authz import Decision

    tool = registry.get("create_calendar_event")
    assert tool is not None and tool.is_outbound

    res = authz.authorize_call(
        user_id="u", tenant_id="11111111-1111-1111-1111-111111111111",
        agent_id="primary", session_id="s", tool_name="create_calendar_event",
        arguments={"title": "x", "start": "tomorrow 3pm"},
        granted=["create_calendar_event"], tool=tool)
    assert res.decision is Decision.APPROVAL_REQUIRED
    assert res.rule == "outbound"


def test_kill_switch_also_covers_runtime_b(guardrails_with):
    """If an operator throws the switch, it must apply everywhere — a tool disabled
    on one runtime and live on the other is the exact class of split-brain the audit
    found with web_search."""
    import backend.orchestrator  # noqa: F401
    from backend.orchestrator import authz, registry
    from backend.orchestrator.authz import Decision

    guardrails_with("create_calendar_event")
    res = authz.authorize_call(
        user_id="u", tenant_id="11111111-1111-1111-1111-111111111111",
        agent_id="primary", session_id="s", tool_name="create_calendar_event",
        arguments={}, granted=["create_calendar_event"],
        tool=registry.get("create_calendar_event"))
    assert res.decision is Decision.DENY
    assert res.rule == "kill_switch"
