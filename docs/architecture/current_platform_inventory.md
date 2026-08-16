# Current Platform Inventory

**Read-only audit.** Nothing in this repository was modified, staged or committed to produce it.

| | |
|---|---|
| Repository | `/home/matrix/aganetiAi` |
| Branch | `feat/enterprise-agentic-os` |
| HEAD | `432fd5e` |
| Working tree | 82 uncommitted files from several developers — inspected, untouched |
| Method | 16 parallel read-only auditors + orchestrator verification; every non-MISSING claim carries file:line, symbol, service, table, route or test evidence |

This document records **what exists today**. The comparison against Target Architecture v1.0 and the
gap list live in `target_architecture_v1_gap_analysis.md` and `target_architecture_v1_gap_matrix.json`.

---

## 1. Runtime shape

A **single uvicorn process** running FastAPI + APScheduler + aiogram on one event loop
(`deploy/aganeti-api.service:11`, no `--workers`). Background work is 18 in-process
`scheduler.add_job` registrations against APScheduler's default `MemoryJobStore`
(`backend/main.py:59`). There is no worker tier, no broker and no queue.

### Declared infrastructure — `docker-compose.yml`

| Service | Image | Purpose |
|---|---|---|
| `qdrant` | `qdrant/qdrant:latest` | vector store, ports 6333/6334 |
| `whisper_stt` | `onerahmet/openai-whisper-asr-webservice` | STT — **orphaned**, imported once, never called |
| `piper_tts` | `ghcr.io/rhasspy/piper` | TTS — **orphaned**, same |
| `neo4j` | `neo4j:5.26-community` | knowledge graph, ports 7474/7687 |

Running **outside** compose: PostgreSQL, LiteLLM (`:4000`), vLLM (`:9002`), SeaweedFS.
`docker-compose.yml:42` contains a hard-coded credential: `NEO4J_AUTH: neo4j/tfyfu8ub`.

### Datastores actually in use

| Store | Role | Tenant dimension |
|---|---|---|
| PostgreSQL (`aganeti`) | relational state, alembic-migrated, `org_id` on ~20–25 tables | column present, **never used as a filter** |
| Supabase Postgres | auth/identity; `backend/db/schema.sql` with RLS on 10 tables | RLS is **user-scoped** (`auth.uid()`), never org-scoped |
| Qdrant | `corporate_memory` (993 pts) + 14 `user_memory_<id>` collections | search filters `user_id` only (`backend/ingest.py:326`); `org_id` stored but unused (`:264`) |
| Neo4j | 522 `:Entity` nodes / 3414 relationships | **none** — no org property, no tenant predicate in any Cypher |
| SQLite `tasks/tasks.db` | events, tool registry, router logs, schedules | **none** |
| SQLite `backend/dashboard/dashboard_configs.db` | chart/board definitions | **none** |
| SeaweedFS | document object storage, one bucket | key-prefix scoping only |
| Flat JSON under `MEMORY_DIR` | conversation + episodic summaries | per-key |

**Two Postgres databases with divergent schemas and divergent isolation models** — Supabase
(`auth.uid()` RLS) and the application DB (`org_id` columns, no RLS). This is the single most
consequential structural fact in the inventory.

---

## 2. Model access

`backend/services/llm.py` is a genuine single gateway client — httpx → LiteLLM `:4000` → `qwen-fast`
→ vLLM `:9002` — with typed failures (`LLMTimeoutError`, `LLMGatewayError`,
`LLMEmptyResponseError`), streaming and `<think>` stripping. **12 modules import it** and correctly
route chat, vision OCR and knowledge-graph extraction through it.

A **second, parallel inference stack** exists: `backend/orchestrator/llm.py` → `router.py`, built on
the OpenAI SDK with its own client pool, its own `MODELS` registry (`router.py:35`), its own
capability/tier planner, its own fallback chain and its own cost logging.

- **The only true provider bypass**: an `AsyncAzureOpenAI` client reaching Azure OpenAI directly,
  live via `DASHBOARD_LLM=azure` in `.env`.
- **The gateway's own model registry is not in the repo** — no yaml/json anywhere declares LiteLLM's
  `model_list`, so routing, keys and fallbacks are configured out-of-band and unversioned.
- **Embeddings never touch the gateway** — three independent `fastembed` loaders, one hard-coding the
  model name and ignoring `EMBED_MODEL_NAME`.
- STT/TTS are deliberately in-process (faster-whisper, Kokoro); the compose services are orphaned.

---

## 3. Agent runtime

`backend/orchestrator/graph.py` is a real LangGraph executor: a cyclic `agent ⇄ tools` loop,
`STEP_BUDGET = 8`, state = the message list, SSE streaming, nested sub-agent delegation, and a
**hard `is_outbound` approval gate** that pauses the run and persists an `awaiting` record so a
paused run survives process restart.

