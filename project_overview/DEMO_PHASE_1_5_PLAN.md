# Dar Al Ber Analytics — Client Demo + Phase 1.5 Architecture Plan

_Production-grade plan for the aganetiAi ("Aria") + Dar Al Ber dashboard integration. **See Revision 1 below for the schema-grounding audit** — which claims are verified against the live system vs. design assumptions vs. new/to-be-built. The original intro's blanket "everything verified" claim was an overstatement, corrected there._

---

# Revision 1 — Single-user correction + schema-grounding audit (2026-07-19)

Two corrections applied after review. **This revision overrides the original text wherever they conflict.**

## R1.1 — This is a SINGLE-USER, single-org instance (no multi-tenancy, no RBAC)

Verified against the **live** Postgres (`docker exec postgres psql`): `organizations` = **1 row**, `users` = **1 row, `role = admin`**, `departments`/`designations` = **0 rows**. The org/RBAC scaffolding physically exists in the schema (migrations applied — `alembic_version` = 1), but it is operationally trivial. All org-scoping and role-gating is therefore **removed** from the design:

**Revised `model_configs` table (replaces the Section 1 definition):**

```sql
CREATE TABLE model_configs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_key     text UNIQUE NOT NULL,                 -- GLOBAL unique; no org scoping
  name          text NOT NULL,
  type          text NOT NULL CHECK (type IN ('local','openai','anthropic')),
  base_url      text,                                 -- local/openai wire base_url
  model         text NOT NULL,                        -- wire model id (qwen2.5-32b, gpt-4o, claude-sonnet-4-5)
  api_key_enc   text,                                 -- token_crypto (Fernet) ciphertext, 'enc::' prefix; NULL for local
  caps          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {tool_call, vision, ctx}
  tier          text NOT NULL DEFAULT 'interactive',
  cost_in       integer NOT NULL DEFAULT 0,           -- micro-USD / 1k tokens; POST rejects 0/0 for non-local
  cost_out      integer NOT NULL DEFAULT 0,
  system_prompt text,
  status        text NOT NULL DEFAULT 'active',       -- active | disabled
  created_by    uuid,                                 -- AUDIT ONLY (the single user); NO permission logic, no FK gate
  created_at    timestamptz NOT NULL DEFAULT now()
);
```
Changes vs. original: **`org_id` dropped entirely**; **no `org_id`/`model_key` composite uniqueness** — `model_key` is globally UNIQUE; **`created_by` is audit-only** (nullable, carries no permission meaning).

**Revised `/models` route auth (replaces the Section 1 + Cross-cutting §1 admin gate):**
- **No role check.** Any request that passes `AuthEnforceMiddleware` (i.e. carries the trusted, server-injected `x-auth-user`) may `POST`/`PATCH`/`DELETE /models`. There is no second class of user to gate against.
- **Kept — not RBAC, just hygiene:** `AuthEnforceMiddleware` still strips any client-supplied `x-auth-user` and injects the trusted one (this is header-trust, independent of user count). `api_key` remains **write-only across the API boundary** — `GET`/`PATCH` responses redact `api_key_enc` to `null`/`"***set***"`; the frontend never reads a key back. That's secret hygiene, not access control, so it stays.
- `created_by` is set from the injected `x-auth-user` for the log trail only.

**ASSUMPTION lines removed (were tied to admin/org, now void):**
- ~~"ASSUMPTION: 'add a model' is an admin action (`users.role == 'admin'`)"~~ → single operator; any authenticated caller manages models.
- ~~"ASSUMPTION: model_configs is org-scoped / uniqueness per org"~~ → global `model_key UNIQUE`.
- Cross-cutting §1 "Admin-only mutation, enforced server-side … resolve `users.role` … require `admin`" → **deleted**; keep only the encryption + write-only-key points.
- Cross-cutting §2 "per-user/per-org daily cost cap" → a **single global** `AGANETI_DAILY_COST_CAP`; `events.cost_since()` runs with no org/user filter (there is one user).
- Section 5 context injection "user role/permissions (`users.role`, `agent_permissions`)" → **dropped**; single admin, nothing to gate. Inject date + metric whitelist + cached schema only.
- Section 2 audit trail "org scoping" → `events.org_id` may be left `NULL` (it is nullable); no org logic.

**Everything else in Section 1 stands** (Anthropic adapter branch in `router.complete()`, DB-backed `MODELS` merge with static-dict fallback, per-run `model_key` pin resolved once at entry, `plan()` fallback chain, short-TTL cache convergence across workers).

## R1.2 — Two more unverified-as-fact claims found (same pattern as `organizations`)

Re-verified against the live host; both force a small plan change:

1. **`agent_runs` is empty (0 rows) — the table exists but is never written.** The original plan threaded `run_id` "via `agent_runs`" as if it were a live audit surface. **Correction:** mint a fresh `run_id` UUID in `ask_stream` (Step 11), carry it in `ctx`, and log it to `events.meta.run_id` (the `events` spine IS live — 1059 rows). Do **not** depend on an `agent_runs` row existing. (Optionally start writing `agent_runs` rows, but that is extra scope, not required for the audit trail.)
2. **`fast-7b` (:9002) and `vision-vl` (:9001) are not running.** `ss -ltnp` shows only `:9000` (tool-32b / `vllm-text`), `:9003` (nazo, separate project), `:4000` (litellm) listening. So `DEFAULT_CHAIN = ["tool-32b", "fast-7b"]` has a **dead fallback tail**, and vision turns would fail. **Correction for the demo:** either boot `fast-7b`, or set the local fallback tail to `["tool-32b"]` only, or register a second *live* local model. Analytics never needs vision, so `vision-vl` being down is irrelevant to this feature — but note it.

## R1.3 — Schema-grounding audit table

Legend: **✓ Verified** (how stated) · **⚙ Assumption** (design proposal, correctly tagged) · **✗→ Corrected** (was stated as fact, now re-tagged) · **◆ New/to-build** (does not exist yet).

| Claim | Cat. | How verified / why it's an assumption |
|---|---|---|
| `STEP_BUDGET=8`, `MAX_TOOL_OUTPUT=6000`, `AgentState` fields, outbound HARD GATE, `run_turn`/`resume`/`astream_turn` | ✓ | Direct read of `backend/orchestrator/graph.py` |
| `router.py` `MODELS` keys, `plan()`, `complete()`, `_log()`→`events.log_event("llm_call")`, `DEFAULT_CHAIN`, `AsyncOpenAI` | ✓ | Direct read of `backend/orchestrator/router.py` |
| `tool-32b` (:9000, qwen2.5-32b) is running | ✓ | `docker ps` (container `vllm-text`) + `ss -ltnp` shows `:9000` listening |
| `fast-7b` (:9002) & `vision-vl` (:9001) are available fallbacks | ✗→ | `ss -ltnp` shows **only** `:9000`/`:9003`/`:4000` — they are configured in `router.py` but **not running** |
| `Tool` dataclass fields, `register`/`get`/`openai_schemas`/`preview`, `is_outbound` gate | ✓ | Direct read of `backend/orchestrator/registry.py` |
| `events.log_event` signature, `events` columns, dual-backend, `AGANETI_DATA_BACKEND=postgres` | ✓ | Direct read of `backend/events.py` + `db/models.py` + redacted `.env` |
| The `events` spine is actively used | ✓ | Live query: `events` = **1059 rows** |
| `coreshare_db` `run_query`/`validate_select_only`/`get_schema`, cold-start retry, SELECT-only regex | ✓ | Direct read of `backend/dashboard/coreshare_db.py` |
| Azure schema: `VRequests`/`VRequestAttributes`/`VRequestsApproved` cols, `SuggestedAssistanceAmount`, `State`, `SubmitDate`; **no** collection/donation table | ✓ | **Live** `coreshare_db.get_schema()` probe + per-view column probe |
| `dashboard_configs` table+funcs; `tools.py` 5 tools; `stream.py` SSE events; `routes/dashboard.py` endpoints; `_uid` reads `x-auth-user` | ✓ | Direct reads of the four `backend/dashboard/*` + `routes/dashboard.py` files |
| `AuthEnforceMiddleware` strips + injects trusted `x-auth-user` | ✓ | `grep` of `backend/auth/enforce.py` lines ~127–128 (exact strip/append) |
| Frontend: React 19 / Vite / **Tailwind v4, no shadcn** / framer-motion / lucide; SSE via `fetch`+`getReader`; `neu-*`/`t-*` classes, `#00D4FF`; board/reconcile | ✓ | Direct reads of `frontend/package.json`, `hooks/useStream.ts`, `pages/DashboardPage.tsx` |
| `token_crypto.py` is Fernet (AES-128-CBC + HMAC-SHA256), key from `TOKEN_ENC_KEY` | ✓ | Direct read of `backend/services/token_crypto.py` (was an inference in the first draft; now read). NB: it currently encrypts only OAuth `access_token`/`refresh_token` — API-key use is a new application of the same primitive |
| `RateLimitMiddleware` = per-IP sliding window, loopback-exempt, tighter LLM bucket | ✓ | Direct read of `backend/ratelimit.py` (`RATE_LIMIT_CHAT_MAX=20`/60s). NB: keyed by **IP**, not user — orthogonal to the cost cap |
| `organizations` table + `users.role` exist | ✓ EXISTS | Live query: `organizations`=1, `users`=1 (`role=admin`) — **but** their multi-tenancy/RBAC *relevance* was ✗→ (unverified assumption; now dropped per R1.1) |
| `agents`/`agent_permissions`/`approvals` exist and are used | ✓ | Live counts: 12 / 42 / 4 |
| `agent_runs` is an active run/audit surface (plan threads `run_id` via it) | ✗→ | Live: `agent_runs` = **0 rows** — table exists but unused (see R1.2) |
| Alembic migrations applied / live schema matches `models.py` | ✓ | `alembic_version`=1 row + `\d users` matches the ORM |
| `metrics.py`, `run_metric`/`list_metrics`, `analytics_agent`, `/dashboard/ask`, Redis cache | ◆ | Phase-1 LOCKED **to-build** — confirmed absent; GATE 0 says verify-first. The Azure columns they will use are ✓ (live probe) |
| `model_configs` / `prompt_versions` tables | ◆ | `SELECT to_regclass(...)` → both **NULL** (do not exist yet) |
| `_dashboard_origins` value, `api/client.ts` baseURL, `lib/supabase.ts` `authHeader` internals | ⚙/✗ | Inferred from usage; file bodies not read — treat specifics as assumption until read |
| Redis running but unused by dashboard; litellm `:4000` running but unused by the router | ✓ | `docker ps` + code reads (`router.py` has no `:4000`; dashboard code has no Redis) |

---

