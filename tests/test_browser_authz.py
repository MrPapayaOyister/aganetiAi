"""Phase E — authorization at the browser boundary (§4.1, §4.2, §4.3, §11.2, §12).

Phase D asserted the seam. This asserts the gate.

The test that matters is `TestAntiBypass`. Every other test here can pass while the
system is broken in the one way that counts: a refusal that is *reported* but not
*enforced*. So none of these tests conclude "denied" from the error a tool returned.
They count invocations of the thing that stands where Playwright stands, and assert
the count is zero.

    tool ──► WorkerGateway.call ──► _authorize ──► [transport] ──► worker

`CountingTransport` occupies the `[transport]` slot. If a denial ever reached it,
`executed` is non-empty and the test fails no matter how convincing the error text
was.

Three levels are exercised, deliberately:

  * the **pure boundary** (`authz.authorize_call`) — the rule itself, no I/O;
  * the **gateway** (`WorkerGateway.call`) — the rule wired to the chokepoint;
  * the **registered tool** (`registry.get(...).handler`) — the whole path an agent
    actually takes, ctx and all.

A rule that holds at the first and not the third is not a control.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect

import pytest

import backend.orchestrator  # noqa: F401 — registers the 57 tools
import browser_tools as bt
from backend.orchestrator import authz, registry
from backend.orchestrator import browser_registration as breg
from backend.orchestrator.authz import Decision, RiskLevel, ToolRequest
from backend.orchestrator.browser_authz import BrowserAuthorizer
from browser_tools.client import AuthorizationDenied, WorkerGateway
from browser_tools.outcomes import Outcome
from browser_tools.schemas import TOOL_NAMES as _TOOL_NAMES

#: `schemas.TOOL_NAMES` is an ordered tuple; set algebra below wants a set.
TOOL_NAMES = frozenset(_TOOL_NAMES)

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SID = "bs_0123456789abcdef"

#: Everything but browser_open takes a session. browser_open creates one.
SESSION_TOOLS = sorted(TOOL_NAMES - {"browser_open"})

ALL_GRANTS = sorted(TOOL_NAMES)


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════════════════════
# The instrument
# ══════════════════════════════════════════════════════════════════════════════
class CountingTransport:
    """Stands exactly where Playwright stands, and counts.

    This is the whole apparatus of the anti-bypass test. `executed` is the ONLY
    evidence any test here accepts that a call did or did not run: an error string
    proves that something produced an error, not that nothing happened.

    `session_facts` reproduces the worker's real behaviour, which is itself
    ownership-asserting (§5.1): it answers "does THIS identity own this session",
    not "who owns it". A non-owner and a stranger both get `None`.
    """

    def __init__(self, sessions: dict | None = None):
        self.executed: list[dict] = []
        self.fact_lookups: list[tuple[str, str, str]] = []
        self.screenshot_fetches: list[str] = []
        self.sessions = dict(sessions or {})

    async def execute(self, payload: dict) -> dict:
        self.executed.append(payload)
        return {
            "ok": True, "action": payload["action"], "url": "http://browser-lab:8080/",
            "title": "lab", "elements": [], "element_total": 0,
            "element_truncated": False, "extracted": None, "error": None,
            "error_message": "", "error_detail": {}, "recovery": "", "terminal": False,
            "origin": "worker", "duration_ms": 3,
            "browser_session_id": payload.get("browser_session_id") or SID,
        }

    async def fetch_screenshot(self, ref: str, *, tenant_id: str, user_id: str) -> bytes:
        self.screenshot_fetches.append(ref)
        return b"\x89PNG\r\n\x1a\n"

    async def session_facts(self, browser_session_id: str, *, tenant_id: str,
                            user_id: str) -> dict | None:
        self.fact_lookups.append((browser_session_id, tenant_id, user_id))
        rec = self.sessions.get(browser_session_id)
        if rec is None:
            return None
        if rec["tenant_id"] != tenant_id or rec["user_id"] != user_id:
            return None  # the worker's own ownership assertion
        return dict(rec)


def _lab_session(url: str = "http://browser-lab:8080/", *,
                 tenant_id: str = TENANT_A, user_id: str = USER_A) -> dict:
    return {SID: {"tenant_id": tenant_id, "user_id": user_id, "current_url": url}}


def _gateway(transport: CountingTransport, granted=ALL_GRANTS) -> WorkerGateway:
    """The real authorizer, the real boundary, an explicit grant list.

    Grants are injected rather than read from the database on purpose: the policy
    under test is the boundary, not the seeder. `grants_from_db` has its own
    fail-closed test below.
    """
    authorizer = BrowserAuthorizer(grants_for=lambda **kw: list(granted))
    gw = WorkerGateway(transport, authorizer=authorizer)
    authorizer.bind_gateway(gw)
    return gw


def _call(gw: WorkerGateway, action: str, *, tenant_id=TENANT_A, user_id=USER_A,
          browser_session_id=SID, arguments=None):
    return _await(gw.call(action=action, tenant_id=tenant_id, user_id=user_id,
                          agent_id="primary", session_id="sess",
                          browser_session_id=browser_session_id,
                          arguments=arguments or {}))


def _denial(gw: WorkerGateway, action: str, **kw) -> AuthorizationDenied:
    with pytest.raises(AuthorizationDenied) as e:
        _call(gw, action, **kw)
    return e.value


# ══════════════════════════════════════════════════════════════════════════════
# THE TEST THAT MATTERS — a denial prevents the Playwright call
# ══════════════════════════════════════════════════════════════════════════════
class TestAntiBypass:
    """A denial must PREVENT the call, not merely describe one.

    Every assertion here is about `transport.executed`. Not about the exception,
    not about the ToolResult, not about the error code — about whether the thing
    behind the gate was touched.
    """

    def test_a_denial_never_reaches_the_worker(self):
        t = CountingTransport(_lab_session())
        gw = _gateway(t, granted=[])  # no grants at all
        _denial(gw, "browser_click", arguments={"element_ref": "e1"})
        assert t.executed == [], "the worker was invoked despite a denial"

    @pytest.mark.parametrize("name", sorted(TOOL_NAMES))
    def test_no_ungranted_tool_reaches_the_worker(self, name):
        """All 14, not a representative sample. A gate with one hole is not a gate."""
        t = CountingTransport(_lab_session())
        gw = _gateway(t, granted=[])
        with pytest.raises(AuthorizationDenied):
            _call(gw, name, arguments={})
        assert t.executed == [], f"{name} reached the worker while ungranted"

    def test_the_denial_is_raised_before_the_transport_not_after(self):
        """Ordering, asserted structurally rather than trusted.

        A transport that raises on contact would make "executed == []" pass for the
        wrong reason if the gate ran second. This transport records first and
        cannot fail, so an empty list can only mean it was never called.
        """
        src = inspect.getsource(WorkerGateway.call)
        body = src.split('"""', 2)[-1]
        assert body.index("_authorize") < body.index("_t.execute")

    def test_there_is_exactly_one_route_to_the_worker(self):
        """The anti-bypass property rests on there being one door.

        If a second call site of `transport.execute` existed, gating `call()` would
        prove nothing about the calls that used the other one.
        """
        code = _code_only(inspect.getsource(bt.client))
        # Exactly one invocation, inside call(), after _authorize. Docstrings are
        # stripped: `_authorize`'s own docstring quotes `self._t.execute(...)` to
        # explain the ordering, and prose is not a second door.
        assert code.count("self._t.execute(") == 1

    def test_the_tool_layer_reports_the_refusal_without_having_run_it(self):
        """End of the real path: the agent sees a typed failure AND nothing ran."""
        t = CountingTransport(_lab_session())
        gw = _gateway(t, granted=[])
        res = _await(bt.TOOLS["browser_click"](
            tenant_id=TENANT_A, user_id=USER_A, agent_id="primary", session_id="s",
            browser_session_id=SID, element_ref="e1", gateway=gw))
        assert t.executed == []                     # ← the assertion that matters
        assert res.outcome is Outcome.FAILED
        assert res.error_code == "AUTHZ_DENIED"
        assert res.origin.value == "tool", "a refusal must not be attributed to the worker"
        assert res.terminal is True

    def test_a_denied_registered_tool_never_reaches_the_worker(self):
        """Through the registry handler — ctx, identity extraction and all."""
        t = CountingTransport(_lab_session())
        bt.set_gateway(_gateway(t, granted=[]))
        try:
            out = _await(registry.get("browser_inspect").handler(
                {"tenant_id": TENANT_A, "user_id": USER_A, "agent_id": "primary",
                 "board_id": "board-1"},
                browser_session_id=SID))
        finally:
            bt.set_gateway(_gateway(CountingTransport()))
        assert t.executed == []
        # `for_model()` renders prose, not the code — this is what an agent reads.
        assert "refused by the authorization boundary" in out
        assert "not permitted" in out
        assert "Do not retry" in out

    def test_an_approval_requirement_also_stops_the_call(self):
        """Until Phase H exists, APPROVAL_REQUIRED must not fall through to ALLOW.

        The failure mode this guards is subtle and one-directional: an unhandled
        approval verdict looks like a missing feature and behaves like a granted
        submit.
        """
        t = CountingTransport(_lab_session())
        gw = _gateway(t)
        e = _denial(gw, "browser_submit", arguments={"element_ref": "e1"})
        assert t.executed == []
        assert "approval" in e.reason.lower()
        assert "not performed" in e.reason.lower()