Around it: a typed tool registry (`orchestrator/registry.py:48`, 36 `Tool` dataclasses with JSON-schema
args, `required_permission` and `is_outbound`), a Postgres agent registry (`agents`,
`agent_permissions`, `agent_runs`, `approvals`) with CRUD at `/agent/agents`, and approval persistence
with a compare-and-swap `decide()` so an outbound action executes exactly once.

**There is a second, entirely separate agent runtime** in `backend/main.py` — a `NATIVE_TOOLS` loop
with `MAX_TOOL_ROUNDS=4` over `backend/tools.py` (34 OpenAI-shaped schemas dispatched by a ~350-line
if/elif). **The shipped React UI streams that one** (`/api/chat`); the LangGraph lane reaches
production only via `/dashboard/chat` and `/dashboard/ask`.

Planning is **not** LLM-driven: routing is a deterministic regex lane router
(`backend/chat/unified.py`, `route() → chart | data | primary`). There is no planner node, no task
decomposition and no plan object.

---

## 4. Multi-agent

Three **mutually disconnected** delegation implementations:

1. the `delegate` tool running a specialist as a nested `graph.run_turn` (`orchestrator/agents.py:67`);
2. a tracked SQLite `delegations` lifecycle with its own agent roster (`backend/delegation.py:34`),
   reachable only via `POST /delegations`;
3. a dead two-node "manager→developer" LangGraph mesh (`backend/main.py:1542-1555`).

Agent-to-agent messaging is a SQLite `agent_messages` table polled every 30 s by APScheduler. Its
**address space is split**: the poller resolves agent UUIDs from `users.primary_agent_id` while
`backend/delegation.py:167` addresses literal names like `"calendar_agent"`, so those messages are
never delivered. `agent_runs` exists in schema and repo helpers with **zero callers**. There is no
escalation mechanism anywhere.

---

## 5. Messaging and events

**No broker of any kind** — no NATS, Redis, Celery, RabbitMQ, Kafka or arq in `requirements.txt` or
in the 581-package virtualenv. What the code calls "events" (`backend/events.py`, the `events` table)
is an **append-only analytics log**: one writer `log_event()`, six read-only aggregations, no
publisher, no subscriber, no dispatcher.

Two naming conventions do exist and are worth preserving: the flat snake_case event `kind`
vocabulary in `backend/events.py`, and the SSE frame vocabulary in `backend/chat/frames.py`.

---

## 6. Tools

MCP was **removed** in commit `de74961` ("chore: remove backend mcp layer"), deleting
`backend/mcp/m365_server.py`; neither `mcp` nor `fastmcp` is in `requirements.txt`.

Two parallel in-process tool layers coexist:

| Layer | Location | Serves | Approval |
|---|---|---|---|
| `TOOL_SCHEMAS` (34 schemas) | `backend/tools.py:27` | legacy `/chat` (the shipped UI) | guardrails checked only for verdict `deny`; anything classified `approval` executes silently |
| `_REGISTRY` (36 `Tool`s) | `backend/orchestrator/registry.py:48` | LangGraph executor | hard `is_outbound` gate |

Roughly a dozen tool **names exist in both** with different handlers, schemas and approval semantics
— e.g. `web_search` is approval-gated in `skills.py` and auto-executed in `tools.py`. Neither
executor imposes a per-tool timeout or retry.

---

## 7. Connectors

Two real credentialed enterprise connectors — **Microsoft 365/Graph** and **Google Workspace** —
both per-user OAuth authorization-code flows sharing one token layer
(`backend/services/provider_tokens.py`, Fernet-encrypted at rest via `token_crypto.py`) and one
retrying HTTP client (`http_client.py`, 20 s timeout, 2 retries on 429/5xx), behind a provider-hiding
facade (`mailbox.py`). A third is a **read-only Azure SQL source for one named customer**
(`backend/dashboard/coreshare_db.py`, SELECT-only + PII-column blocklist).

Everything else is keyless/public: SearXNG web search, Open-Meteo weather, RSS news, YouTube
oEmbed + yt-dlp, iptv-org live TV, radio-browser.info, local QR generation.

**Absent entirely**: SAP, Oracle, ServiceNow, Jira, Power BI, SharePoint/OneDrive, Teams messaging,
government, IoT, Tasree, Yisbir, Nazo — those names appear only as synthetic document metadata or
seeded graph entities.

Two structural problems: a **second, dead, unencrypted OAuth token stack**
(`backend/auth/providers/registry.py` + `backend/auth/routes.py`, never mounted), and the provider
connect/status/disconnect routes sitting on the **unauthenticated public prefix list**
(`backend/auth/enforce.py:68`) while accepting a caller-supplied `user_id`.

