"""The Phase E authorizer: resolve facts, then decide at the boundary.

This is the function injected into `WorkerGateway._authorize`. It runs on the one
path every browser tool takes, before the transport, so a denial means Playwright is
never reached.

    payload ──► resolver (I/O, gathers)  ──► authorize() (pure, decides) ──► raise/return

The split is §4.2 option (i), and it is the only one that keeps both properties the
design needs at once:

  * **ownership is checked AT the boundary**, not in a handler, so it cannot be
    bypassed by a future caller that reaches the handler another way, and it stays
    expressible as OPA policy;
  * **`authorize()` stays pure**, so the 24 tests that rest on that keep working and
    §12's migration is a body swap rather than an architecture change.

**Grants.** The boundary needs the agent's grant list. It is loaded here — I/O, on
the resolve side of the split — and passed in as data, exactly like the session
facts. A per-tool grant (§4.3) is what the seeder writes.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from browser_tools.client import AuthorizationDenied

from . import authz
from .authz import Decision
from .browser_resolver import resolve

log = logging.getLogger("aganeti.orchestrator.browser_authz")


class BrowserAuthorizer:
    """Callable injected into the gateway. One instance per gateway.

    `grants_for` is a callback rather than a hard dependency on the database so the
    Level 4 tests can drive the real boundary with an explicit grant list — the
    policy under test is the boundary, not the DB.
    """

    def __init__(self, *, grants_for: Any = None, gateway: Any = None):
        self._grants_for = grants_for
        self._gateway = gateway

    def bind_gateway(self, gateway: Any) -> None:
        """The resolver needs the gateway to read session facts, and the gateway
        needs the authorizer. Set after construction to break the cycle."""
        self._gateway = gateway

    async def __call__(self, payload: dict) -> None:
        tool_name = payload.get("action", "")
        tenant_id = payload.get("tenant_id", "")
        user_id = payload.get("user_id", "")
        arguments = dict(payload.get("arguments") or {})
        # The gateway carries browser_session_id outside `arguments`; the resolver
        # reads it from arguments, so merge it in rather than teaching the resolver
        # about the payload's shape.
        if payload.get("browser_session_id"):
            arguments.setdefault("browser_session_id", payload["browser_session_id"])

        facts = await resolve(tool_name, arguments,
                              requesting_tenant=tenant_id, requesting_user=user_id,
                              gateway=self._gateway)

        granted = await self._granted(tenant_id=tenant_id, user_id=user_id,
                                      agent_id=payload.get("agent_id", ""))

        from .registry import get as registry_get
        tool = registry_get(tool_name)

        verdict = authz.authorize_call(
            user_id=user_id, tenant_id=tenant_id,
            agent_id=payload.get("agent_id", ""), session_id=payload.get("session_id", ""),
            tool_name=tool_name, arguments=arguments,
            granted=granted, tool=tool,
            **facts.as_kwargs())

        _audit(verdict)

        if verdict.decision is Decision.DENY:
            raise AuthorizationDenied(
                code=_code_for(verdict.rule), reason=verdict.reason,
                rule=verdict.rule, terminal=True)
        # APPROVAL_REQUIRED is NOT raised here. The approval pause/resume machinery
        # is Phase H; until it exists, letting the call proceed would execute the
        # very action approval is meant to hold. So it is refused, with a rule that
        # says why — a temporary state, not the final behaviour.
        if verdict.decision is Decision.APPROVAL_REQUIRED:
            raise AuthorizationDenied(
                code="AUTHZ_DENIED",
                reason=(f"'{tool_name}' requires the user's approval, and the approval "
                        f"flow is not wired yet (Phase H). The action was not performed."),
                rule=verdict.rule, terminal=True)

    async def _granted(self, *, tenant_id: str, user_id: str, agent_id: str) -> Sequence[str]:
        if self._grants_for is None:
            return ()
        out = self._grants_for(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id)
        if hasattr(out, "__await__"):
            out = await out
        return tuple(out or ())


#: Boundary rule -> the code the tool layer reports. Ownership and domain denials
#: are AUTHZ_DENIED and DOMAIN_DENIED respectively — both terminal in §9.1, and
#: both meaning "stop", which is correct: retrying a denial is an escalation
#: attempt, and a domain is not going to become allowed on a retry.
_RULE_CODE = {
    "session_not_owned": "AUTHZ_DENIED",
    "domain_denied": "DOMAIN_DENIED",
    "not_granted": "AUTHZ_DENIED",
    "kill_switch": "AUTHZ_DENIED",
    "guardrail_deny": "AUTHZ_DENIED",
    "no_tenant": "AUTHZ_DENIED",
    "no_subject": "AUTHZ_DENIED",
    "unknown_tool": "BAD_REQUEST",
}


def _code_for(rule: str) -> str:
    return _RULE_CODE.get(rule, "AUTHZ_DENIED")


def _audit(verdict) -> None:
    """Record the decision on the events spine.

    Denials and approvals only, matching `graph._audit`'s existing behaviour. The
    audit found that ALLOWs are never recorded and that this makes "no authorization
    bypass" unprovable from the trail (§11.6). That is a Runtime-B-wide decision
    about events-spine volume, not a browser one, so this does not unilaterally
    change it — it is listed in the contradictions instead.
    """
    if verdict.decision is Decision.ALLOW:
        return
    try:
        from backend import events
        req = verdict.request
        events.log_event("authz_decision", user_id=req.user_id, name=req.tool_name,
                         success=False, meta=verdict.as_dict())
    except Exception:  # noqa: BLE001
        log.debug("authz audit failed", exc_info=True)


def grants_from_db(*, tenant_id: str, user_id: str, agent_id: str) -> list[str]:
    """The production grant source: the requesting user's primary agent's permissions.

    Synchronous by design — it runs inside the resolve half of the split, where I/O
    is allowed. Returns an empty list on any failure, which denies: a grant lookup
    that fails must not be read as "everything is permitted".
    """
    try:
        from backend.db import sync as dbsync
        from backend.db import models as M
        from sqlalchemy import select
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            if user is None:
                return []
            agent = s.execute(
                select(M.Agent).where(M.Agent.user_id == user.id,
                                      M.Agent.kind == "primary",
                                      M.Agent.deleted_at.is_(None))).scalars().first()
            if agent is None:
                return []
            rows = s.execute(
                select(M.AgentPermission.permission)
                .where(M.AgentPermission.agent_id == agent.id)).scalars().all()
            return list(rows)
    except Exception:  # noqa: BLE001
        log.exception("grant lookup failed for %s; denying", user_id)
        return []
