# OPA Audit — and the Authorization Path That Actually Runs

**Audit date:** 2026-08-13 · **Companion to:** [architecture-audit.md](architecture-audit.md)

**Question asked:** does Open Policy Agent participate in any runtime decision?
**Answer: no. OPA does not exist in this system in any form.**

---

## 1. OPA presence — exhaustive negative evidence

| Capability | Class |
|---|---|
| 15 · OPA installation / configuration | `MISSING` |
| 16 · OPA policy definitions | `MISSING` |
| 17 · OPA enforcement points | `MISSING` |

Every probe run against the working tree and the running host:

```bash
# 1. Policy files
find . -name "*.rego" -not -path "./.venv/*" -not -path "./.git/*"
→ (no output)

# 2. Application code
grep -rn "OPA" --include="*.py" .
→ config/settings.py:136:  "# iframe with an OPAQUE origin and no base URL, so any asset they load (the"
   (the substring "OPA" inside the word OPAQUE — the ONLY hit in the entire Python codebase)

# 3. Dependencies
grep -inE "opa|open-policy|opa-client|opa-python" requirements.txt requirements-dev.txt
→ (no output)

# 4. Containers
grep -niE "opa" docker-compose.yml
→ (no output)
   services declared: qdrant, whisper_stt, piper_tts, neo4j

docker ps -a --format "{{.Names}} {{.Image}}" | grep -i opa
→ (no output)
   21 containers running/stopped: vllm-vl, vllm-fast, litellm, nazo-api, seaweed-{master,
   volume,filer,s3}, searxng, neo4j, nginx, redis, postgres, qdrant_memory, gotenberg …

# 5. Sidecars / binaries / bundles
which opa                                  → not found
find . -name "*.tar.gz" -path "*bundle*"   → (no output)
grep -rn "/v1/data/" --include="*.py" .    → (no output)   # the OPA decision API path
```

Where "OPA" *does* appear: `docs/architecture/poc_backlog.json` and
`docs/architecture/target_architecture_v1_gap_matrix.json` — i.e. **as a planned backlog item**,
which is precisely the kind of evidence this audit was instructed not to accept as implementation.

**There is no OPA server, no sidecar, no embedded evaluator, no Rego, no bundle, no decision log,
no client library, and no call site.** OPA participates in zero runtime decisions.

---

## 2. Nor any substitute policy engine

| Engine | Present |
|---|---|
| Open Policy Agent / Rego | No |
| AWS Cedar | No |
| Casbin | No |
| Oso / oso-cloud | No |
| py-abac / vakt | No |
| Keycloak / Zanzibar-style relationship authz | No |
| SQL row-level security (Postgres RLS) | No — `\d` shows no policies |

Authorization is **entirely hand-written Python**, distributed across five layers.

---

## 3. The authorization path that actually runs

Traced for `POST /agent/chat` and `POST /agent/approvals/{id}/approve`.

```
HTTP request
   │
   ├─[1] AuthEnforceMiddleware        backend/auth/enforce.py:93     AUTHENTICATION + IDOR
   ├─[2] RateLimitMiddleware          backend/main.py:886            ABUSE CONTROL
   ├─[3] _TraceASGIMiddleware         backend/main.py:906            (observability, not authz)
   │
   ├─► route handler
   │      └─[4] _uid(request)         routes/agent_os.py:40          IDENTITY (fail-closed)
   │      └─[5] _load_primary         routes/agent_os.py:50          AGENT TOOL ALLOWLIST
   │      └─[6] _get_owned / _resume  routes/agent_os.py:502,383     RESOURCE OWNERSHIP
   │
   └─► LangGraph executor
          └─[7] name in allowed_tools graph.py:84                    TOOL AUTHORIZATION
          └─[8] tool.is_outbound      graph.py:86                    HUMAN APPROVAL GATE
                 │
                 └─► handler
                        └─[9] validate_select_only / validate_no_pii coreshare_db.py:95,124
                        └─[9] search_corporate ACL scoping            registry.py:144
```

### Layer 1 — Authentication + identity normalization