---

## 8. Knowledge / GraphRAG

Mature and, on the retrieval side, genuinely well-built. The frozen configuration is verified in
place and untouched:

| Parameter | Value | Location |
|---|---|---|
| resolver threshold | 0.82 | `retrieval/registry.py:62` |
| graph depth | 1 | `config/settings.py:237` |
| graph_top_k | 8 | `context/providers/graph.py:35` |
| hop_decay | 0.55 | `retrieval/ranking.py:45` |
| fusion threshold | 0.60 | `context/fusion.py:42` |
| max_nodes | 100 | `retrieval/retriever.py:30` |

It is fully wired into production chat (`main.py:2382` → `build_ranked_context(...)`), fused with
Qdrant into a corroboration-counted bundle and rendered as `[KNOWLEDGE GRAPH]`.

Two data-flow facts matter more than any tuning parameter:

- **Graph retrieval has no tenant or ACL scoping at all** — no Cypher carries a user or org
  predicate, `providers/graph.py:71` discards `request.user_id`, and `retriever.py:178` actively
  strips the `user_ids` provenance that would enable filtering. The Qdrant path running in the same
  turn **is** user-scoped, so the two fuse into one prompt under contradictory guarantees.
- **Nothing populates the graph at runtime** — `storage/indexing.py:138` states Neo4j is
  deliberately not invoked, so the 522-entity graph is a manually built batch asset.

---

## 9. Memory

Four tiers exist; none has a single owner.

| Tier | Stores | Note |
|---|---|---|
| Working | Postgres `chat_messages` **and** flat JSON under `MEMORY_DIR` (68 `chat_*.json`) | dual-written |
| Episodic | LLM rolling summariser → `MEMORY_DIR/<key>/summaries.json` | live; downstream extraction job disabled |
| Semantic | Postgres `memory_items` **and** Qdrant `user_memory_<id>` | two competing systems of record |
| Procedural | JSONL style files, `users.settings` JSONB, `agents` table | three unrelated stores |

The declared Postgres→Qdrant bridge is **broken**: `backend/chat/memory.py:68,222` call
`long_term.store_memory` and `long_term.forget`, **neither of which is defined anywhere in the repo**.
`memory_items.org_id` is written once and never appears in a `WHERE` clause. The identity used for
isolation is inconsistent across at least five call sites (session_id, supabase_uid, internal uuid,
`X-Auth-User`, and config aliases like `"user_1"`).

---

## 10. Observability

Three unconnected pillars:

1. **Text logs** — `logging_config.py:37` writes `logs/app.log` (rotating, 5 MB × 3);
   `main.py:906 _TraceASGIMiddleware` stamps `x-trace-id` and logs one line per request. Text, not
   JSON, not shipped anywhere.
2. **DB event spine** — `events.py:57 log_event()` for 14 event kinds, aggregated by five
   `/analytics/*` routes.
3. **Read-only probe dashboard** — 24 GET endpoints across `routes/observability.py` and
   `observability_explorer.py`, rendered by `ObservabilityPage.tsx` + `Phase2Panels.tsx`.

**No OpenTelemetry, Langfuse, Prometheus, Grafana, Sentry, Jaeger or statsd anywhere.** Token and
cost accounting exist only on the orchestrator path (`router.py:135 _log()`); the primary
`/agent/chat` path records none, and `chat_messages.tokens_in/tokens_out/cost_micros/latency_ms`
plus the entire `agent_runs` table are declared but never written.

---

## 11. Governance and security

**Authentication is genuinely centralized and good.** One pure-ASGI `AuthEnforceMiddleware`
(`backend/auth/enforce.py:93`, mounted `main.py:879`) gates every route behind a Supabase ES256 JWT
or an internal service token, then **rewrites** query/body/path identity and injects a non-forgeable
`x-auth-user` header — closing an IDOR class of bug — with regression tests in
`tests/test_identity_normalization.py`.

**Authorization has no decision point at all.** No `PolicyInterface`, no `authorize()`, no
OPA/Rego/Casbin/Cedar. Every decision is inlined at its call site, and the caller-identity helper is
copy-pasted three times (`main.py:815`, `routes/agent_os.py:40`, `routes/dashboard.py:39`). Two
unreconciled policy mechanisms coexist: `backend/guardrails.py` (enforced only in `tools.py:956`)
and `Tool.is_outbound` + `agent_permissions` (enforced only in `graph.py:84`).

Working governance primitives that do exist and are worth keeping: the per-agent capability grant
table, the hard human-in-the-loop approval gate with atomic compare-and-swap and an audit event,
Fernet encryption of OAuth tokens at rest, and per-user Qdrant memory collections.

