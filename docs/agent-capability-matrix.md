# Agent Capability Matrix

**Audit date:** 2026-08-13 · **Companion to:** [architecture-audit.md](architecture-audit.md)

Every row states the classification, the exact files/symbols that justify it, the execution flow,
and — for anything short of `IMPLEMENTED` — precisely what is absent.

Legend: `IMPLEMENTED` · `PARTIAL` · `CONFIGURED_BUT_NOT_ENFORCED` · `PLANNED_ONLY` · `MISSING` · `BROKEN`

---

## 1 · LangGraph production runtime — `PARTIAL`

**Files** [backend/orchestrator/graph.py](../backend/orchestrator/graph.py) ·
[backend/orchestrator/__init__.py](../backend/orchestrator/__init__.py)

**Symbols** `AgentState` (L27) · `_agent_node` (L44) · `_tools_node` (L70) · `_route_agent` (L105) ·
`_route_tools` (L111) · `_build` (L115) · `GRAPH` (L125) · `run_turn` (L153) · `resume` (L167) ·
`astream_turn` (L190)

**Flow**
```
START ──► agent ──_route_agent──► tools ──_route_tools──► agent   (cycle, ≤ STEP_BUDGET=8)
             │                       │
             └──► END                └──► END   (when awaiting approval)
```
`GRAPH.ainvoke(state, {"recursion_limit": 24})` / `GRAPH.astream(..., stream_mode=["updates"])`.

