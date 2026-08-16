# Target Architecture v1.0 — Gap Analysis

**READ-ONLY AUDIT.** No source, configuration, Docker, database, migration or test file was modified.
Nothing was staged or committed. Nothing discovered here was "fixed".

| | |
|---|---|
| Repository | `/home/matrix/aganetiAi` · branch `feat/enterprise-agentic-os` · HEAD `432fd5e` |
| Working tree | 82 uncommitted files from several developers — inspected, untouched |
| Method | 16 parallel read-only auditors (14 domains + contracts + CI gates) with an adversarial completeness critic, plus orchestrator verification of the 12 contracts, the CI configuration and every contradiction |
| Matrix | `target_architecture_v1_gap_matrix.json` — 268 records, every non-MISSING one carrying file:line, symbol, service, table, route or test evidence |
| Inventory | `current_platform_inventory.md` |

**Confidence caveat, stated up front:** the domain auditors self-reported `high` confidence on all
224 of their findings. That uniformity is not calibrated. An adversarial completeness critic
re-opened 20+ of the highest-stakes claims and found one outright false finding and several
unexamined surfaces; its corrections were then verified again by the orchestrator and are recorded
in §8.1. Records that survived verification are marked high; the rest should be read as "evidenced,
not adjudicated".

**Evidence caveat:** `backend/main.py` was being edited by another developer *during* this audit, so
citations into that file past roughly line 1400 may be 13–14 lines low. Symbols and behaviour are
correct; exact line numbers there should be re-resolved before use.

---

## 1. Executive summary

The platform is **substantially more built than a greenfield**, and **structurally single-tenant**.
Those two facts, together, define the whole gap.

What exists is real: a working LangGraph agent executor with a hard human-approval gate that
survives restarts, a typed tool registry, a centralized authentication middleware with IDOR
protection and tests, an encrypted OAuth token layer, a durable idempotent document-indexing state
machine, and a frozen, production-wired, thirteen-step-evaluated GraphRAG pipeline. None of this
should be rebuilt.

What is absent is the entire **product architecture**: not one of the twelve frozen interface
contracts exists by name, there is no pack/plugin/manifest/entitlement layer of any kind, and there
is no tenant. `org_id` is a column on ~25 Postgres tables that is faithfully **written** and
**never read as a filter** — a repository-wide search for `org_id ==` finds only a verification
script. The identifier `tenant_id` does not appear in application code at all. The enforced
isolation boundary everywhere is `user_id`.

Three findings dominate everything else:

1. **Customer behaviour is compiled into core.** An entire `backend/dashboard/` package is hardwired
   to one customer's Azure SQL database, one customer's aid-request schema is written into shared
   prompt constants, the single chat router branches on charity-domain nouns including Arabic
   `زكاة`, and identity defaults name a customer (`DEFAULT_ORG_SLUG = "meerana"`,
   `ALLOWED_EMAIL_DOMAINS = "meerana.ae"`). **One signed artifact cannot serve two customers today
   without a code fork.** This is the target architecture's defining requirement, and it is the
   requirement furthest from being met.

2. **Nearly every capability exists twice.** Two agent runtimes, two tool registries, two model
   gateway clients, three delegation implementations, two OAuth token stacks, two memory systems of
   record, two evaluation entrypoints, two Postgres databases with divergent schemas. 22 records are
   classified `DUPLICATED`. The duplicates disagree on security-relevant behaviour — `web_search` is
   approval-gated on one path and auto-executed on the other — so "which code runs" is currently a
   security question, not a style question.

3. **There are live cross-user data exposures**, independent of the tenancy gap.
   `/observability/sessions` and `/observability/trace/{session_id}` return any user's chat content
   to any authenticated caller; `/auth/provider/status` and `DELETE /auth/provider/{provider}` sit
   on the middleware's unauthenticated public-prefix list while accepting a caller-supplied
   `user_id`. These are P0 regardless of the architecture programme.

A fourth finding is about the audit's own foundations: **`git ls-files docs` returns 0 and
`reports/` is gitignored**, so the thirteen-step GraphRAG evaluation artifact set and 58 of the 174
golden cases exist **only in this uncommitted working tree**. The requirement that those artifacts
"remain reproducible" is currently one `git clean` away from being false.

**Recommended sequencing:** Phase 0 establishes contracts + `TenantContext` + CI gates + the
governance foundations, because every other track depends on tenant identity existing. Four tracks
can then run in parallel. Nothing should be built on top of the current duplication — the
consolidation decisions in §7 are prerequisites, not cleanup.

---

## 2. Current architecture as actually implemented

A single uvicorn process (`deploy/aganeti-api.service:11`, no `--workers`) running FastAPI +
APScheduler + aiogram on one event loop. Background work is 18 in-process `scheduler.add_job`
registrations against a `MemoryJobStore`. Durability comes from re-deriving work from database rows
on boot, not from a queue.

