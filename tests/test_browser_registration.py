"""Phase D — the 14 browser tools in the canonical Runtime B registry.

This is the first place browser code touches `backend/`. What is asserted here is
the seam: that the tools are present and classified, that ownership arrives from
`TenantContext` rather than from the model, that a call with no tenant is refused
before the worker, and that artifacts are written with an `org_id`.

**No authorization is asserted here.** It was empty when this was written and Phase
E has since filled it; the policy itself is asserted in `tests/test_browser_authz.py`
and nothing here duplicates it. The one authorization-adjacent property tested is the
§5.2 rule that an unowned browser call must not run, which is a fail-closed refusal
rather than a policy decision. Two tests below carry an AMENDED IN PHASE E note where
E disproved an invariant this file had asserted.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

import backend.orchestrator  # noqa: F401 — registers everything
import browser_tools as bt
from backend.orchestrator import browser_registration as breg
from backend.orchestrator import registry
from browser_tools.schemas import FORBIDDEN_PARAMS, TOOL_NAMES

BROWSER_TOOLS = sorted(TOOL_NAMES)


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════════════════════
# Presence and classification
# ══════════════════════════════════════════════════════════════════════════════
class TestRegistration:
    def test_all_fourteen_are_registered(self):
        missing = [n for n in BROWSER_TOOLS if registry.get(n) is None]
        assert missing == [], f"not registered: {missing}"

    def test_registration_is_consistent_across_processes(self):
        """The canonical registry must be complete in EVERY process, not in
        whichever one happened to import a route — the `list_metrics` failure."""
        import subprocess
        import sys
        out = subprocess.run(
            [sys.executable, "-c",
             "import backend.orchestrator;"
             "from backend.orchestrator import registry;"
             "n=registry.all_names();"
             "print(len(n), len([x for x in n if x.startswith('browser_')]))"],
            capture_output=True, text=True, cwd=".")
        assert out.stdout.strip() == "57 14", (out.stdout, out.stderr[-600:])

    def test_registration_is_idempotent(self):
        before = len(registry.all_names())
        breg.register_browser_tools()
        breg.register_browser_tools()
        assert len(registry.all_names()) == before

    def test_there_is_no_second_registry(self):
        """browser_tools registers nothing; this module is the only meeting point."""
        import inspect
        src = inspect.getsource(bt.tools)
        assert "register(" not in src
        assert "orchestrator" not in src

    @pytest.mark.parametrize("name", BROWSER_TOOLS)
    def test_every_tool_is_classified(self, name):
        import backend.guardrails as g
        assert name in g.TOOL_CATEGORY, f"{name} escapes the policy table"

    @pytest.mark.parametrize("name", [n for n in BROWSER_TOOLS if n != "browser_submit"])
    def test_no_navigational_tool_is_outbound(self, name):
        """§3.2 Trap 2. `is_outbound` forces approval regardless of category, so an
        outbound browser tool would put an approval in front of browser_inspect.
        Thirteen must therefore stay clear of the flag.

        AMENDED IN PHASE E. This was written as "no browser tool is outbound", which
        Phase E's Step 0 disproved as a safe invariant: `comms` alone yields "auto"
        at AUTONOMY_LEVEL=autonomous, so browser_submit — the one irreversible
        action — was UNGATED at that level. The flag is the second, level-independent
        gate that closes it. The invariant that actually matters is not "none are
        outbound" but "only the irreversible one is", which is what this now asserts
        together with `test_exactly_one_browser_tool_is_outbound` below.
        """
        assert registry.get(name).is_outbound is False

    def test_exactly_one_browser_tool_is_outbound(self):
        assert registry.get("browser_submit").is_outbound is True
        outbound = sorted(n for n in BROWSER_TOOLS if registry.get(n).is_outbound)
        assert outbound == ["browser_submit"]

    def test_only_submit_requires_approval(self):
        import backend.guardrails as g
        gated = [n for n in BROWSER_TOOLS
                 if g.decide_tool(n, is_outbound=registry.get(n).is_outbound) == "approval"]
        assert gated == ["browser_submit"], (
            f"expected only browser_submit to be gated, got {gated}")

    def test_every_browser_category_has_a_risk_mapping(self):
        """§3.2 Trap 1: a category with no `_CATEGORY_RISK` entry silently resolves
        to WRITE — including for the eight read-only tools."""
        import backend.guardrails as g
        from backend.orchestrator.authz import _CATEGORY_RISK
        for n in BROWSER_TOOLS:
            cat = g.TOOL_CATEGORY[n]
            assert cat in _CATEGORY_RISK, f"{n}: category {cat!r} has no risk mapping"

    def test_read_tools_derive_read_risk(self):
        from backend.orchestrator.authz import risk_of
        from browser_tools.outcomes import Outcome  # noqa: F401
        for n in ("browser_open", "browser_inspect", "browser_screenshot",
                  "browser_navigate", "browser_extract", "browser_wait",
                  "browser_back", "browser_close"):
            assert risk_of(n, is_outbound=False).value == "read", n

    @pytest.mark.parametrize("name", BROWSER_TOOLS)
    def test_every_tool_declares_a_permission(self, name):
        assert registry.get(name).required_permission


# ══════════════════════════════════════════════════════════════════════════════
# The Phase K tripwire
# ══════════════════════════════════════════════════════════════════════════════
INTERNAL_HOSTS = {"browser-lab", "localhost", "127.0.0.1", "::1", "playwright-worker"}


def test_browser_domain_allowlist_is_internal_only():
    """TRIPWIRE — if this fails, revisit guardrails.TOOL_CATEGORY before shipping.

    Thirteen of the 14 browser tools are classified NOT outbound (browser_submit is
    the exception). That is correct only while a browser session can reach nothing
    but internal compose services. The moment an external host is added to
    ALLOWED_DOMAINS, browser tools egress — and 13 tools marked non-outbound would
    be a lie the authorization boundary believes.

    This test exists to fail at exactly that moment. Do not add the host and
    silence the test: add the host, then decide what `TOOL_CATEGORY` and
    `is_outbound` should say (§3.2 Trap 2), then update both together.
    """
    external = [h for h in breg.ALLOWED_DOMAINS if h not in INTERNAL_HOSTS]
    assert external == [], (
        f"ALLOWED_DOMAINS now contains external host(s) {external}. Thirteen browser "
        f"tools are classified NOT outbound in guardrails.TOOL_CATEGORY on the explicit "
        f"grounds that they reach only internal services. That is no longer true. "
        f"Revisit the browser block in backend/guardrails.py (§3.2 Trap 2) before "
        f"this ships — do not silence this test.")


def test_the_tripwire_actually_fires(monkeypatch):
    """A tripwire nobody has seen fire is a tripwire nobody knows works."""
    monkeypatch.setattr(breg, "ALLOWED_DOMAINS", ("browser-lab", "example.com"))
    with pytest.raises(AssertionError, match="no longer true"):
        test_browser_domain_allowlist_is_internal_only()


def test_the_default_allowlist_is_applied_not_left_to_the_model(monkeypatch):
    """§11.2: the allowlist is policy. A model that could widen it could navigate
    anywhere, so browser_open gets the default injected at the boundary."""
    seen = {}

    async def _fake_open(**kw):
        seen.update(kw)
        from browser_tools.outcomes import Outcome
        from browser_tools.results import ToolResult
        return ToolResult(tool="browser_open", outcome=Outcome.OK)

    monkeypatch.setitem(bt.TOOLS, "browser_open", _fake_open)
    handler = breg._handler_for("browser_open")
    _await(handler({"user_id": "u", "tenant_id": "t", "agent_id": "a", "session_id": "s"}))
    assert seen["allowed_domains"] == list(breg.ALLOWED_DOMAINS)


# ══════════════════════════════════════════════════════════════════════════════
# TenantContext wiring — ownership from ctx, never from the model
# ══════════════════════════════════════════════════════════════════════════════
class TestTenantWiring:
    def test_ownership_is_not_in_the_model_facing_schema(self):
        """browser_tools requires the four fields because it takes them explicitly.
        The MODEL must never supply them — a model that could name a tenant is the
        whole class of bug §5.2 is about."""
        for name in BROWSER_TOOLS:
            props = set(registry.get(name).parameters["properties"])
            assert not (props & {"tenant_id", "user_id", "agent_id", "session_id"}), \
                f"{name} lets the model name its own tenant"

    def test_ownership_comes_from_the_executor_ctx(self, monkeypatch):
        seen = {}

        async def _fake(**kw):
            seen.update(kw)
            from browser_tools.outcomes import Outcome
            from browser_tools.results import ToolResult
            return ToolResult(tool="browser_inspect", outcome=Outcome.OK)

        monkeypatch.setitem(bt.TOOLS, "browser_inspect", _fake)
        handler = breg._handler_for("browser_inspect")
        _await(handler({"user_id": "user-1", "tenant_id": "tenant-1",
                        "agent_id": "primary", "session_id": "sess-1"},
                       browser_session_id="bs_x"))
        assert seen["tenant_id"] == "tenant-1"
        assert seen["user_id"] == "user-1"
        assert seen["agent_id"] == "primary"
        assert seen["session_id"] == "sess-1"

    @pytest.mark.parametrize("ctx", [
        {},                                                   # nothing at all
        {"user_id": "u"},                                     # no tenant
        {"user_id": "u", "tenant_id": ""},                    # empty tenant
        {"user_id": "u", "tenant_id": "   "},                 # whitespace tenant
        {"tenant_id": "t"},                                   # no user
        {"tenant_id": "t", "user_id": ""},                    # empty user
    ])
    def test_a_missing_tenant_refuses_before_the_worker(self, monkeypatch, ctx):
        """§5.2: refuse, never default and never fall back to a shared tenant."""
        called = []

        async def _boom(**kw):
            called.append(kw)
            raise AssertionError("the tool ran without a tenant")

        monkeypatch.setitem(bt.TOOLS, "browser_navigate", _boom)
        handler = breg._handler_for("browser_navigate")
        out = _await(handler(ctx, browser_session_id="bs_x", url="http://browser-lab:8080/"))
        assert called == [], "an unowned call reached browser_tools"
        assert out.startswith("FAILED")
        assert "tenant" in out.lower() or "user" in out.lower()

    def test_the_refusal_never_invents_a_tenant(self):
        for bad in ({}, {"user_id": "u"}, {"tenant_id": ""}):
            with pytest.raises(breg.MissingTenant):
                breg._identity(bad)

    def test_a_valid_ctx_resolves(self):
        ident = breg._identity({"user_id": "u", "tenant_id": "t",
                                "agent_id": "a", "session_id": "s"})
        assert ident == {"tenant_id": "t", "user_id": "u",
                         "agent_id": "a", "session_id": "s"}

    def test_session_id_falls_back_to_board_id_not_to_blank(self):
        """graph.py's ctx historically carried board_id and not session_id. The
        fallback keeps provenance rather than writing an empty string."""
        ident = breg._identity({"user_id": "u", "tenant_id": "t", "board_id": "b1"})
        assert ident["session_id"] == "b1"

    def test_the_executor_ctx_now_carries_session_id(self):
        """A handler needing provenance had to fall back to board_id (Audit Item 10)."""
        import inspect
        from backend.orchestrator import graph as g
        src = inspect.getsource(g._tools_node)
        assert '"session_id": state.get("session_id"' in src


# ══════════════════════════════════════════════════════════════════════════════
# Anti-bypass (Phase C's grep) still holds after registration
# ══════════════════════════════════════════════════════════════════════════════
class TestAntiBypassSurvivesRegistration:
    def test_tools_still_reach_the_worker_only_through_the_gateway(self):
        import inspect
        src = inspect.getsource(bt.tools)
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        for forbidden in ("BrowserWorker(", "httpx.", "InProcessTransport(",
                          "HttpTransport(", ".execute("):
            assert forbidden not in code, f"tools.py reaches the worker via {forbidden!r}"

    def test_the_registration_layer_does_not_reach_the_worker_either(self):
        """A handler that called the worker directly would be a second door, and
        Phase E's anti-bypass test would be unprovable."""
        import inspect
        src = inspect.getsource(breg)
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        for forbidden in ("BrowserWorker(", "httpx.", "playwright", "gateway.call("):
            assert forbidden not in code, f"registration reaches the worker via {forbidden!r}"

    def test_authorize_is_still_empty(self):
        """Phase E fills it. If this fails, authorization landed early and in the
        wrong commit."""
        import inspect
        from browser_tools.client import WorkerGateway
        body = inspect.getsource(WorkerGateway._authorize)
        assert "return None" in body
        assert "raise" not in body

    def test_no_schema_grew_a_selector_channel_during_registration(self):
        for name in BROWSER_TOOLS:
            props = set(registry.get(name).parameters["properties"])
            assert not (props & FORBIDDEN_PARAMS), f"{name} declares a forbidden param"


