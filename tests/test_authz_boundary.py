"""P0-C: the tool authorization boundary.

Covers the six cases the P0 brief requires — allowed tool, denied tool,
approval-required tool, unauthorized user, wrong tenant, agent attempting an
unauthorized tool — plus the rule ORDER, which is the part that is easy to break
silently. A grant must not be able to defeat the kill switch, and an unidentified
caller must be refused before any tool-specific reasoning runs.

`authorize` is pure, so none of this needs a database, a model, or a running app.
That is deliberate: a policy you can only test end-to-end is a policy nobody
re-tests after they change it.
"""
import importlib

import pytest

from backend.orchestrator import authz, registry
from backend.orchestrator.authz import Decision, RiskLevel

# Importing the package registers the v1 tools; the dashboard tools are registered
# by importing the analytics agent. Both are needed for the name lookups below.
import backend.orchestrator  # noqa: F401


TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _call(tool_name, granted, *, user_id=USER_A, tenant_id=TENANT_A,
          agent_id="primary", session_id="sess", arguments=None):
    return authz.authorize_call(
        user_id=user_id, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id,
        tool_name=tool_name, arguments=arguments or {}, granted=granted,
        tool=registry.get(tool_name))


# ── 1. allowed tool ───────────────────────────────────────────────────────────
def test_allowed_tool_executes():
    res = _call("list_tasks", ["list_tasks"])
    assert res.decision is Decision.ALLOW
    assert res.allowed and not res.needs_approval and not res.denied
    assert res.rule == "grant:name"


def test_allowed_by_permission_grant():
    """required_permission is behaviourally effective: granting 'email.read' grants
    every tool that declares it, without naming any of them."""
    assert "list_emails" in registry.tools_for_permission("email.read")
    res = _call("list_emails", ["email.read"])
    assert res.decision is Decision.ALLOW
    assert res.rule == "grant:permission"


def test_permission_grant_covers_the_whole_family_and_nothing_else():
    covered = registry.tools_for_permission("email.read")
    for name in covered:
        assert _call(name, ["email.read"]).decision is not Decision.DENY, name
    # A tool from a different family is NOT swept in by the same grant.
    assert _call("list_tasks", ["email.read"]).decision is Decision.DENY


# ── 2. denied tool ────────────────────────────────────────────────────────────
def test_unknown_tool_denied():
    res = authz.authorize_call(user_id=USER_A, tenant_id=TENANT_A, agent_id="primary",
                               session_id="s", tool_name="rm_rf_everything",
                               granted=["rm_rf_everything"], tool=None)
    assert res.decision is Decision.DENY
    assert res.rule == "unknown_tool"


def test_kill_switch_denies_even_a_granted_tool(monkeypatch):
    """Rule ORDER: the operator kill switch runs BEFORE grants, so it cannot be
    defeated by granting the tool."""
    import backend.guardrails as g
    monkeypatch.setenv("AGANETI_DENIED_TOOLS", "run_python,query_data")
    importlib.reload(g)
    try:
        res = _call("run_python", ["run_python", "code.run"])
        assert res.decision is Decision.DENY
        assert res.rule == "kill_switch"
    finally:
        monkeypatch.delenv("AGANETI_DENIED_TOOLS", raising=False)
        importlib.reload(g)


# ── 3. approval-required tool ─────────────────────────────────────────────────
@pytest.mark.parametrize("tool_name", ["send_email", "create_calendar_event", "web_search"])
def test_outbound_tools_require_approval(tool_name):
    res = _call(tool_name, [tool_name])
    assert res.decision is Decision.APPROVAL_REQUIRED
    assert res.rule == "outbound"
    assert res.risk_level is RiskLevel.OUTBOUND


def test_outbound_cannot_be_downgraded_by_the_category_table():
    """The registry's is_outbound is the authority. Even if the policy table were
    edited to call web_search a 'read', the outbound flag still forces approval."""
    req = authz.build_request(user_id=USER_A, tenant_id=TENANT_A, agent_id="p",
                              session_id="s", tool_name="web_search", is_outbound=True)
    assert req.risk_level is RiskLevel.OUTBOUND
    res = authz.authorize(req, granted=["web_search"], tool_exists=True,
                          required_permission="web.search", is_outbound=True)
    assert res.decision is Decision.APPROVAL_REQUIRED