**Runtime evidence** 1,104 `llm_call` events emitted by `router._log`
([router.py:141](../backend/orchestrator/router.py#L141)), which only this runtime calls.

**Absent**
- No checkpointer — `g.compile()` takes no `checkpointer=`; no `MemorySaver`/`PostgresSaver`/
  `thread_id` anywhere in the repo.
- Not the live runtime — most recent `agent_id='primary'` LLM call **2026-08-06**, while
  `tool_called` events from the legacy runtime run to **2026-08-13**.
- Two unrelated `StateGraph`s in `main.py` ([L1542](../backend/main.py#L1542) `/delegate` toy graph,
  [L3267](../backend/main.py#L3267) `email_workflow`).

---

## 2 · Multi-agent architecture — `PARTIAL`

**Files** [backend/orchestrator/agents.py](../backend/orchestrator/agents.py) ·
[backend/orchestrator/templates.py](../backend/orchestrator/templates.py)

**Symbols** `SPECIALISTS` (agents.py L17, 6 entries) · `_delegate` (L67) · `delegate` Tool (L81) ·
`specialist_names` (L99) · a **second** `SPECIALISTS` (templates.py L57, 4 entries)

**Flow**
```
primary agent
  └─ tool_call delegate(to_agent, task)
       └─ _delegate → graph.run_turn(agent={"id": to_agent, "tools": spec["tools"]},
                                     system_prompt=spec["prompt"])     ← nested LangGraph run
             └─ returns "[Specialist Name] <answer>" as a tool result
```

**Roster (executing)** `calendar_agent` · `research_agent` · `task_agent` · `email_agent` ·
`analyst_agent` · `predictive_agent`

**Runtime evidence** `llm_call` events with `agent_id ∈ {research_agent (6), analyst_agent (8),
predictive_agent (2), email_agent (1)}` — last 2026-07-09.

**Absent**
- Roster is a module-level dict; the tool `enum` is `list(SPECIALISTS.keys())` (L92) → no runtime
  registration.
- The DB `agents` table's specialists are never executed (see row 4).
- `agents.SPECIALISTS` and `templates.SPECIALISTS` **disagree** — `templates`' `email_agent` grants
  `send_email`; `agents`' does not.
- No recursive delegation (`delegate` is in no specialist's tool list), no concurrency, no shared
  state, no agent-initiated messaging.

---

## 3 · Supervisor — `PARTIAL`

**File** [backend/chat/unified.py](../backend/chat/unified.py) · **Symbol** `route()` (L71),
`unified_stream()` (L132)

**Flow**
```
POST /agent/chat → agent_chat (routes/agent_os.py:354)
  → unified_stream(user_id, message, session_id, images, dashboard_model())
      → lane = "primary" if images else route(message)
          _PERSONAL ∧ ¬_CHART → primary   → agent_os._sse → graph.astream_turn
          _CHART              → chart     → dashboard.stream.stream_dashboard
          _DATA ∧ _DOMAIN     → data      → dashboard.ask.ask_stream
          otherwise           → primary
```
Regexes: `_CHART` L33 · `_DATA` L38 · `_DOMAIN` L53 · `_PERSONAL` L66. All three lanes emit one
unified SSE frame vocabulary ([backend/chat/frames.py](../backend/chat/frames.py)).

**Absent**
- Routes *lanes*, not agents — cannot select among registered agents.
- No planning, no decomposition, no re-routing on failure, no plan artifact, no confidence.
- Customer-specific: `_DOMAIN` matches `zakat|donation|beneficiar|emirate` + Arabic `زكاة|تبرع|مستفيد`.
- Within the primary lane the agent is fixed (the user's single `primary`).

---

## 4 · Agent registry / routing — `PARTIAL` (registry real, routing absent)

**Files** [backend/routes/agent_os.py](../backend/routes/agent_os.py) L473–621 ·
[backend/db/models.py](../backend/db/models.py) L99 (`Agent`), L122 (`AgentPermission`) ·
[backend/db/repo.py](../backend/db/repo.py)

**Endpoints** `GET /agent/agents` (L487) · `GET /agent/agents/{id}` (L516) ·
`POST /agent/agents` (L530) · `PATCH /agent/agents/{id}` (L566) ·
`PUT /agent/agents/{id}/permissions` (L586) · `DELETE /agent/agents/{id}` (L606)

**Ownership** `_get_owned` (L502) — UUID parse + `ag.user_id == user.id`.
**Concurrency** partial-unique index on one primary per user; `IntegrityError` → 409 (L558).

**Live data** 16 agents / 144 permissions:
```
primary    Aria               active   ×7
specialist Finance Agent      active   ×1
specialist HR Compliance      active   ×1
specialist IT Security Agent  active   ×1
specialist Legal Agent        active   ×1
specialist Research Agent     active   ×2   (+2 archived, +1 archived Calendar Agent)
```

**Routing flow that exists**
```
_load_primary(user_id)                      routes/agent_os.py:50
  → repo.resolve_user
  → repo.get_primary_agent(s, user.id)      ← the ONLY registry read on an execution path
  → repo.allowed_tools(s, ag.id)            → filtered against registry.all_names()
  → {"id": "primary", "tools": [...], "model_key": ..., "fallback_models": [...]}
```

**Absent** `repo.get_agent` is called **only** from CRUD handlers. No code selects a non-primary
agent for execution. **Finance / HR Compliance / IT Security / Legal Agent are inert rows.**
Note the deliberate lockdown semantics at L66–69: an empty permission list is honoured verbatim
rather than re-injecting defaults.

---

## 5 · Agent-to-agent communication — `PARTIAL`

| Mechanism | Location | Class | Evidence |
|---|---|---|---|
| `delegate` → nested `graph.run_turn` | [agents.py:67](../backend/orchestrator/agents.py#L67) | **works** | 17 specialist `llm_call` events |
| `delegations` SQLite lifecycle | [backend/delegation.py](../backend/delegation.py) — own `KNOWN_AGENTS` tuple (L34), own schema (L36) | legacy | 2 rows; `delegation_sent`/`delegation_completed` ×2 |
| `agent_messages` Postgres transport | [models.py:217](../backend/db/models.py#L217) · `integrations/agent_inbox.py` · `GET /agent/inbox`, `POST /agent/message` ([main.py:1854](../backend/main.py#L1854)) | **dead** | **0 rows** |
| `POST /delegate` 2-node graph | [main.py:1542](../backend/main.py#L1542) `manager_officer → developer_officer` | toy | retrieves corporate context with `owner=None` |

**Characteristics of the working path** synchronous · one hop · request/response only · no
callback · no shared state · no bus.

---

## 6 · Tool registry — `PARTIAL` (two registries; permission field dead)

**Registry B (orchestrator)** [backend/orchestrator/registry.py](../backend/orchestrator/registry.py)
`Tool` dataclass (L38) · `_REGISTRY` (L48) · `register` (L51) · `get` (L56) ·
`openai_schemas(allowed)` (L60) · `all_names` (L66) · `preview` (L70).
Populated by `registry.py` (10) + `skills.py` (17) + `agents.py` (1) + `dashboard/*_tools.py` (7)
= **35 tools**.

**Registry A (legacy)** [backend/tools.py](../backend/tools.py) — `tools_for()`, `TOOL_GROUPS`,
`dispatch_tool_call` (L944), `execute_single_tool` — **34 tools**.

**Defect — `required_permission` is never read.** Repository-wide, the identifier appears at
exactly three sites: the dataclass field (registry.py:44), one assignment (agents.py:95), one API
response field (routes/agent_os.py:433). Enforcement is by tool **name**:
```python
# graph.py:84
elif name not in state["allowed_tools"]:
```
and `AgentPermission.permission` stores tool names ([routes/agent_os.py:600](../backend/routes/agent_os.py#L600)).
The `email.read` / `calendar.write` / `code.run` vocabulary has no consumer.

Full listing: [tool-inventory.md](tool-inventory.md).

---

## 7 · Tool calling — `IMPLEMENTED`

**File** [backend/orchestrator/graph.py](../backend/orchestrator/graph.py) `_tools_node` (L70)

**Flow**
```
last assistant message .tool_calls
  → json.loads(arguments) with {} fallback           L78
  → registry.get(name)   → unknown → "error: unknown tool"          L81
  → name in allowed_tools? → no → "error: not permitted"            L84
  → tool.is_outbound?      → yes → pause (never execute)            L86
  → await tool.handler(ctx, **args)                                 L95
       TypeError  → "error: bad arguments"                          L96
       Exception  → "error: {name} failed"                          L98
  → {"role":"tool","tool_call_id":..,"name":..,"content":str(...)[:6000]}
```
Every call — including the paused one — is answered with a `tool` message, keeping the protocol
valid. `_agent_node` (L44) supplies schemas via `registry.openai_schemas(allowed_tools)`, with
per-agent temperature (0.0 for `dashboard`/`analytics`), `tool_choice="required"` on the dashboard's
first step, and vision turns sent with **no** tools (deployed vision model lacks
`--enable-auto-tool-choice`).

**Runtime evidence** 4,980 `tool_called` events (Runtime A) + 1,104 `llm_call` events (Runtime B).

---

## 8 · MCP integration — `MISSING`

| Probe | Result |
|---|---|
| `grep -rn "FastMCP\|@mcp\.\|from mcp\|import mcp" --include=*.py .` (excl. venv) | **0 hits** |
| `grep -i mcp requirements.txt` | **0 hits** — not a declared dependency |
| `ls .venv/.../site-packages \| grep -i mcp` | `fastmcp 3.4.6`, `mcp 1.29.0` present (transitive/leftover) |
| Only reference in code | [backend/dashboard/tools.py:3–7](../backend/dashboard/tools.py#L3) — a docstring recording that these tools were **ported away from** Hermes' `daralber/mcp_server.py`: *"FastMCP `@mcp.tool()` functions become `registry.Tool` entries"* |

No MCP server, no MCP client, no transport, no tool-discovery. MCP was **removed**, not integrated.

---

## 9 · Tool execution gateway — `MISSING`

Tool handlers are `await`-ed directly in `_tools_node` inside the API process. There is no
intermediating layer of any kind.

Absent: per-tool timeout budget · concurrency/quota control · egress allowlist · circuit breaker ·
retry policy · result-envelope schema · per-invocation credential brokering · sandboxing (except
`run_python`'s ad-hoc Docker call) · async/long-running job contract.

The closest existing analogues are **not** a gateway:
- `_internal_post` ([registry.py:81](../backend/orchestrator/registry.py#L81)) — an HTTP loopback to
  the app's own `/send_email` with `X-Internal-Token`.
- `run_python` ([skills.py:175](../backend/orchestrator/skills.py#L175)) — a fixed-argument
  `docker run --rm --network none --memory 256m --cpus 1 --pids-limit 128 --read-only
  --tmpfs /tmp:size=32m -v <tmp>:/work:ro python:3.12-slim`, 25 s timeout.

---

## 10 · State management / checkpointing — `PARTIAL`

| State | Mechanism | Durable | Evidence |
|---|---|---|---|
| Paused approval | [orchestrator/store.py](../backend/orchestrator/store.py) → `approvals.payload` JSONB `{agent, messages, approval, supabase_uid}` | **Yes** | 6 rows |
| Conversation | [orchestrator/conversation.py](../backend/orchestrator/conversation.py) JSON file store | Yes (file) | — |
| Session registry | `chat_sessions` / `chat_messages` | Yes | live |
| Artifacts | `chat_artifacts` | Yes | 111 `chart_created` events |
| Long-term memory | `memory_items` + per-user Qdrant collections | Yes | 11 collections |
| In-flight graph state | in-process | **No** | — |
| LangGraph checkpoints | — | **None** | no `Saver` in repo |
| `agent_runs.state` | table + `repo.create_run`/`save_run_state`/`finish_run` | **BROKEN — no callers** | **0 rows**; [analytics_tools.py:10](../backend/dashboard/analytics_tools.py#L10) states "the agent_runs table is currently unused" |

**Resume flow (works, restart-safe)**
```
graph._tools_node sets awaiting → _route_tools → END
routes/agent_os._sse:125    store.create_approval(user_id, agent, _strip_images(messages), approval)
                            (images stripped so the blob stays small — L81)
POST /agent/approvals/{id}/approve
  → _resume (L383)  resolve_user(caller) == resolve_user(record.user_id)?  else 404
                    record.status == "pending"?                            else 409
                    store.decide(...)  ← CAS: UPDATE ... WHERE status='pending', rowcount>0
                                          + repo.log_event(kind='approval', decided_by=...)
                    graph.resume(...)  → tool.handler(**approval["args"])  ← exactly once
                    store.set_result(...)
```
`_pg_decide` ([store.py:191](../backend/orchestrator/store.py#L191)) is the single-execution guarantee.

---

## 11 · PostgreSQL task/state metadata — `PARTIAL`

**DB** `aganeti` on `postgres:15-alpine` (container up 8 days) · `.env`
`AGANETI_DATA_BACKEND=postgres` · Alembic-managed (`alembic_version` present) · 34 tables.

**Live counts** `users` 7 · `agents` 16 · `agent_permissions` 144 · `approvals` 6 · `events` 9,435 ·
`delegations` 2 · **`agent_runs` 0** · **`agent_messages` 0** · `provider_connections` 3

**Models** [backend/db/models.py](../backend/db/models.py) — `Organization`(L40) `Department`(L48)
`Designation`(L58) `User`(L68) `EmployeeProfile`(L85) `Agent`(L99) `AgentPermission`(L122)
`AgentRun`(L133) `Approval`(L153) `Task`(L172) `Event`(L188) `Delegation`(L204) `AgentMessage`(L217)
`Initiative`(L231) `Schedule`(L245) `Contact`(L258) `Opportunity`(L273) `Interaction`(L292)
`Document`(L313) `DocumentChunk`(L332) `EmailThread`(L352) `EmailMessage`(L363) `Meeting`(L378)
`MeetingSegment`(L391) `GmailSyncState`(L403) `ChatSession`(L419) `ChatMessage`(L451)
`ChatArtifact`(L483) `MediaSource`(L512) `MediaDirectory`(L548) `MediaDirectoryState`(L568)
`MemoryItem`(L582)

**Deficiencies**
1. `org_id` is written on ~25 tables and **never read as a filter** — `grep -rn "org_id ==" backend/`
   returns nothing. Isolation is `user_id` only.
2. `agent_runs` has no writer → no run-of-record for the executor.
3. Dual-backend SQLite remnants against `tasks/tasks.db`: `events.py` (L22), `store.py` (L26),
   `tools.py` (L24), `router.py` (L25); **`delegation.py` is SQLite-only** (L31).
4. Two Postgres databases with divergent schemas: `aganeti` and `aganeti_genesis`.

---

## 12 · Redis transient state / queues — `PLANNED_ONLY`

| Evidence | Detail |
|---|---|
| Container | `redis:7-alpine`, **up 8 days**, `0.0.0.0:6379` |
| Settings | [config/settings.py:268–277](../config/settings.py#L268) — `REDIS_ENABLED` (default **`false`**), `REDIS_URL` (db **1**, since db0 is another app's Celery broker), `REDIS_CONNECT_TIMEOUT`, `REDIS_DEFAULT_TTL`, `REDIS_KEY_PREFIX` |
| `.env` | `REDIS_*` **absent** → `REDIS_ENABLED` resolves `false` |
| Requirements | `redis` **not** in `requirements.txt` |
| Only `import redis` in the repo | [backend/routes/observability.py:171](../backend/routes/observability.py#L171) — inside `_probe_redis()`, a health check that returns `"REDIS_ENABLED=false — the platform does not use Redis yet"` |

Zero application reads or writes. No caching, no queue, no session store, no rate-limit backend,
no pub/sub. A dependency, a container and a settings block — exactly the pattern this audit was
told not to accept as implementation.

---

## 13 · Qdrant integration — `IMPLEMENTED`

**Files** [backend/ingest.py](../backend/ingest.py) · [backend/memory_admin.py](../backend/memory_admin.py) ·
`backend/context/providers/corporate.py`, `memory.py`

**Symbols** `get_client` (ingest L70) · `ensure_collection` (L74) · `_ensure_payload_indexes` (L88) ·
`_delete_source` (L218) · `ingest_file` (L231) · `search_corporate`

**Live**
```
corporate_memory : 999 points, 384-dim cosine, status green, 8 segments
+ 11 × user_memory_<uuid>, face_embeddings, nazo_library, known_collection
```

**Reachable from the agent**
```
LLM tool_call search_documents(query)
  → graph._tools_node → registry._search_documents (registry.py:140)
      → asyncio.to_thread(search_corporate, query, 6, ctx["user_id"])
            ← ACL: caller's own docs + shared org corpus only
      → "Relevant excerpts from the user's documents: [source] text…"
```
Also consumed by the context builder (`corporate`, `memory` providers) and long-term memory
(`backend/chat/memory.py`).

---

## 14 · Neo4j integration — `PARTIAL` (populated + full API; unreachable from the agent)

**Live graph** 523 nodes · 3,414 relationships · 25 labels (`User, Project, Email, Document,
Conversation, Entity, Organization, Person, Company, Location, Message, Meeting, Memory, Task,
Repository, Issue, PR, Calendar, Technology, Provider, Model, Plugin, API, Service, Server`).
Container `neo4j:5.26-community`, up 8 days. `NEO4J_ENABLED` default `true`; `.env` supplies
`NEO4J_URI/USERNAME/PASSWORD/DATABASE`.

**Subsystem** [backend/knowledge_graph/](../backend/knowledge_graph/) — `client.py` (singleton driver,
`is_enabled` L50, `get_driver` L55), `extractor.py`, `normalizer.py`, `builder.py`, `pipeline.py`,
`provenance.py`, `queries.py`, `service.py`, and `retrieval/` (`api.py`, `resolver.py`,
`retriever.py`, `ranking.py`, `registry.py`).

**Consumers of `build_ranked_context`** — the only production path to the graph:
| Caller | Nature |
|---|---|
| [backend/main.py:2377](../backend/main.py#L2377) | **Runtime A `/chat`** — `only=("corporate","memory","graph","calendar","tasks")` |
| `backend/routes/observability.py:511,844` · `observability_explorer.py:180` | diagnostics |
| `backend/evals/runner.py:296` | evaluation harness |

**Absent** No graph tool exists in the 35-tool orchestrator registry, and `graph.py` never calls the
context builder. **The knowledge graph is invisible to `/agent/chat`.**

---

## 15 · OPA installation / configuration — `MISSING`

`docker ps -a | grep -i opa` → none · `grep -inE "opa|open-policy" requirements.txt` → 0 ·
`grep -niE "opa" docker-compose.yml` → 0 (services: `qdrant`, `whisper_stt`, `piper_tts`, `neo4j`) ·
no OPA binary, sidecar, bundle server, or config file.
The only `OPA` string in `config/settings.py` is the word **"OPAQUE"** in a comment at L136.

---

## 16 · OPA policy definitions — `MISSING`

`find . -name "*.rego" -not -path "./.venv/*" -not -path "./.git/*"` → **0 files**.
`grep -rn "rego"` matches only `docs/architecture/target_architecture_v1_gap_matrix.json`
(i.e. the planning artifact). No policy bundle, no `data.json`, no test fixtures.

---

## 17 · OPA enforcement points — `MISSING`

No `opa_client`, no `/v1/data/...` HTTP call, no `check_policy`/`authorize` helper, no decision log.
Authorization is entirely Python-native — see [opa-audit.md](opa-audit.md) §3.

---

## 18 · Human-in-the-loop / approval — `PARTIAL`

**Enforced on Runtime B.** Chokepoint [graph.py:86](../backend/orchestrator/graph.py#L86):
```python
elif tool.is_outbound:
    prev = registry.preview(name, args)          # human-readable approval card
    pending.append({"tool_call_id": tc["id"], "name": name, "args": args,
                    "action_type": name, "preview": prev})
    content = f"[AWAITING USER APPROVAL] {prev}"   # handler NOT called
```
Gated tools: `send_email`, `create_calendar_event`, `web_search`.
`preview()` ([registry.py:70](../backend/orchestrator/registry.py#L70)) renders the card.
Persist → `store.create_approval`; decide → CAS + audit; resume → handler runs once.
Endpoints: `GET /agent/approvals`, `POST /agent/approvals/{id}/approve|reject`
([routes/agent_os.py:378–426](../backend/routes/agent_os.py#L378)).
**Live:** 6 approvals, 2 `kind='approval'` audit events.

A second, narrower HITL exists for task proposals: `POST /agent/notifications/{nid}/approve`
([routes/agent_os.py:646](../backend/routes/agent_os.py#L646)) — nothing is created until approved.

**Not enforced on Runtime A.** [backend/guardrails.py](../backend/guardrails.py) computes
`"auto" | "approval" | "deny"`, but the sole enforcement site
([backend/tools.py:956](../backend/tools.py#L956)) checks **only `"deny"`**. A verdict of
`"approval"` falls through and the tool executes. `AUTONOMY_LEVEL` therefore has no behavioural
effect. Since Runtime A carries live traffic, **the approval gate does not cover production**.

---

## 19 · Langfuse tracing — `MISSING` (custom substitute in place)

| Probe | Result |
|---|---|
| `grep -i langfuse requirements.txt` | 0 |
| `ls .venv/.../site-packages \| grep -i langfuse` | not installed |
| `grep -rn "langfuse" --include=*.py .` | 0 (only `docs/architecture/*.json` planning artifacts) |
| OpenTelemetry / Prometheus | also absent from `requirements.txt` |

**Substitute in place**
- `_TraceASGIMiddleware` ([main.py:906](../backend/main.py#L906)) — pure ASGI (explicitly not
  `BaseHTTPMiddleware`, which buffered SSE and broke `/agent/chat` streaming); mints/propagates
  `X-Trace-Id`, echoes it on the response, binds a contextvar, includes it in the 500 envelope.
- Events spine ([backend/events.py](../backend/events.py)) — 9,435 rows; `llm_call` carries
  `agent_id`, `tokens_in`, `tokens_out`, `cost_micros`, `duration_ms`;
  aggregations `summary`/`tools`/`per_agent`/`response_quality`/`active_hours`.
- SSE stage frames ([backend/chat/frames.py](../backend/chat/frames.py)) — `router`/`query`/`analyst`/
  `critic` stage timings surfaced live to the UI.
- Evidence ledger ([backend/insight/evidence.py](../backend/insight/evidence.py)) — records each SQL
  query + typed rows, then **recomputes** every figure the model states
  (`ask.py` L349–380), emitting `verified: true|false|null`.

**Gap vs. Langfuse** no span tree · no parent/child correlation across `delegate` · no
prompt/completion capture · no per-session cost rollup · no dataset/experiment linkage · no UI.

---

## 20 · Enterprise connectors — `PARTIAL`

### Implemented

| Connector | Module | Target | Notes |
|---|---|---|---|
| Gmail | [services/gmail.py](../backend/services/gmail.py) (217 L) | `gmail.googleapis.com/gmail/v1/users/me` | list/read/send |
| Google Calendar | [services/gcalendar.py](../backend/services/gcalendar.py) | `googleapis.com/calendar/v3` | agenda/create |
| Google People | [services/gcontacts.py](../backend/services/gcontacts.py) (107 L) | `people.googleapis.com/v1` | contact resolution |
| MS Mail | [services/msmail.py](../backend/services/msmail.py) (245 L) | `graph.microsoft.com/v1.0` | list/read/send/reply |
| MS Calendar | [services/mscalendar.py](../backend/services/mscalendar.py) (303 L) | Graph | agenda/create |
| MS Contacts | [services/mscontacts.py](../backend/services/mscontacts.py) (131 L) | Graph | lookup |
| Provider abstraction | [services/mailbox.py](../backend/services/mailbox.py) (329 L) | — | one interface over Google/Microsoft |
| OAuth tokens | [services/provider_tokens.py](../backend/services/provider_tokens.py) (393 L) + `token_crypto.py`, `_token_pg_store.py`, `_token_file_store.py` | Google + M365 token endpoints | encrypted at rest, refresh flows |
| Azure SQL (CORE-SHARE) | [dashboard/coreshare_db.py](../backend/dashboard/coreshare_db.py) (314 L) | MS SQL / T-SQL | **read-only**: `validate_select_only` (L95) + `validate_no_pii` (L124); SWR cache |
| SeaweedFS | [backend/storage/](../backend/storage/) (1,031 L) | S3 + filer (4 containers up) | ACL-safe keys, durable indexing |
| SearXNG | [services/websearch.py](../backend/services/websearch.py) | self-hosted `:5555` | no scraper fallback by design |
| LiteLLM gateway | [orchestrator/router.py:33](../backend/orchestrator/router.py#L33) | `:4000` | all model traffic; Azure OpenAI optional with local fallback |
| Supabase | `auth/supabase_client.py`, `auth/jwt_verify.py` | Supabase | ES256 JWT via JWKS |
| Telegram | [integrations/telegram_bot.py](../integrations/telegram_bot.py) (1,048 L) | Bot API | routes to Runtime A `/chat` (L274) |
| Whisper / Piper | `integrations/whisper_transcriber.py`, `tts.py` | local containers | STT/TTS |
| LibreOffice / Gotenberg | `integrations/libreoffice_converter.py` | local | document conversion |

**Live** `provider_connections`: google ×2, microsoft ×1.

### Missing
No ERP (SAP/Oracle/Dynamics) · no CRM (Salesforce/HubSpot) · no ITSM (ServiceNow/Jira) ·
no HRIS · no Slack/Teams · no SharePoint/Confluence.

### Structural gap
There is **no connector framework**. Each integration is a bespoke module with its own auth
handling, retry policy and error shape. Absent: connector registry · manifest / capability
declaration · uniform credential brokering · per-connector quota and rate limiting · uniform
egress control · health/circuit-breaker contract (`services/provider_health.py` covers only
Google/Microsoft).

---

## Summary counts

| Class | Count | Capabilities |
|---|---|---|
| `IMPLEMENTED` | 2 | Tool calling (7) · Qdrant (13) |
| `PARTIAL` | 11 | 1, 2, 3, 4, 5, 6, 10, 11, 14, 18, 20 |
| `CONFIGURED_BUT_NOT_ENFORCED` | 1 | `guardrails.py` autonomy policy (within 18) |
| `PLANNED_ONLY` | 1 | Redis (12) |
| `MISSING` | 5 | MCP (8) · Tool gateway (9) · OPA install (15) · OPA policies (16) · OPA enforcement (17) · Langfuse (19) |
| `BROKEN` | 1 | `agent_runs` run-of-record (within 10) |
