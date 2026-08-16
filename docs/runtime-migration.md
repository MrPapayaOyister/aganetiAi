# Runtime Migration — Runtime A → Runtime B

**Status:** switch implemented, **no traffic moved** · **Date:** 2026-08-14
**Decision (P0-B):** `backend/orchestrator` (LangGraph) is the target production runtime.
Runtime A is **not** being deleted and receives **no new capabilities**.

---

## 1. The two runtimes

| | Runtime A (current) | Runtime B (target) |
|---|---|---|
| Entry | `POST /chat` — [backend/main.py:2346](../backend/main.py#L2346) | `POST /agent/chat` — [backend/routes/agent_os.py](../backend/routes/agent_os.py) |
| Loop | hand-rolled, `MAX_TOOL_ROUNDS = 4` | LangGraph `StateGraph`, `STEP_BUDGET = 8` |
| Tools | 34 (`backend/tools.py`) | 37 (`backend/orchestrator/registry.py`) |
| Authorization | `guardrails.decide()` — deny only | `authz.authorize()` — full boundary |
| Approval gate | **none enforced** | hard pause/resume, durable |
| Tenant | none | enforced, fail-closed |
| Context engine | Qdrant + **Neo4j** + calendar + tasks | Qdrant only (no graph tool) |
| Clients | Vite frontend, Telegram bot | external Next.js dashboard |

Runtime B is the target because it is the only one with an enforced approval gate, an
authorization boundary, and a tenant. Runtime A is the only one with the knowledge-graph
context path and 24 consumer-media tools — which is why this is a migration and not a
switch-off. See §7.

---

## 2. How the switch works

One module: [backend/runtime_flag.py](../backend/runtime_flag.py). One bridge:
`main._serve_via_runtime_b`, called as the **first statement** of `chat_endpoint`.

**Defaults are zero-impact.** With no `RUNTIME_B_*` variable set, `choose()` returns
Runtime A for everyone and the bridge is a no-op. An unset environment is byte-identical to
pre-P0 behaviour. Asserted by `test_default_is_runtime_a_for_everyone`.

### Selection order — first match wins

| # | Variable | Reason emitted | Use |
|---|---|---|---|
| 1 | `RUNTIME_B_DENY_USERS` | `deny_user` | **opt-OUT, beats everything below.** The incident lever |
| 2 | `RUNTIME_B_SESSIONS` | `pinned_session` | one conversation — the narrowest blast radius |
| 3 | `RUNTIME_B_USERS` | `pinned_user` | named users (supabase uid **or** email) |
| 4 | `RUNTIME_B_PERCENT` | `percent:N` | deterministic hash bucket, 0–100 |
| 5 | `RUNTIME_B_ENABLED` | `global` | everyone |
| — | *(none)* | `default` | Runtime A |

The percentage bucket is `blake2b(user_id) % 100`, **not** Python's `hash()`, which is
salted per process — the same user would land in a different bucket after every restart and
a canary would reshuffle on every deploy. Asserted by
`test_percentage_bucket_survives_a_process_restart`.

A caller with no identity is **never** in a partial rollout: no identity means no stable
bucket, so they stay on Runtime A rather than landing somewhere arbitrary.

A malformed `RUNTIME_B_PERCENT` (`"abc"`, `"-5"`, `"1e3"`) fails closed to 0.

### What the bridge does

- **Same response shape.** `{"reply": str}` for non-streaming; the caller cannot tell.
- **`x-runtime: B`** and **`x-runtime-reason`** response headers, plus a
  `kind='runtime_selected'` event — this is what makes parity comparison possible.
- **No fallback to Runtime A on failure.** A silent fallback would hide every parity defect
  behind a retry on the old engine, and the operator would conclude Runtime B was fine.
  Failures surface; you roll the cohort back. Asserted by
  `test_bridge_does_not_fall_back_to_runtime_a_on_failure`.
- **Placed before `_persist_turn`**, so a Runtime B turn does not write half its bookkeeping
  through Runtime A's stores. Asserted by
  `test_chat_endpoint_consults_the_flag_before_anything_else`.
- An approval pause is surfaced as `{"reply": "I need your approval first: …",
  "approval": {...}}` and the action is **not** executed.

`GET /runtime/flag` (authenticated) returns the live configuration, so an operator can
confirm what is in effect rather than what they believe they deployed.

---

## 3. Route ONE test user through Runtime B

This is the procedure the brief asks for. Production traffic is unaffected throughout.

### Step 0 — confirm the starting state

```bash
curl -s -H "Authorization: Bearer $ADMIN_JWT" http://127.0.0.1:8000/runtime/flag | jq
# {"enabled": false, "percent": 0, "pinned_users": [], "pinned_sessions": [],
#  "denied_users": [], "default_runtime": "A", "target_runtime": "B"}
```

### Step 1 — get the test user's identity

The value to pin is what `/chat` receives as `user_id`, i.e. the Supabase `sub` the auth
middleware injects.

```bash
docker exec postgres psql -U postgres -d aganeti -t -A -c \
  "SELECT supabase_uid, email, org_id FROM users WHERE email = 'tester@meerana.ae';"
```

A user with a **NULL `org_id` cannot be a canary** — the authorization boundary will deny
every tool call under `AUTHZ_STRICT_TENANT`. Verify `org_id` is non-null before proceeding.

### Step 2 — confirm the user has a DB primary agent

Runtime B loads tools from `agent_permissions`. A user with no agent row falls back to the
in-code template **with no tenant**, and every tool call is denied.

```bash
docker exec postgres psql -U postgres -d aganeti -c \
  "SELECT a.id, a.kind, a.status, count(p.id) AS perms
     FROM agents a LEFT JOIN agent_permissions p ON p.agent_id = a.id
     JOIN users u ON u.id = a.user_id
    WHERE u.email = 'tester@meerana.ae' AND a.kind = 'primary'
    GROUP BY a.id, a.kind, a.status;"
```

Expect one `primary` row with `status='active'` and a non-zero permission count.

### Step 3 — pin the user

Prefer **`RUNTIME_B_SESSIONS`** for the very first trial (one conversation), then widen to
`RUNTIME_B_USERS`.

```bash
# systemd
sudo systemctl edit aganeti-backend      # add under [Service]:
#   Environment="RUNTIME_B_USERS=<supabase-uid>"
sudo systemctl restart aganeti-backend

# or, for a shell-launched dev instance
export RUNTIME_B_USERS='<supabase-uid>'
```

Leave `RUNTIME_B_ENABLED` unset and `RUNTIME_B_PERCENT` at 0. Only the pinned identity moves.

### Step 4 — verify the pin took, and that nobody else moved

```bash
curl -s -H "Authorization: Bearer $ADMIN_JWT" http://127.0.0.1:8000/runtime/flag | jq .pinned_users
# ["<supabase-uid>"]

# The canary — expect x-runtime: B
curl -si -X POST http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer $CANARY_JWT" -H 'Content-Type: application/json' \
  -d '{"message":"what are my tasks?","session_id":"canary-1"}' | grep -i '^x-runtime'
# x-runtime: B
# x-runtime-reason: pinned_user

# Any other user — expect NO x-runtime header at all (Runtime A does not set one)
curl -si -X POST http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer $OTHER_JWT" -H 'Content-Type: application/json' \
  -d '{"message":"hello","session_id":"other-1"}' | grep -ci '^x-runtime'
# 0
```

### Step 5 — watch the canary

```sql
-- which runtime served whom
SELECT ts, user_id, meta->>'reason' AS reason
  FROM events WHERE kind = 'runtime_selected' ORDER BY ts DESC LIMIT 20;

-- Runtime B tool traffic (only orchestrator/router.py emits llm_call)
SELECT ts, name, meta->>'agent_id' AS agent, duration_ms
  FROM events WHERE kind = 'llm_call' ORDER BY ts DESC LIMIT 20;

-- authorization refusals (new in P0)
SELECT ts, name, meta->'request'->>'tenant_id' AS tenant, meta->>'rule' AS rule
  FROM events WHERE kind = 'authz_decision' ORDER BY ts DESC LIMIT 20;
```

A burst of `authz_decision` rows with `rule = 'no_tenant'` means Step 1/2 was not satisfied —
roll back and fix the user's org/agent rather than relaxing `AUTHZ_STRICT_TENANT`.

### Step 6 — roll back

```bash
sudo systemctl edit aganeti-backend      # remove RUNTIME_B_USERS
sudo systemctl restart aganeti-backend
```

Or, without waiting for an edit window:

```bash
# add the user to the deny list — it beats every opt-in
Environment="RUNTIME_B_DENY_USERS=<supabase-uid>"
```

No code change, no deploy, no state to unwind. Asserted by `test_rollback_is_one_variable`
and `test_deny_list_beats_every_opt_in`.

---

## 4. Widening the rollout

Do **not** skip stages. Each one must clear the parity checklist in §5 first.

| Stage | Setting | Cohort |
|---|---|---|
| 0 | *(none)* | nobody — current state |
| 1 | `RUNTIME_B_SESSIONS=<one session>` | one conversation |
| 2 | `RUNTIME_B_USERS=<uid>` | one internal user |
| 3 | `RUNTIME_B_USERS=<uid>,<uid>,<uid>` | the internal team |
| 4 | `RUNTIME_B_PERCENT=5` → `25` → `50` | progressive |
| 5 | `RUNTIME_B_ENABLED=true` | everyone |
| 6 | — | retire Runtime A (a **separate** decision, not P0) |

At every stage `RUNTIME_B_DENY_USERS` remains available to exempt an individual.

---

## 5. Parity checklist — must clear before widening

These are **known, measured differences**, not speculation. Each must be resolved or
consciously accepted.

| # | Difference | Severity | Status |
|---|---|---|---|
| P1 | **Tool catalogue.** Runtime A has 24 tools with no Runtime B counterpart — YouTube, live TV, radio, QR, weather, news. They are **3,400+ of its 4,980 recorded calls**. A user pinned to Runtime B loses them | **blocking** for general users | open |
| P2 | **Knowledge graph.** Runtime A calls `build_ranked_context` (Qdrant + **Neo4j** + calendar + tasks, fused and ranked). Runtime B does not — there is no graph tool. Answers that depend on the graph will differ | **blocking** | open — P1-7 in the audit |
| P3 | **SSE frame vocabulary.** Runtime A emits `thinking/token/action/error/done`; Runtime B emits `backend/chat/frames.py`. A streaming client will not understand the other's frames | **blocking for `stream: true`** | open — enable streaming cohorts only after the client handles both |
| P4 | **Approval pause.** Runtime B pauses outbound actions; Runtime A executes them. This is the *point* of migrating, but it is a visible UX change: `schedule_meeting` currently creates a real calendar event with no sign-off | intended | verify the client renders the `approval` field |
| P5 | **Passive task detection** and the task-confirmation state machine exist only on Runtime A | medium | open |
| P6 | **`web_search`** is approval-gated on B, auto on A | intended (B is correct) | — |
| P7 | **Telegram** posts to `/chat` and will follow the flag. Pin Telegram users explicitly or leave them on A | medium | open |

**Recommended first canary: an internal user who only exercises mail, calendar, tasks and
documents** — the overlap where both runtimes have real coverage.

---

## 6. What was deliberately not done

- Runtime A was **not** deleted, and no capability was added to it.
- The approval gate was **not** retrofitted onto Runtime A. It has no HITL mechanism, so
  "enforcing" the `approval` verdict there would mean *blocking* `draft_email` and
  `schedule_meeting`, not gating them. That is a behaviour change to live traffic with no
  user-visible recovery path. `guardrails.decide()` is therefore unchanged for Runtime A,
  and the real fix is this migration. **Tracked as remaining risk R1 in
  [p0-remediation.md](p0-remediation.md).**
- No LangGraph checkpointer (P1-3), no agent-registry routing (P1-1), no MCP, no browser
  automation.

---

## 7. Definition of done for the migration

Runtime A may be retired when all of the following hold:

1. Parity items P1–P3 and P5 are closed or explicitly accepted.
2. `RUNTIME_B_ENABLED=true` has run for two weeks with no `runtime_selected` rollback.
3. `kind='tool_called'` events from `backend/tools.py` have gone to zero.
4. Telegram and the Vite frontend are validated against Runtime B's frames.
5. A LangGraph checkpointer exists (P1-3) so an interrupted turn is recoverable — Runtime A
   loses nothing on restart today only because it holds nothing.

Until then, both runtimes stay, and the flag is the only thing that decides.