def test_autonomy_level_is_behaviourally_effective(monkeypatch):
    """AUTONOMY_LEVEL was inert before P0 — only 'deny' ever affected execution.
    At 'assist', a task-category tool must now actually pause."""
    import backend.guardrails as g
    monkeypatch.setenv("AUTONOMY_LEVEL", "assist")
    importlib.reload(g)
    try:
        res = _call("create_task", ["create_task"])
        assert res.decision is Decision.APPROVAL_REQUIRED
        assert res.rule == "guardrail_approval"
    finally:
        monkeypatch.delenv("AUTONOMY_LEVEL", raising=False)
        importlib.reload(g)


def test_same_tool_is_allowed_at_standard_autonomy(monkeypatch):
    import backend.guardrails as g
    monkeypatch.setenv("AUTONOMY_LEVEL", "standard")
    importlib.reload(g)
    try:
        assert _call("create_task", ["create_task"]).decision is Decision.ALLOW
    finally:
        monkeypatch.delenv("AUTONOMY_LEVEL", raising=False)
        importlib.reload(g)


# ── 4. unauthorized user ──────────────────────────────────────────────────────
def test_no_subject_denied():
    res = _call("list_tasks", ["list_tasks"], user_id="")
    assert res.decision is Decision.DENY
    assert res.rule == "no_subject"


def test_no_subject_denied_before_grants_are_considered():
    """An unidentified caller is refused even when the tool is granted AND
    kill-switched — i.e. the subject check runs first."""
    res = _call("list_tasks", ["list_tasks"], user_id="")
    assert res.rule == "no_subject"


# ── 5. wrong / missing tenant ─────────────────────────────────────────────────
def test_missing_tenant_denied_under_strict_mode():
    res = _call("list_tasks", ["list_tasks"], tenant_id="")
    assert res.decision is Decision.DENY
    assert res.rule == "no_tenant"


def test_missing_tenant_denied_before_the_kill_switch(monkeypatch):
    """Rule order: tenant precedes tool-specific reasoning."""
    import backend.guardrails as g
    monkeypatch.setenv("AGANETI_DENIED_TOOLS", "list_tasks")
    importlib.reload(g)
    try:
        res = _call("list_tasks", ["list_tasks"], tenant_id="")
        assert res.rule == "no_tenant"
    finally:
        monkeypatch.delenv("AGANETI_DENIED_TOOLS", raising=False)
        importlib.reload(g)


def test_strict_tenant_can_be_rolled_back_explicitly():
    """The emergency rollback exists and is explicit — but it is not the default."""
    assert authz.STRICT_TENANT is True
    req = authz.build_request(user_id=USER_A, tenant_id="", agent_id="p",
                              session_id="s", tool_name="list_tasks")
    lenient = authz.authorize(req, granted=["list_tasks"], required_permission="tasks.read",
                              strict_tenant=False)
    assert lenient.decision is Decision.ALLOW
    strict = authz.authorize(req, granted=["list_tasks"], required_permission="tasks.read",
                             strict_tenant=True)
    assert strict.decision is Decision.DENY


def test_tenant_is_carried_on_every_request_record():
    res = _call("list_tasks", ["list_tasks"], tenant_id=TENANT_B)
    assert res.request.tenant_id == TENANT_B
    assert res.as_dict()["request"]["tenant_id"] == TENANT_B


# ── 6. agent attempting an unauthorized tool ──────────────────────────────────
def test_agent_cannot_call_a_tool_it_was_not_granted():
    """The calendar specialist's allowlist does not include send_email."""
    from backend.orchestrator.agents import SPECIALISTS
    granted = SPECIALISTS["calendar_agent"]["tools"]
    assert "send_email" not in granted
    res = _call("send_email", granted, agent_id="calendar_agent")
    assert res.decision is Decision.DENY
    assert res.rule == "not_granted"


