# P0 Remediation — Implementation Report

**Date:** 2026-08-14 · **Scope:** P0 only (P0-A security, P0-B runtime decision, P0-C authorization foundation)
**Follows:** [architecture-audit.md](architecture-audit.md) · **Details:** [authorization-model.md](authorization-model.md), [runtime-migration.md](runtime-migration.md)

**Not implemented, by instruction:** Playwright, Agent Reach, browser automation, MCP, OPA.
Runtime A was not deleted and received no new capability.

---

## 1. Summary

| Item | Status | Proof |
|---|---|---|
| P0-A1 unauthenticated OAuth credential deletion | **fixed** | live `401`; `test_s1_anonymous_cannot_delete_another_users_credentials` |
| P0-A2 unauthenticated provider status | **fixed** | live `401`; `test_s2_anonymous_cannot_read_another_users_provider_status` |
| P0-A3 observability session/trace authorization | **fixed** | 6 tests incl. cross-user `404` |
| P0-A4 TenantContext + tenant ownership | **implemented** | `backend/auth/tenant.py`; 9 predicate tests |
| P0-A5 cross-user / cross-tenant regression tests | **implemented** | 20 tests in `test_tenant_isolation.py` |
| P0-B6 Runtime B is the target runtime | **decided + documented** | [runtime-migration.md](runtime-migration.md) |
| P0-B7 Runtime A not removed | **honoured** | untouched except the bridge entry |
| P0-B8 no new capability on Runtime A | **honoured** | `backend/tools.py` not edited by this work |
| P0-B9 controlled migration flag | **implemented** | `backend/runtime_flag.py`; 25 tests |
| P0-B10 switch + rollback documented | **done** | [runtime-migration.md §3–4](runtime-migration.md) |
| P0-C11 canonical `ToolRequest` | **implemented** | all 8 fields; `test_tool_request_carries_every_required_field` |
| P0-C12 one enforcement boundary | **implemented** | `authz.authorize_call`; `test_tools_node_has_no_second_policy_path` |
| P0-C13 `required_permission` + guardrails effective | **implemented** | `test_allowed_by_permission_grant`, `test_autonomy_level_is_behaviourally_effective` |
| P0-C14 OPA not installed | **honoured** | zero OPA code/deps/containers |
| P0-C15 six authorization test cases | **implemented** | mapped in [authorization-model.md §8](authorization-model.md) |

**65 new tests. 603 passed / 4 pre-existing failures. No database migration.**

---

## 2. Exact files changed

### New (5)

| File | Lines | Purpose |
|---|---|---|
| `backend/auth/tenant.py` | 233 | `TenantContext`, resolution, ownership predicates, ContextVar |
| `backend/orchestrator/authz.py` | 240 | `ToolRequest`, `RiskLevel`, `Decision`, `AuthzResult`, `authorize()` |
| `backend/runtime_flag.py` | 130 | `choose()`, `use_runtime_b()`, `snapshot()` |
| `tests/test_authz_boundary.py` | 24 tests | policy correctness + rule order |
| `tests/test_executor_enforcement.py` | 16 tests | the executor actually consults the boundary |
| `tests/test_tenant_isolation.py` | 20 tests | context predicates + S1/S2/S3 endpoints |
| `tests/test_runtime_flag.py` | 25 tests | flag precedence, stability, rollback |

### Modified (12)

| File | Change |
|---|---|
| `backend/auth/enforce.py` | removed `"/auth/provider"` from `_PUBLIC_PREFIXES` (**the S1/S2 root cause**) |
| `backend/routes/provider_auth.py` | `provider_status` + `disconnect_provider` now take `request: Request`, derive identity via `_self_identity()` → `tenant.require()`; query `user_id` ignored; unknown provider → 404 |
| `backend/routes/observability.py` | `sessions()` + `trace()` take `request`, call `tenant.require()`, scope in SQL; non-owner trace → 404; non-uuid session → 404 before any query |
| `backend/guardrails.py` | `DENIED_TOOLS` kill switch; `TOOL_CATEGORY` (37 entries); `_verdict()` extracted as the single policy function; `decide_tool()`; `policy_snapshot()` extended. **`decide()` behaviour unchanged** |
| `backend/orchestrator/graph.py` | `AgentState` gains `tenant_id` + `session_id`; `_tools_node` rewritten around `authz.authorize_call`; `_audit()`; `resume()` re-authorizes; `_init`/`run_turn`/`astream_turn`/`resume` accept `tenant_id`/`session_id` |
| `backend/orchestrator/registry.py` | `all_permissions()`, `tools_for_permission()` |
| `backend/orchestrator/agents.py` | `_delegate` propagates the tenant into the nested run |
| `backend/routes/agent_os.py` | `_load_primary` returns `tenant_id`; permission filters accept names **or** permission strings; `astream_turn`/`resume` receive the tenant |
| `backend/dashboard/ask.py` | `_init_state` carries `tenant_id`/`session_id`; resolved via `tenant.resolve_tenant_id` |
| `backend/dashboard/stream.py` | same |
| `backend/main.py` | `_serve_via_runtime_b()` bridge; flag check as the first statement of `chat_endpoint`; `GET /runtime/flag` |
| `config/settings.py` | `AUTHZ_STRICT_TENANT`, `AGANETI_DENIED_TOOLS`, `RUNTIME_B_*` |