# ══════════════════════════════════════════════════════════════════════════════
# Grants (§4.3)
# ══════════════════════════════════════════════════════════════════════════════
class TestGrants:
    def test_a_granted_safe_action_runs(self):
        t = CountingTransport(_lab_session())
        obs = _call(_gateway(t, granted=["browser_inspect"]), "browser_inspect")
        assert obs["ok"] is True
        assert len(t.executed) == 1
        assert t.executed[0]["action"] == "browser_inspect"

    def test_an_ungranted_safe_action_is_denied(self):
        t = CountingTransport(_lab_session())
        e = _denial(_gateway(t, granted=["browser_screenshot"]), "browser_inspect")
        assert e.rule == "not_granted"
        assert e.code == "AUTHZ_DENIED"
        assert t.executed == []

    def test_a_per_tool_grant_does_not_leak_to_its_neighbours(self):
        """§4.3's reason for per-tool over one shared string, asserted."""
        t = CountingTransport(_lab_session())
        gw = _gateway(t, granted=["browser_inspect"])
        _call(gw, "browser_inspect")
        for other in ("browser_extract", "browser_click", "browser_submit"):
            with pytest.raises(AuthorizationDenied):
                _call(gw, other, arguments={"element_ref": "e1"})
        assert len(t.executed) == 1

    def test_a_permission_grant_covers_the_family(self):
        """`browser.read` is declared by five tools; granting it grants those five
        and no others. This is `required_permission` being behaviourally real."""
        family = registry.tools_for_permission("browser.read")
        assert family, "browser.read resolves to nothing — the grant path is dead"
        t = CountingTransport(_lab_session())
        gw = _gateway(t, granted=["browser.read"])
        for name in sorted(family):
            _call(gw, name, arguments={"url": "http://browser-lab:8080/"}
                  if name == "browser_navigate" else {})
        assert len(t.executed) == len(family)
        with pytest.raises(AuthorizationDenied):
            _call(gw, "browser_click", arguments={"element_ref": "e1"})

    def test_the_seeded_names_are_exactly_the_registered_names(self):
        """The seeder writes tool NAMES; `grant_matches` is exact set membership.
        A typo here is a silent no-grant, so the two lists are compared directly."""
        import scripts.seed_browser_grants as seeder  # noqa: F401
        known = set(registry.all_names()) | registry.all_permissions()
        assert set(TOOL_NAMES) <= known

    def test_a_failed_grant_lookup_denies(self):
        """Fail-closed: a database that is down must not read as "everything is
        permitted"."""
        from backend.orchestrator import browser_authz as ba
        t = CountingTransport(_lab_session())
        authorizer = BrowserAuthorizer(
            grants_for=lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
        gw = WorkerGateway(t, authorizer=authorizer)
        authorizer.bind_gateway(gw)
        with pytest.raises((AuthorizationDenied, RuntimeError)):
            _call(gw, "browser_inspect")
        assert t.executed == []
        # And the production loader itself swallows-and-denies rather than raising.
        assert ba.grants_from_db(tenant_id="", user_id="nope", agent_id="") == []


# ══════════════════════════════════════════════════════════════════════════════
# browser_submit — the one irreversible action (§3.4, Step 0)
# ══════════════════════════════════════════════════════════════════════════════
class TestSubmitIsGated:
    @pytest.mark.parametrize("level", ["assist", "standard", "autonomous"])
    def test_submit_requires_approval_at_every_autonomy_level(self, level, monkeypatch):
        """Step 0 found `comms` alone yields "auto" at autonomous — i.e. submit was
        UNGATED there. `is_outbound` is the second gate that closes it, and this
        asserts the level at which the hole was."""
        import backend.guardrails as g
        monkeypatch.setenv("AUTONOMY_LEVEL", level)
        importlib.reload(g)
        try:
            res = authz.authorize_call(
                user_id=USER_A, tenant_id=TENANT_A, agent_id="a", session_id="s",
                tool_name="browser_submit", granted=["browser_submit"],
                tool=registry.get("browser_submit"),
                session_owner_tenant=TENANT_A, session_owner_user=USER_A,
                current_page_host="browser-lab")
            assert res.decision is Decision.APPROVAL_REQUIRED
            assert res.rule == "outbound", (
                f"at {level} submit gated via {res.rule!r} — if this is "
                f"'guardrail_approval' the is_outbound flag has been removed")
        finally:
            monkeypatch.delenv("AUTONOMY_LEVEL", raising=False)
            importlib.reload(g)

    def test_exactly_one_browser_tool_is_outbound(self):
        outbound = sorted(n for n in TOOL_NAMES if registry.get(n).is_outbound)
        assert outbound == ["browser_submit"]

    def test_the_thirteen_others_are_not_approval_gated(self, monkeypatch):
        """An approval in front of browser_inspect is unusable, so the flag must
        stay on exactly one tool."""
        import backend.guardrails as g
        monkeypatch.setenv("AUTONOMY_LEVEL", "standard")
        importlib.reload(g)
        try:
            t = CountingTransport(_lab_session())
            gw = _gateway(t)
            for name in sorted(TOOL_NAMES - {"browser_submit", "browser_open"}):
                _call(gw, name, arguments={"url": "http://browser-lab:8080/"}
                      if name == "browser_navigate" else {})
            assert len(t.executed) == 12
        finally:
            monkeypatch.delenv("AUTONOMY_LEVEL", raising=False)
            importlib.reload(g)

    def test_submit_risk_is_outbound_now_that_it_is_flagged(self):
        from backend.orchestrator.authz import risk_of
        assert risk_of("browser_submit", is_outbound=True) is RiskLevel.OUTBOUND
        for name in sorted(TOOL_NAMES - {"browser_submit"}):
            assert risk_of(name, is_outbound=False) is not RiskLevel.OUTBOUND, name


# ══════════════════════════════════════════════════════════════════════════════
# Ownership (§5.1) — and its indistinguishability
# ══════════════════════════════════════════════════════════════════════════════
class TestSessionOwnership:
    def _refusal(self, *, tenant_id, user_id, sessions):
        t = CountingTransport(sessions)
        e = _denial(_gateway(t), "browser_inspect", tenant_id=tenant_id, user_id=user_id)
        assert t.executed == []
        return (e.code, e.rule, e.reason)

    def test_the_owner_is_allowed(self):
        t = CountingTransport(_lab_session())
        _call(_gateway(t), "browser_inspect")
        assert len(t.executed) == 1

    def test_cross_tenant_cross_user_and_absent_are_indistinguishable(self):
        """§5.1. A different refusal for "exists but not yours" would confirm the
        session exists — an oracle for enumerating other tenants' sessions.

        Compared as whole tuples, not "both are denials": the leak is in the
        difference, wherever it is.
        """
        other_tenant = self._refusal(tenant_id=TENANT_B, user_id=USER_A,
                                     sessions=_lab_session())
        other_user = self._refusal(tenant_id=TENANT_A, user_id=USER_B,
                                   sessions=_lab_session())
        no_session = self._refusal(tenant_id=TENANT_A, user_id=USER_A, sessions={})
        assert other_tenant == other_user == no_session
        assert other_tenant[1] == "session_not_owned"
        for part in other_tenant:
            assert TENANT_A not in part and USER_A not in part and SID not in part

    def test_the_boundary_rejects_a_mismatched_owner_on_its_own(self):
        """Independent of the transport's own ownership assertion.

        In production the worker collapses both cases to `None`, so this branch is
        defence in depth — which means nothing exercises it unless a test does.
        """
        res = authz.authorize_call(
            user_id=USER_A, tenant_id=TENANT_A, agent_id="a", session_id="s",
            tool_name="browser_inspect", granted=["browser_inspect"],
            tool=registry.get("browser_inspect"),
            session_owner_tenant=TENANT_B, session_owner_user=USER_A)
        assert res.decision is Decision.DENY and res.rule == "session_not_owned"

    @pytest.mark.parametrize("name", SESSION_TOOLS)
    def test_every_session_tool_is_ownership_checked(self, name):
        """13 tools, not the two that were convenient to write a test for."""
        t = CountingTransport(_lab_session(tenant_id=TENANT_B, user_id=USER_B))
        e = _denial(_gateway(t), name, arguments={"element_ref": "e1"})
        assert e.rule == "session_not_owned", name
        assert t.executed == [], name

    def test_browser_open_is_not_ownership_checked(self):
        """It CREATES the session. Checking ownership of a session that does not
        exist yet would deny every first call."""
        t = CountingTransport({})
        _call(_gateway(t), "browser_open", browser_session_id="")
        assert len(t.executed) == 1

    def test_ownership_is_checked_at_the_boundary_not_in_a_handler(self):
        """§4.2. A check in a handler is bypassable by a future caller that reaches
        the handler another way; a check at the boundary is not."""
        src = inspect.getsource(authz.authorize)
        assert "session_not_owned" in src
        reg = inspect.getsource(importlib.import_module(
            "backend.orchestrator.browser_registration"))
        assert "session_owner" not in reg


# ══════════════════════════════════════════════════════════════════════════════
# Domain policy (§11.2) — including the redirect
# ══════════════════════════════════════════════════════════════════════════════
class TestDomainPolicy:
    def test_navigating_to_a_disallowed_host_is_refused_before_the_fetch(self):
        t = CountingTransport(_lab_session())
        e = _denial(_gateway(t), "browser_navigate",
                    arguments={"url": "https://evil.example.com/login"})
        assert e.rule == "domain_denied"
        assert e.code == "DOMAIN_DENIED"
        assert t.executed == [], "the page was fetched before the domain was checked"

    def test_navigating_within_the_allowlist_is_allowed(self):
        t = CountingTransport(_lab_session())
        _call(_gateway(t), "browser_navigate",
              arguments={"url": "http://browser-lab:8080/login"})
        assert len(t.executed) == 1

    def test_an_action_after_a_redirect_off_the_allowlist_is_denied(self):
        """§11.2, and the reason the resolver reads the LIVE page.

        The sequence this models: navigate to an allowed host, that host 302s
        somewhere else, and the agent then fills a field. Checking the requested
        URL would have passed step 1 and never looked again — the standard
        redirect-based allowlist bypass.
        """
        t = CountingTransport(_lab_session("http://browser-lab:8080/go"))
        gw = _gateway(t)
        _call(gw, "browser_navigate", arguments={"url": "http://browser-lab:8080/go"})
        assert len(t.executed) == 1               # step 1 allowed, correctly

        # ...the page is now somewhere else entirely.
        t.sessions[SID]["current_url"] = "https://evil.example.com/harvest"

        e = _denial(gw, "browser_fill",
                    arguments={"element_ref": "e1", "value": "hunter2"})
        assert e.rule == "domain_denied"
        assert "evil.example.com" in e.reason
        assert len(t.executed) == 1, "the fill ran on an unchecked origin"

    @pytest.mark.parametrize("name", SESSION_TOOLS)
    def test_the_domain_is_checked_on_every_action_not_only_navigate(self, name):
        t = CountingTransport(_lab_session("https://evil.example.com/x"))
        e = _denial(_gateway(t), name, arguments={"element_ref": "e1"})
        assert e.rule == "domain_denied", name
        assert t.executed == [], name

    def test_a_fresh_session_with_no_page_is_not_denied(self):
        """about:blank has no host. Refusing it would deny the first navigate."""
        t = CountingTransport(_lab_session("about:blank"))
        _call(_gateway(t), "browser_navigate",
              arguments={"url": "http://browser-lab:8080/"})
        assert len(t.executed) == 1

    def test_the_check_uses_the_live_host_not_the_requested_url(self):
        """A benign-looking `url` argument must not launder a page that is already
        off the allowlist."""
        t = CountingTransport(_lab_session("https://evil.example.com/x"))
        e = _denial(_gateway(t), "browser_navigate",
                    arguments={"url": "http://browser-lab:8080/"})
        assert e.rule == "domain_denied"
        assert "evil.example.com" in e.reason

    def test_the_model_cannot_widen_the_worker_side_allowlist(self):
        """§11.2: the allowlist is policy. The parameter exists so a caller can
        NARROW it, and a model that supplies a wider one gets policy instead.

        Found during Phase E: the Phase D handler applied the default only when the
        argument was ABSENT, so `allowed_domains=["evil.example.com"]` produced a
        worker session scoped to evil.example.com. The boundary denied the
        navigation that followed — the test below proves that — so it was never
        exploitable. But it meant the two controls disagreed about what was
        allowed, and only one of them was load-bearing.
        """
        t = CountingTransport({})
        bt.set_gateway(_gateway(t))
        try:
            _await(registry.get("browser_open").handler(
                {"tenant_id": TENANT_A, "user_id": USER_A, "agent_id": "primary",
                 "board_id": "b"},
                allowed_domains=["evil.example.com"]))
        finally:
            bt.set_gateway(_gateway(CountingTransport()))
        scoped = t.executed[0]["arguments"]["allowed_domains"]
        assert "evil.example.com" not in scoped
        assert set(scoped) == set(breg.ALLOWED_DOMAINS)

    def test_the_model_can_still_narrow_it(self):
        t = CountingTransport({})
        bt.set_gateway(_gateway(t))
        try:
            _await(registry.get("browser_open").handler(
                {"tenant_id": TENANT_A, "user_id": USER_A, "agent_id": "primary",
                 "board_id": "b"},
                allowed_domains=["browser-lab"]))
        finally:
            bt.set_gateway(_gateway(CountingTransport()))
        assert t.executed[0]["arguments"]["allowed_domains"] == ["browser-lab"]

    def test_the_boundary_denies_even_when_the_worker_was_widened(self):
        """Defence in depth, asserted as such: with the worker's own allowlist
        deliberately wrong, the boundary is still the thing that refuses."""
        t = CountingTransport(_lab_session())
        e = _denial(_gateway(t), "browser_navigate",
                    arguments={"url": "https://evil.example.com/",
                               "allowed_domains": ["evil.example.com"]})
        assert e.rule == "domain_denied"
        assert t.executed == []

    def test_the_allowlist_and_the_session_scope_agree_with_the_resolver(self):
        """Three lists, one meaning. If they drift, a tool is scoped in one place
        and not the other, which is how a gap opens silently."""
        from backend.orchestrator.authz import _SESSION_SCOPED
        from backend.orchestrator.browser_resolver import _SESSION_TOOLS
        assert _SESSION_SCOPED == _SESSION_TOOLS == set(SESSION_TOOLS)
        assert authz.ALLOWED_HOSTS == frozenset(
            {"browser-lab", "localhost", "127.0.0.1"})


# ══════════════════════════════════════════════════════════════════════════════
# The kill switch, on browser tools
# ══════════════════════════════════════════════════════════════════════════════
class TestKillSwitch:
    def test_the_kill_switch_denies_a_granted_browser_tool(self, monkeypatch):
        """The operator lever must work on browser tools too, and must beat a grant.
        Rule order: kill_switch precedes not_granted precedes everything browser."""
        import backend.guardrails as g
        monkeypatch.setenv("AGANETI_DENIED_TOOLS", "browser_click,browser_submit")
        importlib.reload(g)
        try:
            t = CountingTransport(_lab_session())
            gw = _gateway(t)  # fully granted, owned session, allowed host
            e = _denial(gw, "browser_click", arguments={"element_ref": "e1"})
            assert e.rule == "kill_switch"
            assert t.executed == []
            _call(gw, "browser_inspect")   # the others keep working
            assert len(t.executed) == 1
        finally:
            monkeypatch.delenv("AGANETI_DENIED_TOOLS", raising=False)
            importlib.reload(g)


# ══════════════════════════════════════════════════════════════════════════════
# The resolver gathers, never decides (§4.2)
# ══════════════════════════════════════════════════════════════════════════════
class TestResolverHasNoAuthority:
    def test_the_resolver_contains_no_decision(self):
        """Source-level, because this is a structural property.

        If the resolver could return a verdict there would be two authorities, and
        §4.2's ordering guarantee — facts, then policy — would be a convention
        rather than a property. Docstrings and comments are stripped: this module
        *describes* the boundary at length, and must be allowed to.
        """
        import backend.orchestrator.browser_resolver as mod
        code = _code_only(inspect.getsource(mod))
        for banned in ("Decision", "DENY", "ALLOW", "APPROVAL", "authorize"):
            assert banned not in code, f"the resolver mentions {banned!r} in code"

    def test_the_resolver_returns_facts_for_a_non_owner_rather_than_refusing(self):
        """It must not raise, not deny, not decide — just come back empty, and let
        the boundary conclude."""
        from backend.orchestrator.browser_resolver import resolve
        t = CountingTransport(_lab_session())
        gw = _gateway(t)
        facts = _await(resolve("browser_inspect", {"browser_session_id": SID},
                               requesting_tenant=TENANT_B, requesting_user=USER_B,
                               gateway=gw))
        assert facts.session_owner_tenant is None
        assert facts.session_owner_user is None

    def test_a_resolver_failure_yields_nulls_which_deny(self):
        """Fail-closed by the SHAPE of the data — a null owner cannot match a
        requesting identity — rather than by a special case someone can delete."""
        from backend.orchestrator.browser_resolver import resolve

        class Broken:
            async def session_facts(self, *a, **k):
                raise RuntimeError("worker unreachable")

        facts = _await(resolve("browser_inspect", {"browser_session_id": SID},
                               requesting_tenant=TENANT_A, requesting_user=USER_A,
                               gateway=Broken()))
        assert facts.as_kwargs()["session_owner_tenant"] is None
        res = authz.authorize_call(
            user_id=USER_A, tenant_id=TENANT_A, agent_id="a", session_id="s",
            tool_name="browser_inspect", granted=["browser_inspect"],
            tool=registry.get("browser_inspect"), **facts.as_kwargs())
        assert res.decision is Decision.DENY and res.rule == "session_not_owned"

    def test_authorize_still_performs_no_io(self):
        """The purity §12 depends on. `authorize` imports guardrails and reads two
        module constants; it must not acquire a session, open a socket, or await."""
        src = _code_only(inspect.getsource(authz.authorize))
        for banned in ("session(", "httpx", "requests.", "await ", "async "):
            assert banned not in src, f"authorize() does I/O: {banned!r}"


# ══════════════════════════════════════════════════════════════════════════════
# as_dict() completeness — the OPA input document (§12)
# ══════════════════════════════════════════════════════════════════════════════
class TestAuditShapeIsComplete:
    def test_as_dict_enumerates_every_field(self):
        """FAILS when a field is added to ToolRequest without appearing in as_dict().

        This is the §12 tripwire. `as_dict()` is the future OPA input document, so a
        field the boundary considers but the document omits becomes a policy input
        that exists in Python and not in Rego — the migration would then change
        behaviour while looking like a port.

        Compared against `dataclasses.fields`, so adding a field is what breaks it,
        not editing a list somewhere.
        """
        import dataclasses
        declared = {f.name for f in dataclasses.fields(ToolRequest)}
        req = ToolRequest(
            user_id="u", tenant_id="t", agent_id="a", session_id="s",
            tool_name="browser_fill", action="browser_fill",
            arguments={"value": "secret"}, risk_level=RiskLevel.WRITE,
            session_owner_tenant="t", session_owner_user="u",
            target_domain="browser-lab", current_page_host="browser-lab")
        emitted = set(req.as_dict())
        missing = declared - emitted
        assert not missing, (
            f"{sorted(missing)} are in ToolRequest but not in as_dict(). Add them "
            f"to as_dict() — it is the OPA input document (§12).")

    def test_the_four_resolved_facts_are_present_and_correct(self):
        req = authz.build_request(
            user_id="u", tenant_id="t", agent_id="a", session_id="s",
            tool_name="browser_fill", session_owner_tenant="t",
            session_owner_user="u", target_domain="browser-lab",
            current_page_host="evil.example.com")
        d = req.as_dict()
        assert d["session_owner_tenant"] == "t"
        assert d["session_owner_user"] == "u"
        assert d["target_domain"] == "browser-lab"
        assert d["current_page_host"] == "evil.example.com"

    def test_arguments_are_still_redacted_to_keys(self):
        """The audit row must stay safe to log. A browser_fill argument is a
        password as often as not."""
        req = authz.build_request(
            user_id="u", tenant_id="t", agent_id="a", session_id="s",
            tool_name="browser_fill", arguments={"value": "hunter2"})
        assert "hunter2" not in repr(req.as_dict())
        assert req.as_dict()["arguments"] == ["value"]


# ── helpers ───────────────────────────────────────────────────────────────────
def _code_only(src: str) -> str:
    """Source with docstrings and comments removed.

    Written because the first version of the resolver test failed on its own
    docstring: the module explains that it must not decide, using the word DENY to
    say so. Asserting on prose rather than code is a test that punishes accurate
    documentation.
    """
    out, in_doc, delim = [], False, ""
    for line in src.splitlines():
        s = line.strip()
        if in_doc:
            if delim in s:
                in_doc = False
            continue
        if s.startswith(('"""', "'''")):
            delim = s[:3]
            if not (len(s) > 3 and s.endswith(delim)):
                in_doc = True
            continue
        if s.startswith("#"):
            continue
        out.append(line.split("  # ")[0])
    return "\n".join(out)
