"""TenantContext — the resolved, non-forgeable identity of the caller.

Before this module the enforced isolation boundary was `user_id` alone. `org_id`
was written on ~25 tables and never read as a filter (audit finding S4/S6), so a
row belonging to another organization was invisible to the schema but reachable by
any handler that only checked `user_id`.

This is the single place an external identity becomes an authorization subject:

    X-Auth-User (injected by AuthEnforceMiddleware, never client-supplied)
        -> repo.resolve_user
            -> TenantContext(user_id, tenant_id, supabase_uid, email, role)

`tenant_id` IS `organizations.id`. The name is deliberate: `org_id` is the storage
column, `tenant_id` is the security concept, and downstream code should reason
about the latter. They are the same UUID today; keeping the vocabulary separate is
what lets the org column later become a real multi-tenant boundary without another
audit of every call site.

Handlers must obtain a context via `require(request)` and then use `owns_user` /
`assert_same_tenant` rather than comparing raw ids. Two rules make that safe:

  * `require` fails CLOSED — a missing header is 401, an unresolvable identity is
    403. There is no anonymous or default subject.
  * Every ownership helper compares BOTH tenant and user. A user id alone is never
    sufficient, so a uuid collision or a leaked id from another org cannot widen
    access.

The ContextVar is a convenience for code far from the request (the tool
authorization boundary); it is never the authority. Anything that can take the
context as an argument should.
"""
from __future__ import annotations

import contextvars
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("aganeti.auth.tenant")


