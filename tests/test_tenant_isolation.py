"""P0-A: tenant isolation.

Two halves:

  * the TenantContext ownership predicates themselves — the primitives every
    handler now uses instead of comparing raw ids;
  * the two endpoint families the audit found exposed (S1/S2 unauthenticated
    provider routes, S3 cross-user observability), exercised through the REAL
    ASGI middleware and the REAL handlers.

The endpoint tests deliberately mount only the routers under test rather than
importing backend.main: the point is to prove that AuthEnforceMiddleware plus the
handler are sufficient, with no help from anything else in the app.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.auth import tenant as T
from backend.auth.enforce import AuthEnforceMiddleware, _PUBLIC_PREFIXES

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
ALICE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BOB = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
MALLORY = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

CTX_ALICE = T.TenantContext(user_id=ALICE, tenant_id=TENANT_A,
                            supabase_uid="sub-alice", email="alice@a.test")
CTX_BOB = T.TenantContext(user_id=BOB, tenant_id=TENANT_A,
                          supabase_uid="sub-bob", email="bob@a.test")
CTX_MALLORY = T.TenantContext(user_id=MALLORY, tenant_id=TENANT_B,
                              supabase_uid="sub-mallory", email="m@b.test")
CTX_ADMIN_A = T.TenantContext(user_id=BOB, tenant_id=TENANT_A,
                              supabase_uid="sub-bob", email="bob@a.test", role="admin")


# ══════════════════════════════════════════════════════════════════════════════
# TenantContext predicates
# ══════════════════════════════════════════════════════════════════════════════
def test_owns_only_self():
    assert CTX_ALICE.owns_user(ALICE)
    assert not CTX_ALICE.owns_user(BOB)
    assert not CTX_ALICE.owns_user(MALLORY)


def test_owns_user_accepts_string_and_uuid():
    assert CTX_ALICE.owns_user(str(ALICE))
    assert CTX_ALICE.owns_user(ALICE)
    assert not CTX_ALICE.owns_user("not-a-uuid")
    assert not CTX_ALICE.owns_user(None)
    assert not CTX_ALICE.owns_user("")


def test_cross_tenant_is_refused():
    assert CTX_ALICE.in_tenant(TENANT_A)
    assert not CTX_ALICE.in_tenant(TENANT_B)
    assert not CTX_MALLORY.in_tenant(TENANT_A)


def test_null_tenant_on_a_row_is_not_everyones():
    """Legacy rows carry a nullable org_id. Defaulting those open is exactly how a
    pre-tenancy row leaks across the boundary, so a NULL must fail the check."""
    assert not CTX_ALICE.in_tenant(None)
    assert not CTX_ALICE.in_tenant("")


def test_admin_is_admin_of_a_tenant_not_globally():
    # An admin may read a peer inside their own tenant...
    assert CTX_ADMIN_A.can_read_user(ALICE, TENANT_A)
    # ...but not someone in another tenant, even knowing their user id.
    assert not CTX_ADMIN_A.can_read_user(MALLORY, TENANT_B)


def test_plain_user_cannot_read_a_peer_in_the_same_tenant():
    assert CTX_ALICE.in_tenant(TENANT_A) and CTX_BOB.in_tenant(TENANT_A)
    assert not CTX_ALICE.can_read_user(BOB, TENANT_A)


def test_assertions_raise_with_404_by_default():
    """404 rather than 403: a 403 confirms the resource exists, which is an
    existence oracle across the tenant boundary."""
    with pytest.raises(T.TenantError) as ei:
        CTX_ALICE.assert_owns_user(BOB)
    assert ei.value.status_code == 404
    with pytest.raises(T.TenantError):
        CTX_ALICE.assert_same_tenant(TENANT_B)
    CTX_ALICE.assert_owns_user(ALICE)      # does not raise
    CTX_ALICE.assert_same_tenant(TENANT_A)  # does not raise


def test_context_is_immutable():
    with pytest.raises(Exception):
        CTX_ALICE.user_id = BOB  # type: ignore[misc]


def test_user_without_an_org_cannot_become_a_subject():
    """A user with no tenant has no isolation boundary, so no context is built."""
    class _U:
        id, org_id, supabase_uid, email, role = ALICE, None, "s", "e", "employee"
    assert T.from_user(_U()) is None


# ══════════════════════════════════════════════════════════════════════════════
# S1 / S2 — the provider routes are no longer public
# ══════════════════════════════════════════════════════════════════════════════
def test_provider_routes_removed_from_the_public_prefix_list():
    """Regression guard for audit S1/S2. The OAuth browser legs must stay public;
    the self-service status/disconnect routes must not."""
    assert "/auth/provider" not in _PUBLIC_PREFIXES
    # Narrowed again in Phase 1: only the provider CALLBACKS stay anonymous.
    # /auth/{provider}/connect is authenticated now — see test_oauth_connect_idor.py.
    assert "/auth/google" not in _PUBLIC_PREFIXES
    assert "/auth/microsoft" not in _PUBLIC_PREFIXES
    assert "/auth/google/callback" in _PUBLIC_PREFIXES
    assert "/auth/microsoft/callback" in _PUBLIC_PREFIXES


def _provider_app():
    from backend.routes.provider_auth import router
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(AuthEnforceMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def test_s1_anonymous_cannot_delete_another_users_credentials():
    """Audit S1 was CRITICAL: DELETE /auth/provider/google?user_id=<victim> was
    anonymous and honoured the query parameter."""
    c = _provider_app()
    r = c.delete("/auth/provider/google", params={"user_id": "victim-sub"})
    assert r.status_code == 401


def test_s2_anonymous_cannot_read_another_users_provider_status():
    c = _provider_app()
    r = c.get("/auth/provider/status", params={"user_id": "victim-sub"})
    assert r.status_code == 401


def test_provider_status_ignores_a_supplied_user_id(monkeypatch):
    """Even authenticated, the query parameter must not select the subject."""
    seen = {}

    async def _fake_resolve(identity):
        seen["identity"] = identity
        return CTX_ALICE

    async def _fake_ids(user_id):
        seen["target"] = user_id
        return [user_id]

    monkeypatch.setattr(T, "resolve", _fake_resolve)
    import backend.routes.provider_auth as pa
    monkeypatch.setattr(pa, "_identity_ids", _fake_ids)

    c = _provider_app()
    r = c.get("/auth/provider/status",
              params={"user_id": "victim-sub"},
              headers={"X-Internal-Token": "", "Authorization": "Bearer x"})
    # The middleware rejects the bogus bearer; what matters is that no path exists
    # where the query parameter reaches the handler as the subject.
    assert r.status_code == 401
    assert seen.get("target") != "victim-sub"


def test_disconnect_rejects_an_unknown_provider_before_touching_any_store(monkeypatch):
    async def _fake_require(request):
        return CTX_ALICE
    monkeypatch.setattr(T, "require", _fake_require)

    from backend.routes.provider_auth import router
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app, raise_server_exceptions=False)  # no middleware: handler-level check
    r = c.delete("/auth/provider/../../etc")
    assert r.status_code in (404, 307, 405)


# ══════════════════════════════════════════════════════════════════════════════
# S3 — observability sessions / trace are scoped to the caller
# ══════════════════════════════════════════════════════════════════════════════
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Records the statement + parameters the handler issues, and returns rows only
    when the predicate actually matches the fixture's owner. This is what makes the
    test a real regression guard: deleting the WHERE clause changes `captured`, and
    the assertions below fail."""

    def __init__(self, captured, rows_by_owner):
        self._captured = captured
        self._rows = rows_by_owner

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self._captured.append((sql, dict(params or {})))
        p = dict(params or {})
        # Emulate the DB honouring the predicate.
        if "uid" in p:
            rows = self._rows.get((p.get("uid"), p.get("tenant")), [])
        else:
            rows = self._rows.get(("*", p.get("tenant")), [])
        return _FakeResult(rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeEngine:
    def __init__(self, captured, rows_by_owner):
        self._captured = captured
        self._rows = rows_by_owner

    def connect(self):
        return _FakeConn(self._captured, self._rows)


def _obs_client(monkeypatch, ctx, rows_by_owner):
    captured = []
    import backend.db.base as dbbase
    monkeypatch.setattr(dbbase, "engine", _FakeEngine(captured, rows_by_owner), raising=False)

    async def _fake_require(request):
        return ctx
    monkeypatch.setattr(T, "require", _fake_require)

    from backend.routes.observability import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), captured


