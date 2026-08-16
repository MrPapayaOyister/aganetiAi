"""The tool authorization boundary — the single gate every tool invocation passes.

Before this module, authorization for a tool call was three inline `elif`s inside
`graph._tools_node`: unknown-tool, name-in-allowlist, and `is_outbound`. That had
three consequences the P0 audit recorded:

  * `Tool.required_permission` was declared on 33 tools, published by
    `GET /agent/tools`, and compared against nothing — a control that did not exist.
  * `guardrails.decide()` produced auto/approval/deny and only "deny" ever affected
    execution, so `AUTONOMY_LEVEL` was inert.
  * There was no tenant in the decision at all, so a tool call carried no isolation
    boundary beyond whatever the calling route had already checked.

Everything is now expressed as one pure function over one value:

    ToolRequest  ──►  authorize()  ──►  AuthzResult(ALLOW | DENY | APPROVAL_REQUIRED)

`authorize` performs NO I/O. It takes the grant list as an argument rather than
loading it, which is what makes the whole policy exhaustively unit-testable without
a database, and what will let OPA replace the rule body later without any call site
moving. Do not add a DB lookup here.

Rule order is significant and is asserted by the tests: a kill-switched tool is
refused before grants are consulted, and a missing tenant is refused before
anything else, so neither can be bypassed by a grant.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

log = logging.getLogger("aganeti.authz")

# Fail-closed tenancy. A tool call whose ToolRequest carries no tenant_id is
# refused. Set AUTHZ_STRICT_TENANT=false ONLY as an emergency rollback — it
# reinstates the pre-P0 behaviour in which a tool call had no isolation boundary.
STRICT_TENANT = os.getenv("AUTHZ_STRICT_TENANT", "true").strip().lower() not in ("0", "false", "no")

#: §11.2 domain allowlist, as DATA. A set the boundary compares against, not a
#: lookup it performs — which is what keeps `authorize()` pure and keeps the check
#: expressible as OPA policy later (§12).
#:
#: Overridable by env for a deployment that changes hosts, but the default is the
#: lab and `test_browser_domain_allowlist_is_internal_only` guards it.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    h.strip().lower() for h in
    os.getenv("BROWSER_ALLOWED_HOSTS", "browser-lab,localhost,127.0.0.1").split(",")
    if h.strip())

#: Tools whose call carries a live browser session, and therefore must be checked
#: for ownership and for the page's CURRENT host. `browser_open` is absent: it
#: creates the session, so there is nothing yet to own or to be on.
_SESSION_SCOPED: frozenset[str] = frozenset({
    "browser_close", "browser_navigate", "browser_inspect", "browser_screenshot",
    "browser_extract", "browser_wait", "browser_back", "browser_click",
    "browser_fill", "browser_select", "browser_check", "browser_upload",
    "browser_submit",
})


class RiskLevel(str, Enum):
    """What kind of blast radius the tool has. Derived, never caller-supplied."""

    READ = "read"          # no state change anywhere
    WRITE = "write"        # writes the user's own records
    DATA = "data"          # reads the customer's production database
    CODE = "code"          # executes caller-supplied code in a sandbox
    OUTBOUND = "outbound"  # leaves the system (mail, calendar, public internet)
    UNKNOWN = "unknown"    # not classified — treated as WRITE by policy, flagged here


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


# guardrails category -> risk level. One mapping, so a category cannot mean two
# different risks in two places.
_CATEGORY_RISK = {
    "read": RiskLevel.READ,
    "task": RiskLevel.WRITE,
    "data": RiskLevel.DATA,
    "code": RiskLevel.CODE,
    "comms": RiskLevel.OUTBOUND,
}


@dataclass(frozen=True)
class ToolRequest:
    """The canonical description of one tool invocation.

    Frozen and complete: everything the policy may consider is here, so a rule can
    never reach around the request for extra context. `action` is the verb being
    attempted (today always the tool name — it exists so a single tool can later
    expose several actions, e.g. query_data:select vs query_data:export, without a
    signature change).
    """

    user_id: str
    tenant_id: str
    agent_id: str
    session_id: str
    tool_name: str
    action: str
    arguments: dict = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.UNKNOWN

    # ── resolved facts (§4.1 option (a)) ──────────────────────────────────────
    # Optional, null for the 43 non-browser tools. They exist so the boundary can
    # decide ownership and domain WITHOUT doing I/O: something upstream resolves
    # them and hands them in, and `authorize()` compares values it was given.
    #
    # That is not a workaround for purity — it is what purity buys. §12 makes
    # `as_dict()` the OPA input document, and OPA is a pure function over an input
    # document. Anything the boundary must look up is a fact that has to be IN the
    # document by then. Resolving now is meeting that constraint early rather than
    # discovering it during the migration.
    #: Owner of `browser_session_id`, as resolved by the session resolver. None
    #: means "no such session" — which is deliberately the same input the boundary
    #: sees for a session owned by someone else, so the two cannot be told apart.
    session_owner_tenant: str | None = None
    session_owner_user: str | None = None
    #: The host this action is aimed at (from a `url` argument, when there is one).
    target_domain: str | None = None
    #: The host the page is ACTUALLY on — post-redirect, read from the live page.
    #: This is the one the domain check uses. Checking `target_domain` instead
    #: would be the standard redirect bypass.
    current_page_host: str | None = None

    def as_dict(self, *, redact_arguments: bool = True) -> dict:
        """Audit shape. Arguments are redacted to their KEYS by default — the audit
        record must be safe to log, and tool arguments routinely carry message
        bodies, recipient addresses and SQL."""
        return {
            "user_id": self.user_id, "tenant_id": self.tenant_id,
            "agent_id": self.agent_id, "session_id": self.session_id,
            "tool_name": self.tool_name, "action": self.action,
            "risk_level": self.risk_level.value,
            # Resolved facts. Enumerated here because this method does NOT reflect
            # over the dataclass — a field added above without a line here is
            # silently absent from the audit record AND from the future OPA input,
            # and neither failure is loud. `test_as_dict_enumerates_every_field`
            # fails when that happens.
            "session_owner_tenant": self.session_owner_tenant,
            "session_owner_user": self.session_owner_user,
            "target_domain": self.target_domain,
            "current_page_host": self.current_page_host,
            "arguments": (sorted(self.arguments.keys()) if redact_arguments
                          else self.arguments),
        }


@dataclass(frozen=True)
class AuthzResult:
    decision: Decision
    reason: str      # human-readable, surfaced to the model on a refusal
    rule: str        # stable machine id of the rule that fired — asserted by tests
    request: ToolRequest
    risk_level: RiskLevel = RiskLevel.UNKNOWN

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.decision is Decision.APPROVAL_REQUIRED

    @property
    def denied(self) -> bool:
        return self.decision is Decision.DENY

    def as_dict(self) -> dict:
        return {"decision": self.decision.value, "reason": self.reason, "rule": self.rule,
                "risk_level": self.risk_level.value, "request": self.request.as_dict()}


# ── risk derivation ───────────────────────────────────────────────────────────
def risk_of(tool_name: str, *, is_outbound: bool = False) -> RiskLevel:
    """The tool's risk level. `is_outbound` (the registry flag) always wins: the
    registry is the authority on what leaves the system and no category table may
    downgrade it."""
    if is_outbound:
        return RiskLevel.OUTBOUND
    from backend import guardrails
    cat = guardrails.TOOL_CATEGORY.get(tool_name)
    if cat is None:
        return RiskLevel.UNKNOWN
    return _CATEGORY_RISK.get(cat, RiskLevel.WRITE)


def build_request(*, user_id: str, tenant_id: str, agent_id: str, session_id: str,
                  tool_name: str, arguments: dict | None = None,
                  action: str | None = None, is_outbound: bool | None = None,
                  session_owner_tenant: str | None = None,
                  session_owner_user: str | None = None,
                  target_domain: str | None = None,
                  current_page_host: str | None = None) -> ToolRequest:
    """Assemble a ToolRequest, deriving `risk_level` from the registry + policy table.

    `is_outbound` is looked up from the registry when not supplied, so a caller
    cannot accidentally (or deliberately) declare an outbound tool inbound.
    """
    if is_outbound is None:
        try:
            from backend.orchestrator import registry
            t = registry.get(tool_name)
            is_outbound = bool(t and t.is_outbound)
        except Exception:  # noqa: BLE001 — an import failure must not de-risk a tool
            is_outbound = False
    return ToolRequest(
        user_id=str(user_id or ""), tenant_id=str(tenant_id or ""),
        agent_id=str(agent_id or ""), session_id=str(session_id or ""),
        tool_name=tool_name, action=action or tool_name,
        arguments=dict(arguments or {}),
        risk_level=risk_of(tool_name, is_outbound=bool(is_outbound)),
        session_owner_tenant=session_owner_tenant,
        session_owner_user=session_owner_user,
        target_domain=target_domain,
        current_page_host=current_page_host,
    )


# ── grants ────────────────────────────────────────────────────────────────────
def grant_matches(tool_name: str, required_permission: str,
                  granted: Iterable[str]) -> tuple[bool, str]:
    """Is this tool granted, and by which form of grant?

    Two grant forms are recognised, both explicit:

      * by NAME          — "list_emails". The historical form; every stored
                           AgentPermission row uses it.
      * by PERMISSION    — "email.read". This is what makes `Tool.required_permission`
                           behaviourally effective. Granting a permission grants every
                           tool that declares it, so revoking "email.read" removes
                           list_emails, read_email and email_digest together instead
                           of requiring three separate revocations.

    Neither form widens beyond what an operator actually wrote. A tool with an empty
    `required_permission` can only ever be granted by name.
    """
    g = set(granted or ())
    if tool_name in g:
        return True, "grant:name"
    if required_permission and required_permission in g:
        return True, "grant:permission"
    return False, "not_granted"


# ── the boundary ──────────────────────────────────────────────────────────────
def authorize(request: ToolRequest, *, granted: Sequence[str],
              tool_exists: bool = True, required_permission: str = "",
              is_outbound: bool = False, strict_tenant: bool | None = None) -> AuthzResult:
    """Decide one tool invocation. Pure — no I/O, no globals except policy config.

    Rules, in order. The order is part of the contract and is asserted by tests:

      1. unknown_tool        the registry has no such tool                  -> DENY
      2. no_tenant           the request carries no tenant                  -> DENY
      3. no_subject          the request carries no user                    -> DENY
      4. kill_switch         AGANETI_DENIED_TOOLS names it                  -> DENY
      5. not_granted         neither name nor permission is granted         -> DENY
      6. session_not_owned   session absent, or owned by another identity   -> DENY
      7. domain_denied       current page host not in the allowlist         -> DENY
      8. guardrail_deny      policy category refuses it                     -> DENY
      9. approval            policy requires sign-off                       -> APPROVAL
     10. allow                                                              -> ALLOW

    Note on 9: `outbound` and `guardrail_approval` are two LABELS on one branch,
    selected by the `is_outbound` flag — not two ordered rules. The flag makes
    `decide_tool` short-circuit to "approval" before the category is consulted, so
    a tool carrying it is gated at every autonomy level; a tool gated only by its
    category is not (`comms` is "auto" at AUTONOMY_LEVEL=autonomous).
    `browser_submit` carries both, deliberately.

    Note 4 precedes 5: the kill switch must not be defeatable by a grant. Notes 2
    and 3 precede 4 so that an unidentified caller is refused before any tool-
    specific reasoning runs at all.
    """
    from backend import guardrails

    strict = STRICT_TENANT if strict_tenant is None else strict_tenant
    risk = request.risk_level

    def _r(decision: Decision, rule: str, reason: str) -> AuthzResult:
        res = AuthzResult(decision=decision, reason=reason, rule=rule,
                          request=request, risk_level=risk)
        if decision is not Decision.ALLOW:
            log.info("authz %s rule=%s tool=%s agent=%s tenant=%s",
                     decision.value, rule, request.tool_name, request.agent_id,
                     request.tenant_id or "-")
        return res

    if not tool_exists:
        return _r(Decision.DENY, "unknown_tool",
                  f"unknown tool '{request.tool_name}'")

    if strict and not request.tenant_id:
        return _r(Decision.DENY, "no_tenant",
                  "this call has no tenant context and cannot be authorized")

    if not request.user_id:
        return _r(Decision.DENY, "no_subject",
                  "this call has no authenticated subject and cannot be authorized")

    if request.tool_name in guardrails.DENIED_TOOLS:
        return _r(Decision.DENY, "kill_switch",
                  f"'{request.tool_name}' is disabled by operator policy")

    ok, grant_rule = grant_matches(request.tool_name, required_permission, granted)
    if not ok:
        return _r(Decision.DENY, "not_granted",
                  f"this agent is not permitted to use '{request.tool_name}'")

    # ── 6. session ownership (§5.1) ───────────────────────────────────────────
    # A comparison of two values the caller was HANDED, not a lookup. The resolver
    # (browser_resolver.resolve) gathered them before this function ran, which is
    # what lets ownership live at the boundary — §4.2's requirement — without
    # `authorize()` doing I/O.
    #
    # A session that does not exist and one owned by someone else BOTH arrive here
    # as `session_owner_* = None`, because the resolver collapses them. So this one
    # branch produces one outcome for both, and a distinguishable denial — which
    # would confirm the session exists — is not merely avoided, it is unexpressible.
    if request.tool_name in _SESSION_SCOPED:
        owner_t, owner_u = request.session_owner_tenant, request.session_owner_user
        if (owner_t is None or owner_u is None
                or owner_t != request.tenant_id or owner_u != request.user_id):
            return _r(Decision.DENY, "session_not_owned",
                      "no such browser session")

    # ── 7. domain policy (§11.2) ──────────────────────────────────────────────
    # Checked on EVERY session-scoped action, not only browser_navigate: a page can
    # redirect after navigate was allowed, and a later browser_fill would then act
    # on an origin nobody checked.
    #
    # The host compared is `current_page_host` — read from the live page, POST
    # redirect — never `target_domain`, which is only what was asked for.
    # Redirect-based allowlist bypass is the standard way this control fails.
    if request.tool_name in _SESSION_SCOPED:
        host = (request.current_page_host or "").lower()
        # An empty host is a fresh session that has not navigated yet (about:blank).
        # Nothing has been loaded, so there is no origin to refuse.
        if host and host not in ALLOWED_HOSTS:
            return _r(Decision.DENY, "domain_denied",
                      f"the page is on {host!r}, which is not an allowed host")
    # browser_navigate additionally checks where it is being ASKED to go, so a
    # disallowed destination is refused before the page is fetched rather than after.
    if request.tool_name == "browser_navigate":
        target = (request.target_domain or "").lower()
        if target and target not in ALLOWED_HOSTS:
            return _r(Decision.DENY, "domain_denied",
                      f"{target!r} is not an allowed host")

    verdict = guardrails.decide_tool(request.tool_name, is_outbound=is_outbound)
    if verdict == "deny":
        return _r(Decision.DENY, "guardrail_deny",
                  f"'{request.tool_name}' is refused by the action policy")
    if verdict == "approval":
        rule = "outbound" if is_outbound else "guardrail_approval"
        return _r(Decision.APPROVAL_REQUIRED, rule,
                  f"'{request.tool_name}' requires the user's approval")

    return _r(Decision.ALLOW, grant_rule, "permitted")


def authorize_call(*, user_id: str, tenant_id: str, agent_id: str, session_id: str,
                   tool_name: str, arguments: dict | None = None,
                   granted: Sequence[str] = (), tool: Any = None,
                   session_owner_tenant: str | None = None,
                   session_owner_user: str | None = None,
                   target_domain: str | None = None,
                   current_page_host: str | None = None) -> AuthzResult:
    """Convenience wrapper: build the request from a registry Tool and decide.

    This is what the executor calls. `tool` is a `registry.Tool` or None (None means
    the registry did not resolve the name, which is rule 1).
    """
    is_outbound = bool(getattr(tool, "is_outbound", False))
    required_permission = getattr(tool, "required_permission", "") or ""
    req = build_request(user_id=user_id, tenant_id=tenant_id, agent_id=agent_id,
                        session_id=session_id, tool_name=tool_name,
                        arguments=arguments, is_outbound=is_outbound,
                        session_owner_tenant=session_owner_tenant,
                        session_owner_user=session_owner_user,
                        target_domain=target_domain,
                        current_page_host=current_page_host)
    return authorize(req, granted=granted, tool_exists=tool is not None,
                     required_permission=required_permission, is_outbound=is_outbound)