class TenantError(Exception):
    """Raised when a tenant/ownership assertion fails. Callers map it to 403/404."""

    def __init__(self, message: str, *, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TenantContext:
    """The authorization subject for one request.

    Frozen so it cannot be mutated after the middleware resolved it — a handler
    that wants to act as someone else has to say so explicitly, and there is no
    supported way to do that.
    """

    user_id: uuid.UUID          # users.id          — the acting principal
    tenant_id: uuid.UUID        # organizations.id  — the isolation boundary
    supabase_uid: str           # the external identity (still needed for OAuth stores)
    email: str = ""
    role: str = "employee"      # employee | manager | admin

    # ── predicates ────────────────────────────────────────────────────────────
    @property
    def is_admin(self) -> bool:
        return (self.role or "").lower() == "admin"

    def owns_user(self, other_user_id: Any) -> bool:
        """True iff `other_user_id` is this exact principal. Identity, not authority —
        an admin does NOT own another user's rows; use `can_read_user` for that."""
        oid = _as_uuid(other_user_id)
        return oid is not None and oid == self.user_id

    def in_tenant(self, other_tenant_id: Any) -> bool:
        """True iff the row belongs to this caller's tenant.

        A NULL/absent org on the row is NOT treated as 'everyone's' — it fails.
        Several tables carry a nullable org_id from before tenancy existed, and
        defaulting those open is exactly how a legacy row leaks across a boundary.
        """
        tid = _as_uuid(other_tenant_id)
        return tid is not None and tid == self.tenant_id

    def can_read_user(self, other_user_id: Any, other_tenant_id: Any = None) -> bool:
        """Ownership OR same-tenant admin. The tenant check is mandatory for the
        admin branch: an admin is an admin OF A TENANT, never globally."""
        if self.owns_user(other_user_id):
            return True
        if self.is_admin and other_tenant_id is not None:
            return self.in_tenant(other_tenant_id)
        return False

    # ── assertions (raise instead of returning False) ─────────────────────────
    def assert_owns_user(self, other_user_id: Any, *, status_code: int = 404) -> None:
        if not self.owns_user(other_user_id):
            raise TenantError("resource does not belong to the caller", status_code=status_code)

    def assert_same_tenant(self, other_tenant_id: Any, *, status_code: int = 404) -> None:
        if not self.in_tenant(other_tenant_id):
            raise TenantError("resource belongs to another tenant", status_code=status_code)

    def assert_can_read_user(self, other_user_id: Any, other_tenant_id: Any = None,
                             *, status_code: int = 404) -> None:
        if not self.can_read_user(other_user_id, other_tenant_id):
            raise TenantError("not permitted to read this resource", status_code=status_code)

    def as_dict(self) -> dict:
        return {"user_id": str(self.user_id), "tenant_id": str(self.tenant_id),
                "supabase_uid": self.supabase_uid, "email": self.email, "role": self.role}


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    if isinstance(value, uuid.UUID):
        return value
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


# ── request-scoped propagation ────────────────────────────────────────────────
# For code too far from the request to be handed the context (the tool
# authorization boundary runs inside a LangGraph node). Always a fallback:
# anything that CAN take the context as a parameter must.
_current: contextvars.ContextVar[Optional[TenantContext]] = contextvars.ContextVar(
    "aganeti_tenant_context", default=None)


def current() -> Optional[TenantContext]:
    return _current.get()


def bind(ctx: Optional[TenantContext]):
    """Bind for the current async context. Returns the token for `reset`."""
    return _current.set(ctx)


def reset(token) -> None:
    try:
        _current.reset(token)
    except (ValueError, LookupError):  # different context — nothing to reset
        pass


# ── resolution ────────────────────────────────────────────────────────────────
async def resolve(identity: str) -> Optional[TenantContext]:
    """Map an external identity (Supabase uid / User.id / email) to a context.

    Returns None when the identity is unknown or the account is not active.
    Never raises for an unknown user — callers decide 401 vs 403 vs 404.
    """
    if not identity:
        return None
    try:
        from backend.db.base import SessionLocal
        from backend.db import repo
        async with SessionLocal() as s:
            user = await repo.resolve_user(s, identity)
            if user is None:
                return None
            if (getattr(user, "status", "active") or "active") != "active":
                log.warning("tenant: identity %s resolves to a non-active user", identity)
                return None
            return from_user(user)
    except Exception:  # noqa: BLE001 — a DB failure must not authorize anybody
        log.exception("tenant: resolution failed for %s", identity)
        return None


def from_user(user) -> Optional[TenantContext]:
    """Build a context from a loaded User row. None when the row has no org —
    a user without a tenant has no isolation boundary and must not be a subject."""
    uid = _as_uuid(getattr(user, "id", None))
    tid = _as_uuid(getattr(user, "org_id", None))
    if uid is None or tid is None:
        log.warning("tenant: user %s has no org_id — refusing to build a context",
                    getattr(user, "id", "?"))
        return None
    return TenantContext(
        user_id=uid, tenant_id=tid,
        supabase_uid=getattr(user, "supabase_uid", "") or "",
        email=getattr(user, "email", "") or "",
        role=(getattr(user, "role", "employee") or "employee"),
    )


_TENANT_TTL = 300.0
_tenant_cache: dict[str, tuple[float, str]] = {}


async def resolve_tenant_id(identity: str) -> str:
    """The tenant id for an external identity, or "" when it cannot be established.

    For code paths that hold only a user string and need to stamp a ToolRequest —
    the dashboard/analytics generators, which are entered from several routes. It
    returns "" rather than raising so the caller keeps its own error handling; the
    empty value is refused at the authorization boundary, which is where an
    unidentifiable tenant should fail.

    Short TTL cache: this is called once per turn on a hot path, and a tenant move
    is rare enough that 5 minutes of staleness is acceptable — the boundary still
    re-decides every single tool call.
    """
    import time
    if not identity:
        return ""
    now = time.monotonic()
    hit = _tenant_cache.get(identity)
    if hit and now - hit[0] < _TENANT_TTL:
        return hit[1]
    ctx = await resolve(identity)
    tid = str(ctx.tenant_id) if ctx else ""
    _tenant_cache[identity] = (now, tid)
    return tid


def _clear_caches() -> None:
    """Test hook — the TTL cache would otherwise leak state between cases."""
    _tenant_cache.clear()


def caller_identity(request) -> str:
    """The raw external identity for this request.

    Read ONLY from X-Auth-User, which AuthEnforceMiddleware strips from the client
    request and re-injects from the verified token. Body/query/X-Internal-User are
    never trusted here.
    """
    return (request.headers.get("x-auth-user") or "").strip()


async def require(request) -> TenantContext:
    """The authenticated, tenant-scoped subject for this request.

    401 when the request carries no trusted identity (should be unreachable behind
    the middleware, but this is the fail-closed backstop for any route that is on
    the public-prefix list by mistake).
    403 when the identity does not resolve to an active user in a tenant.
    """
    from fastapi import HTTPException

    identity = caller_identity(request)
    if not identity:
        raise HTTPException(status_code=401, detail="unauthenticated")
    ctx = await resolve(identity)
    if ctx is None:
        raise HTTPException(status_code=403, detail="no tenant for this identity")
    bind(ctx)
    return ctx


async def optional(request) -> Optional[TenantContext]:
    """Like `require`, but returns None instead of raising. For endpoints that
    degrade rather than fail (never for ones that return data)."""
    identity = caller_identity(request)
    if not identity:
        return None
    ctx = await resolve(identity)
    if ctx is not None:
        bind(ctx)
    return ctx
