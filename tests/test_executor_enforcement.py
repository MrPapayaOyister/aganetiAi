"""P0-C12: the executor really goes through the boundary.

test_authz_boundary.py proves the POLICY is correct. This file proves the LangGraph
executor actually consults it — that the decision is not computed and then ignored,
which is precisely the defect the audit found in `guardrails.decide()`.

`_tools_node` is driven directly with a hand-built state, so nothing here needs a
model, a network call, or a database.
"""
import asyncio
import importlib

import pytest

import backend.orchestrator  # noqa: F401 — registers the v1 tools
from backend.orchestrator import graph, registry

TENANT = "11111111-1111-1111-1111-111111111111"
USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture
def spy_tool():
    """A registered tool that records whether its handler ran. Removed afterwards so
    it cannot leak into the registry-wide assertions in other test modules."""
    calls = []

    async def _handler(ctx, **kw):
        calls.append({"ctx": ctx, "kw": kw})
        return "SPY-RAN"

    tool = registry.Tool(
        name="_spy_tool", description="test probe",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        handler=_handler, required_permission="spy.read", is_outbound=False)
    registry.register(tool)
    import backend.guardrails as g
    g.TOOL_CATEGORY["_spy_tool"] = "read"
    try:
        yield calls
    finally:
        registry._REGISTRY.pop("_spy_tool", None)
        g.TOOL_CATEGORY.pop("_spy_tool", None)


def _state(tool_name, granted, *, tenant_id=TENANT, user_id=USER, args="{}"):
    return {
        "messages": [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "type": "function",
             "function": {"name": tool_name, "arguments": args}}]}],
        "user_id": user_id, "tenant_id": tenant_id, "session_id": "sess-1",
        "agent_id": "primary", "board_id": "", "allowed_tools": granted,
        "step": 1, "awaiting": None, "model_key": None, "fallback_models": [],
        "has_image": False,
    }


