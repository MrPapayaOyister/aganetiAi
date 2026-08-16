"""Runtime B's streaming bridge routes through the lane router. (POC-3 cutover)

`_serve_via_runtime_b` used to stream via `agent_os._sse`, the PRIMARY AGENT
generator. A Runtime B turn on /chat therefore skipped `unified.route()`
entirely: /agent/chat got the lanes and /chat did not, so the analytics, chart
and POC-3 `agent` lanes were unreachable from the customer entry point.

The fix is one call site. These tests pin the three properties that make it
safe:

  * an ordinary message still lands on `primary`, which delegates back to the
    same generator — so normal chat is unchanged;
  * `/agent` and `/verify` reach the POC-3 loop through /chat;
  * the tenant is still resolved server-side and never taken from the message.

Runtime A is untouched and is asserted to stay untouched: with no RUNTIME_B_*
environment the bridge is not reached at all.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from backend.chat import unified


# ── the seam itself ─────────────────────────────────────────────────────────

def test_the_streaming_bridge_calls_the_lane_router_not_the_primary_generator():
    """The regression this exists for. Asserted on the source of the bridge,
    because the alternative — mocking a StreamingResponse — proves less."""
    import backend.main as main
    src = inspect.getsource(main._serve_via_runtime_b)
    stream_branch = src.split("if request.stream:")[1].split("# Non-streaming")[0]
    assert "unified_stream" in stream_branch, \
        "the streaming bridge no longer routes through the lane router"
    assert "_agent_os._sse(" not in stream_branch, \
        "the bridge still calls the primary-agent generator directly"


def test_the_signatures_are_compatible_at_the_seam():
    """`unified_stream` must remain a superset of `_sse`, or the swap needs an
    adapter and this test is the place that says so."""
    from backend.routes.agent_os import _sse
    sse_params = list(inspect.signature(_sse).parameters)
    uni_params = list(inspect.signature(unified.unified_stream).parameters)
    assert uni_params[:len(sse_params)] == sse_params, \
        f"positional shape diverged: {uni_params} vs {sse_params}"


def test_runtime_a_is_untouched_by_default(monkeypatch):
    """No RUNTIME_B_* environment ⇒ the bridge is never reached."""
    from backend import runtime_flag
    for var in ("RUNTIME_B_ENABLED", "RUNTIME_B_PERCENT", "RUNTIME_B_USERS",
                "RUNTIME_B_SESSIONS", "RUNTIME_B_DENY_USERS"):
        monkeypatch.delenv(var, raising=False)
    importlib_reload(runtime_flag)
    choice = runtime_flag.choose(user_id="u-1", session_id="s-1")
    assert choice is None or not choice.is_b, "a default install moved traffic to Runtime B"


def importlib_reload(mod):
    import importlib
    return importlib.reload(mod)


# ── lane routing reached through the bridge's generator ─────────────────────

def _drain(gen) -> list[dict]:
    async def run():
        out = []
        async for chunk in gen:
            for line in str(chunk).splitlines():
                if not line.startswith("data:"):
                    continue
                raw = line.split(":", 1)[1].strip()
                if raw == "[DONE]":
                    continue
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    pass
        return out
    return asyncio.run(run())


@pytest.fixture
def primary_probe(monkeypatch):
    """Replace the primary generator so 'did it reach primary?' is observable."""
    seen = []

    async def fake_sse(user_id, message, session_id, images=None):
        seen.append({"user_id": user_id, "message": message})
        yield 'data: {"type": "token", "content": "primary answered"}\n\n'

    from backend.routes import agent_os
    monkeypatch.setattr(agent_os, "_sse", fake_sse)
    fake_sse.seen = seen
    return fake_sse


@pytest.fixture
def loop_probe(monkeypatch):
    """Replace the POC-3 loop so 'did it reach the agent lane?' is observable."""
    seen = []

    async def fake_loop(*, request, user_id, tenant_id="", session_id="", agent_id="poc3"):
        seen.append({"request": request, "user_id": user_id, "tenant_id": tenant_id})
        return {"request": request, "plan": {"goal": "g", "steps": []},
                "evidence": {"citations": [], "counts": {"total": 0},
                             "tenant_enforced_by": [], "tenant_id": tenant_id},
                "verification": {"verdict": "UNSUPPORTED", "releasable": False,
                                 "explanation": "", "missing": [], "conflicts": []},
                "final_answer": "no answer", "trace": [], "errors": []}

    monkeypatch.setattr("backend.agents.run_agent_loop", fake_loop)
    monkeypatch.setattr("backend.auth.tenant.resolve_tenant_id",
                        lambda uid: asyncio.sleep(0, result="tenant-a"))
    fake_loop.seen = seen
    return fake_loop


def test_an_ordinary_message_still_reaches_the_primary_lane(primary_probe, loop_probe):
    frames = _drain(unified.unified_stream("u-1", "what does the policy say?", "s-1"))
    assert primary_probe.seen, "an ordinary message did not reach the primary agent"
    assert not loop_probe.seen, "an ordinary message entered the POC-3 loop"
    assert any(f.get("type") == "token" and "primary answered" in f.get("content", "")
               for f in frames), "the primary answer did not survive the lane router"


def test_the_agent_prefix_reaches_poc3_through_the_bridge(loop_probe, primary_probe):
    _drain(unified.unified_stream("u-1", "/agent what expires?", "s-1"))
    assert loop_probe.seen, "/agent did not reach the POC-3 loop"
    assert loop_probe.seen[0]["request"] == "what expires?"
    assert not primary_probe.seen, "/agent leaked into the primary lane"


def test_the_verify_prefix_reaches_poc3_through_the_bridge(loop_probe):
    _drain(unified.unified_stream("u-1", "/verify is that right?", "s-1"))
    assert loop_probe.seen and loop_probe.seen[0]["request"] == "is that right?"


def test_the_prefix_must_be_anchored(loop_probe, primary_probe):
    """Mentioning the marker mid-sentence is an ordinary question."""
    for msg in ("tell me about /agent mode", "what does /agent do?",
                "explain the /verify command"):
        loop_probe.seen.clear(); primary_probe.seen.clear()
        _drain(unified.unified_stream("u-1", msg, "s-1"))
        assert not loop_probe.seen, f"{msg!r} wrongly activated POC-3"
        assert primary_probe.seen, f"{msg!r} did not reach the primary lane"


# ── tenant safety across the bridge ─────────────────────────────────────────

def test_the_tenant_is_server_resolved_not_taken_from_the_message(loop_probe):
    _drain(unified.unified_stream(
        "u-1", "/agent tenant_id=tenant-b org_id=tenant-b what expires?", "s-1"))
    assert loop_probe.seen[0]["tenant_id"] == "tenant-a", \
        "a tenant supplied in the message was honoured"


def test_no_tenant_id_appears_on_the_wire(loop_probe):
    frames = _drain(unified.unified_stream("u-1", "/agent what expires?", "s-1"))
    blob = json.dumps(frames)
    assert "tenant-a" not in blob, "the tenant id was sent to the client"
    assert "tenant_id" not in blob, "a tenant field was sent to the client"


def test_the_bridge_does_not_read_a_tenant_from_the_request_body():
    """The bridge must never accept a browser-supplied tenant."""
    import backend.main as main
    src = inspect.getsource(main._serve_via_runtime_b)
    for forbidden in ("request.tenant_id", "request.org_id", 'body.get("tenant',
                      'payload.get("tenant'):
        assert forbidden not in src, f"the bridge reads a client tenant: {forbidden}"


# ── the frames a client will now see ────────────────────────────────────────

def test_the_bridge_emits_only_known_frame_types(primary_probe, loop_probe):
    """The swap adds `start` and a router `stage` to the Runtime B stream. Both
    already exist in frames.py; no new type is introduced."""
    known = {"start", "stage", "tool_call", "tool_result", "token",
             "artifact", "done", "error"}
    for msg in ("what does the policy say?", "/agent what expires?"):
        frames = _drain(unified.unified_stream("u-1", msg, "s-1"))
        seen = {f.get("type") for f in frames}
        assert seen <= known, f"unknown frame type(s) for {msg!r}: {seen - known}"


def test_poc3_still_emits_its_three_artifacts_through_this_path(loop_probe):
    frames = _drain(unified.unified_stream("u-1", "/agent q?", "s-1"))
    kinds = {f["artifact"]["kind"] for f in frames if f.get("type") == "artifact"}
    assert kinds == {"agent_plan", "agent_evidence", "agent_verification"}, kinds