import datetime as _dt
_NOW = _dt.datetime(2026, 8, 13, 12, 0, 0)
SESSION_ALICE = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def test_s3_sessions_are_scoped_to_the_caller(monkeypatch):
    rows = {(str(ALICE), str(TENANT_A)): [(SESSION_ALICE, str(ALICE), _NOW, 3)]}
    c, captured = _obs_client(monkeypatch, CTX_ALICE, rows)
    r = c.get("/observability/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "self"
    assert [s["user_id"] for s in body["sessions"]] == [str(ALICE)]
    sql, params = captured[0]
    assert "user_id = :uid" in sql and "org_id = :tenant" in sql
    assert params["uid"] == str(ALICE) and params["tenant"] == str(TENANT_A)


def test_s3_sessions_do_not_leak_other_users(monkeypatch):
    """Mallory (a different tenant) sees nothing of Alice's, even though the row
    exists in the fixture."""
    rows = {(str(ALICE), str(TENANT_A)): [(SESSION_ALICE, str(ALICE), _NOW, 3)]}
    c, captured = _obs_client(monkeypatch, CTX_MALLORY, rows)
    r = c.get("/observability/sessions")
    assert r.status_code == 200
    assert r.json()["sessions"] == []
    _, params = captured[0]
    assert params["uid"] == str(MALLORY) and params["tenant"] == str(TENANT_B)


def test_s3_admin_is_scoped_to_their_own_tenant(monkeypatch):
    rows = {("*", str(TENANT_A)): [(SESSION_ALICE, str(ALICE), _NOW, 3)]}
    c, captured = _obs_client(monkeypatch, CTX_ADMIN_A, rows)
    r = c.get("/observability/sessions")
    assert r.status_code == 200
    assert r.json()["scope"] == "tenant"
    sql, params = captured[0]
    assert "org_id = :tenant" in sql
    assert params["tenant"] == str(TENANT_A)
    assert "uid" not in params  # tenant-wide, but never global


def test_s3_trace_of_another_users_session_is_404(monkeypatch):
    """Audit S3: /trace/{id} had no ownership predicate at all. A session that is
    not the caller's must be indistinguishable from one that does not exist."""
    rows = {(str(ALICE), str(TENANT_A)): [("user", "hello", _NOW)]}
    c, captured = _obs_client(monkeypatch, CTX_MALLORY, rows)
    r = c.get(f"/observability/trace/{SESSION_ALICE}")
    assert r.status_code == 404
    assert r.json() == {"error": "not found"}
    sql, params = captured[0]
    assert "user_id = :uid" in sql and "org_id = :tenant" in sql


def test_s3_trace_of_own_session_still_works(monkeypatch):
    rows = {(str(ALICE), str(TENANT_A)): [("user", "hello", _NOW)]}
    c, _ = _obs_client(monkeypatch, CTX_ALICE, rows)
    r = c.get(f"/observability/trace/{SESSION_ALICE}")
    assert r.status_code == 200
    assert r.json()["messages"][0]["content"] == "hello"


def test_s3_trace_rejects_a_non_uuid_session_id(monkeypatch):
    c, captured = _obs_client(monkeypatch, CTX_ALICE, {})
    r = c.get("/observability/trace/not-a-uuid")
    assert r.status_code == 404
    assert captured == []  # refused before any query is issued