```
                    ┌──────────────────────── single uvicorn process ────────────────────────┐
   React UI ──────► │  /api/chat ──► main.py NATIVE_TOOLS loop ──► backend/tools.py (34)     │
                    │                    (guardrails; approval NOT enforced)                 │
                    │                                                                        │
   /dashboard/* ──► │  chat/unified.py route() ──► LangGraph graph.py ──► registry.py (36)   │
                    │       regex lane router          (is_outbound approval gate)           │
                    │                                                                        │
                    │  ContextBuilder ──► graph | corporate | memory | tasks | calendar …    │
                    └───────┬──────────────┬───────────────┬──────────────┬─────────────────┘
                            │              │               │              │
                    services/llm.py   orchestrator/    Qdrant         Neo4j
                     (httpx→LiteLLM)   router.py       (user-scoped)  (NO scoping)
                            │          (OpenAI SDK)
                            ▼               ├──────────► Azure OpenAI  ◄── the one true bypass
                      LiteLLM :4000         └──────────► LiteLLM :4000
                            ▼
                        vLLM :9002
```

Datastores: PostgreSQL (application, alembic), Supabase Postgres (auth, RLS), Qdrant, Neo4j, **two
SQLite files** (`tasks/tasks.db`, `backend/dashboard/dashboard_configs.db`), SeaweedFS, and flat
JSON under `MEMORY_DIR`. Full inventory in `current_platform_inventory.md`.

The project's own canonical spec (`project_overview/ENTERPRISE_AGENTIC_OS.md:5`) describes the
system as **"On-prem, single tenant, multi-tenant-ready schema"**. The implementation matches that
description precisely. The gap to Target Architecture v1.0 is therefore a **deliberate design
delta**, not an oversight — which matters, because it means the fix is a change of intent, not a
bug fix.

That same spec has already diverged from the code in three places worth recording: locked decision
#1 ("Postgres now — migrate off SQLite `tasks/tasks.db`") is unfinished, since five modules still
write that file; the declared stack ("Redis pub/sub + Arq async workers") exists nowhere; and
declared Postgres RLS exists only in the Supabase schema, user-scoped.

---

## 3. Target architecture

> Core provides capabilities. Packs provide customer behaviour. Plugins provide external
> connectivity. The Control Plane manages configuration. The Runtime executes manifests. Gateways
> enforce policy.
>
> The same **signed platform artifact** must deploy to Customer #1 and Customer #2. Customer
> difference may only be expressed as tenant configuration, entitlements, packs, overlays, approved
> plugins/connectors, model profile, or deployment profile. Customer-specific core source changes,
> patches, branches or manual modifications are not allowed.

Twelve frozen interface contracts (§9) and eight mandatory CI gates (§21) enforce that separation.

---

## 4. Capability-by-capability gap matrix

245 records in `target_architecture_v1_gap_matrix.json`.

| Status | Count | Meaning here |
|---|---:|---|
| `EXISTS` | 69 | present and usable as-is |
| `PARTIAL` | 60 | present but incomplete against the target |
| `MISSING` | 53 | absent |
| `WRONG_ARCHITECTURE` | 30 | present but structurally incompatible with the target |
| `DUPLICATED` | 22 | implemented more than once, divergently |
| `DEPRECATED` | 11 | superseded, dead, or actively misleading |

| Priority | Count |
|---|---:|
| **P0** — architecture/blocking foundation | **84** |
| P1 — required for enterprise platform | 81 |
| P2 — important capability | 66 |
| P3 — future enhancement | 14 |

| Domain | Records | Headline |
|---|---:|---|
| A. Multi-tenancy | 17 | org column written, never enforced; cross-user route exposures |
| B. Model Gateway | 16 | one live provider bypass; two parallel gateway stacks |
| C. Agent runtime | 17 | real LangGraph executor exists — and a second runtime ships to users |
| D. Multi-agent | 16 | three delegation implementations; inbox address space split |
| E. NATS readiness | 17 | no broker of any kind; `events` is a log, not a bus |
| F. MCP / tools | 15 | MCP removed; two tool registries with divergent approval semantics |
| G. Connectors | 17 | M365 + Google real; no SAP/Oracle/Jira/ServiceNow/Teams/SharePoint |
| H. Knowledge/GraphRAG | 14 | frozen config verified in place; graph retrieval has no ACL |
| I. Memory | 16 | four tiers, no owner; a declared bridge calls functions that do not exist |
| J. Observability | 18 | no OTel/Langfuse/Prometheus; per-turn trace columns never written |
| K. Governance | 16 | auth centralized and good; authorization has no decision point |
| L. Packs/plugins | 16 | none of it exists; customer behaviour compiled into core |
| M. Processing fabric | 14 | one process, no queue, no backpressure |
| N. Evaluation | 15 | GraphRAG closed and rigorous; artifacts untracked by git |
| Contracts | 12 | zero of twelve exist by name |
| CI Gates | 11 | one 24-line workflow; seven gates missing, one partial |

---

## 5. Existing components we should KEEP

| Component | Evidence | Why |
|---|---|---|
| Agent executor | `backend/orchestrator/graph.py` | Real cyclic LangGraph loop, step budget, SSE, restart-surviving approval pause. This *is* the Runtime. |
| Tool registry | `backend/orchestrator/registry.py:48` | Typed `Tool` with JSON-schema args, `required_permission`, `is_outbound` — the seed of PluginManifest. |
| Approval gate | `graph.py:84-92` → `routes/agent_os.py:383-427` | Hard human-in-the-loop with compare-and-swap `decide()`; exactly-once outbound execution. |
| Auth middleware | `backend/auth/enforce.py:93` | Centralized, identity-normalizing, IDOR-closing, tested. The right place to add tenant. |
| Model gateway client | `backend/services/llm.py` | Typed failures, streaming; the right survivor of the two stacks. |
| Model router logic | `backend/orchestrator/router.py:35` | Capability/tier routing, fallback chains, cost logging — keep the *logic*, move it onto the gateway client. |
| Indexing state machine | `backend/storage/indexing.py` | Durable, retried, and genuinely idempotent (`uuid5` point ids). The template for the processing fabric. |
| Token layer | `services/provider_tokens.py`, `token_crypto.py`, `mailbox.py` | Fernet-at-rest, provider-hiding facade — the seed of ConnectorInterface. |
| GraphRAG pipeline | `backend/knowledge_graph/`, `backend/context/` | Frozen, evaluated, production-wired. Treat as an existing capability. |
| Evaluation harness | `backend/evals/`, `run_evals.py` | 174-case frozen dataset, 9 stages, 6 metric families, 43 self-tests. Extend it; do not start a second one. |