# ══════════════════════════════════════════════════════════════════════════════
# Grants — the mechanism accepts these names (§4.3). No seeding: that is Phase E.
# ══════════════════════════════════════════════════════════════════════════════
#: Phase E added two rules ahead of the guardrail branch — session ownership and
#: domain — so a bare `authorize_call` for a session-scoped tool now denies with
#: `session_not_owned` before any grant reasoning is reached. These tests are about
#: the GRANT rule, so they hand in facts that satisfy the two preceding rules; the
#: rules themselves are tested in `tests/test_browser_authz.py`.
OWNED = {"session_owner_tenant": "t", "session_owner_user": "u",
         "current_page_host": "browser-lab"}


class TestGrantMechanism:
    def test_the_write_time_filter_accepts_all_fourteen_names(self):
        """The audit found grants are filtered against
        `set(all_names()) | all_permissions()` at three routes, and an unknown
        string is DROPPED rather than rejected. A name that fails this would be a
        silent no-grant — an agent that looks configured and is not."""
        known = set(registry.all_names()) | registry.all_permissions()
        rejected = [n for n in BROWSER_TOOLS if n not in known]
        assert rejected == [], f"the write-time filter would drop {rejected}"

    def test_the_permission_strings_are_also_accepted(self):
        known = set(registry.all_names()) | registry.all_permissions()
        perms = set(breg.PERMISSIONS.values())
        rejected = [p for p in perms if p not in known]
        assert rejected == [], f"the write-time filter would drop {rejected}"

    def test_a_per_tool_grant_authorizes_exactly_that_tool(self):
        from backend.orchestrator import authz
        from backend.orchestrator.authz import Decision
        res = authz.authorize_call(
            user_id="u", tenant_id="t", agent_id="a", session_id="s",
            tool_name="browser_inspect", granted=["browser_inspect"],
            tool=registry.get("browser_inspect"), **OWNED)
        assert res.decision is Decision.ALLOW and res.rule == "grant:name"
        other = authz.authorize_call(
            user_id="u", tenant_id="t", agent_id="a", session_id="s",
            tool_name="browser_fill", granted=["browser_inspect"],
            tool=registry.get("browser_fill"), **OWNED)
        assert other.decision is Decision.DENY

    def test_a_permission_grant_covers_its_family(self):
        from backend.orchestrator import authz
        from backend.orchestrator.authz import Decision
        for n in ("browser_navigate", "browser_inspect", "browser_screenshot"):
            res = authz.authorize_call(
                user_id="u", tenant_id="t", agent_id="a", session_id="s",
                tool_name=n, granted=["browser.read"], tool=registry.get(n), **OWNED)
            assert res.decision is Decision.ALLOW and res.rule == "grant:permission", n
        # ...and does not confer write or submit.
        for n in ("browser_fill", "browser_submit"):
            res = authz.authorize_call(
                user_id="u", tenant_id="t", agent_id="a", session_id="s",
                tool_name=n, granted=["browser.read"], tool=registry.get(n), **OWNED)
            assert res.decision is Decision.DENY, n

    def test_a_wildcard_is_still_impossible(self):
        """§4.3: `browser_*` is not merely inadvisable, it cannot be expressed."""
        from backend.orchestrator import authz
        from backend.orchestrator.authz import Decision
        res = authz.authorize_call(
            user_id="u", tenant_id="t", agent_id="a", session_id="s",
            tool_name="browser_inspect", granted=["browser_*"],
            tool=registry.get("browser_inspect"), **OWNED)
        assert res.decision is Decision.DENY
        known = set(registry.all_names()) | registry.all_permissions()
        assert "browser_*" not in known, "a wildcard would now be storable"

    def test_the_canary_grants_are_seeded_and_scoped_to_one_agent(self):
        """AMENDED IN PHASE E — was `test_no_grants_were_seeded`.

        Phase D asserted zero rows, because seeding was explicitly Phase E's job.
        Phase E has now seeded them, so the assertion inverts: exactly 14 rows, all
        on ONE agent. The second half is the part still worth testing — a seeder
        that granted browser access org-wide would satisfy "14 rows exist" only if
        nobody counted the agents.
        """
        import subprocess

        def q(sql):
            out = subprocess.run(
                ["docker", "exec", "postgres", "psql", "-U", "postgres", "-d",
                 "aganeti", "-t", "-A", "-c", sql], capture_output=True, text=True)
            if out.returncode != 0:
                pytest.skip("postgres not reachable")
            return out.stdout.strip()

        assert q("SELECT count(*) FROM agent_permissions "
                 "WHERE permission LIKE 'browser%';") == "14"
        assert q("SELECT count(DISTINCT agent_id) FROM agent_permissions "
                 "WHERE permission LIKE 'browser%';") == "1"
        # And the outbound column mirrors the registry, so an operator reading the
        # table sees which grant is approval-gated.
        assert q("SELECT count(*) FROM agent_permissions "
                 "WHERE permission LIKE 'browser%' AND is_outbound;") == "1"