> The working tree was already dirty before this work (uncommitted changes and untracked
> files from prior sessions), so `git diff HEAD` line counts do **not** isolate this change
> set. The list above is what this change set touched; `backend/tools.py`,
> `backend/analytics.py` and `migrations/` were not edited here despite showing a diff
> against `HEAD`.

---

## 3. Database migrations

**None. No schema change was required and none was made.**

Tenant enforcement uses the `org_id` columns that already exist on `users`,
`chat_messages`, `chat_sessions`, `approvals` and ~20 other tables. The audit's finding was
that `org_id` was *written and never read as a filter* — the fix is to read it, not to add
anything.

Verified: `alembic_version` unchanged, no `ALTER TABLE` issued, and no migration file was
added by this work. (`migrations/versions/` does contain three untracked files dated
2026-08-07/11/12 — they predate this change set and belong to earlier sessions.)

One consequence to be aware of: **`agent_permissions.permission` may now contain a
permission string** (e.g. `email.read`) as well as a tool name. The column is `Text` with no
constraint, so this is a data-shape widening, not a schema change, and existing rows are
unaffected.

---

## 4. API behaviour changes

### Breaking

| Endpoint | Before | After |
|---|---|---|
| `GET /auth/provider/status` | anonymous; `?user_id=` selected the subject | **401** without a token; subject is the caller; `user_id` ignored |
| `DELETE /auth/provider/{provider}` | anonymous; `?user_id=` selected the victim | **401** without a token; deletes the **caller's** credentials only; unknown provider → 404 |
| `GET /observability/sessions` | every user's sessions | caller's own (`scope: "self"`); admins get their tenant (`scope: "tenant"`). New `scope` field; ids stringified |
| `GET /observability/trace/{id}` | any session's content | caller's own; otherwise **404**; non-uuid → **404** |

Frontend impact: the Vite client calls `/auth/provider/status` with `user_id` — it will now
receive 401 unless it sends the bearer. **This must be verified before deploy.** The
parameter itself can stay; it is ignored.

### Additive

| Endpoint | Change |
|---|---|
| `GET /runtime/flag` | **new** (authenticated) — live migration configuration |
| `PUT /agent/agents/{id}/permissions` | accepts permission strings as well as tool names; unknown strings still dropped |
| `POST /agent/agents` | same |
| `GET /guardrails` | `policy_snapshot()` gains `denied_tools` and `tools` |
| `POST /chat` | when a cohort is enabled: `x-runtime` + `x-runtime-reason` headers; an approval pause returns an `approval` object alongside `reply`. **No change with the default (empty) flag configuration** |

### Executor-visible

- A refused tool call returns `error: <reason>` as the tool result (previously
  `error: this agent is not permitted to use '<name>'` only for the allowlist case).
- The `awaiting` approval record gains `rule` and `risk_level`.
- A tool call with no resolvable tenant is **denied** under `AUTHZ_STRICT_TENANT=true`.

---

## 5. Test results

```
$ python -m pytest -q --ignore=tests/test_insight_evidence.py --ignore=tests/test_routing.py
4 failed, 603 passed, 1 warning in 113.07s
```

### New tests — 85 cases, all passing

```
tests/test_authz_boundary.py .......................  24 passed
tests/test_executor_enforcement.py ...............    16 passed
tests/test_tenant_isolation.py ....................   20 passed
tests/test_runtime_flag.py .........................  25 passed
```

### Pre-existing failures (4) — not caused by this work

`tests/test_analytics.py::{test_summary, test_completed_window, test_unknown_metric_rejected, test_user_scoping}`
— `AttributeError: module 'backend.analytics' has no attribute 'DB_PATH'`.

Verified pre-existing: `git show HEAD:backend/analytics.py | grep -c DB_PATH` → `0`, and
`git status --porcelain backend/analytics.py tests/test_analytics.py` → empty. The test
monkeypatches a constant the module no longer has.