---

## 6. Components that need REFACTORING

| Component | Problem | Direction |
|---|---|---|
| `UserContext` (`auth/context.py:19`) | **Dead code** — its router is never mounted (§8.1). No tenant field; `available_tools()` hardcodes an authorization ladder; embeds a persona | Decide explicitly: revive as `TenantContext` + entitlements + a policy call, or delete. Do not build on it while it is unreachable |
| `auth/enforce.py:159` | Resolves `org_id` per request and **discards it** | Smallest possible first step toward tenancy: keep the value and put it on the request |
| `backend/dashboard/` | Entire package hardwired to one customer's Azure SQL + schema | Extract to a pack; core keeps only a generic chart/SQL capability |
| `backend/chat/unified.py:53-57` | Routes on charity-domain nouns incl. Arabic `زكاة` | Move lane definitions into pack configuration |
| `backend/events.py` | A log table doing an event bus's job | Keep as audit sink; introduce EventEnvelope separately |
| `config/settings.py` | Flat process-wide `os.getenv()` | Typed ConfigSchema with a per-tenant overlay |
| Identity defaults | `repo.py:37`, `onboarding.py:29`, `identity.py:24` | Remove customer names from core defaults |
| SQLite `tasks/tasks.db` | Load-bearing for events, tools, router, scheduler | Finish the migration the project's own spec already mandated |

---

## 7. Components that must NOT be duplicated further

These are already duplicated. **Consolidate before building on them** — every new track that picks
the wrong twin doubles the problem.

| Capability | Twin A | Twin B | Divergence that matters |
|---|---|---|---|
| Agent runtime | `orchestrator/graph.py` (LangGraph) | `main.py` NATIVE_TOOLS loop | **The shipped UI uses B**; A has the approval gate |
| Tool registry | `orchestrator/registry.py` (36 typed) | `backend/tools.py` (34 schemas) | ~12 shared names, different handlers and approval semantics |
| Model access | `services/llm.py` (httpx) | `orchestrator/router.py` (OpenAI SDK) | B holds capability routing *and* the Azure bypass |
| Delegation | `delegate` tool (nested run) | `backend/delegation.py` (SQLite lifecycle) | plus a third dead mesh in `main.py:1542` |
| OAuth tokens | `services/provider_tokens.py` (encrypted) | `auth/providers/registry.py` (dead, unencrypted) | B is unmounted but present |
| Long-term memory | Postgres `memory_items` | Qdrant `user_memory_<id>` | bridge functions `store_memory`/`forget` **do not exist** |
| Schema of record | `backend/db/schema.sql` (Supabase, RLS) | `backend/db/models.py` (SQLAlchemy, `org_id`) | different isolation models |
| Evaluation | `backend/evals/` | `evals/tool_calling_eval.py` | no shared code |

---

## 8. Architectural conflicts

1. **Contradictory isolation guarantees inside one prompt.** In a single turn the Qdrant provider
   filters by `user_id` (`ingest.py:326`) while the graph provider applies no predicate at all
   (`providers/graph.py:71` discards `request.user_id`; `retriever.py:178` strips the `user_ids`
   provenance). The two are then fused into one context bundle. The prompt therefore mixes
   ACL-filtered and unfiltered evidence with no marker distinguishing them.

2. **Two Postgres databases, two isolation models.** *Resolved contradiction:* one auditor reported
   "no Postgres RLS"; the orchestrator found RLS in `backend/db/schema.sql`. Both are correct.
   `schema.sql:2-3` declares itself the **Supabase** schema (`auth.users` is managed by Supabase),
   with user-scoped RLS via `auth.uid()`. The **application** database is built by alembic
   (`migrations/versions/…genesis.py`), which creates `org_id` columns and indexes and **no RLS at
   all**. The conflict is not a mistake in either place; it is that no single schema owns tenancy.

3. **Two product personas in core.** "You are Aria, a personal AI assistant"
   (`auth/context.py:125`) and "You are Kannan Kuttan, the user's primary AI assistant"
   (`orchestrator/templates.py:13`).

4. **Approval semantics depend on which runtime answers.** `web_search` is approval-gated in
   `skills.py` and auto-executed in `tools.py`; `tools.py:957` checks guardrails only for the
   verdict `deny` and silently executes anything classified `approval`.

5. **Documentation asserts the opposite of the wiring.** Four knowledge-domain docstrings and
   `services/llm.py:6` ("Nothing talks to … any model port directly any more") state invariants the
   code violates.