---

## 12. Packs, plugins, control plane

**None of it exists.** No `pyproject.toml`, `setup.py`, `VERSION`, `Dockerfile`, manifest or config
schema. The nearest thing to a plugin system is a process-global in-memory dict populated by import
side effects, with no version, signature, owner or tenant scope.

Customer-specific behaviour is **compiled into core**:

| Location | What |
|---|---|
| `backend/dashboard/` (whole package) | hardwired to one customer's Azure SQL (`coreshare_db.py:67`, `DABS_CORESHARE_*`); ported from `daralber/` per `config_db.py:8` |
| `dashboard/metric_contract.py:38-48`, `stream.py:38`, `ask.py:50`, `metrics.py:53` | one customer's aid-request schema written into shared prompt constants |
| `backend/chat/unified.py:53-57` | the single chat router branches on `zakat` / `beneficiar` / `emirate` / Arabic `زكاة` |
| `backend/db/repo.py:37` | `DEFAULT_ORG_SLUG = "meerana"` |
| `backend/onboarding.py:29` | `org_slug: str = "meerana"` |
| `backend/auth/identity.py:24` | `ALLOWED_EMAIL_DOMAINS` defaulting to `"meerana.ae"` |
| repo root | `seed_demo_daralber.py`, `remove_demo_daralber.py` |

Two conflicting product personas are hard-coded in core: **"You are Aria"**
(`backend/auth/context.py:125`) and **"You are Kannan Kuttan"** (`orchestrator/templates.py:13`).

---

## 13. Processing fabric

One in-process `AsyncIOScheduler` plus `asyncio.create_task`. The one genuinely well-engineered
piece is `backend/storage/indexing.py`: a Postgres-backed state machine
(`stored → processing → indexed/failed`) with `MAX_ATTEMPTS=3`, backoff 1 m/5 m/30 m, a 15-minute
stale threshold, a 2-minute recovery sweep, and **real idempotency** — Qdrant point ids are
`uuid5(owner::type::source::index::version)` and the source's points are deleted before upsert, so a
retry replaces rather than duplicates.

Everything around it is thinner: the email pipeline dedups through a JSON file capped at 500 ids and
triages 3 emails/user/cycle; drop-folder ingest re-hashes the whole vault every 300 s; OCR is a
sequential 6-page vision loop in the same worker thread; KG extraction is CLI-only with
`--workers` defaulting to 1 because LiteLLM 408s above 3–4 concurrent extractions.

**No backpressure anywhere**: `schedule_indexing` creates unbounded asyncio tasks with no semaphore,
and `store_document` performs blocking SeaweedFS I/O directly on the event loop.

---

## 14. Evaluation

Two disconnected systems.

The mature one is `backend/evals/` — eight modules plus the `run_evals.py` CLI — driving a frozen
**174-case golden dataset** (8 core_retrieval + 58 discrimination + 108 enterprise, correction
version v3.1) through nine pipeline stages into six metric families, defended by 43 self-tests
including `assert len(cases) == 174`. **That workstream is CLOSED**: `graphrag_quality_gate_final_v1.md`
freezes the six parameters and 41 artifacts across 13 steps record how each number was obtained.

The second is `evals/tool_calling_eval.py` — 23 inline prompts, stdout only, no artifact, no shared
code with `backend/evals`.

Outside GraphRAG the coverage is thin: **no test imports `backend.orchestrator`**, so the outbound
approval chokepoint is untested; connectors have no tests; policy is covered only as a pure function,
with **zero tenant-isolation tests**.

Two facts about durability deserve emphasis:

- `.github/workflows/ci.yml` runs `python -m pytest -q` and **explicitly excludes `evals/`**.
- `reports/` is gitignored (`.gitignore:65`) and **`git ls-files docs` returns 0** — so the entire
  13-step artifact set and 58 of the 174 golden cases exist **only in this uncommitted working tree**.

---

## 15. What already works well

Recorded so that later phases reuse rather than rebuild:

- `backend/services/llm.py` — a well-designed gateway client with typed failure modes.
- `backend/orchestrator/graph.py` + `registry.py` — a real agent executor with a typed tool registry
  and a hard, restart-surviving human approval gate.
- `backend/auth/enforce.py` — centralized authentication with identity normalization and tests.
- `backend/storage/indexing.py` — a durable, idempotent indexing state machine.
- `backend/knowledge_graph/` + `backend/context/` — a frozen, evaluated, production-wired GraphRAG
  pipeline with 13 steps of reproducible evidence.
- `backend/services/provider_tokens.py` + `token_crypto.py` — encrypted OAuth token storage with a
  provider-hiding mailbox facade.