### Pre-existing collection errors (2) — excluded, not fixed

`tests/test_insight_evidence.py` and `tests/test_routing.py` are standalone scripts that
call `sys.exit()` at module level, which crashes pytest **collection for the whole run**
(`INTERNALERROR`). Both are committed and unmodified by this work. They were excluded to get
a suite result at all. **This is a real CI hazard and is listed as risk R7.**

### Live verification (running instance, port 8001)

```
DELETE /auth/provider/google?user_id=victim  -> 401   (was 200)
GET    /auth/provider/status?user_id=victim  -> 401   (was 200)
GET    /auth/google/connect                  -> 307   (still public — OAuth redirect intact)
GET    /observability/sessions               -> 401
GET    /health                               -> 200
GET    /runtime/flag                         -> 401   (authenticated, as intended)
```

---

## 6. Remaining risks

### Still P0

| id | Risk | Why it remains |
|---|---|---|
| **R1** | **Runtime A executes `schedule_meeting` — a real calendar event on the user's provider — with no approval.** `guardrails.decide()` returns `"approval"` and [backend/tools.py:956](../backend/tools.py#L956) checks only `"deny"` | Runtime A has no HITL mechanism. Enforcing the verdict there would *block* the tool, not gate it — a live behaviour change with no recovery path, and it would violate "no new capabilities on Runtime A". **The fix is the migration.** Interim lever: `AGANETI_DENIED_TOOLS=schedule_meeting` disables it outright |
| **R2** | **`GET /auth/{provider}/connect` is still public and accepts a caller-supplied `user_id`.** An attacker can start an OAuth flow that links *their* provider account into *another* user's record | Out of the stated P0-A scope. Fixing it requires the connect leg to authenticate, which needs a frontend change (the flow is a top-level browser redirect that carries no bearer). Recommend a signed, short-lived state token minted by an authenticated endpoint. **Should be promoted to P0** |
| **R3** | Tenant enforcement covers the **tool boundary and the three fixed endpoints**. It is not yet applied to the other ~30 routes that filter by `user_id` alone | Those routes are not currently known to be cross-user exposed (ownership is checked), but they have no tenant predicate. A systematic sweep is the completion of P0-A4 |

### P1

| id | Risk |
|---|---|
| **R4** | No LangGraph checkpointer; `agent_runs` still has no writer. An interrupted Runtime B turn is lost. Blocks migration stage 5 |
| **R5** | Six registered specialist agents still cannot execute — `delegate` routes to a hardcoded dict |
| **R6** | Runtime B has no knowledge-graph tool; `build_ranked_context` is still Runtime-A-only. Parity blocker P2 |
| **R7** | Two committed test files crash pytest collection. Any CI gate on this suite is currently unreliable |
| **R8** | DENY has four sources but none is a **per-agent explicit deny**. Needed before OPA is meaningful |
| **R9** | `authorize()` has no resource-level predicate — `query_data` is granted or not; "only these tables" is inexpressible |
| **R10** | `tenant.resolve_tenant_id` caches for 300 s. A tenant move takes up to 5 minutes to propagate to the dashboard lanes (the boundary still re-decides every call; only the tenant value is stale) |
| **R11** | Customer logic still compiled into core (`_DOMAIN` Arabic charity nouns, `backend/dashboard/` bound to one Azure SQL schema, `DEFAULT_ORG_SLUG = "meerana"`) |

---

## 7. Rollback

| Change | Rollback |
|---|---|
| Runtime migration | unset `RUNTIME_B_*`, or add the user to `RUNTIME_B_DENY_USERS`. No deploy |
| Strict tenancy | `AUTHZ_STRICT_TENANT=false` — reinstates pre-P0 tool-call behaviour. **Emergency only** |
| Kill switch | unset `AGANETI_DENIED_TOOLS` |
| Endpoint authorization (S1/S2/S3) | **no runtime toggle, by design.** These are the security fixes; reverting them requires a code revert |

---

## 8. Recommended next steps

1. **Verify the Vite frontend sends a bearer to `/auth/provider/status`** before deploying —
   this is the one client-visible breaking change.
2. Promote **R2** (OAuth connect account-linking) to P0 and fix it.
3. Fix **R7** so the suite can gate CI, then wire the 85 new tests into a required check.
4. Begin the migration at stage 1 with one internal user, per
   [runtime-migration.md §3](runtime-migration.md).
5. Then P1-3 (checkpointer) and P1-1 (agent-registry routing), which are the two items
   blocking a full cutover.