# ══════════════════════════════════════════════════════════════════════════════
# Artifact rows are stamped with org_id (§5.2)
# ══════════════════════════════════════════════════════════════════════════════
class _FakeStorage:
    """Stands in for SeaweedFS. The object path is not what this section tests."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, key, data, *, content_type="application/octet-stream", metadata=None):
        self.objects[key] = data
        return type("Meta", (), {"key": key, "size": len(data)})()

    def get(self, key):
        return self.objects[key]

    def exists(self, key):
        return key in self.objects


@pytest.fixture
def pg_users():
    """Two REAL users in different organizations, so cross-tenant is genuine."""
    import subprocess
    out = subprocess.run(
        ["docker", "exec", "postgres", "psql", "-U", "postgres", "-d", "aganeti",
         "-t", "-A", "-F", "|", "-c",
         "SELECT u.supabase_uid, u.org_id FROM users u WHERE u.org_id IS NOT NULL "
         "ORDER BY u.created_at;"],
        capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("postgres not reachable")
    rows = [l.split("|") for l in out.stdout.strip().splitlines() if "|" in l]
    by_org: dict[str, str] = {}
    for uid, org in rows:
        by_org.setdefault(org, uid)
    if len(by_org) < 2:
        pytest.skip("need two users in different orgs")
    (org_a, uid_a), (org_b, uid_b) = list(by_org.items())[:2]
    return {"a": (uid_a, org_a), "b": (uid_b, org_b)}


@pytest.mark.slow
class TestArtifactRowsAreStamped:
    def _store(self, monkeypatch):
        from backend.orchestrator.browser_artifacts import SeaweedArtifactStore
        fake = _FakeStorage()
        monkeypatch.setattr("backend.storage.object_store.get_storage", lambda: fake)
        return SeaweedArtifactStore(), fake

    def test_org_id_is_stamped_and_matches_the_requesting_tenant(self, monkeypatch, pg_users):
        """A screenshot of a filled form with a null org_id is the §5.2 defect in a
        worse place than the graph."""
        import subprocess
        store, _ = self._store(monkeypatch)
        uid, org = pg_users["a"]
        ref = store.put(data=b"\x89PNG\r\n\x1a\nscreenshot", kind="browser_screenshot",
                        content_type="image/png", tenant_id=org, user_id=uid,
                        meta={"url": "http://browser-lab:8080/application",
                              "session_id": str(uuid.uuid4()), "masked_fields": 2})
        try:
            out = subprocess.run(
                ["docker", "exec", "postgres", "psql", "-U", "postgres", "-d", "aganeti",
                 "-t", "-A", "-F", "|", "-c",
                 f"SELECT org_id, user_id, session_id, kind, uri IS NOT NULL "
                 f"FROM chat_artifacts WHERE id = '{ref.artifact_id}';"],
                capture_output=True, text=True)
            row = out.stdout.strip().split("|")
            assert row[0] and row[0] != "", "org_id is NULL on a browser artifact"
            assert row[0] == org, f"org_id {row[0]} != requesting tenant {org}"
            assert row[1], "user_id is NULL"
            assert row[2], "session_id is NULL"
            assert row[3] == "browser_screenshot"
            assert row[4] == "t", "uri (object key) not recorded"
        finally:
            subprocess.run(
                ["docker", "exec", "postgres", "psql", "-U", "postgres", "-d", "aganeti",
                 "-c", f"DELETE FROM chat_artifacts WHERE id = '{ref.artifact_id}';"],
                capture_output=True, text=True)

    def test_a_second_tenant_cannot_read_it_by_id(self, monkeypatch, pg_users):
        import subprocess
        from browser_tools.artifacts import ArtifactError, ArtifactState
        store, _ = self._store(monkeypatch)
        uid_a, org_a = pg_users["a"]
        uid_b, org_b = pg_users["b"]
        ref = store.put(data=b"secret screenshot", kind="browser_screenshot",
                        content_type="image/png", tenant_id=org_a, user_id=uid_a,
                        meta={"session_id": str(uuid.uuid4())})
        try:
            assert store.state(ref.artifact_id, tenant_id=org_a,
                               user_id=uid_a) is ArtifactState.AVAILABLE
            # Knowing the id is not enough.
            assert store.state(ref.artifact_id, tenant_id=org_b,
                               user_id=uid_b) is ArtifactState.DENIED
            # Nor is the right user with the wrong tenant, or vice versa.
            assert store.state(ref.artifact_id, tenant_id=org_b,
                               user_id=uid_a) is ArtifactState.DENIED
            assert store.state(ref.artifact_id, tenant_id=org_a,
                               user_id=uid_b) is ArtifactState.DENIED
            with pytest.raises(ArtifactError):
                store.get(ref.artifact_id, tenant_id=org_b, user_id=uid_b)
        finally:
            subprocess.run(
                ["docker", "exec", "postgres", "psql", "-U", "postgres", "-d", "aganeti",
                 "-c", f"DELETE FROM chat_artifacts WHERE id = '{ref.artifact_id}';"],
                capture_output=True, text=True)

    def test_an_unresolvable_user_is_refused_rather_than_written_unowned(self, monkeypatch):
        from browser_tools.artifacts import ArtifactError
        store, _ = self._store(monkeypatch)
        with pytest.raises(ArtifactError):
            store.put(data=b"x", kind="browser_screenshot", content_type="image/png",
                      tenant_id="11111111-1111-1111-1111-111111111111",
                      user_id="no-such-user", meta={})

    def test_a_tenant_mismatch_is_refused(self, monkeypatch, pg_users):
        """Writing the row would launder a broken ownership chain."""
        from browser_tools.artifacts import ArtifactError
        store, _ = self._store(monkeypatch)
        uid_a, _ = pg_users["a"]
        _, org_b = pg_users["b"]
        with pytest.raises(ArtifactError, match="does not match"):
            store.put(data=b"x", kind="browser_screenshot", content_type="image/png",
                      tenant_id=org_b, user_id=uid_a, meta={})

    def test_missing_is_still_distinguishable_from_expired(self, monkeypatch, pg_users):
        import subprocess
        from browser_tools.artifacts import ArtifactState
        store, fake = self._store(monkeypatch)
        uid, org = pg_users["a"]
        ref = store.put(data=b"png", kind="browser_screenshot", content_type="image/png",
                        tenant_id=org, user_id=uid, meta={"session_id": str(uuid.uuid4())},
                        ttl_s=0)
        try:
            # The ROW is the manifest here, exactly as the file is in the
            # filesystem store: expired but recorded, versus never recorded.
            assert store.state(ref.artifact_id, tenant_id=org,
                               user_id=uid) is ArtifactState.EXPIRED
            assert store.state(str(uuid.uuid4()), tenant_id=org,
                               user_id=uid) is ArtifactState.MISSING
        finally:
            subprocess.run(
                ["docker", "exec", "postgres", "psql", "-U", "postgres", "-d", "aganeti",
                 "-c", f"DELETE FROM chat_artifacts WHERE id = '{ref.artifact_id}';"],
                capture_output=True, text=True)