6. **Dead addresses in the agent inbox.** The poller resolves agent UUIDs from
   `users.primary_agent_id`; `delegation.py:167` addresses literal names like `"calendar_agent"`.
   Those messages are never delivered.

### 8.1 Corrections made during the audit

The completeness critic disproved claims made by both an auditor and the orchestrator. All four
corrections below were re-verified directly before being accepted.

| Claim | Correction | Verification |
|---|---|---|
| "PostgreSQL RLS is MISSING" (Domain A) | **False.** RLS is enabled on 10 tables with per-table policies — *and* the correct finding is stronger than either version: the backend connects as **`service_role`**, and `provider_connections.sql:36-42` defines an explicit `service_role_bypass` policy `using (true) with check (true)`. **RLS therefore provides no isolation for any server-side call.** | `backend/db/schema.sql:162-204`; `backend/auth/supabase_client.py:37` `create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)`; `provider_connections.sql:36-42` |
| "`UserContext` is the request context, missing `org_id`" (orchestrator + Domain A) | **Misleading.** `backend/auth/routes.py` is **never mounted** — `main.py` includes only the provider, observability, explorer, agent_os and dashboard routers — so `UserContext` and its plan/feature entitlement ladder are **dead code that never executes**. The live request identity is the `x-auth-user` header injected by `auth/enforce.py:183`. | `grep include_router backend/main.py` → 5 routers, none from `auth/routes.py` |
| "`docker-compose.yml` declares three services" (orchestrator, inherited by the Contracts agent) | **False.** Four services: `qdrant:3`, `whisper_stt:14`, `piper_tts:22`, **`neo4j:31`**. The Domain H auditor caught this independently. | `docker-compose.yml` read in full |
| "`docs/` contains a pre-existing audit of this same HEAD" (critic) | **False positive.** That is this audit's own output, written minutes earlier in the same session. `docs/architecture/` did not exist when the audit began. | `ls docs/` at audit start returned only `evaluation/` |

The critic also surfaced a genuine, previously unexamined gap: **`sqlalchemy`, `alembic`, `supabase`,
`PyJWT`, `cryptography` and `numpy` are imported but declared in no requirements file** — which is a
second, independent reason the single CI job cannot run (§20).

---

## 9. Missing contracts

All twelve verified by direct search: `grep -rl <Name> --include=*.py .` returns **0 files** for
every one.

| # | Contract | Status | Closest existing thing |
|---|---|---|---|
| 1 | AgentManifest | MISSING | `SPECIALISTS` dict (`orchestrator/agents.py:17`), `agents` table |
| 2 | WorkflowManifest | MISSING | code-defined LangGraph + regex lane router |
| 3 | PromptTemplate | PARTIAL | module constants in `orchestrator/templates.py`; two conflicting personas |
| 4 | PackManifest | MISSING | nothing — no `pyproject.toml`, `setup.py`, `VERSION` or Dockerfile |
| 5 | PluginManifest | MISSING | process-global dict populated by import side effects |
| 6 | ConfigSchema | PARTIAL | flat `os.getenv()` in `config/settings.py`, no validation, no tenant layer |
| 7 | ModelProviderInterface | PARTIAL | two concrete clients, no Protocol/ABC |
| 8 | ConnectorInterface | MISSING | per-service modules sharing `http_client.py` + `provider_tokens.py` |
| 9 | PolicyInterface | MISSING | two unreconciled mechanisms, both inlined at call sites |
| 10 | **TenantContext** | **MISSING** | the `x-auth-user` header (`auth/enforce.py:183`) — a user id, no org. `UserContext` is **dead code**: its router is never mounted (§8.1) |
| 11 | EntitlementLicense | PARTIAL | `plan` + `features` on `UserContext` — i.e. **in dead code**; a `feature_flags` table exists but the auth middleware never consults it |
| 12 | **EventEnvelope** | **MISSING** | `events` table |

**EventEnvelope, field by field** — the only event structure is `events(id, ts, user_id, kind, name,
success, duration_ms, meta)` (`backend/events.py:29-31`):

| Required | Present? |
|---|---|
| `event_type` | partial — `kind` / `name` |
| `event_version` | ❌ |
| `tenant_id` | ❌ (only `user_id`) |
| `trace_id` | ❌ (an `x-trace-id` exists in logs, never joined to events) |
| `causation_id` | ❌ |
| `correlation_id` | ❌ |
| `producer` | ❌ |
| `timestamp` | ✅ `ts` |
| `payload` | ✅ `meta` |

**NATS subject convention:** none. Two vocabularies exist that a future convention should reuse —
the snake_case event `kind` set in `backend/events.py`, and the SSE frame names in
`backend/chat/frames.py`.

---

## 10. Multi-tenant risks

**Status: WRONG_ARCHITECTURE.** The system is single-tenant with a tenant column.

| Layer | Enforced boundary | Evidence |
|---|---|---|
| Request context | **none** | the live identity is the `x-auth-user` header (`auth/enforce.py:183`), which carries a user and no org. `auth/identity.resolve_or_provision` *does* compute `org_id` and `enforce.py:159` **discards the return value** |
| Application Postgres | user (by convention in queries) | `org_id` on ~25 tables; `org_id ==` appears only in `phaseF_verify.py:50,58` |
| Supabase Postgres | **none server-side** | RLS exists but is user-scoped, and the backend connects as `service_role` with an explicit `service_role_bypass` policy — see §8.1 |
| Qdrant corporate | user | `ingest.py:326` filters `user_id`; one global `corporate_memory` with a shared `__org__` sentinel |
| Qdrant memory | identity (inconsistent) | `user_memory_<id>`; the id differs across ≥5 call sites |
| Neo4j | **none** | no org property on any node, no tenant predicate in any Cypher |
| Object storage | key prefix | one SeaweedFS bucket |
| Logs / metrics / events | user | `events.user_id`; no tenant column |
| Background jobs | **none** | MemoryJobStore, no tenant dimension |

