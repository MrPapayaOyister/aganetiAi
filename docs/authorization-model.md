# Authorization Model

**Status:** implemented, P0 scope · **Date:** 2026-08-14
**Companion to:** [p0-remediation.md](p0-remediation.md) · supersedes the authorization
section of [opa-audit.md](opa-audit.md)

This document is the contract. OPA is **not** installed and is not part of this work —
the point of P0-C is that the contract must be real, enforced and tested *before* a
policy engine becomes the backend for it. §7 states what changes when OPA arrives and
what does not.

---

## 1. What was wrong

The audit found three defects that together meant the system had a permission *vocabulary*
but not a permission *system*:

| Defect | Evidence |
|---|---|
| `Tool.required_permission` was declared on 33 tools, published by `GET /agent/tools`, and compared against nothing | 3 references repo-wide: the field, one assignment, one API response |
| `guardrails.decide()` returned `auto`/`approval`/`deny` but only `deny` affected execution | [backend/tools.py:956](../backend/tools.py#L956) — `if decide(name) == "deny"`. `AUTONOMY_LEVEL` had no behavioural effect at all |
| A tool call carried no tenant | `org_id` written on ~25 tables, `grep "org_id =="` → 0 results |

Authorization was three inline `elif`s inside `graph._tools_node`. Because policy was
expressed twice (there and in `guardrails`), the two runtimes disagreed on the same tool:
`web_search` was approval-gated on one and auto-executed on the other.

---

## 2. The model

Two values and one function.

```
TenantContext          who is asking          backend/auth/tenant.py
ToolRequest            what they want to do   backend/orchestrator/authz.py
authorize()            the answer             backend/orchestrator/authz.py
```

### 2.1 `TenantContext` — the subject

```python
@dataclass(frozen=True)
class TenantContext:
    user_id: uuid.UUID        # users.id          — the acting principal
    tenant_id: uuid.UUID      # organizations.id  — the isolation boundary
    supabase_uid: str         # the external identity (OAuth stores are keyed by it)
    email: str = ""
    role: str = "employee"    # employee | manager | admin
```

`tenant_id` **is** `organizations.id`. The rename is deliberate: `org_id` is the storage
column, `tenant_id` is the security concept, and keeping the vocabulary separate is what
lets the column become a real multi-tenant boundary later without re-auditing every call
site.

Resolution is one path and one path only:

```
X-Auth-User  (injected by AuthEnforceMiddleware from the verified token,
              stripped from the inbound request — not forgeable)
   └─ repo.resolve_user  ─┬─ user not found            -> 403
                          ├─ user.status != "active"   -> 403
                          ├─ user.org_id is NULL       -> 403  (no tenant, no subject)
                          └─ TenantContext
```

Predicates, used instead of comparing raw ids:

| Method | Semantics |
|---|---|
| `owns_user(id)` | identity only. An admin does **not** own a peer's rows |
| `in_tenant(id)` | same tenant. A **NULL** org on the row fails — legacy rows are not everyone's |
| `can_read_user(uid, tid)` | ownership **or** same-tenant admin. The tenant check is mandatory on the admin branch: an admin is an admin *of a tenant*, never globally |
| `assert_*` | the same, raising `TenantError(status_code=404)` |

404 rather than 403 throughout: a 403 confirms the resource exists, which is an existence
oracle across the tenant boundary.

### 2.2 `ToolRequest` — the canonical request

Every field the brief specified, frozen, and complete — a rule can never reach around the
request for extra context.

```python
@dataclass(frozen=True)
class ToolRequest:
    user_id: str
    tenant_id: str
    agent_id: str
    session_id: str
    tool_name: str
    action: str                 # the verb; today always the tool name
    arguments: dict
    risk_level: RiskLevel
```

`action` exists so one tool can later expose several verbs (`query_data:select` vs
`query_data:export`) without a signature change.

`risk_level` is **derived, never caller-supplied**:

| RiskLevel | Meaning | Source |
|---|---|---|
| `READ` | no state change | guardrails category `read` |
| `WRITE` | writes the user's own records | category `task` |
| `DATA` | reads the customer's production database | category `data` |
| `CODE` | executes caller-supplied code in a sandbox | category `code` |
| `OUTBOUND` | leaves the system | `registry.Tool.is_outbound` — **always wins** |
| `UNKNOWN` | unclassified | absent from the table |

`as_dict()` redacts argument **values** to their keys by default. Tool arguments routinely
carry message bodies, recipient addresses and SQL; the audit record must be safe to log.

### 2.3 `authorize()` — the decision

```python
def authorize(request, *, granted, tool_exists, required_permission,
              is_outbound, strict_tenant=None) -> AuthzResult
```

**Pure. No I/O.** The grant list is an argument rather than something the function loads.
That is what makes the policy exhaustively unit-testable without a database, and what will
let OPA replace the rule body without any call site moving. *Do not add a DB lookup here.*

---

## 3. Rules, in order

The order is part of the contract and is asserted by tests.

| # | Rule id | Condition | Decision |
|---|---|---|---|
| 1 | `unknown_tool` | the registry has no such tool | **DENY** |
| 2 | `no_tenant` | `tenant_id` empty and `AUTHZ_STRICT_TENANT` | **DENY** |
| 3 | `no_subject` | `user_id` empty | **DENY** |
| 4 | `kill_switch` | `AGANETI_DENIED_TOOLS` names it | **DENY** |
| 5 | `not_granted` | neither the name nor the permission is granted | **DENY** |
| 6 | `guardrail_deny` | policy category refuses it | **DENY** |
| 7 | `outbound` | `registry.Tool.is_outbound` | **APPROVAL_REQUIRED** |
| 8 | `guardrail_approval` | category requires sign-off at this autonomy level | **APPROVAL_REQUIRED** |
| 9 | `grant:name` / `grant:permission` | — | **ALLOW** |

Two orderings carry weight:

- **4 before 5** — the operator kill switch must not be defeatable by granting the tool.
- **2 and 3 before 4** — an unidentified caller is refused before any tool-specific
  reasoning runs.

`AuthzResult` carries `decision`, `reason` (human-readable, returned to the model on a
refusal), `rule` (stable machine id), `risk_level` and the originating request.

---

## 4. Grants — how `required_permission` became real

Two grant forms are recognised, both explicit:

```python
def grant_matches(tool_name, required_permission, granted) -> (bool, str):
    if tool_name in granted:            return True, "grant:name"
    if required_permission in granted:  return True, "grant:permission"
    return False, "not_granted"
```

| Form | Example | Effect |
|---|---|---|
| by **name** | `"list_emails"` | the historical form; every existing `AgentPermission` row uses it |
| by **permission** | `"email.read"` | grants **every** tool declaring it |

`email.read` now confers `draft_email`, `email_digest`, `list_emails`, `read_email` — and
one revocation removes all four, instead of requiring four separate revocations that
someone will eventually get wrong.

Neither form widens beyond what an operator wrote: a tool with an empty
`required_permission` can only ever be granted by name, and a permission grant sweeps in
only tools that declare exactly that string.

**17 permissions** are currently declared:
`analytics.read`, `calendar.read`, `calendar.write`, `code.run`, `contacts.read`,
`deals.read`, `deals.write`, `delegate`, `documents.read`, `email.read`, `email.send`,
`memory.read`, `memory.write`, `predictions.read`, `tasks.read`, `tasks.write`,
`web.search`.

New helpers: `registry.all_permissions()` and `registry.tools_for_permission(p)` — the
latter so an operator can see what a grant confers before saving it.

**Backward compatible.** Existing name grants behave exactly as before; the permission
form is additive.

---

## 5. Guardrails — how the verdicts became effective

`backend/guardrails.py` now has one verdict function serving both runtimes:

```python
def _verdict(category, level=None) -> "auto" | "approval" | "deny":
    if category is None:                return "deny"
    if category in ("read", "data"):    return "auto"
    if category in ("task", "code"):    return "auto" if level in ("standard","autonomous") else "approval"
    if category == "comms":             return "auto" if level == "autonomous" else "approval"
    return "approval"
```

- `decide(action)` — Runtime A's catalogue (`ACTION_CATEGORY`). **Behaviour unchanged.**
- `decide_tool(name, is_outbound=)` — Runtime B's catalogue (`TOOL_CATEGORY`, new, 37 entries).

Two deliberate differences in `decide_tool`:

1. `is_outbound` forces `"approval"` regardless of the category table. The registry is the
   authority on what leaves the system; the table must never be able to downgrade it.
2. An **unlisted** tool is not auto-denied (unlike `decide`). Runtime B's grant model is an
   allowlist the caller has already checked, so an unclassified tool is a gap in the table,
   not a hallucination. It is treated as `task`. A test asserts the table has no gaps.

`AUTONOMY_LEVEL` is now behaviourally effective on Runtime B: at `assist`, a `task`- or
`code`-category tool actually pauses for approval. Verified by
`test_autonomy_level_is_behaviourally_effective`.

**Categories were chosen to preserve today's effective Runtime B behaviour at the default
`AUTONOMY_LEVEL=standard`.** Nothing that ran before now pauses, and nothing that paused
before now runs. The policy table is the knob; P0 installed the knob without turning it.

New: `AGANETI_DENIED_TOOLS` — a comma-separated kill switch, refused ahead of any grant.
Before P0 there was no way to disable a tool short of editing every agent's permissions
one row at a time.

---

## 6. The enforcement boundary

**One call site.** `backend/orchestrator/graph.py::_tools_node`:

```python
tool = registry.get(name)
verdict = authz.authorize_call(
    user_id=state["user_id"], tenant_id=state.get("tenant_id", ""),
    agent_id=state["agent_id"], session_id=state.get("session_id", ""),
    tool_name=name, arguments=args, granted=state["allowed_tools"], tool=tool)
_audit(verdict)

if verdict.denied:          content = f"error: {verdict.reason}"      # handler NOT called
elif verdict.needs_approval: pending.append({...}); content = "[AWAITING USER APPROVAL] ..."
else:                        content = await tool.handler(ctx, **args)
```

The three inline checks are **gone**. `test_tools_node_has_no_second_policy_path` reads the
function's source and fails if `tool.is_outbound` or an inline allowlist check reappears, or
if `authorize_call` is called more than once.

### 6.1 Refusals are tool results, not exceptions

A DENY is returned as the `role: "tool"` message content. The model must see *why* it was
refused so it can answer without the tool, and the tool-call protocol requires every call
to be answered.

### 6.2 Approval carries its reason

The `awaiting` record now includes `rule` and `risk_level`, so an operator reviewing the
queue can tell an outbound action from a policy-driven one.

### 6.3 Resume re-authorizes

An approval is durable — it can sit in the queue across a permission revocation, an agent
edit, or a kill switch being thrown. `graph.resume` therefore re-decides before executing,
and requires the verdict to still be `APPROVAL_REQUIRED`:

- `ALLOW` → the tool stopped being approval-gated (policy changed under the approval)
- `DENY` → the grant is gone

Neither is a mandate to execute what the user signed off on, so both refuse.
Verified by `test_resume_refuses_a_revoked_tool`.

### 6.4 Delegation inherits the tenant

`agents._delegate` passes `ctx["tenant_id"]` into the nested `graph.run_turn`. A sub-agent
run is not a way to escape the isolation boundary, and its own tool calls re-enter the same
boundary with the specialist's narrower allowlist.

### 6.5 Tenancy fails closed

`AUTHZ_STRICT_TENANT` defaults to **true**: a tool call with no `tenant_id` is DENIED. The
failure mode of a forgotten tenant must be "denied", not "unscoped". Setting it `false` is
an emergency rollback that reinstates pre-P0 behaviour.

Tenant is threaded from: `_load_primary` (route layer, from `user.org_id`),
`ask_stream` / `stream_dashboard` (resolved internally via
`tenant.resolve_tenant_id`, so it cannot depend on which route the request entered
through), and `_resume` (from the **owner's live user row**, not the persisted blob — an
approval can outlive an org move).

---

## 7. What OPA changes, and what it does not

OPA remains **P3**. The prerequisite this document discharges is that there is now
something for a policy engine to decide.

**Does not change:** `ToolRequest` (it is already the input document), the call site, the
`Decision` vocabulary, or the rule ids.

**Changes:** the body of `authorize()` becomes a call to the policy engine with
`request.as_dict(redact_arguments=False)` as input. Because `authorize` is pure and its
contract is pinned by 24 tests, the swap is testable by running the same suite against both
implementations.

Prerequisites still outstanding before that is worth doing:

- a first-class **deny list** per agent (today DENY has four sources, none of them a
  per-agent explicit deny);
- **resource-level** predicates — `query_data` is granted or not; there is no way to say
  "only these tables" or "only this tenant's rows";
- the **tool execution gateway** (P2-1), which is the natural single PEP.

---

## 8. Test coverage

| File | Cases | Covers |
|---|---|---|
| `tests/test_authz_boundary.py` | 24 | the six required cases, rule order, grant forms, request/result shape, argument redaction, immutability, table completeness |
| `tests/test_executor_enforcement.py` | 16 | ALLOW executes / DENY blocks / APPROVAL pauses **in the real executor**; resume re-authorization; no second policy path |
| `tests/test_tenant_isolation.py` | 20 | context predicates, admin scoping, S1/S2/S3 endpoints through the real middleware |

All 60 pass. Mapping to the brief's required cases:

| Required case | Test |
|---|---|
| allowed tool | `test_allowed_tool_executes`, `test_allow_runs_the_handler` |
| denied tool | `test_unknown_tool_denied`, `test_kill_switch_denies_even_a_granted_tool`, `test_deny_does_not_run_the_handler` |
| approval-required tool | `test_outbound_tools_require_approval`, `test_outbound_pauses_and_does_not_execute`, `test_guardrail_approval_also_pauses` |
| unauthorized user | `test_no_subject_denied`, `test_no_subject_denied_before_grants_are_considered` |
| wrong tenant | `test_missing_tenant_denied_under_strict_mode`, `test_cross_tenant_is_refused`, `test_s3_sessions_do_not_leak_other_users` |
| agent attempting unauthorized tool | `test_agent_cannot_call_a_tool_it_was_not_granted`, `test_empty_grant_list_is_a_lockdown_not_a_default` |