## Contents
- [1. Model Selection (Frontend + Backend)](#1-model-selection)
- [2. Agentic Capability Upgrade](#2-agentic-upgrade)
- [3. Future Prediction (Phase 1.5)](#3-forecasting)
- [4. Dashboard Integration + UI/UX](#4-dashboard-ui)
- [5. Data Extraction + System Prompt Management](#5-prompt-and-extraction)
- [Cross-Cutting Concerns & Exact Implementation Order](#cross-cutting--exact-order)

---

<a id="1-model-selection"></a>

# 1. Model Selection (Frontend + Backend)

**Decision** — Build a first-class model registry so operators can register/select LLM backends at runtime across three provider wires (local vLLM OpenAI-compatible at `localhost:9000/v1`, OpenAI API, Anthropic API) without touching the Phase-1 LOCKED `analytics_agent`, its `run_metric`/`list_metrics` tools, or `backend/dashboard/metrics.py`. Concretely:

1. A **Settings panel (NEW React route/section)** to add/list/disable models. Fields: `name`, `type` (`local | openai | anthropic`), `endpoint` (base_url for local/openai) **OR** `api_key` (openai/anthropic), optional `system_prompt` override, plus `model` (wire id, e.g. `qwen2.5-32b`, `gpt-4o`, `claude-sonnet-4-5`), `caps` (`tool_call`/`vision`/`ctx`), `tier`, and `cost_in`/`cost_out`. Rendered in the confirmed `neu-*` / Tailwind-v4 system (no shadcn).
2. **Persistence in Postgres** — a NEW `model_configs` table (justified below). API keys stored **encrypted** via the existing `backend/services/token_crypto.py` (`TOKEN_ENC_KEY`, Fernet-style), never plaintext.
3. **Extend the existing `backend/orchestrator/router.py`** (`AsyncOpenAI`-based, no LiteLLM) with a NEW **Anthropic adapter branch inside `complete()`**. The hardcoded `MODELS` dict becomes a DB-backed merge of the static keys + `model_configs` rows. LiteLLM (`:4000`) stays running but unused, as ground truth states. (Full LiteLLM-vs-router justification in Architecture.)
4. **Selection resolution**: a per-run pin of `AgentState.model_key`, seeded from an optional per-request selector or a per-board/session default; `router.plan()` continues to own fallback ordering. The switch applies at the **next run boundary only** (see Identify).
5. **Fallback**: selected model fails → `plan()` chain → `DEFAULT_CHAIN` → the existing `events.log_event("llm_call", success=False)` path in `router._log()`, extended with `meta.selected_model_key`/`meta.fallback_from`/`meta.actual_model_key`.

ASSUMPTION: "add a model" is an **admin** action (`users.role == "admin"`), not per-employee — model backends are org infrastructure. UI and CRUD are gated accordingly.

ASSUMPTION: registering a model does **not** auto-add it to `DEFAULT_CHAIN`; it becomes reachable only when explicitly selected per-run or set as an agent's `agents.model_key`/`agents.fallback_models`. This prevents a bad new endpoint from poisoning every run.

ASSUMPTION: mid-turn *token-level* provider hot-swap is explicitly out of scope for the live demo (unsafe); switching is a next-turn-boundary operation only.

---

**Architecture** — Components, data flow, exact modules/tables.

*New table — Postgres, via `backend/db/models.py` (`Base` in `backend/db/base.py`) + Alembic migration under `migrations/`:*
`model_configs(id uuid pk, org_id uuid fk→organizations, model_key TEXT (UNIQUE per org), name TEXT, type TEXT CHECK(local|openai|anthropic), base_url TEXT NULL, model TEXT, api_key_enc TEXT NULL (token_crypto ciphertext), caps JSONB {tool_call,vision,ctx}, tier TEXT, cost_in INT, cost_out INT, system_prompt TEXT NULL, status TEXT (active|disabled), created_by uuid, created_at timestamptz)`. The column set mirrors the hardcoded `MODELS` value shape in `router.py` (`base_url, model, caps, tier, cost_in, cost_out`) so the merge is 1:1.

*Why Postgres over the `dashboard_configs.db` SQLite pattern:*
- The natural join partners are already in PG: `agents.model_key`, `agents.fallback_models` (JSONB), and the `events` audit spine (`cost_micros`, `meta`, keyed by `model_key` as `name` in `log_event("llm_call", name=model_key, …)`). The `events.py` aggregations (`per_agent`, `summary`, `tools`) run over PG when `AGANETI_DATA_BACKEND=postgres` (current setting); a separate SQLite file cannot be joined against `agents` or `events`.
- `model_configs` is **org-scoped, multi-user, RBAC-gated** — the opposite profile of `dashboard_configs.db`, which is deliberately self-contained, board-scoped, single-purpose, with no org/role dimension.
- Concurrency: multiple uvicorn workers + the LangGraph executor read this on a hot path (every `_agent_node`). SQLite's single-writer/file-lock model is a poor fit; PG pooling is already wired via `backend/db/sync.py session()` and async SQLAlchemy.
- Secrets: PG keeps `api_key_enc` inside the same encrypted-token posture as `token_crypto`/`TOKEN_ENC_KEY`. A new API-key-bearing SQLite file next to a module would weaken the `chmod 600 ~/aganetiAi/.env` secret story.

*LiteLLM vs the existing router — keep `router.py`, add an Anthropic branch (option a); do NOT proxy through LiteLLM (option b):*
1. **Cost logging** — `router.complete()._log()` already emits `events.log_event("llm_call", name=model_key, success, duration_ms, meta={tokens_in,tokens_out,cost_micros})` into the PG `events` spine that powers `/analytics/*`. LiteLLM moves token/cost accounting behind a proxy and forces re-plumbing the audit spine.
2. **Fallback chain** — `plan()` + `DEFAULT_CHAIN` is a bespoke per-agent capability/tier planner reading `agents.model_key`/`fallback_models`. LiteLLM's own router fallback would duplicate and fight this logic.
3. **tool_choice + streaming + HARD GATE** — the SSE path (`astream_turn` → `stream.stream_dashboard` events) and the outbound HARD GATE in `graph._tools_node` are tightly coupled to the `_normalise()` tool-call shape. One adapter branch is a smaller, controllable surface than re-deriving tool-call ids through a proxy.
4. Only **one** provider (Anthropic) is non-OpenAI-wire-compatible today; a single adapter branch is far less risk than a new always-on network hop (`:4000`) in front of every LLM call during a live client demo.

*Backend (edited):*
- `backend/orchestrator/router.py`:
  - `MODELS` stops being a bare literal. NEW `_load_models(org_id)` merges the static dict (`tool-32b`, `fast-7b`, `vision-vl`) with `model_configs` rows (DB wins on `model_key` collision), cached with a short TTL. **If `_load_models` throws (PG blip), fall back to the static dict** — routing must never 500 the SSE stream. `_clients` cache keyed by `model_key` stays.
  - NEW `_client_for(model_key)`: for `type in {local, openai}` returns `openai.AsyncOpenAI(base_url=…, api_key=decrypt(api_key_enc) or "local")` (unchanged wire). For `type == anthropic` returns a cached `anthropic.AsyncAnthropic(api_key=decrypt(…))` — **NEW `anthropic` SDK dependency** added to the host venv `~/aganetiAi/.venv`.
  - `complete(messages, tools, agent, need_vision, tier, temperature, max_tokens, ctx)`: inside the per-`model_key` plan loop, branch on the resolved config's `type`. OpenAI/local path unchanged (`client.chat.completions.create(timeout=90, tools=…, tool_choice="auto")` → `_normalise()`). NEW `_complete_anthropic(...)`: translates OpenAI-shape messages → Anthropic Messages API (system extracted to top-level `system`; `tool` role → `tool_result` content blocks; assistant `tool_calls` → `tool_use` blocks), maps json-schema `tools` → Anthropic `tools`, `tool_choice="auto"` → `{"type":"auto"}`, then normalises the Anthropic response **back** into the exact `assistant_msg_dict` shape `_normalise()` returns — so `graph._tools_node` sees identical `tool_calls` (`id/name/args`) and the HARD GATE / `resume(approved)` `tool_call_id` matching is unaffected. `_log()` → `events.log_event("llm_call", …)` fires unchanged for Anthropic (cost from `cost_in/out`).
  - `plan(agent, need_tools, need_vision, tier)`: same logic, now over the merged set. Anthropic keys with `caps.tool_call=True` are eligible for tool turns. NEW guard: **filter out `disabled`/unknown `model_key`s** (including stale entries in `agents.fallback_models`) before building the chain, and **skip fallback candidates whose `caps.ctx` < current token estimate**.
  - `llm.chat(...)` wrapper: signature unchanged.
- `backend/orchestrator/graph.py`: `AgentState.model_key` is **pinned per run**. The pin is set once at `run_turn`/`astream_turn` entry from the resolved selector; `_agent_node` continues to read `state["model_key"]` and pass `agent={model_key, fallback_models}` into `llm.chat` — it does **not** re-read a mutable session default mid-loop. `STEP_BUDGET=8` and `recursion_limit=3*STEP_BUDGET` unchanged.
- **NEW `backend/routes/models.py`** (mirrors the `_uid`/auth pattern of `backend/dashboard/routes/dashboard.py`, `APIRouter(prefix="/models")`): `GET /models` (list active; keys **REDACTED** — never return `api_key_enc` or plaintext), `POST /models` (admin-gated: read trusted `request.headers["x-auth-user"]`, resolve role via `db.sync.resolve_user` and require `admin`; encrypt key with `token_crypto`; **cheap reachability probe** — models-list/ping, never a completion), `PATCH /models/{model_key}` (status/system_prompt), `DELETE /models/{model_key}` (soft-disable, sets `status="disabled"`). Registered in `backend/main.py` via `include_router(models_router)` alongside the guarded `dashboard_router`, behind `AuthEnforceMiddleware` (which strips any client-supplied `x-auth-user` and injects the trusted one).
- Selector plumbing: `backend/dashboard/stream.py` `_init_state` and `backend/dashboard/routes/dashboard.py` `POST /dashboard/ask` (the LOCKED analytics surface) and `POST /dashboard/chat` accept an optional `model_key` in the request body; it is validated against active `model_configs` for the org, then written **once** into the initial `AgentState.model_key`. `analytics_agent`'s prompt and `run_metric`/`list_metrics` tools are untouched → the numbers still come only from verified `metrics.py` SQL, so the agent is **model-agnostic** and the exact-number answer is identical regardless of backend.

*Frontend (edited/new):*
- **NEW `src/pages/ModelsSettings.tsx`** (or a section of an existing settings page), styled with `neu`, `neu-inset`, `neu-pill`, `composer-dock`, type classes `t-heading/t-body/t-label`, accent `#00D4FF`, danger `#FF4466`. Form: `name`; a `neu-pill` segmented control for `type`; conditional field (`endpoint` for local/openai, `api_key` password field for openai/anthropic); `model` text; optional `system_prompt` textarea; `caps` toggles. CRUD via the `http` axios instance from `src/api/client.ts`.
- **NEW `src/components/ModelSelector.tsx`**: a `neu-pill` dropdown in `src/pages/DashboardPage.tsx` (and the `/ask` composer) listing active models; sets the per-request `model_key` sent in the SSE POST body via `apiFetch`.
- SSE consumption in `DashboardPage.tsx` unchanged — still `fetch()` → `res.body.getReader()` + `TextDecoder`, `buffer.split("\n")`, `data:` lines, `JSON.parse`, dispatch on `token/tool_call/chart_saved/done/error`, `[DONE]` sentinel, `AbortController` (NOT `EventSource`).

---

**Implementation Order** —

1. **DB**: add `M.ModelConfig` to `backend/db/models.py`; Alembic migration for `model_configs`. Seed the migration with the three static keys (`tool-32b`, `fast-7b`, `vision-vl`) so DB becomes source of truth. *(Dep: none.)*
2. **Repo/crypto read path**: NEW `backend/db/models_repo.py` (or extend `db/sync.py`) — `list_active(org_id)`, `get(org_id, model_key)`, encrypt/decrypt via `token_crypto`. *(Dep: 1.)*
3. **Router merge**: `router._load_models` + `_client_for` + short TTL cache; keep the static dict as fallback when DB is empty/unavailable. *(Dep: 2.)*
4. **Anthropic adapter**: add `anthropic` SDK to the venv; implement `_complete_anthropic` + message/tool translation + normalise-back; unit-test that a tool call round-trips to the identical `assistant_msg_dict` shape as OpenAI, including the outbound HARD GATE pause → `resume(approved)` `tool_call_id` path. *(Dep: 3.)*
5. **Run-pin**: `graph.py`/`run_turn`/`astream_turn` set `AgentState.model_key` once at entry; confirm `_agent_node` reads only the pinned value. *(Dep: 3.)*
6. **API**: `backend/routes/models.py` CRUD + admin gate (`resolve_user` role check) + key redaction + cheap endpoint probe on create; wire into `main.py`. *(Dep: 2.)*
7. **Selector plumbing**: accept `model_key` in `/dashboard/ask` and `/dashboard/chat` bodies → validate → initial state. *(Dep: 5, 6.)*
8. **Frontend settings panel + selector dropdown**. *(Dep: 6, 7.)*
9. **Fallback logging extension**: add `meta.selected_model_key`/`meta.fallback_from`/`meta.actual_model_key` to the `_log()` call in `complete()`. *(Dep: 3.)*

---

**Risks & Pitfalls** —

- **Anthropic tool-call id drift** — Anthropic emits `tool_use` blocks with `toolu_*` ids vs OpenAI `call_*`. `graph._tools_node` matches `tool_call_id` when building the tool result and the pending-approval record; the outbound HARD GATE `resume(approved)` swaps a specific `tool_call_id`. If normalise-back doesn't preserve a stable id across assistant → tool → next-assistant turn, the loop silently loses the tool result (burning toward `STEP_BUDGET=8`) or resume swaps the wrong id. **Mitigation**: test the full outbound-approval pause/resume path on Anthropic explicitly.
- **`tool_choice` + interleaved text** — Anthropic interleaves `text` and `tool_use`. `_normalise()` must not drop the text portion (the LOCKED `analytics_agent` states the number in words — text is the answer). **Mitigation**: normalise both content parts.
- **`caps.ctx` mismatch on fallback** — a run under a large-ctx Anthropic model that falls back to `fast-7b` mid-chain can overflow ctx and hard-fail. `MAX_TOOL_OUTPUT=6000` truncation helps but doesn't bound total history. **Mitigation**: the `plan()` ctx guard skips candidates smaller than the current token estimate.
- **Key leakage** — `GET /models` must redact; a naive full-row serializer exposes `api_key_enc`. Even ciphertext must not be returned. `TOKEN_ENC_KEY` rotation invalidates all stored keys — document, do not auto-rotate.
- **Cost-logging holes** — local models are `cost_in/out=0`; a new OpenAI/Anthropic model with unset costs logs `cost_micros=0`, under-reporting spend in `events` (`summary`, `per_agent`). **Mitigation**: require non-zero cost entry (or a sane default) in `POST /models`.
- **Cache staleness across workers** — after `POST`/`PATCH`, each uvicorn worker's in-process `_load_models` cache must converge. **Mitigation**: short TTL + a Redis version key on `:6379` (Redis is now used by the Phase-1 charts TTL cache, so it is already in the request path). A restart is not acceptable for a live demo.
- **DB down at request time** — `_load_models` must fall back to the static `MODELS` dict, not 500 the SSE stream (preserve the "never raise into request path" property `events.log_event` already holds).
- **Endpoint probe** — keep it a cheap models-list/ping, never a completion (avoids token spend and rate-limit on create).

---

**Blocked by** —

- Nothing hard-blocks DB/table/router/adapter work — it builds directly on `backend/db/models.py`, `backend/orchestrator/router.py`, `backend/services/token_crypto.py`, `backend/events.py`, and `backend/db/sync.py`. Steps 1–6, 9 can start immediately.
- **Step 7 end-to-end selector testing on `POST /dashboard/ask`** depends on the Phase-1 LOCKED `analytics_agent` + `run_metric`/`list_metrics` + `metrics.py` being present, since that is the surface where "swap the model, same exact-number answer" is demonstrated to the client.
- Frontend settings panel (step 8) is blocked by the CRUD API (step 6); the selector dropdown by both step 6 and per-request plumbing (step 7).
- No dependency on LiteLLM (`:4000`) — out of scope by the decision above.

**Demo-safety callouts**: (a) The **Azure serverless cold start (15–60s)** is upstream of model selection and unrelated to it — but the model probe must never hit Azure; probe only the LLM endpoint. Analytics answers still route through `coreshare_db.run_query` with its HYT00/HYT01 retry, so a model swap changes nothing about the SQL cold-start path. (b) `dbo.*` returns no rows — never used; all money answers JOIN `VRequestsApproved.RequestID = VRequestAttributes.RequestId` inside the LOCKED `metrics.py`, so model-agnostic answers stay non-empty and correct. (c) `x-auth-user` is trusted only because `AuthEnforceMiddleware` strips the client value and injects the verified one; the admin gate on `POST /models` reads that trusted header — no client can self-elevate to register a rogue endpoint.

---

**Identify** — *What breaks if you swap models MID-SESSION (mid `astream_turn` / mid multi-step tool loop), and the clean handling:*

Swapping mid-run breaks in four concrete ways in this code:

1. **`AgentState.model_key` is read per `_agent_node`, but `AgentState.messages` (`operator.add`) is shared and provider-shaped.** If turn N ran on OpenAI (assistant message carries OpenAI `tool_calls` with `call_*` ids) and turn N+1 switches to Anthropic, the Anthropic request now contains a prior assistant turn whose `tool_use`/`tool_result` pairing doesn't match Anthropic's expectations → Anthropic 400s on a mismatched/missing `tool_result` for a `tool_use` id. The `_tools_node` HARD GATE `resume(approved)` (swapping a specific `tool_call_id`) dangles if the id convention changed mid-flight.
2. **Tool-call id/format differences** — OpenAI `call_*` vs Anthropic `toolu_*`. A mid-loop switch leaves the transcript with mixed conventions the new provider rejects or fails to correlate; the tool result is silently dropped and the agent loops without its data toward `STEP_BUDGET=8`.
3. **Context-window (`caps.ctx`) differences** — switching to a smaller-ctx model mid-run against an already-large accumulated `messages` list overflows ctx → hard failure, not graceful degradation.
4. **Partial streamed tokens** — `astream_turn` has already emitted `type:token` events for the in-flight assistant turn. A mid-turn switch appends a second, differently-voiced continuation onto tokens the client already rendered — visible incoherence in the live demo, and `conversation.py` per-board history persists a Frankenstein turn.

**Clean handling (what to build):**
- **Pin the model per run.** At `run_turn`/`astream_turn`/`stream.stream_dashboard` entry, resolve the selector **once** — precedence: per-request body `model_key` > per-board/session default > agent `agents.model_key` > `DEFAULT_CHAIN` — and write it into the initial `AgentState.model_key`. `_agent_node` reads only this pinned value; there is no mutable "current session model" re-read mid-loop.
- **Apply a user's switch only at the next run boundary.** The `ModelSelector` dropdown changes the *default for the next message*; the in-flight run finishes on its pinned model. This guarantees id-format, ctx, and voice consistency across a single tool loop and the tokens already streamed.
- **Re-plan only on failure, family-aware.** When the pinned model fails, `complete()` walks the `plan()` chain → `DEFAULT_CHAIN`. To avoid id/shape corruption on cross-provider fallback: prefer same-wire-family candidates first (local/OpenAI are interchangeable; Anthropic → Anthropic if available). Cross-family fallback (Anthropic → OpenAI-family) is permitted **only at a clean turn boundary** where the last message is plain user/assistant text — never between a `tool_use` and its unfilled `tool_result`. The `plan()` `caps.ctx` guard also skips candidates smaller than the current token estimate.
- **Log every switch/fallback.** Extend the existing `_log()` in `complete()` (already `events.log_event("llm_call", success=False)` on a failed attempt) with `meta.selected_model_key`, `meta.fallback_from`, and `meta.actual_model_key`, so `/analytics/*` shows exactly which model served each turn and why any fallback occurred.

---

<a id="2-agentic-upgrade"></a>

## 2. Agentic Capability Upgrade

**Decision** — Turn the Phase-1 LOCKED `analytics_agent` role (the exact-number answerer behind `POST /dashboard/ask`) into a genuine multi-step reasoner without touching the primary Aria path (`PRIMARY_TOOLS` in `backend/orchestrator/templates.py`) or the existing chart-authoring `dashboard` agent (`backend/dashboard/stream.py`, `agent_id="dashboard"`). We build five things, all scoped to the analytics tool surface:

1. **Multi-step reasoning** stays on the existing compiled `GRAPH` in `backend/orchestrator/graph.py` — no new graph. We make the step ceiling per-run instead of the hard `STEP_BUDGET = 8` constant, and the `/dashboard/ask` SSE generator sets it to **12 for the analytics run only**. The chain `list_metrics -> run_metric -> run_metric -> (compare in-model) -> explain` fits in ~5 agent turns; 12 leaves headroom for one self-correction retry plus one clarification turn while still ending a runaway loop. `STEP_BUDGET = 8` remains the module default for every other agent (Aria, specialists, the chart `dashboard` agent).
2. **Self-correction lives in the tool handler, not the graph.** `run_metric` (Phase-1 `backend/dashboard/metrics.py`) returns a **structured envelope serialized to a string** the model can read and react to. The existing `agent -> tools -> agent` cycle in `graph.py` *is* the retry mechanism; we only make tool output actionable. No graph-level retry loop is added.
3. **Confidence scoring** is computed **in the `run_metric` handler** from the actual result set (row count, null ratio on `SuggestedAssistanceAmount`, `SubmitDate`/`FinishDate` coverage, and the count of rows dropped by amount-hygiene) and surfaced two ways: embedded in the tool's returned string so the model can quote it in words, and emitted as a **new SSE `confidence` event** at `done` time.
4. **Clarification loop** is prompt-driven: the `analytics_agent` system prompt tells the model to call a **new lightweight tool `ask_clarification(question)`** rather than guess. The tool carries a new `is_clarify` flag so `_tools_node` turns it into a clean run-ender and the generator emits a **new SSE `clarify` event** — deliberately NOT routed through the outbound-approval `is_outbound` HARD GATE, so it never inherits the Approve/Reject pause UX.
5. **Audit trail** reuses `events.log_event(kind="tool_called", ...)` — **no new table**. `events.meta` (JSONB) holds args / output-preview / model_key / confidence / run_id. `run_id` is the `agent_runs.id` opened per turn.

> **ASSUMPTION (Phase-1):** `POST /dashboard/ask` and its `stream_dashboard`-style SSE generator exist as LOCKED work; this section extends that generator, it does not create the endpoint.
> **ASSUMPTION:** the analytics run uses a distinct `agent_id` (e.g. `"analytics"`), separate from the chart path's `agent_id="dashboard"`, so raising the step budget for analytics cannot bleed into chart authoring. Step budget is set **explicitly in state by the generator**, never inferred from the `agent_id` string.
> **ASSUMPTION:** `/dashboard/ask` uses an analytics-specific allow-list `ANALYTICS_TOOL_NAMES = [list_metrics, run_metric, ask_clarification, current_time]` — none of which are `is_outbound`.

**Architecture** —

Data flow (one `/dashboard/ask` turn):
`routes/dashboard.py POST /dashboard/ask` → new `ask_stream()` SSE generator in `backend/dashboard/stream.py` (sibling of `stream_dashboard`) opens an `agent_runs` row, builds state with `allowed_tools=ANALYTICS_TOOL_NAMES`, `step_budget=12`, `agent_id="analytics"`, and `ctx={user_id, agent_id, run_id}` → `GRAPH.astream(state, {"recursion_limit": 3*step_budget}, stream_mode=["updates"])` → per-node `updates` decoded into SSE events → model calls `list_metrics`/`run_metric`/`ask_clarification` → `_tools_node` executes handlers → `run_metric` computes confidence + logs audit → rows land in Postgres `events` via `db.sync.session()`.

Files/modules/functions/tables affected:

- **`backend/orchestrator/graph.py`** (EDIT):
  - `AgentState` TypedDict: add `step_budget: int | None` and `clarifying: dict | None` (parallel to the existing `awaiting: dict | None`).
  - `_route_agent`: replace the hard `step >= 8` check with `step >= (state.get("step_budget") or STEP_BUDGET)`. **The parentheses are load-bearing** — `step >= state.get("step_budget") or STEP_BUDGET` binds as `(step >= …) or STEP_BUDGET` and returns a truthy int, ending every run. `STEP_BUDGET = 8` stays as the default when `step_budget` is `None`.
  - `_tools_node`: add a branch that fires when a tool's new `is_clarify` attribute is set — record `clarifying = {"question": <text>, "tool_call_id": <id>}` and answer that tool call with the question text. This is placed with the other post-execution branches; because `ask_clarification` is not `is_outbound`, it never touches the approval gate.
  - `_route_tools`: return `"end"` when **`awaiting` OR `clarifying`** is set (currently only `awaiting`), else `"agent"`.
  - `_tools_node`: keep the `MAX_TOOL_OUTPUT = 6000` truncation, but the handlers must place the `status`/`confidence`/error block at the **front** of their return string so truncation can only ever eat the row dump, never the caveat.
- **`backend/orchestrator/registry.py`** (EDIT): add `is_clarify: bool = False` to `@dataclass Tool` (mirrors `is_outbound`). `register`, `get`, `openai_schemas`, `preview` unchanged.
- **`backend/dashboard/metrics.py`** (Phase-1 file, EDIT): `run_metric` wraps its `coreshare_db.run_query` result in an envelope (front-loaded status/confidence):
  - Success: `{"status":"ok","metric_id","rows":N,"value"|"series":…,"confidence":{…},"hygiene_dropped":M}`.
  - Empty: `{"status":"empty","metric_id","hint":"call list_metrics for alternatives; check date_from/date_to","tried_filters":{…}}`.
  - Warming/timeout: `coreshare_db.run_query` already retries once on cold-start `HYT00`/`HYT01` (2s sleep, 90s per-query timeout). If it still raises, catch and return `{"status":"error","reason":"warming","retry_hint":"the data source was resuming; ask me to run the SAME metric again"}` instead of letting the exception bubble — the model re-issues the *same* metric, not a fallback.
  - All money math stays on the LOCKED whitelist SQL: amounts (`SuggestedAssistanceAmount`/`RequiredAmountForAssistance`) come from `DataShare.VRequestAttributes` JOINed to `DataShare.VRequestsApproved` on `RequestID = RequestId`, with hygiene `> 0 AND <= 1000000`. `run_metric` never authors SQL; it selects a pre-verified metric.
- **`backend/dashboard/confidence.py`** (NEW — justified: the scoring math is non-trivial and shared by `run_metric` and the SSE `done` handler; folding it into the metric whitelist would bloat `metrics.py`). Exposes `score(rows, money_col="SuggestedAssistanceAmount", date_col="SubmitDate", hygiene_dropped=0) -> dict` returning `{"level":"high|medium|low","row_count","null_ratio","date_coverage_days","hygiene_dropped","reasons":[…]}`.
- **`backend/dashboard/tools.py`** (EDIT): register new tool `ask_clarification` with `is_clarify=True`, params `{"question": {"type":"string"}}`, handler returns the question verbatim. Add its name to `ANALYTICS_TOOL_NAMES` (the `/dashboard/ask` allow-list), **not** to `DASHBOARD_TOOL_NAMES` (the chart-authoring allow-list) — the chart agent must not gain a clarify tool.
- **`backend/orchestrator/templates.py`** (EDIT): the Phase-1 `analytics_agent` prompt gains three rules — (a) "If the request is ambiguous about metric, date range, emirate (`State`), or category, call `ask_clarification` with ONE targeted question and stop; do not guess." (b) "After every `run_metric`, read the `confidence` block; if `level` is `low`, say so in words and explain why (few matching records, cold data)." (c) "If two `run_metric` attempts return `status:empty`, state plainly that no data matches the request and stop — do not thrash." Reaffirm the LOCKED honesty rule: collection/donation questions get "no collection data available in this source."
- **`backend/dashboard/stream.py`** (EDIT — new `ask_stream`): map graph `updates` to SSE:
  - `token` / `done` / `error` / `[DONE]` — unchanged.
  - `tool_call` (existing shape) — emitted when the agent node yields a tool call; carries `{name, args}`.
  - **`tool_result`** (NEW) — emitted when a tool node completes; carries `{name, ok, preview}` (first ~200 chars of the envelope).
  - **`confidence`** (NEW) — emitted at `done`; carries the `confidence.score(...)` dict for the last `run_metric`.
  - **`clarify`** (NEW) — emitted when `state["clarifying"]` is set; carries `{question}`; the generator then emits `[DONE]` and returns. **Run complete, not paused.**
  - Compute `recursion_limit` from the per-run budget (`3*step_budget`), not the constant, or LangGraph raises `GraphRecursionError` mid-stream (surfaces as an `error` SSE event).
- **`backend/events.py`** (REUSE, no edit): handlers log via the existing `class timed("tool_called", name=<tool>)` context manager to auto-capture `duration_ms` + `success`, which calls `log_event(kind="tool_called", name=<tool>, user_id=ctx["user_id"], meta={"agent_id":ctx["agent_id"], "run_id":ctx["run_id"], "args":args, "output_preview":<=200 chars, "model_key":<from ctx/router>, "confidence":<level>})`. Rows land in the Postgres **`events`** table (the audit spine) via `db.sync.session()`. `log_event` is best-effort and never raises into the request path.
- **`agent_runs`** table (REUSE): `run_id = agent_runs.id`, created by `ask_stream` at turn start and threaded into `ctx` so handlers can stamp `meta.run_id`.
  > **ASSUMPTION:** `ctx` gains a `run_id` key (today the executor builds `ctx={user_id, agent_id}`); `ask_stream` populates it from the `agent_runs` row it opens.

No new table: `events.meta` JSONB already carries args/output_preview/model_key/confidence/run_id, and `events.py` already aggregates by `kind` under `/analytics/*` (`tools`). A dedicated `tool_calls` table would fragment that spine.

**Implementation Order** —

1. **`backend/dashboard/confidence.py`** (NEW) — pure function, no deps. Unit-test `score()` against synthetic sets (all-null money col, 0 rows, 1-day span, wide span). *Depends on: nothing.*
2. **`registry.py`** — add `is_clarify` to `Tool`. *Depends on: nothing; must precede graph edits that read it.*
3. **`graph.py`** — per-run `step_budget` in `_route_agent` (parenthesized), `clarifying` state field, `_tools_node` clarify branch, `_route_tools` end-on-clarify, front-loaded truncation contract. *Depends on: 2.*
4. **`metrics.py`** — wrap `run_metric` in ok/empty/warming envelopes; call `confidence.score()`; log via `timed("tool_called", …)`. *Depends on: 1 and Phase-1 `metrics.py`.*
5. **`tools.py`** — register `ask_clarification` (`is_clarify=True`); define `ANALYTICS_TOOL_NAMES`. *Depends on: 2.*
6. **`templates.py`** — extend the `analytics_agent` prompt (clarify + confidence-reading + two-empties-then-stop rules). *Depends on: 4, 5.*
7. **`stream.py`** — `ask_stream` emits `tool_result`/`confidence`/`clarify`, threads `run_id` into `ctx`, computes `recursion_limit=3*step_budget`. *Depends on: 3–6.*
8. **Frontend `src/pages/DashboardPage.tsx` + `src/hooks/useStream.ts`** — new event cases (see Identify). *Depends on: 7.*
9. **End-to-end dress rehearsal** against a **pre-warmed** Azure source. *Depends on: all.*

**Risks & Pitfalls** —

- **Cold-start eats the demo.** `coreshare_db.run_query` blocks 15–60s on the first query after DABS-CORE-SHARE serverless auto-pause. A multi-step chain fires several `run_metric` calls; if the first hits cold start the model must not read it as `empty` and fall back to a *different* metric (wrong-looking answer). Mitigation: the `status:error, reason:warming` envelope is **textually distinct** from `status:empty` and its `retry_hint` says "run the SAME metric again"; the generator fires a throwaway warm-up (`run_metric("approvals_count")`) at handler start, and the Phase-1 Redis TTL cache blunts repeats. Pre-warm with a curl before the client arrives.
- **Precedence bug in the step-budget gate.** `step >= state.get("step_budget") or STEP_BUDGET` silently always ends the run (and `TypeError`s when `step_budget` is `None`). Must be `step >= (state.get("step_budget") or STEP_BUDGET)`. Verified in Implementation Order step 3.
- **`recursion_limit` desync.** Raising the per-run budget to 12 without recomputing `recursion_limit` (still `3*8=24`) risks `GraphRecursionError` on a long chain, surfacing as an `error` SSE event mid-stream. `ask_stream` must pass `3*step_budget`.
- **Self-correction thrash within budget.** If every `run_metric` returns empty (bad date range), the model could burn all 12 steps rephrasing, and `_route_agent` then emits a numberless partial `final` — the worst case for an exact-number agent. Mitigation: the empty `hint` steers to `list_metrics` once, and prompt rule (c) forces "two empties → state plainly and stop."
- **`MAX_TOOL_OUTPUT = 6000` silently drops confidence.** If confidence/error sits after a large row dump it is truncated and the model answers with no caveat. Front-loading the envelope (step 3) is load-bearing, not cosmetic.
- **Clarify vs outbound-approval confusion.** Both end via `_route_tools -> "end"`, but `clarifying` (ask the user, resumed by a *new* `/dashboard/ask` message) and `awaiting` (outbound approval, resumed via `resume(...,approved)`) are semantically different. If the generator mapped `clarifying` to `approval_required`, the stage would show an Approve/Reject card for a *question*. The `clarify` and `approval_required` events stay strictly separate; analytics never emits the latter.
- **`DurationIn*` / `DecisionDate` traps.** The model cannot author SQL here (only `run_metric`/`list_metrics`), which is the safety. But any whitelist metric that `SELECT`s `DurationInDays/Hours/Minutes` or does date math on the char `DecisionDate` full-scans/times out and reads as a false low-confidence answer. Guard at whitelist-review time (coordinate with Section 1); use `SubmitDate`/`FinishDate` for all date logic.
- **`x-auth-user` / `run_id` attribution.** `_uid(request)` trusts the `x-auth-user` header that `AuthEnforceMiddleware` injects (it strips any client-supplied value first, then appends the effective user). `ask_stream` must open the `agent_runs` row with that trusted uid — not the legacy `"user_1"` fallback — or audit rows attribute tool calls to the wrong user.
- **`log_event` is best-effort.** A Postgres hiccup silently loses audit rows (by design — it never raises into the request path). Do not promise a 100%-complete live audit trail on stage.
- **Confidence is heuristic, not statistical.** A correct "yesterday" number legitimately has few rows and may score `low`. Phrase low confidence as "based on limited matching records" — never imply the number is wrong.

**Blocked by** —

- **Phase-1 LOCKED work must exist first:** `backend/dashboard/metrics.py` (whitelist + `run_metric`/`list_metrics`), the `analytics_agent` role in `templates.py`, and `POST /dashboard/ask` with its SSE generator. This section edits those; it does not create them.
- **Section 1 (Data/Metrics foundation)** if it owns the curated whitelist and the exact JOIN/amount-hygiene SQL: confidence scoring reads `hygiene_dropped`, so each metric's SQL (or its `_dry_run`-style probe) must expose pre- and post-hygiene row counts. Coordinate the envelope shape with whoever owns `metrics.py`.
- **Section 3 (Forecasting)** is downstream, not a blocker; if it adds tools they should adopt the same `tool_called` audit convention and confidence envelope defined here.
- No new infra and no new Python dependency: Postgres `events`/`agent_runs`, the `tool-32b` router (`qwen2.5-32b` at `localhost:9000/v1`), and the Redis cache all already run.

**Identify** — *Where LangGraph's loop/interrupt design creates friction with SSE streaming, and how the frontend handles partial output + tool-call visibility.*

Four concrete friction points and their handling:

1. **The outbound-approval HARD GATE pauses the whole run — analytics structurally cannot trip it.** In `_tools_node`, an `is_outbound` tool records a pending approval, answers `[AWAITING USER APPROVAL] <preview>`, and `_route_tools` returns `"end"`, freezing the run until `resume(...,approved)`. On `/dashboard/ask` the allow-list is `list_metrics/run_metric/ask_clarification/current_time` — **none are `is_outbound`** — so the analytics agent cannot pause for approval. We route clarification through the separate `is_clarify` end-condition (a fresh, resumable-by-message question), never the approval gate. Frontend consequence: no Approve/Reject card ever appears on the analytics board.

2. **Tokens are per-node `updates`, not true token-by-token.** `GRAPH.astream(..., stream_mode=["updates"])` yields one payload per node completion, so a `token` SSE event carries the *whole* assistant message for that agent turn. On a 5-step chain the client sees text arrive in bursts with `tool_call`/`tool_result` events between them. Handling: lean into it — the bursts read as "thinking → ran a metric → thinking," which is the selling point. A token-level alternative (`stream_mode=["messages"]`) exists but complicates interleaving with `tool_call` events; **not recommended for the demo.**

3. **`MAX_TOOL_OUTPUT = 6000` truncation.** A many-row metric is cut before the model sees it; if confidence/error sat at the tail it would vanish. Handled by front-loading `status`/`confidence` in every envelope (graph.py contract, step 3), so the model always sees the caveat first.

4. **Step-budget end yields a partial final.** `_route_agent` ending at `step >= step_budget` emits whatever the last agent message was. Handled by prompt rule (c) ("two empties → state plainly and stop") and the `confidence` event: even a thin answer carries an explicit low-confidence annotation instead of a silent stop.

**Frontend mapping — extends `src/hooks/useStream.ts` + `DashboardPage.tsx`.** The existing reader (`fetch()` → `res.body.getReader()` + `TextDecoder`, `buffer.split("\n")`, `data:`-prefixed lines, `JSON.parse`, switch on `parsed.type`, `[DONE]` sentinel, `AbortController` — **not** native `EventSource`, which cannot POST with the Supabase auth header) already dispatches `token/tool_call/chart_saved/done/error`. Add only new `case` arms — zero protocol change:

- **`tool_call`** (existing) → in-progress chip (`neu-pill` + `lucide-react` `Loader2` spinner). Label mapped from `name`: `run_metric` → "Calculating…", `list_metrics` → "Finding metrics…", `ask_clarification` → no chip. Key it by `name`/`tool_call_id` so `tool_result` can resolve it.
- **`tool_result`** (NEW) → flip the matching chip to done (`Check`, accent cyan `#00D4FF`) or failed (`X`, danger `#FF4466`) via `ok`; show `preview` on hover. Gives the client visible "it ran a real query" feedback per step.
- **`confidence`** (NEW) → small badge under the final answer (high = cyan `#00D4FF`; medium = `--text-secondary`; low = danger-tinted with `reasons[]` in a tooltip). Use `t-caption`/`t-label` type classes.
- **`clarify`** (NEW) → render `question` as an assistant bubble and focus the existing `composer-dock`. **Do NOT show any Approve/Reject affordance.** The user answers in the next message, which starts a fresh `/dashboard/ask` turn; per-board history via `orchestrator.conversation` carries context. `clarify` is immediately followed by `[DONE]`, so the reader closes normally.
- **paused-for-approval state** lives only on Aria/primary boards (the `approval_required` event from `astream_turn`), rendered as the distinct Approve/Reject card — kept entirely separate so the analytics board never shows it.

`[DONE]` remains the single terminator for the token, `clarify`, and `done` paths alike.

---

<a id="3-forecasting"></a>

# 3. Future Prediction (Phase 1.5)

**Decision** — Build a lightweight, statistics-only forecaster exposed as one new **read-only** tool `run_forecast(metric_name, horizon_months)` that projects the next 1–3 monthly points (point estimate + 95% interval) for exactly two quantities already defined in the Phase-1 `backend/dashboard/metrics.py` whitelist: **monthly expenditure** (`SUM(SuggestedAssistanceAmount)` with amount hygiene, over the `VRequestsApproved × VRequestAttributes` JOIN) and **monthly approved-request count** (`COUNT(*)` over `VRequestsApproved` alone). Method is **ordinary least squares (OLS) linear trend on a dense monthly series**, implemented with **numpy only (no statsmodels, no scipy)** plus a small hard-coded Student-t table; Holt / Holt-Winters are rejected (justified below).

`metric_name` accepts the same identifiers as the Phase-1 `run_metric(metric_id, …)` / `list_metrics()` tools, but `run_forecast` only honors the two metrics that have a monthly-bucketable base definition (expenditure, approved-count); any other `metric_name` is refused with an explicit message, never silently coerced.

The tool pulls trailing history through `coreshare_db.run_query`, bucketing strictly on `FinishDate` (real `datetime`; never `DecisionDate`, which is `char`), reuses each metric's own base SELECT/JOIN/WHERE from `metrics.py`, and refuses to forecast unless a **minimum of 4 complete, settled, non-null months** survive the guard rails. Every projected number is labelled `"projected"` in the tool output and never mixed with actuals. `run_forecast` is `is_outbound=False`, `required_permission=""` — read-only, so it does **not** hit the `_tools_node` outbound HARD GATE and needs no approval. It is wired into the Phase-1 `analytics_agent` role's `tools` allow-list and answered through the existing `POST /dashboard/ask` SSE path. An optional `"forecast"` chart type is a stretch item gated behind the core tool.

*Why OLS, not Holt-Winters:* the series is 4–6 monthly points. Holt-Winters seasonal needs ≥2 full seasonal cycles (≥24 monthly points) — impossible here. Holt's linear (double exponential smoothing) must estimate α and β from a handful of observations and overfits with no honest interval. OLS linear trend is closed-form and yields a defensible prediction interval whose width **blows up automatically at tiny `n`** via the `t_{n-2}` quantile and the leverage term `(x₀−x̄)²/Sxx` — exactly the "widen CI on low n" behaviour required. statsmodels' `OLS.get_prediction()` would be convenient but adds a heavy dependency the brief forbids; the numpy closed form is ~15 lines.

**Architecture** —

Data flow: `analytics_agent` (via `POST /dashboard/ask`) → LangGraph `_tools_node` invokes the `run_forecast` handler → `forecast.py` builds a monthly-aggregate SQL string from the corresponding `metrics.py` base fragment → `coreshare_db.run_query` (Azure, cold-start aware) → dense-fill + maturity-trim + gate in Python → numpy OLS + t-interval → compact, `"projected"`-labelled text (well under `MAX_TOOL_OUTPUT = 6000`) back to the agent, which states the numbers in words.

NEW files/functions:
- **`backend/dashboard/forecast.py` (NEW)** — the engine:
  - `build_monthly_sql(metric_id, date_from, date_to) -> str` **(NEW)** — wraps the base fragment owned by `metrics.py` into a monthly aggregate. Date bounds are computed **server-side** and **inlined as ISO literals** (not `?` bind params — `coreshare_db.run_query(sql)` takes a single SQL string and does no parameter binding; the bounds are derived from the server clock, never user input, so there is no injection surface). The generated SQL passes `validate_select_only` (starts `SELECT`, single statement, no semicolon, no forbidden keyword) and never references any `DurationIn*` column or `DecisionDate`.
    - **Expenditure** (reuses the expenditure metric's JOIN + hygiene; dates come from the `VRequestsApproved` side `r`, amounts from the `VRequestAttributes` side `a`):
      ```sql
      SELECT YEAR(r.FinishDate) AS y, MONTH(r.FinishDate) AS m,
             SUM(a.SuggestedAssistanceAmount) AS value
      FROM DataShare.VRequestsApproved r
      JOIN DataShare.VRequestAttributes a ON r.RequestID = a.RequestId
      WHERE a.SuggestedAssistanceAmount > 0
        AND a.SuggestedAssistanceAmount <= 1000000
        AND r.FinishDate >= '2025-10-01' AND r.FinishDate < '2026-07-01'
      GROUP BY YEAR(r.FinishDate), MONTH(r.FinishDate)
      ```
    - **Approved-count** (reuses the approvals-count metric; **no JOIN, no amount hygiene** — count is not money, so hygiene must not filter it):
      ```sql
      SELECT YEAR(FinishDate) AS y, MONTH(FinishDate) AS m, COUNT(*) AS value
      FROM DataShare.VRequestsApproved
      WHERE FinishDate >= '2025-10-01' AND FinishDate < '2026-07-01'
      GROUP BY YEAR(FinishDate), MONTH(FinishDate)
      ```
    - **ASSUMPTION:** both series bucket on `FinishDate` (money is disbursed / a request is approved at settlement, not submission). This is documented in the metric definition and surfaced in the answer ("based on settled data through <month>"). `SubmitDate` remains available as an alternate bucketing key if the count metric is later redefined as a submission-activity series.
  - `_dense_months(rows, first, last) -> list[(ym, value)]` — reindexes onto a complete month calendar between `first` and `last`, filling **missing months with 0** (a `GROUP BY` month with no qualifying rows is genuine zero activity, not a null gap).
  - `_trim_immature(series, now, maturity_lag_days) -> series` — drops the current partial calendar month **and** any month whose end falls inside the settlement/backfill window (see Identify §3).
  - `_gate(series, min_months=4) -> (ok: bool, reason: str)` — minimum-history gate, applied **after** trimming.
  - `_ols_forecast(y, horizon) -> list[{month, point, lo, hi}]` — numpy `polyfit(deg=1)`; residual variance `s² = SSE/(n−2)`; `SE_pred = s·sqrt(1 + 1/n + (x₀−x̄)²/Sxx)`; interval `± T[n−2]·SE_pred` from a hard-coded 95% two-sided t-table `{1:12.71, 2:4.303, 3:3.182, 4:2.776, 5:2.571, 6:2.447, 7:2.365, 8:2.306}`; then **clamp point and lower bound to ≥ 0** (upper bound may stay).
  - `run_forecast_handler(ctx, metric_name, horizon_months) -> str` — orchestration: clamp `horizon_months` to `[1,3]`; Redis-cached actuals pull; dense-fill → trim → gate → OLS; compact `"projected"`-labelled output; best-effort `events.log_event("forecast_run", …)`.
  - `register_forecast_tool()` (idempotent) — builds `registry.Tool(name="run_forecast", description=…, parameters=<json-schema>, handler=run_forecast_handler, required_permission="", is_outbound=False)` and calls `registry.register(...)`.

EDITED files:
- **`backend/dashboard/metrics.py`** (Phase-1 LOCKED — extend, do not re-debate) — expose the base FROM/JOIN/WHERE+hygiene fragment of the expenditure and approvals-count metrics as importable definitions (e.g. a `METRICS[metric_id]` entry carrying the base fragment) so `forecast.py` imports one source of truth. **No metric semantics change** — reuse only, so the fitted series equals the dashboard's own KPI numbers.
- **`backend/dashboard/__init__.py`** — call `register_forecast_tool()` alongside `register_dashboard_tools()`; add `"run_forecast"` to the module's exported tool-name set. (Like the existing 5 dashboard tools, it is registered into the shared registry but stays **out of `PRIMARY_TOOLS`**.)
- **`backend/orchestrator/templates.py`** — add `"run_forecast"` to the Phase-1 `analytics_agent` role's `tools` list (its per-call `allowed_tools`). Do **not** add it to `PRIMARY_TOOLS` — Aria must never forecast, consistent with the existing dashboard-tool exclusion.
- **`backend/dashboard/stream.py`** — in the `/ask` analytics `system_prompt(board_id)`, add: forecasts come **only** from `run_forecast`; every projected figure must be spoken as "projected"/"estimated" and attributed to settled data through the stated month; the model must **never** invent a projection or narrate a forecast for a metric the tool refused; and the Phase-1 "no collection/donation data available" rule is unchanged.
- **Redis (`:6379`, reuse the Phase-1 TTL cache helper)** — new key namespace `forecast:actuals:{metric_name}:{first}:{last}`, TTL ~300s (deliberately longer than the 30–60s dashboard-render cache: the actuals pull is the expensive cold-start query and forecast inputs do not need second-fresh data).

OPTIONAL (stretch, gated behind the core tool):
- **`backend/dashboard/tools.py`** — extend `_dry_run_chart_sql`'s alias check to accept a `"forecast"` chart type; `config_db.py` needs **no new column** (`type` is already free-text). **`src/components/charts/DashCharts.tsx`** — new `ForecastChartI` (actuals solid line reusing `LineChartI` styling + dashed projected tail + shaded CI band), hand-rolled SVG, no chart lib. **`src/pages/DashboardPage.tsx`** — add `"forecast"` to the chart render switch; it reaches the frontend through the same SSE `fetch()` + `res.body.getReader()` + `TextDecoder` pattern already used (not native `EventSource`).

Tables/columns touched: Azure read objects `DataShare.VRequestsApproved` (dates `FinishDate`/`SubmitDate`, `RequestID`) and `DataShare.VRequestAttributes` (`SuggestedAssistanceAmount`, `RequestId`), JOIN `r.RequestID = a.RequestId`. Local `dashboard_configs.db` only if the optional chart type persists a saved forecast. Audit via `events.log_event(kind="forecast_run", user_id=…, name=metric_name, success=…, duration_ms=…, meta={…})` — best-effort, never raised into the request path.

**Implementation Order** —
1. **Extend `metrics.py`** to expose the expenditure and approvals-count base fragments as importable definitions. *Depends on: Phase-1 `metrics.py` existing.*
2. **Write `build_monthly_sql` + `_dense_months` + `_trim_immature` + `_gate`** in `forecast.py`; unit-test monthly bucketing, 0-fill of missing months, current-month drop, and the maturity trim against a fixed fixture (no Azure yet). Assert the generated SQL starts `SELECT`, is single-statement, contains no `DecisionDate` and no `DurationIn*`, and passes `validate_select_only`. *Depends on 1.*
3. **Write `_ols_forecast`** (numpy closed form + t-table + non-negative clamp); unit-test on synthetic upward/flat/downward series, on `n=4` (widest CI, `df=2`, `t≈4.30`), and on `n<4` (gate must reject before OLS runs). *Depends on nothing external.*
4. **Wire `run_forecast_handler`** with the Redis actuals cache and the cold-start-safe pull through `coreshare_db.run_query`; any `run_query` exception is a **hard abort** (return "history unavailable, cannot project"), never a forecast on partial data; format the `"projected"`-labelled string within `MAX_TOOL_OUTPUT`. *Depends on 2, 3, and a warm/reachable Azure source.*
5. **Register the tool** (`register_forecast_tool` in `forecast.py`, called from `__init__.py`) and add `"run_forecast"` to `analytics_agent` in `templates.py`. *Depends on 4.*
6. **Update the `/ask` prompt** in `stream.py`; end-to-end test through `POST /dashboard/ask` that the agent (a) forecasts when history is sufficient and labels every figure "projected", (b) says "insufficient history to project" when gated, (c) still says "no collection data available" for donation/collection questions (Phase-1 rule, unchanged). *Depends on 5.*
7. **(Optional) Forecast chart type** — `_dry_run_chart_sql` alias tolerance, `ForecastChartI`, `DashboardPage.tsx` switch. *Depends on 6; ship the tool without this if time is short.*

**Risks & Pitfalls** —
- **Wrong-table date reference (corrected from the draft).** `FinishDate`/`SubmitDate` live on `VRequestsApproved` (the 17-col shape), **not** on `VRequestAttributes`. The builder must read dates from the `r` (approved) alias and amounts from the `a` (attributes) alias; `YEAR(a.FinishDate)` would be a hard SQL error / wrong column. Test asserts the date functions reference the `VRequestsApproved` alias.
- **Recent-month undercount from status backfill — the #1 silent corruptor.** `VRequestsApproved` contains only already-approved requests; not every request that will eventually carry a `FinishDate` in month M has settled at query time, so the most recent 1–2 months are **systematically undercounted and keep growing after the fact**. OLS reads this as a false downward trend. Guard = maturity lag (Identify §3). This passes every unit test yet is wrong in the live demo if not trimmed.
- **Trim window can starve the gate.** After dropping the current partial month **plus** the maturity-lag tail, a naïve trailing-6-month pull can leave <4 mature months and force a permanent "insufficient history" answer. Guard: **pull a wider raw window than the fit window** — trailing ~9 months of `FinishDate` history so that after trimming, 4–6 mature months remain to fit. (**ASSUMPTION:** raw pull = 9 months; fit window = the surviving 4–6. This honors the "3–6 month" fit-window intent while absorbing the trim.)
- **Cold-start partial / timed-out series.** `run_query` retries once on `HYT00`/`HYT01` after a 2s sleep, but a monthly aggregate over a cold serverless resume can still exceed the 90s per-query timeout. Guard: **treat any surviving exception as a hard forecast abort** ("history unavailable, cannot project") — never forecast on a caught error — and cache the successful pull so the demo's second call is warm.
- **Current partial month drags the trend down.** Today is 2026-07-19; July is ~11 days short. Guard: always drop the current calendar month and set the SQL upper bound to first-of-current-month (`< '2026-07-01'`).
- **Missing vs zero months.** `GROUP BY` emits no row for a zero-activity month; `_dense_months` reindexes and 0-fills so the series length and slope are correct.
- **Amount-hygiene uneven trimming.** `> 0 AND <= 1000000` can zero out a month that legitimately had one very large disbursement. Guard: keep hygiene **byte-identical to the `metrics.py` expenditure KPI** so the fitted series equals the dashboard's numbers; log per-month pre/post-hygiene row counts and flag (as a spoken caveat, not a silent drop) any fitted month that lost a material share.
- **Ramadan / seasonal spikes.** UAE charity disbursement spikes around Ramadan (2026 ≈ Feb 18–Mar 19). With <24 months there is no way to deseasonalize. Guard: label all output "projected (trend only; seasonal effects such as Ramadan not modelled)"; the wide small-`n` interval partly absorbs it — do not claim seasonal accuracy.
- **`DecisionDate` is `char`** — unreliable for date math. Guard: builder references only `FinishDate`/`SubmitDate`; a test asserts no `DecisionDate` in the generated SQL.
- **`DurationIn*` full-scan timeout.** Selecting `DurationInDays/Hours/Minutes` triggers a scan/timeout. Guard: builder never emits them; test enforces it.
- **Overconfident CI at boundary `n`.** `df = n−2`; at `n=4`, `t≈4.30` (wide by design). At `n<4` (`df≤1`) the interval is meaningless — the min-history gate must reject **before** OLS runs, not clamp after.
- **Negative projections.** A steep downward slope can push the point or lower bound below 0. Guard: clamp point and lower CI bound to ≥ 0 for both expenditure and count.
- **Tool-output truncation.** `MAX_TOOL_OUTPUT = 6000` — emit only a few months × `{point, lo, hi}` plus the settled-through caveat; never dump the full historical series through the tool channel.
- **Agent over-reach.** The `analytics_agent` might narrate a projection the tool refused. Guard: the `/ask` prompt forbids inventing projections; the gated path is tested explicitly.

**Blocked by** —
- **Phase-1 LOCKED additions** must exist first: `backend/dashboard/metrics.py` (the curated verified SQL reused here), the `run_metric` / `list_metrics` tools, the `analytics_agent` role, and `POST /dashboard/ask`. **ASSUMPTION:** delivered by Section 1 (metrics layer) and Section 2 (analytics agent + `/ask`) of this same plan.
- **Redis TTL cache helper** from Phase 1 (`redis:7` on `:6379`, running but unused by the dashboard before Phase 1) — the actuals cache reuses it.
- A **reachable, warm Azure `DABS-CORE-SHARE`** path (Tailscale → GCP socat relay → Azure) with `coreshare_db.run_query` operational, holding at least ~6–9 months of `FinishDate` history in `VRequestsApproved`/`VRequestAttributes`. If real settled history is <4 mature months, the tool is correct but will only ever return the gated "insufficient history" message in the demo — **verify the data window (and run one warm-up query) before the client session.**
- No dependency on the optional `"forecast"` chart type: the tool + `/ask` text answer is the shippable unit.

**Identify** — *What data sparsity/irregularity could silently corrupt forecasts, and the exact guard rails:*

1. **Serverless cold-start partial / timed-out series** → never forecast on a caught error. `run_query` retries once on `HYT00/HYT01` after 2s; wrap the pull so **any** surviving exception aborts with an explicit "history unavailable, cannot project" message. **Cache the successful actuals pull in Redis** (`forecast:actuals:{metric_name}:{first}:{last}`, TTL ~300s) so the expensive cold query runs once and the live demo reuses a warm series. Fire one warm-up query before the demo.
2. **Current partial month dragging the trend down** → compute "current month" from the server clock, **drop it from the fit**, and set the SQL upper bound to first-of-current-month (`FinishDate < '2026-07-01'`) so it is never even fetched.
3. **Status backfill / immature recent months** (most dangerous — biases the slope *downward*) → apply a **data-maturity lag**: exclude any month whose end falls within a settlement window. **ASSUMPTION: `maturity_lag_days = 45`** (drops roughly the last 1–2 months in addition to the current partial one). Only months old enough that approvals have stabilised enter the fit. Document it in the answer ("based on settled data through <month>"). To keep ≥4 mature months after this trim, pull a **wider raw window (~9 months)** than the fit window.
4. **Zero-request months as MISSING rows, not zeros** → `_dense_months` builds a complete monthly calendar between the first and last observed month and **fills gaps with 0** before fitting.
5. **Amount-hygiene removing outliers unevenly** → keep `> 0 AND <= 1000000` **byte-identical to the `metrics.py` expenditure KPI** (so the fitted series equals the dashboard's own numbers) and applied **only** to the expenditure series (not the count series); **log per-month filtered-row/amount counts** and surface any month that lost a material share as a spoken caveat rather than silently trusting it.
6. **`DecisionDate` being `char`** → the builder references **only** `FinishDate`/`SubmitDate` (real `datetime`) for bucketing; a test asserts the generated SQL contains no `DecisionDate` and no `DurationIn*` column.
7. **Ramadan / seasonal spikes** → not model-able with <24 months; **label all output "projected (trend only; seasonal effects such as Ramadan not modelled)"** and rely on the wide small-`n` interval rather than pretending to seasonal accuracy.
8. **Insufficient / degenerate history** → a **minimum-history gate of ≥ 4 complete, settled, non-null months** applied *after* dropping the current partial month and the maturity-lagged tail; below that, return "insufficient history to project" (no OLS run). **CI widens automatically on low `n`** via `t_{n-2}` (`t≈4.30` at `n=4`) plus the leverage term `(x₀−x̄)²/Sxx`. **Clamp the point estimate and lower CI bound to ≥ 0** so a steep negative slope cannot produce a negative expenditure/count projection.

---

<a id="4-dashboard-ui"></a>

**Decision** — Build an on-click chart expansion side panel (framer-motion slide-over, right side) over the existing `DashCharts.tsx` tiles + `DashboardPage.tsx` grid, containing three things: (1) a **client-side paginated data table** rendered over the already-fetched `chart.data` (NO new data endpoint), (2) an **analytics narrative explanation** fetched from a NEW `GET /dashboard/charts/{id}/explain` route that is Redis-cached keyed by `chart_id + sha256(normalized_rendered_sql)`, TTL-aligned to the Phase-1 render cache and invalidated by that same key rotation + TTL expiry, and (3) a **chart-scoped follow-up box** that POSTs to the existing Phase-1 `POST /dashboard/ask` SSE endpoint with the chart's `chart_id`/`title`/`sql` injected as context. Add **per-chart configurable auto-refresh** (Off / 30s / 60s / 5min) driven by a single page-level poll loop that reuses `reconcile()` against the Redis-TTL-cached `GET /dashboard/charts`. Add a **model selector** in the `DashboardPage` header (wired to the Section-1 per-agent `agents.model_key`), not in a buried drawer. The one-at-a-time `AnimatePresence` reveal and the localStorage board model (`BOARD_KEY`) stay exactly as-is.

Rationale for client-side table over a new `/charts/{id}/data?page` endpoint: `GET /dashboard/charts` already returns each chart's full `data` array via `_render()` (runs `cfg.sql` with `{where}`→`""` through `coreshare_db.run_query`). The curated Phase-1 metrics are aggregates (expenditure by category/emirate, monthly trend, approvals count, yesterday activity) — bounded cardinality (State/emirate ∈ {Dubai, Ajman, RAK, Sharjah, UAQ} = 5; categories/months small). A dedicated paginated endpoint would re-hit **serverless auto-pausing Azure SQL** (cold-start 15–60s) per page flip — unacceptable during a live client demo. Paginate client-side over the array already in memory. **ASSUMPTION:** no single curated metric returns thousands of rows; if a future free-form `save_chart` SQL does, the table caps display at the fetched array and shows a "showing first N" note rather than paging the DB.

Rationale for `explain` reading pre-rendered data over re-querying: the explain route MUST NOT trigger a second Azure query on panel open (that reintroduces cold-start stalls). It reads the already-rendered rows for this chart out of the **Phase-1 Redis render cache** (populated when the grid rendered `GET /dashboard/charts`), and only falls back to a single `coreshare_db.run_query` on a cache miss. This keeps the LLM summarization purely deterministic over numbers already produced by `_render()`, so it cannot drift from the grid.

**Architecture** — Data flow and exact modules:

Frontend (edited, `~/aganetiAi/frontend`, Vite/React 19/Tailwind v4, hand-rolled charts):
- `src/pages/DashboardPage.tsx` (EDIT): add `expandedChartId` state; add a `refreshConfig` map `{[chartId]: 0|30|60|300}` persisted to localStorage under a NEW key `DASH_REFRESH_KEY` (sibling of the existing `BOARD_KEY`); add ONE page-level `useEffect` poll loop (see Identify §6) that calls the existing `reconcile()` (`GET /dashboard/charts` → merge-by-id via `setCharts`). Header gains the model selector. Clicking a tile sets `expandedChartId`; the slide-over reads that chart object out of the existing `charts` state (no refetch for the table). Keep the existing SSE `send()` (fetch → `res.body.getReader()` + `TextDecoder` + `buffer.split("\n")` + `AbortController`) untouched except for the abort/finally hardening in Identify §1–2.
- `src/components/charts/DashCharts.tsx` (EDIT): `KpiTile`, `BarChartI`, `DonutChartI`, `LineChartI` each get an `onExpand(chartId)` prop / wrapping click handler. Keep the `AnimatePresence` one-at-a-time reveal untouched — the slide-over is a sibling overlay, NOT a member of the reveal list.
- `src/components/charts/ChartDetailPanel.tsx` (NEW): framer-motion slide-over (`motion.div`, `initial={{x:'100%'}} animate={{x:0}} exit={{x:'100%'}}`, `neu`/`neu-inset` classes, `lucide` `X`/`RefreshCw`/`Send`). Three sections:
  1. **Data table** — client paginates `chart.data`; page size ~12; `t-label` headers, `neu-inset` rows; renders whatever keys exist in the row objects.
  2. **Explanation** — on mount, `apiFetch('/api/dashboard/charts/{id}/explain')` (GET, non-SSE JSON `{explanation, generated_at, cache_hit, model_key}`). `apiFetch` injects the Supabase auth header, which `AuthEnforceMiddleware` verifies and converts into the trusted `x-auth-user` the route reads via `_uid`. Shows a `neu` shimmer "generating…" placeholder until resolved; `react-markdown` + `remark-gfm` renders the result; `generated_at` + `cache_hit` shown in a `t-caption` footer.
  3. **Follow-up box** — reuses the SSE reader pattern from `useStream.ts`/`DashboardPage.send()` (fetch → `getReader()` + `TextDecoder`, `buffer.split("\n")`, `data:` lines, dispatch on `type` token/tool_call/done/error, `[DONE]` sentinel, `AbortController`). POSTs to `POST /dashboard/ask` with body `{message, board_id, context:{chart_id, title, sql}}`. Because the Phase-1 analytics/dashboard tools are all `is_outbound=False`, this stream never hits the outbound HARD GATE / approval pause — it runs to `done` within `STEP_BUDGET=8`.
- `src/components/ModelSelector.tsx` (NEW, OWNED by Section 1): dropdown of router `MODELS` keys (`tool-32b`, `fast-7b`, `vision-vl`); writes the chosen `agents.model_key` via Section-1's agent-config endpoint. Section 4 only mounts it in the header. **ASSUMPTION:** Section 1 exposes an endpoint that sets `agents.model_key` (+ optional `fallback_models`) for `agent_id` of the dashboard / analytics agents; if not yet present, the header renders a disabled placeholder behind a flag.

Backend (edited/new, `~/aganetiAi/backend`):
- `backend/dashboard/routes/dashboard.py` (EDIT): add `GET /dashboard/charts/{id}/explain`. Reads the trusted user via existing `_uid(request)` (401 otherwise — MUST use `_uid`; a hand-rolled route that trusts a client-supplied `x-auth-user` is an auth bypass, since `AuthEnforceMiddleware` strips client copies at lines ~127–128 before injecting the effective one). Loads cfg via `config_db.get_config(id)`; computes the rendered SQL exactly as `_render()` does (`cfg.sql` with `{where}`→`""`); normalizes it with `_normalize_sql` (from `tools.py`) and computes cache key `explain:{id}:{sha256(normalized_sql)}`. Cache hit → return cached JSON (`cache_hit:true`). Miss → obtain this chart's rows from the Phase-1 Redis render cache (fall back to a SINGLE `coreshare_db.run_query(rendered_sql)` only if absent), call `explain.py`, store the result in Redis with the SAME TTL as the render cache (30–60s, Phase-1), return `{explanation, generated_at, cache_hit:false, model_key}`.
- `backend/dashboard/explain.py` (NEW, small): `async def explain_chart(cfg, data, ctx) -> dict`. Calls `orchestrator.router.llm.chat(messages, agent={model_key, fallback_models}, ctx=ctx)` directly — NOT a full `GRAPH.astream`/`run_turn` LangGraph run. Justification: the numbers are already computed by the render, so no tools and no re-query are needed; this is a single deterministic summarization call (cheaper/faster than routing through the graph, and it cannot re-query Azure). `messages` = the charity-staff persona borrowed from `stream.system_prompt(board_id)` + the pre-computed rows + "explain these already-computed figures; every number you state must appear verbatim in the provided rows; do not mention SQL; if asked about collections/donations, answer that no collection data is available." Logs via `events.log_event("chart_explained", user_id=..., name=model_key, success=..., duration_ms=..., meta={"agent_id":"analytics_agent","tokens_in":...,"tokens_out":...,"cost_micros":...})`. **ASSUMPTION:** using `llm.chat` with the analytics agent's `model_key`/persona (rather than the full `analytics_agent` graph role) satisfies the "generated by the analytics_agent" requirement; if Section 3 wants strict role parity, swap the body for a tool-less `run_turn` with the analytics prompt — the endpoint contract is unchanged.
- Redis: reuse the client Phase-1 introduced for the `/dashboard/charts` render cache (`redis:7` on `:6379`). Two key families: render cache (Phase-1) and NEW `explain:{id}:{sqlhash}`. Explanation invalidation is automatic — the key embeds `sha256(normalized_rendered_sql)`, so changing the curated metric SQL rotates the key, and TTL expiry (≤60s) bounds data drift; no manual bust. **ASSUMPTION:** the Phase-1 render cache is keyed by `board_id` (+ `include_unclaimed`) and holds each chart's rendered rows retrievable by `chart_id`; both the explain lookup and the poll loop rely on it being populated by the first render.

Tables/columns: **no schema changes.** Explanation + refresh state are ephemeral (Redis + localStorage). Charts still persist in the LOCAL SQLite `dashboard_configs` table via `config_db` (columns `id,type,title,sql,created_by,created_at,board_id` — note there is **no `metric_id` column**, so the follow-up box passes `sql`/`title`, not a metric id; see Risks). Money/date rules unchanged — the explanation only narrates figures the render already produced from `DataShare.VRequestAttributes.SuggestedAssistanceAmount` / `RequiredAmountForAssistance` under the required JOIN `DataShare.VRequestsApproved.RequestID = DataShare.VRequestAttributes.RequestId`, with `>0 AND <=1000000` hygiene; `DurationIn*` and `DecisionDate` are never selected.

**Implementation Order** —
1. `ChartDetailPanel.tsx` skeleton (slide-over + click wiring in `DashCharts.tsx`/`DashboardPage.tsx`), table section only, over existing `chart.data`. No backend dependency — ships immediately.
2. Backend `GET /dashboard/charts/{id}/explain` + `explain.py`. Depends on Phase-1 `analytics_agent` persona/`model_key` + the Redis render cache. Test cold-miss vs warm-hit; assert the miss path reads Redis and does NOT open a second Azure connection when the render cache is warm.
3. Wire the panel's explanation section to the endpoint with the "generating…" placeholder + `cache_hit`/`generated_at` display.
4. Follow-up box → `POST /dashboard/ask` SSE with `{chart_id,title,sql}` context. Depends on Phase-1 `/dashboard/ask`.
5. Per-chart auto-refresh: `DASH_REFRESH_KEY` state + single poll loop reusing `reconcile()`. Depends on the Phase-1 Redis TTL cache on `/dashboard/charts` (else every tick cold-hits Azure).
6. Mount `ModelSelector` in the header. Depends on the Section-1 endpoint (can land last / behind a flag).
7. Harden SSE remount/abort behavior (Identify block).

**Risks & Pitfalls** —
- **Serverless cold-start amplification (demo-fatal):** auto-refresh at 30s across N charts must NOT fan out N Azure queries per tick. Enforce ONE `reconcile()` (single `GET /dashboard/charts` returns all charts) gated by the Phase-1 Redis render cache. If that cache is missing/misconfigured, a 30s interval hammers the auto-pausing DB and the client watches 15–60s stalls. Likewise the `explain` route must read the render cache, not re-query. Hard dependency, not optional.
- **Explanation regenerated on every open = LLM cost + latency:** the whole point of `explain:{id}:{sqlhash}`. If TTL is too short or the key doesn't stabilize (SQL whitespace/case), you regenerate every open — normalize with `_normalize_sql` (from `tools.py`) BEFORE hashing so cosmetically-different-but-identical SQL shares one entry.
- **Explanation staleness vs data:** a TTL-bounded (≤60s) explanation can describe numbers ~1 min older than a just-refreshed table. Surface `generated_at` in the panel so the mismatch is legible, not silent.
- **Hallucinated numbers:** because `explain.py` passes the already-rendered rows (no tools, no re-query), the narration cannot drift from the render — but the prompt must still forbid inventing figures and must route collection/donation asks to "no collection data available" (that data does not exist in this DB; Phase-1 rule).
- **Missing `metric_id`:** `config_db` charts store `sql`/`title`, not a `metric_id`. The follow-up context therefore passes `chart_id`/`title`/`sql`; `/dashboard/ask` resolves intent from those. Passing a fabricated `metric_id` would be an invented field. **ASSUMPTION:** if Phase-1's `run_metric` later persists an originating metric id, add it to the context then.
- **Free-form `save_chart` columns in the table:** a user-authored SQL could surface ugly columns (e.g. an accidentally-selected `DurationIn*`); the table just displays whatever keys `data` contains. Acceptable — curated metrics never select those, and the render, not the panel, owns column selection.
- **Multi-board race:** `reconcile()` merges by chart id; the panel reads from `charts` state. If a refresh tick replaces the array while the panel is open, the open chart could momentarily be absent (unclaimed/600s claim window). Guard: re-resolve the chart by `expandedChartId` each render; if not found, keep the last-known snapshot rather than crashing the panel.
- **AbortController leak:** an unmounted panel with an in-flight `/dashboard/ask` stream must abort on unmount or the reader buffers into a dead component (React 19 StrictMode double-invoke makes this visible). See Identify §1.
- **Trusted-header bypass:** the `explain` route MUST use `_uid(request)` (the middleware-injected `x-auth-user`); never trust a client-supplied one.

**Blocked by** —
- **Section 1 (model routing/selector):** the endpoint that sets `agents.model_key` (+ `fallback_models`) for the dashboard/analytics agents. Header selector stubs (disabled placeholder) until then.
- **Section 3 / Phase-1 LOCKED:** the `analytics_agent` persona/`model_key`, `POST /dashboard/ask` SSE, and the **Redis TTL cache on the `/dashboard/charts` render** must exist first — auto-refresh, explanation caching, AND the explain route's no-requery path all depend on it. `metrics.py` + `run_metric`/`list_metrics` back the curated numbers `/dashboard/ask` narrates.
- No block from Section 2 (data/SQL) beyond the already-LOCKED curated metrics.

**Identify — SSE reconnection, click-away, and partial-save handling (exact behavior):**

Current state: `useStream.ts` and `DashboardPage.send()` use `fetch()` → `res.body.getReader()` + `TextDecoder` + manual `buffer.split("\n")` + `AbortController`. There is **NO auto-reconnect** — this is a raw fetch reader, not native `EventSource` (which reconnects on its own; we can't use it because SSE here requires POST + the Supabase auth header via `apiFetch`).

What happens today on a network blip / Azure relay drop mid-stream:
- `reader.read()` either rejects or the stream closes early; the loop exits WITHOUT ever seeing `done`/`[DONE]`. `reply`/`status` are frozen on the last partial tokens. No error toast unless the server emitted a `type:error` frame before the drop. The run keeps executing server-side (LangGraph state survives restarts), and if a `save_chart` tool already completed, the chart IS persisted in `config_db` even though the frontend never received the `chart_saved` frame.
- Click-away mid-response: unmount/navigation fires `AbortController.abort()`; `reader.read()` rejects `AbortError`; the loop is abandoned; `reply` and the `tool_call` status label dangle on the now-hidden component; a chart may be half the truth — saved server-side, not reconciled client-side.

Exact handling to implement:

1. **Abort-on-unmount (both the panel follow-up stream and main `send()`):** store the `AbortController` in a ref; the `useEffect` cleanup calls `controller.abort()`. Swallow `AbortError` explicitly (never surface it as an error toast — it's a user action). This stops the dead reader from buffering into an unmounted component.

2. **Idempotent reconcile on remount = the recovery mechanism.** Because `chart_saved` only triggers `reconcile()`, and `reconcile()` is `GET /dashboard/charts` → merge-by-id into `charts`, a chart persisted server-side whose `chart_saved` frame was lost STILL appears on the next `reconcile()`. So call `reconcile()` (a) on `DashboardPage` mount and (b) in a `finally` block after ANY stream ends — success, error, OR abort. Merge-by-id makes repeated calls safe (no duplicates; server-side `_chart_signature` dedup already enforced at save time).

3. **Restart, not resume.** The stream is NOT resumable — `stream_dashboard`/`/dashboard/ask` carry no server-side cursor/offset and `run_turn` has no partial-token replay. On a detected drop: end the stream cleanly, run the `finally` `reconcile()` (picks up any completed chart/tool side-effects), and show a non-blocking "Connection interrupted — refreshed from server" note. If the user wants the narrative again they re-ask (idempotent for read-only metrics). Never auto-reconnect the same generation stream: re-running could re-execute tools, whereas the only durable side-effect (chart save) is already reconcilable via REST, so restart-on-demand is strictly safer than resume.

4. **"Generating…" placeholder lifecycle:** the follow-up/explanation shows a `neu` shimmer while awaiting (mirrors the `AgentState.awaiting` concept). It clears on the first `token` frame OR on `done`/`error`/`abort`. The `finally` block clears it in ALL exit paths, so a dropped stream never leaves a permanent spinner.

5. **Partial chart visibility guarantee (explicit):** a half-saved chart appears via the REST `GET /dashboard/charts` reconcile even if the SSE died, because persistence (`config_db.insert_chart`) and streaming (`chart_saved` frame) are decoupled — the REST render is the source of truth; the SSE frame is only an optimization to trigger reconcile early. The auto-refresh poll loop (§6) also calls `reconcile()`, so within one interval (≤5min, ≤60s if configured) any orphaned-but-saved chart self-heals into the grid with no user action.

6. **Auto-refresh timer model — one poll loop, not per-chart `setInterval`:** run a SINGLE `setInterval` at the *minimum enabled* interval across all charts (if any chart is 30s, tick at 30s). Each tick calls the ONE Redis-TTL-served `reconcile()`. Per-chart intervals are honored by tracking `lastRefreshed[chartId]` and visually pulsing only charts whose own interval has elapsed — but exactly one network call (`GET /dashboard/charts`) per tick regardless of chart count. This avoids N parallel intervals each fetching (which would defeat the Redis cache and, on miss, stampede the serverless Azure DB). Diffing happens inside `reconcile()`'s existing merge-by-id `setCharts`, so unchanged charts don't re-animate and the one-at-a-time `AnimatePresence` reveal fires only for genuinely new ids.

**ASSUMPTION:** Phase-1's Redis render cache keys `/dashboard/charts` by `board_id` (+ `include_unclaimed`) and stores each chart's rendered rows retrievable by `chart_id`; the explain lookup and the poll loop both rely on that cache being populated by the first render so ticks and panel-opens stay cheap and never cold-hit Azure.

---

<a id="5-prompt-and-extraction"></a>

# 5. Data Extraction + System Prompt Management

**Decision** — Build a versioned system-prompt admin surface, a deterministic context-injection and SQL-validation layer for the `analytics_agent` path (Phase-1's `POST /dashboard/ask`), and a structured JSON output contract for analytics answers. Concretely:

1. A **(NEW)** `prompt_versions` Postgres table holding immutable, numbered versions of the three managed prompts (`analytics_agent`, `dashboard` persona, `primary`), with exactly one `is_active=true` row per role. A **(NEW)** admin API (create / activate / rollback) plus a **(NEW)** loader that resolves the active body at invocation time. The existing `agents.system_prompt` column stays as the live cached copy for the agent-row runtime path and is kept in sync by the activation write.
2. A **(NEW)** deterministic context builder assembling the per-invocation system message for the analytics agent from four cached/cheap sources: current date, the Phase-1 active metric whitelist (`list_metrics`), a PRE-COMPUTED schema summary (a cached `schema.table -> cols` map, never a live `coreshare_db.get_schema()` per turn), and the caller's `users.role` + `agent_permissions`.
3. A **(NEW)** identifier-whitelist validator that parses every model-generated SQL statement on the `query_data` / `save_chart` free-SQL path and rejects any table/column not in the cached schema map, returning a STRUCTURED error — layered on top of the existing `validate_select_only` and the read-only DABS-CORE-SHARE grant.
4. A **(NEW)** structured JSON output contract for `analytics_agent`, produced by a final formatting **tool** (not model `response_format`), with the `/dashboard/ask` SSE generator streaming friendly prose to the user while delivering the full JSON object as a terminal event to the frontend.

**ASSUMPTION**: The three managed prompts are keyed by a stable string `agent_role` (`analytics_agent` / `dashboard` / `primary`), not per-`agents.id` UUID, because the dashboard/analytics personas are singletons loaded by `stream.system_prompt(board_id)` and the `/dashboard/ask` init, not per-tenant `agents` rows. Per-`agents.id` prompt overrides remain in the existing `agents.system_prompt` column and are out of scope.

**ASSUMPTION**: `sqlglot` is acceptable as the single new Python dependency (justified under *Architecture*); if it is not, a constrained regex + alias resolver in `sql_guard.py` is the fallback, failing CLOSED.

---

**Architecture**

*Prompt versioning — storage & loader*

- **(NEW)** table `prompt_versions` (added to `backend/db/models.py` as an SQLAlchemy model, Alembic migration under `migrations/`): `id uuid PK, agent_role TEXT (analytics_agent|dashboard|primary), version INT, body TEXT, is_active BOOL, created_at TIMESTAMPTZ, created_by uuid (FK users.id), note TEXT`. Partial unique index `(agent_role) WHERE is_active` enforces exactly one active row per role; unique `(agent_role, version)`.
  - **Justification for a new table over `agents.config` JSONB**: (a) history is append-only and queried by `(role, version)` — a JSONB blob on one agent row cannot represent N immutable historical versions cleanly nor enforce "exactly one active" with a DB constraint; (b) these three prompts are role-level singletons and do not belong to any single `agents` row (`dashboard`/`analytics_agent` are dashboard-package personas with no agent row at all); (c) `agents.system_prompt` already exists as the *live* value, so we keep it as the hot read path for agent-row runtimes and treat `prompt_versions` as source of truth + audit trail, avoiding re-plumbing every `agents.system_prompt` reader.

- **(NEW)** module `backend/orchestrator/prompts.py`:
  - `get_active_prompt(agent_role) -> str` — reads the `is_active` row via `db/sync.py` `session()`, wrapped in `events.timed("prompt_load", name=agent_role)`. In-process TTL cache (60s) keyed by role so the analytics hot path does not hit Postgres every turn; cache busted explicitly on activation.
  - `list_versions(agent_role) -> list[dict]`, `create_version(agent_role, body, created_by, note) -> int` (`version = max+1`, `is_active=false`), `activate(agent_role, version)`, `rollback(agent_role, version) = activate` of an older version.
  - `activate()` runs in ONE `session()` transaction: flip the old active row off, the target on, and — only for roles backed by `agents` rows (i.e. `primary`, matched by `agents.kind='primary'` / `template_key`) — MIRROR `body` into those `agents.system_prompt` rows. `dashboard` and `analytics_agent` have no agent rows, so no mirror is written for them; they are read live via `get_active_prompt` at invocation. The TTL cache is busted only AFTER commit; the activation is logged via `events.log_event("prompt_activated", name=agent_role, meta={version})`.

- **Loader path integration** (edits to the brief's existing modules):
  - `backend/dashboard/stream.py` `system_prompt(board_id)` — today returns the hardcoded charity-staff persona. EDIT so the static persona text is seeded as `prompt_versions(agent_role='dashboard', version=1)` and the function calls `prompts.get_active_prompt('dashboard')`, then appends the board-specific line in code (board context stays code-side, never stored in the versioned body).
  - The Phase-1 `POST /dashboard/ask` analytics "state the number" prompt — its init reads `prompts.get_active_prompt('analytics_agent')`.
  - `backend/orchestrator/templates.py` — `primary_template()` seeds `prompt_versions(agent_role='primary', version=1)` from the existing `PRIMARY_PROMPT` constant. The primary runtime path (`_agent_node` → the agent row's `system_prompt`) is untouched at read time; `activate()` keeps `agents.system_prompt` in sync.

- **(NEW)** admin API `backend/routes/prompts.py` — `APIRouter(prefix="/admin/prompts")`, guarded and registered via `include_router` in `backend/main.py` like `dashboard_router`. Endpoints: `GET /admin/prompts/{role}` (versions + active), `GET /admin/prompts/{role}/{version}`, `POST /admin/prompts/{role}` (create draft), `POST /admin/prompts/{role}/{version}/activate`, `POST /admin/prompts/{role}/{version}/rollback`. Reads the caller from the trusted `x-auth-user` header injected by `AuthEnforceMiddleware` (same `_uid(request)` pattern as `routes/dashboard.py`, `401` if absent), then `db/sync.py resolve_user` → require `users.role == 'admin'`, else `403`. Non-admins may read; only admins may create/activate/rollback.

- **(NEW)** frontend admin view in `~/aganetiAi/frontend`: `src/pages/PromptAdminPage.tsx`, using the existing `http` axios instance from `src/api/client.ts` (calls `/api/...`), the neumorphic `neu` / `t-heading` / `t-body` classes, a version list with an active badge, a `<textarea>` body editor, and Save-as-new-version / Activate / Rollback buttons. Read-only for non-admins.

*Context injection — deterministic, cached*

- **(NEW)** `backend/dashboard/context.py`, `build_analytics_context(user_id, board_id) -> str`, appended as system-role content AFTER the active `analytics_agent` body:
  1. **Date**: reads the server clock (the same source the `current_time` tool handler uses) and injects a literal (`today = 2026-07-19`) so the model never guesses "yesterday"/relative dates.
  2. **Active metric whitelist**: calls Phase-1 `list_metrics()` and injects each `metric_id`, its human description, and accepted `group_by`/`filters`, steering the model toward `run_metric` (the safe path).
  3. **Schema summary**: renders the PRE-COMPUTED cached `schema.table -> [col:type]` map, restricted to the USABLE OBJECTS only — `DataShare.VRequests`, `DataShare.VRequestsApproved`, `DataShare.VExceptionRequestsApproved`, `DataShare.VRequestAttributes`, `DataShare.VRequestSteps`, `Analytics.ServiceStepMetrics`, `Analytics.RequestAttribute` — explicitly excluding `dbo.*` (returns no rows). Carries the money/JOIN rules already in `stream.system_prompt`: amounts are only `SuggestedAssistanceAmount` / `RequiredAmountForAssistance`, live in `VRequestAttributes`, and require `JOIN VRequestsApproved.RequestID = VRequestAttributes.RequestId`; amount hygiene `> 0 AND <= 1000000`; NEVER select `DurationInDays/Hours/Minutes`; `DecisionDate` is char (avoid for date math), use `SubmitDate`/`FinishDate`.
  4. **Role/permissions**: `db/sync.py resolve_user` → `users.role`; `agent_permissions` rows for the analytics agent → injects e.g. "user role=employee; agent may not call outbound tools" so the model does not offer unavailable actions.
- **Schema cache**: **(NEW)** `get_schema_cached() -> dict` (in `coreshare_db.py`) wraps the existing `get_schema()`, persisting the map in Redis (`:6379`, currently unused — reuse the same client Phase-1 opens for the `/dashboard/charts` TTL cache) with a long TTL (24h), plus a manual bust endpoint. Warmed at app startup in `main.py` lifespan so the FIRST analytics call does NOT eat the 15–60s serverless cold-start inside a live query — `get_schema()` hits `INFORMATION_SCHEMA.COLUMNS` and can itself cold-start, and that cost is paid once, off the request path.

*Data-extraction validation — identifier whitelist*

- **(NEW)** `backend/dashboard/sql_guard.py`, `validate_identifiers(sql, schema_map) -> None | dict`:
  - Parses the SELECT with `sqlglot` (dialect `tsql`) to extract referenced tables and columns, resolves aliases/CTEs, normalizes case, and checks: every `schema.table` ∈ `schema_map` keys (USABLE OBJECTS only), every qualified/bare column ∈ that table's column list, and that no `DurationInDays/Hours/Minutes` column is projected. On violation returns a STRUCTURED dict `{code:"unknown_identifier", identifier:"dbo.users", allowed_tables:[...]}` — never raw pyodbc/SQL text. Fails CLOSED: any parse error → structured `{code:"unparseable_sql"}`, never execute.
  - **Call-site ordering** in `backend/dashboard/tools.py`: `validate_select_only(sql)` (existing: SELECT-only, no semicolons/multi-statement) → `validate_identifiers(sql, get_schema_cached())` **(NEW)** → for `save_chart`, the existing `_dry_run_chart_sql` probe → `coreshare_db.run_query`. The whitelist sits AFTER select-only and BEFORE dry-run/execute.
  - **Path contrast (explicit)**: `run_metric(metric_id, date_from, date_to, group_by, filters)` executes ONLY curated SQL from `metrics.py` with baked-in amount hygiene and the correct `VRequestsApproved`↔`VRequestAttributes` JOIN; the model supplies parameters, never SQL, so it needs no identifier whitelist and is the safest surface. `query_data` / `save_chart` accept model-generated free SQL and therefore MUST pass `validate_identifiers`. The context builder biases the model toward `run_metric`; free SQL is the harder-gated fallback.
- **Structured-error surfacing**: `tools.py` returns the structured error dict as the tool-message content (still truncated to `MAX_TOOL_OUTPUT`); the `/dashboard/ask` and `stream.py` generators map it to a `{type:"error", code, message}` SSE event. The user-facing text says "I couldn't run that — [reason]" with NO SQL echoed and only the offending identifier token (e.g. `dbo.users`) surfaced.

*Output formatting — structured JSON*

- Produced by a **(NEW)** final formatting tool `format_analytics_answer`, registered in `backend/orchestrator/registry.py` (non-outbound, so it passes straight through `_tools_node` — no HARD GATE) and added to the `analytics_agent` allow-list in `templates.py` alongside `run_metric` / `list_metrics`.
  - **Why a tool, not `response_format`**: `router.complete` drives the served `qwen2.5-32b` (`tool-32b`, `localhost:9000/v1`) with `tool_choice="auto"` and falls back down `DEFAULT_CHAIN` to `fast-7b`; relying on an OpenAI-compatible `response_format`/JSON-mode guarantee is brittle across that chain. A tool with a strict JSON-schema `parameters` reuses the already-exercised tool-calling machinery, and its args ARE the structured object. `_tools_node` executes it and its return is the canonical JSON.
  - **Schema** (`parameters`): `{answer:string, supporting_data:[{metric:string, value:number|string, period:string}], confidence:number 0..1, chart_suggestion:string|null (a metric_id), follow_up_questions:[string] (2–3)}`. Every `supporting_data.value` must trace to a prior `run_metric` result in the message list (the Phase-1 "every number comes from a tool call" rule).
  - **SSE streaming**: EDIT the `/dashboard/ask` generator to stream `token` events for the friendly `answer` prose as it is produced, then emit ONE terminal event `{type:"analytics_result", payload:<full JSON>}` before `done` / `[DONE]`. `src/pages/DashboardPage.tsx` (or the ask panel) extends its existing manual-buffer SSE reader (`res.body.getReader()`, `TextDecoder`, `buffer.split("\n")`, `data:` lines, `JSON.parse`, dispatch on `parsed.type` — NOT `EventSource`) with two new types: `token` → live reply text; `analytics_result` → render `supporting_data` as chips, `confidence` as a coarse label, `chart_suggestion` as a one-click "add this chart" (calls `save_chart` then `reconcile()` = `GET /dashboard/charts`), and `follow_up_questions` as clickable prompts.

*New dependency*: `sqlglot` (parse-only SQL parser, no DB coupling) — `validate_select_only` is regex-only and the Identify requirement is to "parse referenced identifiers"; a tokenizing parser is materially safer than regex for T-SQL identifier extraction (aliases, CTEs, schema-qualified names). Regex+alias fallback noted above if the dep is rejected.

---

**Implementation Order**

1. **Schema cache** (no deps): `get_schema_cached()` in `coreshare_db.py` backed by the Redis client + startup warm in `main.py` lifespan. Confirms cold-start is paid off-path. Everything downstream reads this map.
2. **`prompt_versions` table + Alembic migration** (`db/models.py`). Independent of 1.
3. **`backend/orchestrator/prompts.py` loader** (`get_active_prompt`, `create_version`, `activate`, `rollback`, TTL cache, one-transaction mirror). Depends on 2.
4. **Seed existing prompts as version 1**: data step pulling `PRIMARY_PROMPT` (`templates.py`), the `stream.system_prompt` persona, and the Phase-1 `analytics_agent` prompt into `prompt_versions`. Depends on 3 and the Phase-1 analytics prompt existing (see *Blocked by*).
5. **Wire loaders**: EDIT `stream.py system_prompt` and the `/dashboard/ask` init to call `get_active_prompt`; leave the primary path reading `agents.system_prompt` (synced by `activate`). Depends on 3, 4.
6. **`backend/dashboard/context.py build_analytics_context`**: date + `list_metrics` + cached schema + role/permissions; appended to the analytics system message in `/dashboard/ask`. Depends on 1 and Phase-1 `metrics.py`/`list_metrics`.
7. **`backend/dashboard/sql_guard.py validate_identifiers`** + add `sqlglot`; wire into `tools.py` `query_data`/`save_chart` AFTER `validate_select_only`, BEFORE `_dry_run_chart_sql`/`run_query`; return structured errors. Depends on 1.
8. **`format_analytics_answer` tool** in `registry.py`; add to the `analytics_agent` allow-list in `templates.py`. Depends on the Phase-1 `analytics_agent` role.
9. **SSE terminal event**: EDIT the `/dashboard/ask` generator to emit `analytics_result`. Depends on 8.
10. **Admin API** `backend/routes/prompts.py` + register in `main.py`. Depends on 3.
11. **Frontend**: `PromptAdminPage.tsx` (admin) + `DashboardPage.tsx` handling of `analytics_result`. Depends on 9, 10.
12. **End-to-end structured-error test**: unknown table, `dbo.users`, `DurationInDays`, injection string. Depends on 7, 9.

---

**Risks & Pitfalls**

- **Schema-cache staleness vs. LOCKED schema**: if Azure objects drift but the cache does not, `validate_identifiers` wrongly rejects valid columns (false positive) or advertises stale columns. Mitigate with the manual bust endpoint + startup re-warm; the schema is LOCKED so drift risk is low, but a silent stale cache is worse than a cold `get_schema()`.
- **Cold start still leaks into the first `run_metric`**: warming resumes storage, but if the serverless source auto-pauses between warm and the first user query, that query still blocks 15–60s. `coreshare_db.run_query` retries ONCE on `HYT00/HYT01` after a 2s sleep, but 2s is far shorter than a 15–60s resume, so the retry can itself hit the 90s per-query limit under a bad cold start. **Demo mitigation**: fire one warm-up `run_metric` immediately before the live client interaction.
- **`activate()` / `agents.system_prompt` sync race**: if `is_active` flips but the mirror-write fails partway, versioned truth and runtime read diverge silently. Do both in ONE transaction, bust the TTL cache only after commit, and log via `events.log_event`.
- **TTL cache masks activation**: the 60s `get_active_prompt` cache would hide a just-activated prompt for up to 60s. `activate()` busts the cache explicitly — do not rely on TTL expiry.
- **`sqlglot` T-SQL / alias gaps**: CTEs, derived tables, and aliases can under- or over-extract identifiers. Over-strict rejects legitimate model SQL; under-strict could pass `dbo.users` on a parse miss. Fail CLOSED (parse error → structured reject, never execute) and test against the actual `metrics.py` SQL shapes.
- **Model emits `dbo.*` or `DurationIn*` despite context**: context biasing is soft; the whitelist is the hard gate. Never treat injection as enforcement.
- **`format_analytics_answer` not called / `STEP_BUDGET=8` exhausted**: `_route_agent` ends the run at `step>=8`, so the agent could finish without calling the tool. Server-side fallback: if the run ends with no `format_analytics_answer` call, wrap the last assistant text into a minimal `{answer, supporting_data:[], confidence:0.3, chart_suggestion:null, follow_up_questions:[]}` so the frontend contract never breaks.
- **`MAX_TOOL_OUTPUT=6000` truncation of the JSON**: a large `supporting_data` array could be truncated mid-JSON in `_tools_node`, corrupting the terminal event. Keep `supporting_data` to a handful of metrics, or bypass the 6000-char truncation for this specific tool's result in `_tools_node`.
- **Confidence is model-fabricated**: `confidence` is uncalibrated. Render it as a coarse low/med/high label, never a false-precision percentage, and never let it override the "every number from a tool call" rule.
- **Echoing SQL / driver text**: any path that lets pyodbc's message (which can embed the SQL) reach an SSE `error` event violates the no-echo rule. Centralize error→`{code,message}` mapping in one place and assert no raw driver text passes through.

---

**Blocked by**

- **Phase-1 LOCKED additions** must be BUILT first: `backend/dashboard/metrics.py` (whitelist + `list_metrics`), the `run_metric`/`list_metrics` tools, the `analytics_agent` role and its "state the number" prompt, and `POST /dashboard/ask`. Steps 4, 6, 8, 9 depend on these.
- **Section 3 (Forecasting)** — if it adds metrics/tools, the context builder's `list_metrics` injection and the `format_analytics_answer` `chart_suggestion` metric-id space must include them. Not a hard block, but reconcile the metric-id namespace before the whitelist is frozen.
- **Auth/role plumbing (existing)**: `db/sync.py resolve_user`, `agent_permissions` reads, and the trusted `x-auth-user` header from `AuthEnforceMiddleware` for the admin API — all present; dependencies, not new work.
- **Redis (`:6379`, running, currently unused)** for the schema/prompt cache — depends on the app opening a Redis client, which Phase-1 already plans for the `/dashboard/charts` TTL cache; reuse that client.

---

**Identify** — *Prompt-injection & SQL-safety when charity-staff free text flows into prompt/SQL*

The defense is a strict role/data boundary plus defence-in-depth on the SQL surface, without flattening flexibility:

1. **User text never enters the system prompt.** The versioned `analytics_agent` body plus `build_analytics_context()` output are the ONLY system-role content; the user's question, free-text follow-ups, and board names go exclusively in a user-role message and are NEVER string-concatenated into the system message. So "ignore previous instructions and DROP TABLE" is just user data the model may quote back — it cannot rewrite the persona, because the persona is a separate DB-loaded, immutable-per-version body, not a template interpolated with request data. This is precisely why versioning lives in `prompt_versions` and the loader emits a fixed body.

2. **Board names are labels, not instructions.** A board named `"'; DROP ..."` is used only as an opaque `board_id`/display string appended by code in `stream.system_prompt`, never placed where the model treats it as directive text, and never interpolated into SQL — `board_id` is a parameter to `config_db` functions, not concatenated into any query.

3. **The safest surface is `run_metric`.** The model supplies only `metric_id` + typed `date_from`/`date_to`/`group_by`/`filters`; the SQL is curated in `metrics.py` with parameterized/whitelisted filter values, so injection in a filter value cannot alter query structure. The context builder biases the model here first.

4. **Free SQL (`query_data`/`save_chart`) is gated in three independent layers before execution:** (a) `validate_select_only` — must start `SELECT`, forbids `INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/PRAGMA/CREATE/REPLACE/TRUNCATE/GRANT/EXEC/MERGE`, no semicolons/multi-statement (kills stacked-query injection); (b) **(NEW)** `validate_identifiers` — parses the statement and rejects any table/column outside the cached USABLE-OBJECTS whitelist, so `SELECT * FROM dbo.users` returns `{code:"unknown_identifier", identifier:"dbo.users"}` (`dbo.*` is excluded and returns no rows anyway); (c) the **read-only DABS-CORE-SHARE grant** — even if both parsers were bypassed, the connection physically cannot write. No single bypass is catastrophic.

5. **Flexibility preserved.** Because the whitelist derives from the LIVE cached schema map (all columns of every usable `DataShare.*`/`Analytics.*` object), the model can still write arbitrary analytic SELECTs — joins, aggregates, `GROUP BY`, `SubmitDate`/`FinishDate` filters — as long as every identifier is real. We reject unknown *identifiers*, not unknown query *shapes*, and we do NOT keyword-blacklist WHERE text (which would break legitimate questions containing words like "drop-off").

6. **Never echo raw SQL or driver errors.** All failures map to structured `{code, message}` SSE events; the charity-staff persona already forbids mentioning SQL. This denies an attacker the error-oracle feedback loop. Error payloads carry only the offending identifier token (capped/escaped), never the full statement.

7. **Cap, normalize, fail closed.** `validate_identifiers` lowercases/normalizes identifiers before comparison and rejects on any parse error, so obfuscated/malformed SQL is refused rather than executed. Combined with `MAX_TOOL_OUTPUT` truncation and the read-only grant, the blast radius of a fully-compromised model turn is at most a read of already-authorized, whitelisted charity data — never a write, never `dbo.*`, never a system-prompt override.

---

<a id="cross-cutting--exact-order"></a>

# Cross-Cutting Concerns & Exact Implementation Order

# Consolidation: Cross-Cutting Layer + Global Build Order

Lead-architect pass over Sections 1–5. All module/table/port references are to the live DGX host (`matrix@100.107.179.44`) ground truth. Nothing below re-debates a LOCKED Phase-1 decision; it binds the five sections into one buildable plan.

---

## A) Cross-Cutting Concerns

### 1. Secrets management for multiple model providers

**Reuse the existing encrypted-token posture; do not invent a second one.**

- OpenAI/Anthropic API keys are written **only** into `model_configs.api_key_enc` (Section 1's new Postgres table), ciphertext produced by `backend/services/token_crypto.py` (Fernet, keyed by `TOKEN_ENC_KEY`). This is the *same* primitive already used for provider OAuth tokens — no new crypto, no new key. `TOKEN_ENC_KEY` itself continues to live in `~/aganetiAi/.env` (`chmod 600`), never in Postgres, never in a config table.
- **Decrypt at router call time only.** `router._client_for(model_key)` calls `token_crypto.decrypt(api_key_enc)` when constructing the cached `AsyncOpenAI`/`AsyncAnthropic` client, and the plaintext lives for the lifetime of that client object in `router._clients` — it is never serialized, never logged (the `_log()` → `events.log_event("llm_call", …)` path records `model_key`, tokens, and `cost_micros`, never the key), and never placed in `events.meta`.
- **The key is write-only across the API boundary.** `POST /models` accepts `api_key`, encrypts, stores. `GET /models` and `PATCH /models/{model_key}` return the row with `api_key_enc` **redacted to `null`/`"***set***"`** — even the ciphertext never crosses the wire. The frontend `ModelsSettings.tsx` password field submits a key and can show "key is set" state, but has no read-back path. A naive `SELECT *` serializer is the specific failure to guard against (Section 1 Risk "Key leakage").
- **Admin-only mutation, enforced server-side.** `POST/PATCH/DELETE /models` read the trusted `x-auth-user` that `AuthEnforceMiddleware` injects (it strips any client copy at enforce.py ~127–128 first), resolve `users.role` via `db/sync.py resolve_user`, and require `admin`. No client can self-elevate to register a rogue key-bearing endpoint.
- `INTERNAL_API_TOKEN` continues to gate internal POSTs; the model-probe on create (models-list/ping, never a completion) uses the just-submitted key in-process and discards it if the probe fails — a failed probe should **not** persist the key.
- **Key-rotation caveat (document, do not automate):** rotating `TOKEN_ENC_KEY` invalidates every `api_key_enc` (and every existing provider token). No auto-rotation. If rotated, all model keys must be re-entered through `POST /models`.

### 2. Rate limiting & cost control for API models

**The cost spine already exists — wire caps onto it, don't rebuild it.**

- `router._log()` already emits `events.log_event("llm_call", name=model_key, …, meta={tokens_in, tokens_out, cost_micros})` into the Postgres `events` table, and `events.py` already aggregates `per_agent`/`summary` over `/analytics/*`. `cost_micros` is computed from `MODELS[key].cost_in/cost_out` (micro-USD per 1k tokens; **0 for the three local vLLM keys**).
- **Per-provider costs are mandatory on create.** `POST /models` must reject a `local`/`openai`/`anthropic` model with `cost_in==0 && cost_out==0` for non-local `type` (Section 1 Risk "Cost-logging holes") — otherwise paid calls silently log `cost_micros=0` and under-report spend. Seed realistic values (e.g. `gpt-4o`, `claude-sonnet-4-5`) at registration.
- **Daily cost cap = a read against the existing aggregation, checked before the paid call.** Add a cheap `events.py` helper `cost_since(org_id, user_id, since=start_of_day_utc) -> micros` (a `SUM(cost_micros)` over `events WHERE kind='llm_call'`). In `router.complete()`, *before* dispatching to a `type in {openai, anthropic}` client, if the resolved model has non-zero cost and `cost_since(...) >= ORG_DAILY_CAP_MICROS` (env, e.g. `AGANETI_DAILY_COST_CAP`), **skip that candidate and fall through `plan()` toward the local `DEFAULT_CHAIN`** rather than erroring. This turns a budget breach into a graceful downgrade to free local inference, not a 500 — consistent with the "never raise into the SSE path" property.
- **`RateLimitMiddleware` (backend/ratelimit.py) stays the request-rate gate**; the cost cap is the spend gate. They are orthogonal — keep both.
- **Local vLLM is the default and the demo floor.** `DEFAULT_CHAIN=["tool-32b","fast-7b"]` is all-local, `cost_micros=0`. Paid providers are reachable **only when explicitly pinned per-run** (Section 1 ASSUMPTION: registering a model does not touch `DEFAULT_CHAIN`). So the steady-state and every fallback tail cost ~$0; a paid model is a deliberate, capped, per-turn opt-in.

### 3. Testing strategy — mock vs live

| Surface | CI / unit tests | Pre-demo live | Rationale |
|---|---|---|---|
| `metrics.py` SQL, `forecast.py` OLS/gate/dense-fill, `confidence.score()`, `sql_guard.validate_identifiers` | **MOCK** the Azure engine with a fixture dataset (a fake `coreshare_db.run_query` returning canned `list[dict]`). These are pure/deterministic — test 0-row, all-null-money, `n=4` (widest CI), `n<4` (gate rejects), missing-month 0-fill, `dbo.users` reject, `DurationInDays` reject, injection string. | — | No Azure dependency, fast, hermetic. Forecast t-table and clamp-to-≥0 are pure numpy. |
| Anthropic adapter (`_complete_anthropic` + normalise-back) | **MOCK** `AsyncAnthropic` with a canned `tool_use`/`text` response; assert the round-tripped `assistant_msg_dict` is byte-identical in shape to the OpenAI `_normalise()` output, including a stable `tool_call_id` across assistant→tool→next-assistant and the outbound HARD-GATE `resume(approved)` swap. | **ONE real Anthropic call** validated before the demo (a single tool round-trip), to catch SDK/version drift. | Section 1's #1 risk is tool-call-id drift; the shape contract must be unit-locked, but a real call proves the wire. |
| `tool-32b` (`qwen2.5-32b` @ `localhost:9000/v1`), the analytics agent, `/dashboard/ask` E2E | — | **LIVE** — this is the demo. Run the real graph, real router, real Redis. | The exact-number Q&A on the local model IS the product. Never mock it for the demo dress rehearsal. |
| Azure `DABS-CORE-SHARE` read path | Mocked in CI. | **LIVE + warmed.** Fire a warm-up query (`run_metric("approvals_count")` or a curl) minutes before the client session; verify ≥6–9 months of `FinishDate` history exists in `VRequestsApproved`/`VRequestAttributes` (forecast gate needs ≥4 mature months). | Serverless auto-pause cold start is 15–60s; the demo must pay it off-path. |
| Redis cache (render TTL, schema cache, forecast actuals) | Mock or fakeredis in CI. | **LIVE** — pre-warm the schema cache (`get_schema_cached()` at `main.py` lifespan) and the render cache (one `GET /dashboard/charts` per demo board). | Every "cheap" path (auto-refresh, explain no-requery, forecast reuse) depends on a populated Redis. |
| Prompt versioning `activate()`/mirror, admin API | **MOCK/real Postgres** (the one-transaction flip + `agents.system_prompt` mirror; assert cache bust after commit). | Smoke-test one activate + rollback on staging. | Section 5's activate race is a correctness bug, not a demo risk — cover in unit. |

**Rule of thumb:** mock everything that costs money or cold-starts (Azure, paid LLMs); run live everything that is the demo's on-stage happy path (local qwen, real aid numbers, real charts, Redis). Validate exactly **one** real paid-provider call and **one** warm Azure query before the client arrives.

### 4. What to FAKE/STUB vs what must be REAL for the live demo

**REAL (non-negotiable, on stage):**
- Local `qwen2.5-32b` (`tool-32b`) driving the `analytics_agent` behind `POST /dashboard/ask` — the multi-step exact-number reasoner (Section 2).
- Real Azure aid-request numbers via the LOCKED `metrics.py` whitelist (expenditure/approvals/by-emirate/by-category/monthly-trend/yesterday), every money answer JOINed `VRequestsApproved.RequestID = VRequestAttributes.RequestId`, hygiene `>0 AND <=1000000`.
- Real hand-rolled charts (`DashCharts.tsx`) rendered from live `GET /dashboard/charts`.
- Redis render cache + auto-refresh (Section 4) and the schema cache (Section 5) — real, pre-warmed.
- `sql_guard.validate_identifiers` on the free-SQL path — real (it's a safety gate, cheap, no external dep beyond `sqlglot`).

**STUB / OPTIONAL / "it also supports…" toggles (nice-to-have, degrade gracefully if skipped):**
- **Paid API models (GPT/Claude)** — a "look, it also supports GPT-4o / Claude" `ModelSelector` toggle. Registered, cost-capped, but **not** the default and **not** in `DEFAULT_CHAIN`. If the paid key or network is flaky on demo day, the selector silently falls back to local via `plan()` — the answer is identical because `analytics_agent` is model-agnostic (numbers come only from `run_metric`).
- **Forecasting (`run_forecast`)** — real math, but every output labelled `"projected (trend only; seasonal effects such as Ramadan not modelled)"`. Ship the tool + `/ask` text answer; the `ForecastChartI` SVG is an explicit stretch item — cut it if time is short.
- **Prompt-versioning admin UI (`PromptAdminPage.tsx`)** — backend loader (`get_active_prompt`) should be live so prompts load from `prompt_versions`, but the admin **UI** and rollback flow are post-checkpoint polish, not on the demo happy path.
- **`format_analytics_answer` structured JSON** — nice for the frontend chips/follow-ups, but the server-side fallback (wrap last assistant text into a minimal payload) guarantees the demo works even if the model never calls the tool.

### 5. Rollback plan if the Azure SQL relay DROPS mid-demo

The relay chain is Tailscale → GCP VM socat → Azure serverless. Any hop can drop; serverless can also auto-pause. **The demo must degrade to stale-but-labelled data, never a red error.**

1. **Detection.** `coreshare_db.run_query` already sets `pool_pre_ping=True`, a 90s per-query timeout, 30s login timeout, and retries **once** on cold-start `HYT00`/`HYT01` after a 2s sleep. Treat *any* surviving exception from `run_query` as "source unavailable" — do not distinguish causes on stage.
2. **Redis serves last-good.** The Phase-1 render cache (`/dashboard/charts`, TTL 30–60s) and the forecast actuals cache (TTL ~300s) already hold recent rows. On a live-query failure, the `/dashboard/charts` render path must **return the last cached payload with a `stale: true, as_of: <ts>` flag** instead of surfacing the exception. The frontend renders the chart normally plus a `t-caption` "as of HH:MM — refreshing" note (reuse the `generated_at`/`cache_hit` footer pattern from Section 4's explain panel).
3. **Pre-warmed snapshot.** Before the client session, run each demo board once so every curated metric is cached, and additionally **persist a JSON snapshot of those rendered payloads to disk** (a `demo_snapshot.json` in the scratchpad/board dir). This is the floor below the TTL cache.
4. **Read-only fixture fallback mode.** A single env flag (`AGANETI_FIXTURE_MODE=1`) makes `coreshare_db.run_query` short-circuit to the disk snapshot / a fixture dataset **without touching the relay at all**. Flip it only if the relay is confirmed dead; every number shown is then real-but-frozen aid data, clearly labelled "demo snapshot".
5. **What the UI shows — the contract:** stale/snapshot data with a visible "as of <time>" label and a non-blocking "Connection interrupted — showing last refreshed data" toast (Section 4 Identify §3 "restart, not resume"). **Never** a hard error card on a chart tile, **never** a raw pyodbc/driver string (Section 5 no-echo rule — all failures map to `{code, message}` SSE events with no SQL text). `run_metric` in the analytics path returns its `status:error, reason:warming, retry_hint:"run the SAME metric again"` envelope (Section 2), so the agent asks to retry rather than fabricating a fallback number.
6. **Recovery is idempotent.** When the relay returns, the next auto-refresh tick (`reconcile()` = one `GET /dashboard/charts`) or a manual re-ask re-populates the cache and drops the `stale` flag — no restart needed.

---

## B) Exact Implementation Order (global, all 5 sections + cross-cutting)

Dependencies are named by step number. **Tracks A/B/C/D/E can proceed in parallel** — a step lists its track and blockers. Front-loaded so the exact-number Q&A on the local model + live dashboard + cache works **before** paid providers, forecasting, or prompt UI.

> **GATE 0 — Phase-1 LOCKED must already exist** (assumed built, verify first): `backend/dashboard/metrics.py` (whitelist + `run_metric`/`list_metrics`), the `analytics_agent` role + "state the number" prompt in `templates.py`, `POST /dashboard/ask` + its SSE generator, and the **Redis TTL render cache** on `GET /dashboard/charts`. Steps 1, 4, 6, 12, 13, 16, 18, 20 depend on these. If any is missing, build it before its dependents.

**Foundational (do first, unblocks the most):**

1. **Shared Redis client helper** — a single `get_redis()` used by *all* cache consumers (render cache, schema cache, forecast actuals, model-config version key). *Sections: cross-cutting / 1,3,4,5.* Depends on GATE 0's render cache work (or extract it here). **Blocks 3, 8, 11, 15.** No parallel conflict — everyone imports it.

2. **`M.ModelConfig` model + Alembic migration for `model_configs`**, seeded with the three static keys (`tool-32b`, `fast-7b`, `vision-vl`). *Section 1, step 1.* Depends on nothing. **Track A.** ‖ with 5, 7, 9.

3. **Schema cache** `get_schema_cached()` in `coreshare_db.py` (Redis-backed, 24h TTL, manual-bust) + `main.py` lifespan warm. *Section 5, step 1.* Depends on 1. **Track E.** ‖ with Track A/B.

**Core happy-path — local analytics agent (Track B, the demo spine):**

4. **`confidence.py`** `score(rows, money_col, date_col, hygiene_dropped)` — pure function. *Section 2, step 1.* Depends on nothing. ‖ everything.

5. **`registry.py`: add `is_clarify: bool=False` to `Tool`.** *Section 2, step 2.* Depends on nothing. Must precede 6. ‖ with 2, 7.

6. **`graph.py` edits (single coordinated edit):** per-run `step_budget` in `_route_agent` (parenthesized: `step >= (state.get("step_budget") or STEP_BUDGET)`); `clarifying` + `step_budget` state fields; `_tools_node` clarify branch; `_route_tools` end-on-`clarifying`-or-`awaiting`; **front-load status/confidence contract**; and the **`MAX_TOOL_OUTPUT` bypass for `format_analytics_answer`** (Section 5) folded into the same truncation edit. *Sections 2 + 5.* Depends on 5. **This is the one place `_tools_node` truncation is touched — Sections 2 and 5 must land it together to avoid conflicting edits.**

7. **`metrics.py` envelope:** wrap `run_metric` in `ok`/`empty`/`warming` structured returns (front-loaded status/confidence), call `confidence.score()`, log via `timed("tool_called", …)` with `run_id` in ctx. *Section 2, step 4.* Depends on 4 + GATE 0's `metrics.py`. ‖ with 6.

8. **Selector/model resolution backend (Section 1 core):** `models_repo.py` (`list_active`/`get`, encrypt/decrypt via `token_crypto`) → `router._load_models` + `_client_for` + short-TTL cache (static dict fallback on PG/Redis blip) → run-pin in `graph.py`/`run_turn`/`astream_turn` (resolve `model_key` **once** at entry) → `plan()` guard (drop disabled/unknown keys, skip `caps.ctx` < token estimate). *Section 1, steps 2,3,5.* Depends on 1, 2. **Track A.** ‖ with Track B (4–7).

9. **`tools.py`: register `ask_clarification` (`is_clarify=True`); define canonical `ANALYTICS_TOOL_NAMES`.** *Section 2, step 5.* Depends on 5. **⚠ This list is the single source of truth for the analytics allow-list — Sections 3 (`run_forecast`) and 5 (`format_analytics_answer`) append to THIS constant, not a fork.** ‖ with 6, 7.

10. **`templates.py`: extend `analytics_agent` prompt** (clarify rule, confidence-reading rule, two-empties-then-stop, reaffirm "no collection data"). *Section 2, step 6.* Depends on 7, 9.

11. **`stream.py` `ask_stream()`** — new SSE generator emitting `tool_result`/`confidence`/`clarify`, threading `run_id` into `ctx`, `recursion_limit=3*step_budget` (=36 for budget 12), throwaway warm-up `run_metric` at start. *Section 2, step 7.* Depends on 6,7,9,10 + 1 (Redis). **Track B integration point.**

12. **Selector plumbing:** accept optional `model_key` in `/dashboard/ask` and `/dashboard/chat` bodies → validate against active `model_configs` → write once into initial `AgentState.model_key`. *Section 1, step 7.* Depends on 8, 11 + GATE 0's `/dashboard/ask`.

13. **Frontend: analytics SSE event arms** in `useStream.ts` + `DashboardPage.tsx` — `tool_call` chip, `tool_result` done/fail, `confidence` badge, `clarify` bubble (no Approve/Reject). *Section 2, step 8.* Depends on 11. **Track F (frontend), ‖ with backend once 11 lands.**

> ### ⛳ DEMO-READY CHECKPOINT
> After steps 1–13: a user asks an aid-data question on the dashboard → the local `qwen2.5-32b` `analytics_agent` runs a real multi-step chain over live Azure numbers → streams thinking/tool-run/confidence → states the exact number in words, with clarify + low-confidence honesty, served through the Redis-cached render path. **This is the core demo. Everything below is enhancement and can slip without breaking the on-stage story.**

**Enhancements (parallelizable after checkpoint):**

14. **Anthropic adapter:** add `anthropic` SDK to `~/aganetiAi/.venv`; `_complete_anthropic` (message/tool translation + normalise-back to identical `assistant_msg_dict`); unit-test tool round-trip + outbound HARD-GATE pause→`resume(approved)` id stability; one real call validated. *Section 1, step 4.* Depends on 8. **Track A.** ‖ with 15–20.

15. **`backend/routes/models.py` CRUD** (admin-gated, key-redacted, cheap probe-on-create, soft-disable) + `main.py` `include_router`. *Section 1, step 6.* Depends on 2, 1. **Track A.** ‖.

16. **Cost-control + fallback logging:** `events.cost_since()` helper + daily-cap check in `router.complete()` before paid dispatch; extend `_log()` with `meta.selected_model_key`/`fallback_from`/`actual_model_key`; enforce non-zero cost on `POST /models`. *Section 1 step 9 + cross-cutting §2.* Depends on 8, 15.

17. **Frontend model settings + selector:** `ModelsSettings.tsx` (admin CRUD, write-only key field) + `ModelSelector.tsx` (per-request `model_key` in SSE body). *Section 1, step 8; mounted in header per Section 4 step 6.* Depends on 15, 12. **Track F.**

18. **Forecasting:** expose `metrics.py` expenditure/approved-count base fragments as importable defs (step 1) → `forecast.py` (`build_monthly_sql`/`_dense_months`/`_trim_immature`/`_gate`/`_ols_forecast`, numpy + t-table, clamp ≥0, wider 9-month raw pull) with unit tests → `run_forecast_handler` (Redis actuals cache, hard-abort on `run_query` exception) → `register_forecast_tool()` in `__init__.py`, append `"run_forecast"` to `ANALYTICS_TOOL_NAMES` (step 9's constant) + `analytics_agent` tools → `/ask` prompt "projected" rules. *Section 3, steps 1–6.* Depends on 1, 9, 11 + GATE 0's `metrics.py`. **Track C.** ‖ with A/D/E.

19. **`sql_guard.py` + `format_analytics_answer`:** `validate_identifiers(sql, get_schema_cached())` (sqlglot, fail-closed, structured errors) wired into `tools.py` AFTER `validate_select_only`, BEFORE `_dry_run`/`run_query` → `format_analytics_answer` tool in `registry.py`, appended to `ANALYTICS_TOOL_NAMES` + `analytics_agent` → `/dashboard/ask` emits terminal `analytics_result` event (with server-side fallback wrapper if tool uncalled) → frontend renders chips/confidence/chart_suggestion/follow-ups. *Section 5, steps 7,8,9,11-frontend.* Depends on 3, 6 (`MAX_TOOL_OUTPUT` bypass), 9. **Track E.**

20. **Dashboard UX panel:** `ChartDetailPanel.tsx` (client-paginated table over in-memory `chart.data`, no new DB endpoint) → `GET /dashboard/charts/{id}/explain` + `explain.py` (`llm.chat`, reads Redis render cache, single-`run_query` fallback, `explain:{id}:{sha256(normalized_sql)}` key) → per-chart auto-refresh (`DASH_REFRESH_KEY`, ONE poll loop reusing `reconcile()`) → SSE abort-on-unmount + `finally`-reconcile hardening. *Section 4, steps 1–5,7.* Depends on 1 (render cache), 12 (`/dashboard/ask` follow-up). **Track D.** ‖ with A/C/E.

21. **Prompt versioning:** `prompt_versions` table + migration → `prompts.py` loader (`get_active_prompt` 60s TTL, `create/activate/rollback`, one-transaction `agents.system_prompt` mirror, cache-bust-after-commit) → seed existing 3 prompts as v1 → wire `stream.system_prompt` + `/dashboard/ask` init to `get_active_prompt` → `context.py build_analytics_context` (date + `list_metrics` + cached schema + role/perms) → admin API `routes/prompts.py` → `PromptAdminPage.tsx`. *Section 5, steps 2–6,10,11.* Depends on 3 (schema cache), 10 (analytics prompt exists), GATE 0. **Track E, post-checkpoint.**

22. **(Stretch) `ForecastChartI`** SVG + `_dry_run_chart_sql` `"forecast"` alias tolerance + `DashboardPage` render switch. *Section 3, step 7.* Depends on 18, 20. **Cut first if time-constrained.**

23. **End-to-end dress rehearsal against pre-warmed Azure:** exact-number chain, clarify path, forecast (sufficient + gated), structured-error (`dbo.users`/`DurationInDays`/injection), model-swap-same-answer, relay-drop → stale-labelled fallback. *All sections.* Depends on all.

---

### Cross-section conflicts & open assumptions

1. **ModelSelector semantics collide (S1 ↔ S4).** S1 makes the selector a **per-request `model_key`** in the SSE body, pinned once per run (never mutating shared state mid-loop). S4 describes the selector as writing **`agents.model_key`** globally via a "Section-1 endpoint." **Resolution: adopt S1's per-request pin as the demo behavior.** Mutating `agents.model_key` during a live demo would change the model for *all* users/runs and re-persist on every click. S4 should mount S1's `ModelSelector` and let it set the per-request body value (precedence: request body > board/session default > `agents.model_key` > `DEFAULT_CHAIN`). The persistent-agent-config endpoint is a separate admin action, not the header dropdown.

2. **Single `ANALYTICS_TOOL_NAMES` (S2 vs S3 vs S5).** S2 defines `[list_metrics, run_metric, ask_clarification, current_time]`; S3 adds `run_forecast`; S5 adds `format_analytics_answer`. **Resolution: one canonical constant in `tools.py`** (built in step 9) = `[list_metrics, run_metric, ask_clarification, run_forecast, format_analytics_answer, current_time]`, with S3/S5 appending to it, not forking. All are `is_outbound=False` (no HARD GATE) — verified. Do **not** add any of these to `PRIMARY_TOOLS` (Aria must never forecast or query).

3. **Step budget referenced two ways (S2 ↔ S5).** S5's `format_analytics_answer` fallback text says "`_route_agent` ends the run at `step>=8`," but S2 raises the analytics budget to **12** per-run. **Resolution: S5's "did the run end without calling the format tool?" fallback must key off the per-run `step_budget` (12 for analytics), not the literal 8.** The `_route_agent` change (step 6) is `step >= (state.get("step_budget") or STEP_BUDGET)`; the parentheses are load-bearing (S2 Risk). Also confirm `recursion_limit=3*step_budget` (=36) in `ask_stream`, or LangGraph raises `GraphRecursionError` mid-stream.

4. **Two notions of "confidence" (S2 ↔ S5).** S2 computes a **heuristic confidence envelope** in `run_metric` (row count, null ratio, date coverage, hygiene_dropped). S5's `format_analytics_answer` schema has a **model-supplied `confidence: 0..1`**. These can contradict on stage (model says 0.9, tool computed "low"). **Resolution: `format_analytics_answer.confidence` must be derived from / bounded by the last `run_metric`'s computed `confidence.level`, not freely invented by the model** — prompt the model to copy the tool's level, and render as a coarse low/med/high label (never a false-precision %). S2's computed envelope is the source of truth; S5's field is a carrier, not an independent judgment.

5. **`_tools_node` truncation edited by two sections (S2 ↔ S5).** S2 front-loads `status`/`confidence` so truncation only eats the row dump; S5 wants to **bypass** the 6000-char cap for `format_analytics_answer`'s JSON result. **Resolution: land both in the single coordinated `graph.py` edit (step 6).** Front-load everywhere + special-case `format_analytics_answer` (identify by tool name) to skip truncation. Two independent edits to the same function will conflict.

6. **`agent_id` string inconsistency (S2 ↔ S4).** S2 uses `agent_id="analytics"`; S4's `explain.py` logs `agent_id="analytics_agent"`. **Resolution: pick one — recommend `agent_id="analytics"`** (S2 owns the run-time path and sets step budget explicitly in state, never inferred from the string). S4's explain logging should match, or the `/analytics/*` per-agent aggregation splits the same logical agent across two names.

7. **Explain uses `llm.chat`, not the graph (S4) — model resolution source.** S4's `explain.py` calls `router.llm.chat(agent={model_key, fallback_models})` directly. **Resolution: it must resolve `model_key` through S1's merged `_load_models`** (so a pinned/registered model is honored and cost is logged via the same `_log()` path), not read a stale hardcoded key. Contract unchanged; just source the key from the S1 layer.

8. **Shared Redis client is an implicit cross-section dependency.** S1 (model-config version key), S3 (forecast actuals), S4 (render + explain), S5 (schema cache) all say "reuse the Phase-1 Redis client." **Resolution: build it once (step 1) as `get_redis()` and import everywhere.** If four sections each open their own client, connection handling and the demo-day relay-drop fallback diverge. This is why step 1 is first.

9. **Open assumption — `run_id` in `ctx` (S2).** Today the executor builds `ctx={user_id, agent_id}`; S2/S3/S5 all want `run_id` for audit `meta`. **Resolution: `ask_stream` opens the `agent_runs` row and adds `run_id` to `ctx` (step 11);** all three sections' handlers read it from there. Confirm the executor passes the extended `ctx` through unchanged (it does — `ctx` is opaque to `_tools_node`).

10. **Open assumption — `sqlglot` dependency (S5).** S5 gates on `sqlglot` being acceptable, with a regex-fallback failing closed. **Resolution: approve `sqlglot` (parse-only, no DB coupling)** — it is materially safer than regex for T-SQL alias/CTE/schema-qualified identifier extraction, and the free-SQL path is a real injection surface. If rejected at deploy time, the fail-closed regex fallback ships instead; either way `query_data`/`save_chart` must not execute on a parse miss.