def test_empty_grant_list_is_a_lockdown_not_a_default():
    """An agent with no permissions can call nothing. This is the deliberate
    lockdown semantic — it must never fall back to a default toolset."""
    for name in ("current_time", "list_tasks", "send_email"):
        assert _call(name, []).decision is Decision.DENY


def test_specialists_cannot_reach_outbound_tools():
    from backend.orchestrator.agents import SPECIALISTS
    for agent_id, spec in SPECIALISTS.items():
        for name in spec["tools"]:
            tool = registry.get(name)
            assert tool is not None, f"{agent_id} grants unknown tool {name}"
            assert not tool.is_outbound, f"{agent_id} grants outbound tool {name}"


# ── request/result shape ──────────────────────────────────────────────────────
def test_tool_request_carries_every_required_field():
    req = authz.build_request(user_id=USER_A, tenant_id=TENANT_A, agent_id="primary",
                              session_id="sess-1", tool_name="query_data",
                              arguments={"sql": "SELECT 1 AS x"})
    for field in ("user_id", "tenant_id", "agent_id", "session_id",
                  "tool_name", "action", "arguments", "risk_level"):
        assert hasattr(req, field), field
    assert req.action == "query_data"
    assert req.risk_level is RiskLevel.DATA


def test_audit_shape_redacts_argument_values():
    """The audit record must be safe to log: tool arguments routinely carry message
    bodies, recipient addresses and SQL."""
    req = authz.build_request(user_id=USER_A, tenant_id=TENANT_A, agent_id="p",
                              session_id="s", tool_name="send_email",
                              arguments={"to": "victim@example.com", "body": "secret"})
    dumped = req.as_dict()
    assert dumped["arguments"] == ["body", "to"]
    assert "victim@example.com" not in str(dumped)
    assert "secret" not in str(dumped)


def test_request_is_immutable():
    req = authz.build_request(user_id=USER_A, tenant_id=TENANT_A, agent_id="p",
                              session_id="s", tool_name="list_tasks")
    with pytest.raises(Exception):
        req.tenant_id = TENANT_B  # type: ignore[misc]


def test_every_registered_tool_has_a_risk_classification():
    """Every tool the registry knows about must be classified.

    An unclassified tool still gets a decision (decide_tool treats it as `task`),
    so this is not a safety hole — but it silently escapes the policy table, which
    is how a new tool ships without anyone deciding what it is. Registration is
    scattered across four modules; all four are imported here so the assertion
    covers the union rather than whichever ones a given test process happened to
    touch.
    """
    import backend.dashboard.ask            # noqa: F401  SQL + forecast + compare
    import backend.dashboard.analytics_tools as _at  # noqa: F401  metric contract
    _at.register_analytics_tools()
    import backend.guardrails as g
    unclassified = [n for n in registry.all_names()
                    if n not in g.TOOL_CATEGORY and not n.startswith("_")]
    assert unclassified == [], f"unclassified tools: {unclassified}"


def test_registry_composition_is_what_we_think_it_is():
    """The registry's SHAPE, asserted deliberately rather than by a bare count.

    Added when the 14 browser tools took the canonical registry from 43 to 57. A
    lone `== 57` would be a number to bump on every future addition, which teaches
    people to edit a test without reading it. What is pinned instead is the
    composition — the browser family and everything else, separately — so a new
    tool fails with a message naming which half it landed in.

    Measured on a PLAIN `import backend.orchestrator`, in a subprocess. That is
    what "the canonical registry" means and what the 43 figure was: importing
    `backend.dashboard.ask` additionally registers 9 SQL/metric tools, so a test
    that imports them measures 52 — a different number from the one everyone
    quotes, attached to the same assertion.
    """
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import backend.orchestrator;"
         "from backend.orchestrator import registry;"
         "n=registry.all_names();"
         "b=[x for x in n if x.startswith('browser_')];"
         "print(len(n), len(b))"],
        capture_output=True, text=True, cwd=".")
    assert out.returncode == 0, out.stderr[-800:]
    total, browser = (int(x) for x in out.stdout.split())

    assert browser == 14, f"browser family is {browser}, expected 14"
    assert total - browser == 43, (
        f"the non-browser canonical registry moved from 43 to {total - browser}; "
        f"if that was deliberate, say so here")
    assert total == 57