def _await(coro):
    """Run a coroutine on a FRESH loop.

    Not asyncio.get_event_loop(): other modules in this suite close or replace the
    process loop, so reusing it makes these tests pass alone and fail in a full
    run — the worst possible failure mode for a security regression test.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run(state):
    return _await(graph._tools_node(state))


# ── ALLOW executes ────────────────────────────────────────────────────────────
def test_allow_runs_the_handler(spy_tool):
    out = _run(_state("_spy_tool", ["_spy_tool"], args='{"x": "1"}'))
    assert len(spy_tool) == 1
    assert out["messages"][0]["content"] == "SPY-RAN"
    assert out["awaiting"] is None


def test_allow_passes_the_tenant_into_the_handler_context(spy_tool):
    _run(_state("_spy_tool", ["_spy_tool"]))
    assert spy_tool[0]["ctx"]["tenant_id"] == TENANT
    assert spy_tool[0]["ctx"]["user_id"] == USER


def test_permission_grant_reaches_the_executor(spy_tool):
    """Granting the permission string, not the name, still runs the tool."""
    out = _run(_state("_spy_tool", ["spy.read"]))
    assert len(spy_tool) == 1
    assert out["messages"][0]["content"] == "SPY-RAN"


# ── DENY blocks (the handler must not run) ────────────────────────────────────
def test_deny_does_not_run_the_handler(spy_tool):
    out = _run(_state("_spy_tool", []))          # not granted
    assert spy_tool == []
    assert out["messages"][0]["content"].startswith("error:")
    assert "not permitted" in out["messages"][0]["content"]
    assert out["awaiting"] is None


def test_missing_tenant_blocks_the_handler(spy_tool):
    out = _run(_state("_spy_tool", ["_spy_tool"], tenant_id=""))
    assert spy_tool == []
    assert "no tenant context" in out["messages"][0]["content"]


def test_kill_switch_blocks_the_handler(spy_tool, monkeypatch):
    import backend.guardrails as g
    monkeypatch.setenv("AGANETI_DENIED_TOOLS", "_spy_tool")
    importlib.reload(g)
    g.TOOL_CATEGORY["_spy_tool"] = "read"
    try:
        out = _run(_state("_spy_tool", ["_spy_tool"]))
        assert spy_tool == []
        assert "disabled by operator policy" in out["messages"][0]["content"]
    finally:
        monkeypatch.delenv("AGANETI_DENIED_TOOLS", raising=False)
        importlib.reload(g)


def test_unknown_tool_is_answered_not_raised():
    """Every tool call must be answered or the tool-call protocol breaks."""
    out = _run(_state("no_such_tool", ["no_such_tool"]))
    assert out["messages"][0]["tool_call_id"] == "call-1"
    assert "unknown tool" in out["messages"][0]["content"]


# ── APPROVAL_REQUIRED pauses through the existing HITL mechanism ──────────────
def test_outbound_pauses_and_does_not_execute():
    out = _run(_state("send_email", ["send_email"],
                      args='{"to": "a@b.test", "subject": "s", "body": "b"}'))
    assert out["awaiting"] is not None
    assert out["awaiting"]["name"] == "send_email"
    assert out["awaiting"]["tool_call_id"] == "call-1"
    assert out["messages"][0]["content"].startswith("[AWAITING USER APPROVAL]")


def test_approval_record_carries_the_deciding_rule_and_risk():
    """New in P0: the approval says WHY it paused, so an operator reviewing the
    queue can tell an outbound action from a policy-driven one."""
    out = _run(_state("web_search", ["web_search"], args='{"query": "x"}'))
    assert out["awaiting"]["rule"] == "outbound"
    assert out["awaiting"]["risk_level"] == "outbound"


def test_guardrail_approval_also_pauses(spy_tool, monkeypatch):
    """A non-outbound tool whose CATEGORY requires sign-off pauses through the same
    mechanism. This is the path that did not exist before P0 — guardrails could say
    'approval' and nothing happened."""
    import backend.guardrails as g
    monkeypatch.setenv("AUTONOMY_LEVEL", "assist")
    importlib.reload(g)
    g.TOOL_CATEGORY["_spy_tool"] = "task"      # task + assist -> approval
    try:
        out = _run(_state("_spy_tool", ["_spy_tool"]))
        assert spy_tool == [], "handler ran despite an approval verdict"
        assert out["awaiting"] is not None
        assert out["awaiting"]["rule"] == "guardrail_approval"
        assert out["awaiting"]["risk_level"] == "write"
    finally:
        monkeypatch.delenv("AUTONOMY_LEVEL", raising=False)
        importlib.reload(g)


# ── resume re-authorizes ──────────────────────────────────────────────────────
def test_resume_refuses_a_revoked_tool(monkeypatch):
    """An approval is durable and can outlive the grant that produced it. Executing
    it blindly would let a revoked permission still fire once."""
    ran = []

    async def _handler(ctx, **kw):
        ran.append(kw)
        return "SENT"

    tool = registry.get("send_email")
    monkeypatch.setattr(tool, "handler", _handler)

    approval = {"tool_call_id": "call-1", "name": "send_email",
                "args": {"to": "a@b.test", "subject": "s", "body": "b"},
                "action_type": "send_email", "preview": "Send email to a@b.test"}
    messages = [{"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call-1", "type": "function",
                     "function": {"name": "send_email", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call-1", "name": "send_email",
                 "content": "[AWAITING USER APPROVAL] ..."}]

    async def _go():
        # The agent no longer grants send_email — the permission was revoked while
        # the approval sat in the queue.
        return await graph.resume(user_id=USER, agent={"id": "primary", "tools": []},
                                  messages=messages, approval=approval, approved=True,
                                  tenant_id=TENANT)

    async def _fake_invoke(state, cfg):
        return state
    monkeypatch.setattr(graph.GRAPH, "ainvoke", _fake_invoke)

    _await(_go())
    assert ran == [], "revoked tool executed on resume"


def test_resume_executes_when_the_grant_is_still_valid(monkeypatch):
    ran = []

    async def _handler(ctx, **kw):
        ran.append(kw)
        return "SENT"

    tool = registry.get("send_email")
    monkeypatch.setattr(tool, "handler", _handler)

    approval = {"tool_call_id": "call-1", "name": "send_email",
                "args": {"to": "a@b.test", "subject": "s", "body": "b"},
                "action_type": "send_email", "preview": "Send email to a@b.test"}
    messages = [{"role": "tool", "tool_call_id": "call-1", "name": "send_email",
                 "content": "[AWAITING USER APPROVAL] ..."}]

    async def _fake_invoke(state, cfg):
        return state
    monkeypatch.setattr(graph.GRAPH, "ainvoke", _fake_invoke)

    _await(
        graph.resume(user_id=USER, agent={"id": "primary", "tools": ["send_email"]},
                     messages=messages, approval=approval, approved=True,
                     tenant_id=TENANT))
    assert len(ran) == 1


def test_resume_refuses_without_a_tenant(monkeypatch):
    ran = []

    async def _handler(ctx, **kw):
        ran.append(kw)
        return "SENT"

    monkeypatch.setattr(registry.get("send_email"), "handler", _handler)

    async def _fake_invoke(state, cfg):
        return state
    monkeypatch.setattr(graph.GRAPH, "ainvoke", _fake_invoke)

    approval = {"tool_call_id": "c", "name": "send_email", "args": {},
                "action_type": "send_email", "preview": "p"}
    msgs = [{"role": "tool", "tool_call_id": "c", "name": "send_email", "content": "..."}]
    _await(
        graph.resume(user_id=USER, agent={"id": "primary", "tools": ["send_email"]},
                     messages=msgs, approval=approval, approved=True, tenant_id=""))
    assert ran == []


def test_rejection_still_short_circuits_without_executing(monkeypatch):
    ran = []

    async def _handler(ctx, **kw):
        ran.append(kw)
        return "SENT"

    monkeypatch.setattr(registry.get("send_email"), "handler", _handler)

    async def _fake_invoke(state, cfg):
        return state
    monkeypatch.setattr(graph.GRAPH, "ainvoke", _fake_invoke)

    approval = {"tool_call_id": "c", "name": "send_email", "args": {},
                "action_type": "send_email", "preview": "p"}
    msgs = [{"role": "tool", "tool_call_id": "c", "name": "send_email", "content": "..."}]
    _await(
        graph.resume(user_id=USER, agent={"id": "primary", "tools": ["send_email"]},
                     messages=msgs, approval=approval, approved=False, tenant_id=TENANT))
    assert ran == []


# ── the boundary is the ONLY decision point ──────────────────────────────────
def _code_of(fn) -> str:
    """Function source with the docstring and comments stripped — the assertions
    below are about CODE, and a docstring that merely mentions `is_outbound` must
    not trip them."""
    import inspect
    lines = []
    for line in inspect.getsource(fn).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split("  #")[0])
    body = "\n".join(lines)
    doc = inspect.getdoc(fn)
    if doc:
        for chunk in doc.split("\n"):
            body = body.replace(chunk, "")
    return body


def test_tools_node_has_no_second_policy_path():
    """Guard against re-introducing an inline check. The executor must reach its
    verdict from authz and nowhere else — a second path is how `web_search` came to
    be gated on one runtime and not the other."""
    src = _code_of(graph._tools_node)
    assert "authorize_call" in src
    assert "tool.is_outbound" not in src, "inline outbound check re-introduced"
    assert "not in state[\"allowed_tools\"]" not in src, "inline allowlist check re-introduced"
    # The single decision point, used three ways (deny / approval / execute).
    assert src.count("authz.authorize_call") == 1


def test_delegation_passes_the_tenant_to_the_sub_agent():
    import inspect
    from backend.orchestrator import agents
    src = inspect.getsource(agents._delegate)
    assert 'tenant_id' in src, "sub-agent run drops the tenant"