[backend/auth/enforce.py:93](../backend/auth/enforce.py#L93) `AuthEnforceMiddleware.__call__`

Three accepted paths, in order:

1. **Dev bypass** (L112) — requires `DEV_AUTH_BYPASS` truthy **and** loopback client **and**
   `DEV_AUTH_USER` set. Triple-gated, warned at startup, deliberately absent from
   `.env`/`.env.example`.
2. **Internal service token** (L120) — `X-Internal-Token` compared with `hmac.compare_digest`;
   `X-Internal-User` is **required** (a previous hardcoded `"user_1"` fallback was removed and
   now returns 400).
3. **Supabase ES256 JWT** (L134) — `verify_supabase_jwt` via JWKS, then
   `identity.resolve_or_provision(sub, email, name)`. `NotAuthorized` → 403; provisioning failure
   → 503.

Anything else → **401**. Verified live: `POST /agent/chat`, `GET /agent/tools`, `GET /guardrails`,
`GET /observability/sessions` all return 401 unauthenticated.

`_forward` (L170) then normalizes identity so no handler can be tricked (IDOR defence):
- strips any client-supplied `x-auth-user` and injects the resolved one (L183) — non-forgeable;
- **unconditionally sets** `user_id` in the query string (L199) — deliberately not "only if
  present", because dozens of handlers declare `user_id: str = "user_1"` defaults that an omitted
  parameter would otherwise reach;
- rewrites any path segment equal to the caller's sub (L203);
- rewrites `user_id` in JSON bodies (L228), keeping `content-length` consistent, with a
  `_receive` shim that delegates to the real `receive()` after first read so SSE disconnect
  detection still works (L241).

### Layer 2 — Rate limiting
`RateLimitMiddleware` ([backend/main.py:886](../backend/main.py#L886), `backend/ratelimit.py`).

### Layer 4 — Fail-closed identity read
```python
# backend/routes/agent_os.py:40
def _uid(request: Request) -> str:
    u = request.headers.get("x-auth-user")
    if not u:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return u
```
Handlers read identity **only** from the middleware-injected header — never from body, query, or
`X-Internal-User`.

### Layer 5 — Per-agent tool allowlist
`_load_primary` ([routes/agent_os.py:50](../backend/routes/agent_os.py#L50)) resolves the user, loads
their primary `Agent`, reads `repo.allowed_tools(s, ag.id)` and filters against
`registry.all_names()`. An **empty** list is honoured verbatim as a deliberate lockdown (L66–69) —
defaults are re-injected only when no agent row exists at all.

### Layer 6 — Resource ownership
- `_get_owned` ([L502](../backend/routes/agent_os.py#L502)) — `ag.user_id == user.id` on every agent
  CRUD route.
- `_resume` ([L383](../backend/routes/agent_os.py#L383)) — resolves **both** the caller and the
  approval record's `user_id` to internal `User` rows and compares ids; a mismatch returns
  **404, not 403**, so a non-owner cannot even confirm the approval exists.
- Chat history/session routes are keyed by the caller's own uid.

### Layer 7 — Tool authorization
```python
# backend/orchestrator/graph.py:84
elif name not in state["allowed_tools"]:
    content = f"error: this agent is not permitted to use '{name}'"
```
Name-based. `Tool.required_permission` is **not** consulted — see
[tool-inventory.md §4](tool-inventory.md).

### Layer 8 — Human approval gate
```python
# backend/orchestrator/graph.py:86
elif tool.is_outbound:
    prev = registry.preview(name, args)
    pending.append({...})
    content = f"[AWAITING USER APPROVAL] {prev}"    # handler is NOT called
```
`_route_tools` → `END`; state persisted by `store.create_approval`. Decision path performs a
compare-and-swap (`UPDATE ... WHERE status='pending'`, `rowcount > 0`) **before** execution
([store.py:191](../backend/orchestrator/store.py#L191)) plus an atomic audit event recording
`decided_by`, guaranteeing the outbound action runs exactly once under a double-click or a
concurrent approve+reject race.

Gated tools: `send_email`, `create_calendar_event`, `web_search` — and nothing else.

### Layer 9 — Data-layer controls
- **SQL:** `validate_select_only` + `validate_no_pii`
  ([coreshare_db.py:95,124](../backend/dashboard/coreshare_db.py#L95)) — SELECT-only, keyword
  blocklist, no bare `SELECT *`, no multi-statement, and a 10-column beneficiary-PII blocklist.
  PII columns are additionally hidden from `get_database_schema` so the model never learns they
  exist.
- **RAG:** `search_corporate(query, 6, ctx["user_id"])` scopes retrieval to the caller's own
  documents plus the shared org corpus.
- **Object storage:** ACL-safe keys, path-traversal fix (`backend/storage/`).

---

## 4. `guardrails.py` — `CONFIGURED_BUT_NOT_ENFORCED`

[backend/guardrails.py](../backend/guardrails.py) is the closest thing to a policy engine in the
repository. It is a real, coherent policy specification: a 40-entry `ACTION_CATEGORY` map, a
`decide()` function returning `"auto" | "approval" | "deny"`, an `AUTONOMY_LEVEL` posture var, and
`policy_snapshot()` exposed at `GET /guardrails`.

```python
def decide(action_type: str) -> str:
    cat = ACTION_CATEGORY.get(action_type)
    if cat is None:   return "deny"                                       # unknown/hallucinated
    if cat == "read": return "auto"
    if cat == "task": return "auto" if AUTONOMY_LEVEL in ("standard","autonomous") else "approval"
    if cat == "comms":return "auto" if AUTONOMY_LEVEL == "autonomous"     else "approval"
    return "approval"
```

**It has exactly one enforcement site, and that site checks only one of the three verdicts:**

```python
# backend/tools.py:956
from backend.guardrails import decide
if decide(name) == "deny":
    return f"⚠️ I'm not able to perform that action ('{name}')."
```

`grep -rn "guardrails"` across the repo returns: this line, the `/guardrails` snapshot endpoint
([main.py:3646](../backend/main.py#L3646)), and three test files. Nothing else.

**Therefore:**

| Verdict | Intended | Actual |
|---|---|---|
| `"deny"` | refuse | ✅ refused |
| `"approval"` | queue for human sign-off | ❌ **falls through and executes** |
| `"auto"` | execute | ✅ executed |

Under the default `AUTONOMY_LEVEL="standard"`, the `comms` actions `draft_email` and
`schedule_meeting` produce verdict `"approval"` — and run anyway. `AUTONOMY_LEVEL` has **no
behavioural effect whatsoever**; changing it changes only what `GET /guardrails` reports.

Because Runtime A (`POST /chat`) is the runtime carrying live traffic (see
[architecture-audit.md §1.1](architecture-audit.md)), **the approval gate does not cover
production**.

---

## 5. Divergent authorization for the same tool name

The two runtimes disagree on security-relevant behaviour for shared tool names. This is the
clearest demonstration that "which code runs" is currently a security question:

| Tool | Runtime B (LangGraph) | Runtime A (live) |
|---|---|---|
| `web_search` | `is_outbound=True` → **hard approval pause** (skills.py:409) | `ACTION_CATEGORY["web_search"]="read"` → **auto-executes** (guardrails.py:39) |
| `draft_email` | non-outbound; formats a draft | category `comms` → verdict `"approval"` → **not enforced** → executes |
| `schedule_meeting` | *(not in registry; `create_calendar_event` is, and is gated)* | category `comms` → verdict `"approval"` → **not enforced** → executes |

`web_search` is the sharpest case: the whole justification for gating it is that the query
**egresses** the on-prem boundary. On the runtime that actually serves users, it does not gate.

---

## 6. Confirmed authorization defects

Verified against the running instance on `127.0.0.1:8001`.

### S1 — Unauthenticated destruction of any user's OAuth credentials · **CRITICAL**

```
DELETE /auth/provider/{provider}?user_id=<victim>
```

- `_PUBLIC_PREFIXES` includes `"/auth/provider"`
  ([enforce.py:67](../backend/auth/enforce.py#L67)).
- The public-prefix branch returns **before** authentication *and before* `_forward`'s `user_id`
  rewrite:
  ```python
  # enforce.py:104
  if method == "OPTIONS" or any(path == p or path.startswith(p + "/") or path.startswith(p)
                                for p in _PUBLIC_PREFIXES):
      return await self.app(scope, receive, send)      # ← no auth, no normalization
  ```
- The handler reads the raw parameter:
  ```python
  # routes/provider_auth.py:338
  async def disconnect_provider(provider: str, user_id: str = Query(...)):
      for uid in await _identity_ids(user_id):
          sb.table("provider_connections").delete().eq("user_id", uid)...
          _file.delete_by_user_provider(uid, provider)
  ```

Any anonymous caller on the network can delete any user's stored Google/Microsoft credentials from
**both** stores. Denial of service against every mail/calendar/contacts capability, no
authentication required.

**Fix:** remove `/auth/provider` from `_PUBLIC_PREFIXES`; derive `user_id` from `X-Auth-User`.
`/auth/{provider}/connect` and `/auth/{provider}/callback` must stay public (browser redirects
carry no bearer) — but `/auth/provider/*` need not.

### S2 — Unauthenticated provider-status disclosure · **HIGH**

```bash
curl -s "http://127.0.0.1:8001/auth/provider/status?user_id=test"
→ 200 {"google":{"connected":false,"configured":true},
       "microsoft":{"connected":false,"configured":true}}
```
Same public-prefix root cause ([provider_auth.py:303](../backend/routes/provider_auth.py#L303)).
For a real `user_id` the response additionally returns `email`, `scopes` and `connected_at`.

### S3 — Cross-user chat exposure · **CRITICAL**

`GET /observability/sessions` requires authentication (verified: 401 without a token) but performs
**no ownership filtering**:

```python
# backend/routes/observability.py:794
async def sessions(limit: int = Query(25, ge=1, le=200)) -> dict:
    "SELECT session_id, user_id, max(created_at), count(*) "
    "FROM chat_messages GROUP BY session_id, user_id ORDER BY last_at DESC LIMIT :lim"
```

It returns **every user's** `session_id` and `user_id`. `GET /observability/trace/{session_id}`
then returns that session's message content:

```python
# backend/routes/observability.py:831
"SELECT role, left(content, 400) AS content, created_at FROM chat_messages "
"WHERE session_id = :s ORDER BY created_at LIMIT 50"
```

No ownership check. Any authenticated user reads any other user's conversations — horizontal
privilege escalation. The whole `/observability` router (14 endpoints) has no role concept.

### S4 — No tenant boundary · **HIGH (architectural)**

`org_id` is a column on ~25 Postgres tables, faithfully **written** and **never read as a filter**:

```bash
grep -rn "org_id ==" --include="*.py" backend/   → (no output)
```

The enforced isolation boundary is `user_id` everywhere. `tenant_id` does not appear in
application code. Multi-tenancy is not partially implemented — it is absent.

### S5 — Dead permission model · **MEDIUM**

`Tool.required_permission` is declared for 33 tools, surfaced in the `GET /agent/tools` API, and
never evaluated. It advertises a control that does not exist, and it is the reason there is
currently **nowhere to attach a policy engine**: authorization decisions are made against tool
names, not against a subject/action/resource/condition tuple that Rego (or Cedar, or Casbin) could
express.

---

## 7. If OPA is to be introduced

OPA is a **P3** item, not a P0 — and deliberately so. Installing OPA today would give a policy
engine nothing meaningful to decide, because there is no policy model to express: no tenant, no
permission semantics, no resource identifiers on tool calls.

The prerequisite ordering is:

1. **P0-6 — `TenantContext`.** A subject needs an org/tenant before any rule can reference one.
2. **P0-7 — a real permission model.** Replace name-matching with
   `(subject, action, resource, condition)`. This is the input document OPA would receive.
3. **P0-4/P0-5 — one runtime, one gate.** Two enforcement paths mean two policy integrations and a
   guaranteed drift; the `web_search` divergence is the proof.
4. **P2-1 — a tool execution gateway.** This is the natural single PEP (policy enforcement point);
   without it, enforcement must be sprinkled through `_tools_node`, the approval route, the SQL
   layer and every connector.

Then OPA becomes tractable, with three enforcement points:

| PEP | Location | Decision |
|---|---|---|
| Tool invocation | the gateway (P2-1), replacing `graph.py:84` | may agent A, for tenant T, invoke tool X with arguments Y? |
| Approval routing | `graph.py:86`, replacing the boolean `is_outbound` | does this action require approval, and from whom? |
| Data access | `coreshare_db.run_query`, `search_corporate` | which tables/columns/rows may this subject read? |

Until then, the honest statement of the current posture is: **authorization in this system is
hand-written Python, enforced at five layers, with a hard human-approval gate that covers three
tools on the runtime that is not currently serving users.**