**Live cross-user exposures (P0, independent of tenancy):**

- `/observability/sessions` and `/observability/trace/{session_id}` return **any** user's chat
  content to **any** authenticated caller.
- `PATCH /agent/message/{id}/resolve` and `POST /initiatives/{id}/ack` mutate by raw id with no
  owner predicate.
- The whole `/dashboard/*` chart+board surface authenticates and then **discards** the identity.
- `/auth/provider/status` and `DELETE /auth/provider/{provider}` sit on the middleware's
  **unauthenticated public-prefix allowlist** (`auth/enforce.py:68`) while accepting a
  caller-supplied `user_id`.

---

## 11. Model Gateway violations

| Violation | Evidence | Severity |
|---|---|---|
| **Direct Azure OpenAI call** — `AsyncAzureOpenAI` reaching the provider without traversing the gateway, **live** via `DASHBOARD_LLM=azure` | Domain B audit | P0 — the only true bypass |
| Second parallel gateway stack — own client pool, `MODELS` registry, planner, fallback, cost logging | `orchestrator/llm.py` → `router.py:35` | P0 duplication |
| Gateway model registry not in the repo — no yaml/json declares LiteLLM `model_list` | repo-wide search | P0 — routing/keys/fallbacks unversioned |
| Embeddings never traverse the gateway — three independent `fastembed` loaders, one ignoring `EMBED_MODEL_NAME` | Domain B audit | P1 |
| STT/TTS outside the gateway; compose `whisper_stt`/`piper_tts` and `TTS_URL`/`STT_URL` orphaned | `docker-compose.yml:14,22` | P2 (deliberate, but undeclared) |
| Dead OpenAI client objects | `backend/main.py` | P3 |

Correctly routed: 12 modules import `services/llm.py` for chat, vision OCR and KG extraction.

---

## 12. Direct enterprise integration violations

Classification of every external integration:

| Integration | Class | Auth | Tenant handling |
|---|---|---|---|
| Microsoft 365 / Graph | PROVIDER | per-user OAuth, Fernet at rest | user only |
| Google Workspace | PROVIDER | per-user OAuth, Fernet at rest | user only |
| Azure SQL (CORE-SHARE) | **DIRECT** | module-level engine from `DABS_CORESHARE_*` | **none — one customer** |
| Azure OpenAI | **DIRECT** | API key in `.env` | none |
| SearXNG, Open-Meteo, RSS news, YouTube, iptv-org, radio-browser | DIRECT | keyless/public | none |
| SeaweedFS | CONNECTOR-ish | SigV4 | key prefix |
| Telegram | DIRECT | bot token | user mapping |
| SMTP/IMAP mailbox | DEPRECATED | — | — |
| SAP, Oracle, Jira, ServiceNow, Power BI, SharePoint, Teams, government, IoT, Tasree, Yisbir, Nazo | **MISSING** | — | — |

No `ConnectorInterface`, so no uniform contract for auth, rate limiting, timeout, retry, audit,
permission scope or data classification. Rate limiting exists only as a global limiter
(`backend/ratelimit.py`), not per connector. `http_client.py` supplies shared timeout/retry to the
two OAuth providers only.

---

## 13. Current messaging / event architecture

There is **no messaging architecture**. There are three unrelated mechanisms:

1. **`events` table** — append-only analytics/audit log. One writer (`log_event()`), six read-only
   aggregations. No publisher, subscriber or dispatcher.
2. **`agent_messages`** (SQLite) — polled every 30 s by an APScheduler job. The only message-passing
   primitive, and its address space is split so a whole class of message is never delivered.
3. **In-process calls** — `asyncio.create_task`, direct function calls, and HTTP self-calls to
   `127.0.0.1:8000` with an `INTERNAL_API_TOKEN`.

---

## 14. NATS readiness

**Status: MISSING — categorically.** No client library, no server, no config key, no subject, no
mention in code, docs or env files; zero matches across 581 site-packages. No `EventEnvelope`.

Readiness is therefore about what a migration would have to *create*, not adapt:

- **Reusable today:** the `kind` vocabulary in `events.py`, the SSE frame vocabulary in
  `chat/frames.py`, and the durable-sweep pattern in `storage/indexing.py`.
- **Must be built:** EventEnvelope (9 fields, of which 2 exist), a subject convention, a publisher,
  subscribers, and tenant identity to put in the envelope.
- **Note for the decision:** the absence is a *documented deliberate stance*
  (`indexing.py:17-29`, `observability.py:197-204`), and the project's own written target names
  **Arq + Redis fan-out, not NATS**. That disagreement should be settled explicitly before any
  broker work starts.

Per instruction, no migration is recommended here.

---

## 15. Redis / RabbitMQ usage

**None.** Neither appears in `requirements.txt`, the virtualenv, `docker-compose.yml`, or any
config. `ENTERPRISE_AGENTIC_OS.md:5` declares "Redis pub/sub + Arq async workers" as the intended
stack; that intent was never implemented. All background work is APScheduler + `asyncio.create_task`
in one process.

---

## 16. Pack / plugin / control-plane gap

**Status: MISSING in full.** No manifest, schema, versioning, signing, verification, installation,
upgrade, rollback, admin console or provisioning. No `pyproject.toml`, `setup.py`, `VERSION` or
`Dockerfile`.

Customer-specific behaviour presently in core:

| Kind | Location |
|---|---|
| Customer data source | `backend/dashboard/coreshare_db.py:67` (`DABS_CORESHARE_*`) |
| Customer schema in prompts | `dashboard/metric_contract.py:38-48`, `stream.py:38`, `ask.py:50`, `metrics.py:53` |
| Customer-domain routing | `backend/chat/unified.py:53-57` (`zakat`, `beneficiar`, `emirate`, `زكاة`) |
| Customer name in defaults | `db/repo.py:37`, `onboarding.py:29`, `auth/identity.py:24` |
| Ported customer code | `dashboard/config_db.py:8` — "Ported from Hermes `daralber/db.py`" |
| Demo scripts | `seed_demo_daralber.py`, `remove_demo_daralber.py` |
| Personas | `auth/context.py:125`, `orchestrator/templates.py:13` |

`tasree` and `yisbir` return **zero hits** repo-wide; `nazo` appears only as seeded graph/document
data.

---

## 17. Observability gap

`EXISTS` for logs and a probe dashboard; `MISSING` for traces, metrics export and LLM tracing.

- No OpenTelemetry, Langfuse, Prometheus, Grafana, Sentry, Jaeger or statsd anywhere.
- `x-trace-id` is stamped per request but never joined to the `events` spine.
- Token/cost accounting exists **only** on the orchestrator path (`router.py:135`); the primary
  `/agent/chat` path records none.
- `chat_messages.tokens_in/tokens_out/cost_micros/latency_ms/pipeline` and the entire `agent_runs`
  table are declared but never written.
- The 24-endpoint dashboard is honest about gaps (`_unavailable()` at `observability.py:68`) but
  fills them with live probes and regex-scraped log lines rather than persisted traces, and its
  activity feed reads a log path the app no longer writes.

---

## 18. Governance gap

**Authentication: EXISTS and is good.** One ASGI middleware, JWT or internal token, identity
normalization closing an IDOR class, with tests.

**Authorization: MISSING as an architecture.** No `PolicyInterface`, no `authorize()`, no
OPA/Rego/Casbin/Cedar. Two unreconciled mechanisms (`guardrails.py`; `Tool.is_outbound` +
`agent_permissions`) each enforced on exactly one of the two runtimes. The caller-identity helper is
copy-pasted three times. `users.role` is never read anywhere.

**Present and worth keeping:** per-agent capability grants, the hard approval gate with atomic
compare-and-swap and audit event, Fernet-encrypted tokens, per-user memory collections.

**Absent:** ABAC, data classification, secrets management (a credential is hard-coded at
`docker-compose.yml:42`), approval workflows beyond the single outbound gate.

---

## 19. Processing-fabric gap

**A 50,000-application pipeline is not supported by this architecture.** One process, one event
loop, a `MemoryJobStore`, no queue, no worker tier, no backpressure — `schedule_indexing` creates
unbounded `asyncio` tasks with no semaphore, and `store_document` performs blocking SeaweedFS I/O on
the event loop.

The exception is `backend/storage/indexing.py`, which is the right template: a Postgres-backed state
machine with bounded retries, backoff, a stale-processing threshold, a recovery sweep, and genuine
idempotency via `uuid5` point ids. Two tables that would give the fabric durability at scale —
`document_chunks` and `agent_runs` — are migrated with **zero writers**.

---

## 20. Evaluation status

**GraphRAG evaluation is CLOSED** and is the strongest engineering artifact in the repository: a
frozen 174-case dataset (v3.1), nine pipeline stages, six metric families, 43 self-tests, and 41
artifacts across 13 steps whose manifests record git commit, dataset hashes, held-constant
parameters and pre/post datastore invariants. The frozen configuration was re-verified in place
during this audit (§Inventory 8).

Three risks to its reproducibility, all P0-for-evidence:

1. **`git ls-files docs` returns 0** and `reports/` is gitignored — the artifact set and 58 of the
   174 golden cases exist only in this uncommitted working tree.
2. `.github/workflows/ci.yml` excludes `evals/` and the one job is **non-functional for two
   independent reasons**: it installs only `requirements-dev.txt` (pytest, pytest-asyncio, httpx)
   while 34 of 35 test modules import `backend`/`config` (needing `fastapi` at import time); and
   `sqlalchemy`, `alembic`, `supabase`, `PyJWT`, `cryptography` and `numpy` are imported but
   declared in **no** requirements file at all.
3. `reports/evals/baseline.json` is a pre-v3 artifact (0.6985) that would report a false ~11pp
   regression against the frozen 0.5867.

Outside GraphRAG: **no test imports `backend.orchestrator`**, so the outbound approval chokepoint is
untested; connectors have no tests; there are **zero tenant-isolation tests**.

---

## 21. P0 blockers

84 records. Consolidated:

| # | Blocker | Evidence |
|---|---|---|
| 1 | No `TenantContext`; `org_id` written but never enforced | §10 |
| 2 | Customer behaviour compiled into core — one artifact cannot serve two customers | §16 |
| 3 | Cross-user data exposure on observability, dashboard, provider and id-addressed routes | §10 |
| 4 | Live Azure OpenAI bypass of the Model Gateway | §11 |
| 5 | Two agent runtimes with divergent approval semantics; the shipped UI uses the ungated one | §7 |
| 6 | Two tool registries, ~12 shared names, different approval behaviour | §7 |
| 7 | No `PolicyInterface`; authorization inlined and duplicated | §18 |
| 8 | Zero of 12 contracts; no `EventEnvelope`; no subject convention | §9 |
| 9 | Zero of 8 CI gates enforced (one partial, user-level); no ratcheting baseline; CI likely non-functional | §21 gates |
| 10 | Graph retrieval unscoped while fused with ACL-scoped Qdrant evidence | §8 |
| 11 | Evaluation artifacts and 58 golden cases untracked by git | §20 |
| 12 | LiteLLM `model_list` absent from the repo — routing/keys/fallbacks unversioned | §11 |

### The eight CI gates

| Gate | Status | Note |
|---|---|---|
| 1 — No customer names/logic in core | MISSING | violations live; tests *assert* customer data rather than forbid it |
| 2 — Cross-tenant isolation | **PARTIAL** | `tests/test_provider_isolation.py` + AST test at `test_identity_normalization.py:177` enforce **user**-level only |
| 3 — No direct provider calls | MISSING | rule stated in prose at `services/llm.py:2`, unenforced, already violated |
| 4 — Plugin compatibility | MISSING | no plugin subsystem to validate |
| 5 — Signed artifact verification | MISSING | no cosign/sigstore/gpg anywhere |
| 6 — Pack/schema compatibility | MISSING | no pack subsystem |
| 7 — No hard-coded customer workflows | MISSING | `chat/unified.py:53-57` |
| 8 — `tenant_id` propagation | MISSING | identifier does not exist in the codebase |
| Ratcheting baseline | MISSING | no such file |

---

## 22. P1 work

81 records. Themes: consolidate the duplicated twins (§7); `ConnectorInterface` + the missing
enterprise connectors; observability persistence (per-turn traces, token/cost on the primary path,
write `agent_runs`); memory consolidation to one system of record and repair of the broken
`store_memory`/`forget` bridge; RBAC (`users.role` is never read); processing-fabric backpressure
and non-blocking object-store I/O; evaluation coverage for agents, connectors and tenancy.

## 23. P2 work

66 records. Themes: repo hygiene (dozens of `.bak` files; `main.py.bak.*` variants); dead code
removal (second OAuth stack, legacy meshes, `[ACTION:{json}]` protocol, MCP prose references);
documentation correction where docstrings assert the opposite of the wiring; orphaned compose
services; scheduler/job telemetry; the consumer-grade surface (radio, live TV, YouTube, QR) whose
place in an enterprise artifact should be an explicit pack decision rather than an accident.

---

## 24. Dependencies between phases

```
                      ┌──────────────────────── PHASE 0 ────────────────────────┐
                      │ Contracts (12) · TenantContext · EventEnvelope          │
                      │ CI gates + ratcheting baseline · Policy foundations     │
                      │ Consolidation decisions (§7) — pick one twin each       │
                      └───────────────┬────────────────────────────────────────┘
                                      │ everything below needs tenant identity
        ┌─────────────────┬───────────┴───────────┬──────────────────┐
        ▼                 ▼                       ▼                  ▼
   TRACK A            TRACK B                TRACK C            TRACK D
   Agent Planning     Multi-Agent / bus      MCP / Connector    Memory / Learning
   & Runtime          (broker TBD)           Gateway
        │                 │                       │                  │
        └─────────────────┴───────────┬───────────┴──────────────────┘
                                      ▼
                   CROSS-CUTTING: Model Gateway · Langfuse/OTel · OPA · Evaluation
```

**Hard prerequisites**

- `TenantContext` blocks **everything**. No track can be made multi-tenant afterwards without
  rework, because tenant identity must be present at the point each subsystem is written.
- The §7 consolidations block their own tracks: Track A cannot proceed while two runtimes exist;
  Track C cannot proceed while two tool registries exist; the Model Gateway work cannot proceed
  while two gateway clients exist.
- `EventEnvelope` blocks Track B and the observability cross-cut.
- `PolicyInterface` blocks the governance cross-cut and gates Track C's connector authorization.
- Fixing the untracked-artifacts problem (§20) blocks *any* claim of evaluation reproducibility.

---

## 25. Proposed parallel implementation tracks

### Phase 0 — Foundations (blocking, sequential)

- **Prerequisites:** none.
- **Reuse:** `backend/auth/enforce.py` (the natural home for tenant resolution),
  `orchestrator/registry.py` `Tool` (the seed of PluginManifest), `events.py` `kind` vocabulary
  (the seed of a subject convention).
- **Missing:** all 12 contracts, the CI gates, the ratcheting baseline.
- **Safe first implementation:** define the 12 contracts as pure types with **no behaviour change**;
  add `TenantContext` alongside `UserContext` and populate it in the auth middleware without yet
  enforcing it; add the CI gates in **report-only** mode with a baseline recording today's violation
  counts. Nothing breaks, and drift stops growing immediately.

### Track A — Agent Planning / Runtime

- **Prerequisites:** Phase 0; runtime consolidation decision.
- **Reuse:** `orchestrator/graph.py`, `registry.py`, the approval gate, `agents`/`agent_permissions`/
  `approvals` tables.
- **Missing:** planner, plan object, task decomposition, `agent_runs` writers, escalation/handoff,
  any test of the executor.
- **Blocking dependency:** the shipped UI currently uses the *other* runtime — migrating `/api/chat`
  onto the LangGraph lane is the real first task, and it is user-visible.
- **Safe first implementation:** write the first executor tests (the approval chokepoint is
  completely untested), then start writing `agent_runs`.

### Track B — Multi-Agent / event backbone

- **Prerequisites:** Phase 0 (`EventEnvelope`); an explicit broker decision (NATS vs the
  already-declared Arq+Redis).
- **Reuse:** `events.py` vocabulary, `chat/frames.py` SSE names, `storage/indexing.py` durable-sweep
  pattern.
- **Missing:** broker, publisher/subscriber, envelope, subject convention, escalation.
- **Blocking dependency:** three delegation implementations must collapse to one first.
- **Safe first implementation:** repair the `agent_messages` address-space split (a live delivery
  bug) and introduce `EventEnvelope` as the shape written to the existing `events` table — no broker
  yet.

### Track C — MCP / Connector Gateway

- **Prerequisites:** Phase 0 (`ConnectorInterface`, `PolicyInterface`); tool-registry consolidation.
- **Reuse:** `provider_tokens.py` + `token_crypto.py` + `mailbox.py`, `http_client.py`,
  `provider_health.py`, and the `Tool` dataclass.
- **Missing:** the interface itself, per-connector rate limiting/audit/data classification, every
  enterprise connector beyond M365/Google.
- **Blocking dependency:** the unauthenticated provider routes must be closed first — they are a
  live exposure.
- **Safe first implementation:** define `ConnectorInterface` and retrofit the two existing OAuth
  providers onto it; delete nothing yet.

### Track D — Memory / Learning

- **Prerequisites:** Phase 0 (`TenantContext`).
- **Reuse:** Qdrant per-user collections, `memory_items`, the episodic summariser.
- **Missing:** a single system of record, the `store_memory`/`forget` functions that are *called but
  do not exist*, tenant dimension, consistent identity.
- **Safe first implementation:** fix the broken bridge, then pick one system of record. Do not add
  a learning loop on top of two divergent stores.

### Cross-cutting

- **Model Gateway** — consolidate to one client, move `router.py`'s capability/fallback/cost logic
  onto it, remove the Azure bypass, and bring LiteLLM's `model_list` into the repo.
- **Langfuse / OTel** — needs `EventEnvelope` and `trace_id` propagation first; today `x-trace-id`
  is never joined to events.
- **OPA** — needs `PolicyInterface` first; do not wire a policy engine to two divergent enforcement
  points.
- **Evaluation** — extend `backend/evals/`; add agent, connector and tenant-isolation suites. Do not
  create a third evaluation entrypoint.

---

## 26. Risks

| Risk | Why it matters |
|---|---|
| **Building on the wrong twin** | Every duplicated capability has a "shipped" side and a "better" side, and they are not the same side. Consolidation decisions must precede feature work. |
| **Retrofitting tenancy** | Adding `TenantContext` after a track is built means revisiting every query, filter, collection name and event. This is the single most expensive mistake available. |
| **Untracked evidence** | `docs/` is untracked and `reports/` gitignored; a `git clean` destroys the GraphRAG evidence base and 58 golden cases. |
| **CI that does not run** | The one gate that exists likely fails at collection, so "tests pass" currently carries little information. |
| **Working tree with 82 uncommitted files from several developers** | Any architectural refactor now collides with in-flight feature work (auth, live-TV, media, news, QR). |
| **Documentation that contradicts code** | Several docstrings assert invariants the code violates, so future work may trust a guarantee that does not hold. |
| **Uniform self-reported confidence** | All 224 domain findings claim `high`; treat individual records as evidenced but not independently adjudicated unless re-verified. |

---

## 27. What MUST NOT be changed yet

- **The frozen GraphRAG configuration** — resolver 0.82, depth 1, `graph_top_k` 8, `hop_decay` 0.55,
  fusion 0.60, `max_nodes` 100. Verified in place; the workstream is closed. Treat as a capability.
- **The 174-case v3.1 dataset and the 13-step artifact set** — reproducibility depends on them being
  byte-stable. (Committing them is a *preservation* action, not a change to them.)
- **The approval gate semantics** — the hard outbound gate is the platform's strongest governance
  primitive; do not weaken it while consolidating the two runtimes.
- **The auth middleware's identity normalization** — it closes a real IDOR class and is tested.
- **Any of the 82 uncommitted files belonging to other developers** — auth, live TV, media, news, QR
  and the staged deletion of `config/users.py`.
- **Production data** — Neo4j (523 nodes / 3414 relationships), Qdrant `corporate_memory` (993
  points), the 310-document corpus.

---

## Appendix — audit safety

Read-only throughout: `Read`, `grep`, `ls`, `find`, `sed -n`, and read-only `git status/log/show`.
No file was created, modified or deleted outside `docs/architecture/`. Nothing was staged or
committed. No database, index, collection or migration was touched. The pre-existing staged deletion
of `config/users.py` and all 82 working-tree modifications belong to other developers and were left
exactly as found.
