# Agentic AI Platform — Implementation Status Audit

**Repository:** `/home/matrix/aganetiAi`
**Audit date:** 2026-08-13
**Method:** static source reading + live runtime probes (Docker, Postgres, Qdrant, Neo4j, HTTP)
**Constraint honoured:** no production code was modified. All probes were read-only.
**Reference:** `docs/architecture/` (target architecture v1 gap analysis, phase-0 plan, PoC backlog)

---

## 0. How to read this document

A capability is called IMPLEMENTED only when a code path was traced from an HTTP entry point
to the effect, and the effect was corroborated by runtime evidence (a database row, a container,
an HTTP response, or a logged event). The presence of a dependency in `requirements.txt`, a
Docker container, a settings constant, or a docstring was treated as **evidence of intent only**.

Classification vocabulary:

| Class | Meaning |
|---|---|
| `IMPLEMENTED` | Working, reachable from a live entry point, verified end-to-end |
| `PARTIAL` | Real code exists and runs, but a material part of the capability is absent or unreachable |
| `CONFIGURED_BUT_NOT_ENFORCED` | Config/policy/schema exists and is even computed, but no code acts on it |
| `PLANNED_ONLY` | Exists only in documentation, backlog, or a dormant schema |
| `MISSING` | No code, no config, no artifact |
| `BROKEN` | Code exists and is wired but cannot work as intended |

---

## 1. Executive summary

### 1.1 The finding that reframes everything else

**There are two complete, independent agent runtimes in this repository, and the LangGraph one is
not the one carrying live traffic.**

| | Runtime A — "legacy" | Runtime B — "orchestrator" |
|---|---|---|
| Entry point | `POST /chat` — [backend/main.py:2266](../backend/main.py#L2266) | `POST /agent/chat` — [backend/routes/agent_os.py:354](../backend/routes/agent_os.py#L354) |
| Loop | Hand-rolled, `MAX_TOOL_ROUNDS = 4` ([backend/main.py:2612](../backend/main.py#L2612)) | LangGraph `StateGraph` ([backend/orchestrator/graph.py:115](../backend/orchestrator/graph.py#L115)) |
| Tool registry | `backend/tools.py` — 34 tools | `backend/orchestrator/registry.py` — 35 tools |
| Authorization | `backend/guardrails.py` `decide()` | per-agent tool-name allowlist |
| Approval gate | none enforced (see §5.3) | hard `is_outbound` pause/resume gate |
| Consumers | this repo's Vite frontend ([frontend/src/api/client.ts:71](../frontend/src/api/client.ts#L71)), Telegram bot ([integrations/telegram_bot.py:274](../integrations/telegram_bot.py#L274)) | an external Next.js dashboard |

Runtime evidence from the `events` table in Postgres `aganeti`:

```
kind='tool_called'  (logged only by backend/tools.py:1322 — Runtime A)
  play_youtube_video  1975   last 2026-08-13
  get_news             916   last 2026-08-13
  get_weather          861   last 2026-08-13
  watch_live_tv        293   last 2026-08-13

kind='llm_call'     (logged only by backend/orchestrator/router.py:141 — Runtime B)
  agent_id=analytics   613   last 2026-08-08
  agent_id=dashboard   327   last 2026-08-10
  agent_id=primary     115   last 2026-08-06
  agent_id=research_agent  6 last 2026-07-06
  agent_id=email_agent     1 last 2026-07-05
```

The two tool catalogues are **almost disjoint**. Ten names overlap (`create_task`, `complete_task`,
`draft_email`, `get_agenda`, `get_analytics`, `read_email`, `remember_fact`, `resolve_contact`,
`set_reminder`, `web_search`) but each has **two separate implementations with different
authorization**. Most consequentially, `web_search` is approval-gated on Runtime B
(`is_outbound=True`, [registry via skills.py:409](../backend/orchestrator/skills.py#L409)) and
auto-executes on Runtime A (`ACTION_CATEGORY["web_search"] = "read"` →
`decide()` returns `"auto"`, [backend/guardrails.py:39](../backend/guardrails.py#L39)).

**Consequence: "which runtime served this request" is a security question in this codebase, not a
style question.** Any architectural change must first decide which runtime is production.

### 1.2 What is genuinely built and should not be rebuilt

- A correct cyclic LangGraph tool-calling executor with a hard outbound-approval gate that
  survives process restarts (`backend/orchestrator/`, `approvals` table — 6 real rows).
- Centralized ASGI authentication with IDOR normalization and a fail-closed identity header
  (`backend/auth/enforce.py`).
- A typed tool registry with JSON-schema args and a single outbound chokepoint.
- Real read-only SQL enforcement with a PII column blocklist against the customer's Azure SQL
  (`backend/dashboard/coreshare_db.py`).
- A deterministic numeric-verification critic that recomputes an analytics answer's figures against
  a query ledger (`backend/insight/evidence.py`, wired in `backend/dashboard/ask.py`).
- Real Google/Microsoft connectors with encrypted token storage.
- A populated Neo4j knowledge graph (523 nodes, 3,414 relationships) and Qdrant corpus
  (`corporate_memory`, 999 points).

### 1.3 What the architecture claims but does not have

- **OPA: entirely absent.** Zero `.rego` files, no container, no client, no dependency, no
  reference in application code. See [docs/opa-audit.md](opa-audit.md).
- **Langfuse: entirely absent.** Not in `requirements.txt`, not installed, not imported.
- **MCP: removed, not integrated.** `fastmcp` and `mcp` are present in the venv but **not in
  `requirements.txt`** and there is no `FastMCP` server, client, or `@mcp.tool` in application code.
  The only reference is a docstring at [backend/dashboard/tools.py:6](../backend/dashboard/tools.py#L6)
  recording that these tools were *ported away from* an MCP server.
- **LangGraph checkpointing: not used.** No `MemorySaver`, no `PostgresSaver`, no `thread_id`
  config anywhere. State survives only for the paused-approval case, via a hand-rolled blob.
- **`agent_runs` table: dead.** 0 rows; `repo.create_run` / `save_run_state` / `finish_run` have
  **no callers**.
- **DB-registered specialist agents are unreachable.** 6 active specialists exist in the `agents`
  table (Finance, HR Compliance, IT Security, Legal, Research ×2) but the only execution path
  loads `get_primary_agent` only; `delegate` routes to a **hardcoded in-code dict**.
- **Redis: unused by the application.** Container up 8 days, `REDIS_ENABLED` absent from `.env`
  (defaults `false`); the only `import redis` in the repo is a health probe.

---

## 2. Capability classification

| # | Capability | Class | One-line basis |
|---|---|---|---|
| 1 | LangGraph production runtime | `PARTIAL` | Real `StateGraph` executor exists and works, but is not the runtime serving live traffic; no checkpointer |
| 2 | Multi-agent architecture | `PARTIAL` | Nested sub-agent runs work, but the roster is hardcoded and the DB agent registry is inert |
| 3 | Supervisor | `PARTIAL` | A deterministic regex lane-router exists (`chat/unified.py`); no LLM supervisor, no planner, no re-routing |
| 4 | Agent registry / routing | `PARTIAL` | Full CRUD + permissions persisted (16 agents, 144 permissions); **no routing to any registered agent except `primary`** |
| 5 | Agent-to-agent communication | `PARTIAL` | `delegate` tool = real nested execution (synchronous, one hop, no return path). Three other A2A mechanisms exist and are dead |
| 6 | Tool registry | `PARTIAL` | Two registries, both real, neither authoritative; `required_permission` never read |
| 7 | Tool calling | `IMPLEMENTED` | OpenAI-schema tool calls executed on both runtimes; 4,980 `tool_called` events |
| 8 | MCP integration | `MISSING` | Packages present in venv only; no server, no client, no requirement pin |
| 9 | Tool execution gateway | `MISSING` | Handlers are called directly in-process; no gateway, broker, quota, or egress control |
| 10 | State management / checkpointing | `PARTIAL` | Approval-pause state is durable and restart-safe; no LangGraph checkpointer; conversational state is a JSON file store |
| 11 | PostgreSQL task/state metadata | `PARTIAL` | 34 tables live and written; `agent_runs` empty; `org_id` written but **never read as a filter**; SQLite still authoritative for some paths |
| 12 | Redis transient state / queues | `PLANNED_ONLY` | Settings block + running container; `REDIS_ENABLED=false`; zero application reads/writes |
| 13 | Qdrant integration | `IMPLEMENTED` | Live client, 999-point `corporate_memory` + per-user collections, reachable by the `search_documents` tool |
| 14 | Neo4j integration | `PARTIAL` | Populated graph + full retrieval API, but **unreachable from the LangGraph runtime** — no graph tool exists |
| 15 | OPA installation / configuration | `MISSING` | No binary, container, dependency, or config |
| 16 | OPA policy definitions | `MISSING` | Zero `.rego` files repository-wide |
| 17 | OPA enforcement points | `MISSING` | No call site; authorization is Python-native |
| 18 | Human-in-the-loop / approval | `PARTIAL` | Genuinely enforced on Runtime B (with CAS + audit); **absent on Runtime A**, which serves live traffic |
| 19 | Langfuse tracing | `MISSING` | Absent from deps, venv, and code. A custom `events` spine + trace-id middleware substitute for it |
| 20 | Enterprise connectors | `PARTIAL` | Google + Microsoft + Azure SQL + SeaweedFS + SearXNG are real; no ERP/CRM/ITSM/HRIS; no connector framework |

Detail, evidence and execution flow for every row: [docs/agent-capability-matrix.md](agent-capability-matrix.md).

---

## 3. Evidence for the IMPLEMENTED and PARTIAL verdicts

### 3.1 LangGraph production runtime — `PARTIAL`

**Proof it is real.** [backend/orchestrator/graph.py](../backend/orchestrator/graph.py):

- `AgentState` TypedDict with `messages: Annotated[list, operator.add]` (line 27)
- `_build()` (line 115) constructs `StateGraph(AgentState)` with nodes `agent` (line 44) and
  `tools` (line 70), `START → agent`, and two conditional edges `_route_agent` (line 105) /
  `_route_tools` (line 111). Compiled once into module-level `GRAPH` (line 125).
- Entry points `run_turn` (line 153), `resume` (line 167), `astream_turn` (line 190).
- `STEP_BUDGET = 8` with `recursion_limit = 3 * STEP_BUDGET` (line 162) — a real termination bound.
- Streaming uses `GRAPH.astream(..., stream_mode=["updates"])`.

**Why it is not IMPLEMENTED.**

1. It is not the production runtime. Live `tool_called` events on 2026-08-13 all originate from
   `backend/tools.py:1322` (Runtime A). The most recent `primary`-agent LangGraph call is
   2026-08-06.
2. No checkpointer. `_build()` calls `g.compile()` with no `checkpointer=`. The module docstring
   states this as a deliberate choice ("no checkpointer gymnastics"). Consequence: an in-flight
   turn that is not an approval pause is lost on restart, there is no time-travel/replay, and
   `thread_id`-scoped resumption does not exist.
3. `main.py` contains **two further** `StateGraph` instances that are unrelated to it:
   - [backend/main.py:1542](../backend/main.py#L1542) `workflow` — a 2-node linear `manager → developer`
     toy graph exposed at `POST /delegate`.
   - [backend/main.py:3267](../backend/main.py#L3267) `email_workflow`.

### 3.2 Multi-agent architecture — `PARTIAL`

**Proof it is real.** [backend/orchestrator/agents.py](../backend/orchestrator/agents.py):

`_delegate(ctx, to_agent, task)` (line 67) looks up `SPECIALISTS[to_agent]`, builds
`sub = {"id": to_agent, "tools": spec["tools"]}` and calls `graph.run_turn(...)` — a genuine
**nested LangGraph execution** with its own persona and its own tool allowlist. Registered as the
`delegate` tool (line 81). Corroborated by `llm_call` events with
`agent_id ∈ {research_agent, analyst_agent, predictive_agent, email_agent}`.

**Limits.**

- The roster is a **module-level Python dict** (`SPECIALISTS`, line 17), 6 entries. The tool's
  `enum` is `list(SPECIALISTS.keys())` (line 92) — a new agent cannot be added at runtime.
- There is a **second, divergent** `SPECIALISTS` dict in
  [backend/orchestrator/templates.py:57](../backend/orchestrator/templates.py#L57) with 4 entries and
  different tool sets. `templates.SPECIALISTS` seeds the DB; `agents.SPECIALISTS` is what actually
  executes. They disagree: `templates`' `email_agent` has `send_email`, `agents`' does not.
- Sub-agents cannot delegate further (`delegate` is not in any specialist's tool list) and cannot
  perform outbound actions (deliberate — outbound stays on the primary).
- No shared state, no blackboard, no agent-initiated messaging, no concurrency.

### 3.3 Supervisor — `PARTIAL`

The supervisor function is served by `route()` in
[backend/chat/unified.py:71](../backend/chat/unified.py#L71) — three compiled regexes
(`_CHART`, `_DATA`, `_DOMAIN`, `_PERSONAL`) selecting one of three lanes:

```
_PERSONAL and not _CHART        → "primary"   (LangGraph primary agent)
_CHART                          → "chart"     (dashboard chart builder)
_DATA and _DOMAIN               → "data"      (analytics agent)
otherwise                       → "primary"
```

This is a real, deterministic, zero-cost supervisor and it is on the live `/agent/chat` path
([routes/agent_os.py:371](../backend/routes/agent_os.py#L371)). But:

- It is **customer-specific**: `_DOMAIN` matches `zakat|donation|beneficiar|emirate` and the Arabic
  `زكاة|تبرع|مستفيد`.
- It routes **lanes**, not agents. It cannot select among registered agents, cannot decompose a
  task, cannot re-route on failure, and produces no plan artifact.
- Within the primary lane, "which agent" is fixed: always the user's single `primary`.

### 3.4 Agent registry / routing — `PARTIAL`

**Registry: real.** [backend/routes/agent_os.py](../backend/routes/agent_os.py) lines 487–621 provide
`GET/POST/PATCH/DELETE /agent/agents` and `PUT /agent/agents/{id}/permissions`, backed by
`Agent` and `AgentPermission` ([backend/db/models.py:99,122](../backend/db/models.py#L99)) with
org+user ownership checks (`_get_owned`, line 502) and a partial-unique index enforcing one primary
per user (`IntegrityError` handling, line 558).

Live: **16 agent rows, 144 permission rows.**

```
primary    | Aria              | active   | 7
specialist | Finance Agent     | active   | 1
specialist | HR Compliance     | active   | 1
specialist | IT Security Agent | active   | 1
specialist | Legal Agent       | active   | 1
specialist | Research Agent    | active   | 2
```

**Routing: absent.** The only read of the registry on an execution path is
`repo.get_primary_agent` ([routes/agent_os.py:62](../backend/routes/agent_os.py#L62), inside
`_load_primary`). `repo.get_agent` is used **only** by CRUD handlers. Therefore **Finance Agent,
HR Compliance, IT Security Agent and Legal Agent can never execute.** They are configuration
without a consumer.

### 3.5 Agent-to-agent communication — `PARTIAL`

Four mechanisms exist. One works.

| Mechanism | Location | Status |
|---|---|---|
| `delegate` tool → nested `graph.run_turn` | [orchestrator/agents.py:67](../backend/orchestrator/agents.py#L67) | **Working**, synchronous, one hop |
| `delegations` SQLite table + lifecycle | [backend/delegation.py](../backend/delegation.py) | Legacy; 2 rows; separate `KNOWN_AGENTS` tuple |
| `agent_messages` Postgres transport | [db/models.py:217](../backend/db/models.py#L217), `integrations/agent_inbox.py` | **0 rows** — dead |
| `POST /delegate` 2-node LangGraph | [main.py:1542](../backend/main.py#L1542) | Toy; unauthenticated user context (`owner=None`) |

No message bus, no async handoff, no result callback, no NATS/RabbitMQ/Redis Streams.

### 3.6 Tool registry — `PARTIAL`

Runtime B's registry ([backend/orchestrator/registry.py](../backend/orchestrator/registry.py)) is a
clean design: a `Tool` dataclass (line 38) with `name`, `description`, JSON-schema `parameters`,
async `handler`, `required_permission`, `is_outbound`; a module-level `_REGISTRY` dict;
`openai_schemas(allowed)` (line 60) filtering to the agent's allowlist.

**Defect: `required_permission` is dead.** A repository-wide search finds exactly three sites —
the dataclass field (registry.py:44), one assignment (agents.py:95), and one API response field
(routes/agent_os.py:433). **It is never compared against anything.** Authorization is by tool
*name* only:

```python
# backend/orchestrator/graph.py:84
elif name not in state["allowed_tools"]:
    content = f"error: this agent is not permitted to use '{name}'"
```

`AgentPermission.permission` stores tool names, not permission strings
([routes/agent_os.py:600](../backend/routes/agent_os.py#L600) filters against
`registry.all_names()`). So the entire `email.read` / `calendar.write` / `code.run` permission
vocabulary is decorative.

Full inventory of both registries: [docs/tool-inventory.md](tool-inventory.md).

### 3.7 Tool calling — `IMPLEMENTED`

Runtime B ([graph.py:70](../backend/orchestrator/graph.py#L70) `_tools_node`) parses
`message.tool_calls`, JSON-decodes arguments with a fallback to `{}`, resolves via `registry.get`,
enforces the allowlist, awaits `tool.handler(ctx, **args)`, catches `TypeError` (bad args) and
generic exceptions separately, and truncates output to `MAX_TOOL_OUTPUT = 6000`. Every tool call is
answered with a `role: "tool"` message so the protocol stays valid — including the approval
placeholder. Verified by 4,980 `tool_called` events and 1,104 `llm_call` events.

### 3.8 State management / checkpointing — `PARTIAL`

| State | Store | Durable? |
|---|---|---|
| Paused approval (agent + full message list + pending call) | `approvals` table JSONB / SQLite `agent_approvals` — [orchestrator/store.py](../backend/orchestrator/store.py) | **Yes**, restart-safe. 6 live rows |
| Conversation history | JSON file store — [orchestrator/conversation.py](../backend/orchestrator/conversation.py) | Yes, but file-based |
| Chat session registry | `chat_sessions` / `chat_messages` Postgres | Yes |
| Long-term memory | `memory_items` + per-user Qdrant collections | Yes |
| **In-flight graph state (non-paused)** | in-process only | **No** |
| **LangGraph checkpoints** | — | **None** |
| `agent_runs.state` (JSONB "messages / checkpoint") | table exists | **0 rows, no writer** |

The approval mechanism is the strongest part of the system. `_pg_decide`
([store.py:191](../backend/orchestrator/store.py#L191)) is a compare-and-swap
`UPDATE ... WHERE status='pending'` returning `rowcount > 0`, so a double-click cannot execute the
outbound action twice, and it writes an atomic audit event recording `decided_by`.

### 3.9 PostgreSQL — `PARTIAL`

34 tables, live. Live counts: `users` 7, `agents` 16, `agent_permissions` 144, `approvals` 6,
`events` 9,435, `delegations` 2, `agent_runs` **0**, `agent_messages` **0**.

Three structural problems:

1. **`org_id` is written on ~25 tables and never read as a filter.** `grep -rn "org_id ==" backend/`
   returns **nothing**. The enforced isolation boundary is `user_id` everywhere.
2. **Dual backend.** `AGANETI_DATA_BACKEND=postgres` in `.env`, but `backend/events.py`,
   `backend/orchestrator/store.py`, `backend/delegation.py` and `backend/tools.py` all retain
   SQLite paths against `tasks/tasks.db`, and several modules (e.g. `delegation.py`) are
   **SQLite-only**.
3. Two Postgres databases with divergent schemas (`aganeti`, `aganeti_genesis`).

### 3.10 Qdrant — `IMPLEMENTED`

Client construction at [backend/ingest.py:70](../backend/ingest.py#L70); collection lifecycle,
payload indexes and per-source deletion at lines 74–231. Live: `corporate_memory` with
**999 points**, 384-dim cosine, status green, plus 11 `user_memory_<uuid>` collections.

Reachable from the agent: `search_documents` tool →
[registry.py:140 `_search_documents`](../backend/orchestrator/registry.py#L140) →
`backend.ingest.search_corporate(query, 6, ctx["user_id"])` — with ACL scoping to the caller's own
documents plus the shared org corpus.

### 3.11 Neo4j — `PARTIAL`

The graph is real and populated:

```
nodes: 523   relationships: 3,414
labels: User, Project, Email, Document, Conversation, Entity, Organization,
        Person, Company, Location, Message, Meeting, Memory, Task, Repository,
        Issue, PR, Calendar, Technology, Provider, Model, Plugin, API, Service, Server
```

A complete subsystem exists — [backend/knowledge_graph/](../backend/knowledge_graph/): singleton
driver (`client.py:70`), extractor, normalizer, resolver, retriever, ranking, provenance, and a
`GraphRetrievalAPI`.

**But it is unreachable from the LangGraph runtime.** There is **no graph tool** in the 35-tool
orchestrator registry. The only production consumer is the context builder
(`backend/context/providers/graph.py:53`), and `build_ranked_context` is called from exactly three
places:

- [backend/main.py:2377](../backend/main.py#L2377) — **Runtime A's `/chat`**
- `backend/routes/observability*.py` — diagnostics
- `backend/evals/runner.py` — evaluation harness

So the knowledge graph serves the legacy chat path and the eval harness, and is invisible to
`/agent/chat`.

### 3.12 Human-in-the-loop — `PARTIAL`

**On Runtime B it is genuinely enforced.** [graph.py:86](../backend/orchestrator/graph.py#L86):

```python
elif tool.is_outbound:
    # HARD GATE: never execute here.
    prev = registry.preview(name, args)
    pending.append({...})
    content = f"[AWAITING USER APPROVAL] {prev}"
```

The handler is *not* called. `_route_tools` (line 111) returns `"end"` when `awaiting` is set,
terminating the graph. `routes/agent_os._sse` (line 125) persists the paused state.
`POST /agent/approvals/{id}/approve` → `_resume` (line 383) verifies ownership by resolving both
caller and record to the same internal `User`, returns 404 (not 403) to avoid leaking existence,
performs the CAS claim **before** executing, then calls `graph.resume`, which invokes the handler
exactly once. 6 approvals and 2 `kind='approval'` audit events are live.

**On Runtime A it is not enforced** — see §5.3.

### 3.13 Tracing — `MISSING` (Langfuse) with a working substitute

There is no Langfuse. What exists instead:

- `_TraceASGIMiddleware` ([main.py:906](../backend/main.py#L906)) — pure-ASGI (deliberately not
  `BaseHTTPMiddleware`, so SSE is not buffered), propagates/mints `X-Trace-Id`, echoes it on the
  response, binds it to a contextvar, and includes it in the 500 envelope.
- The `events` spine ([backend/events.py](../backend/events.py)) — append-only, dual-backend,
  9,435 rows, with `llm_call` carrying `agent_id`, `tokens_in`, `tokens_out`, `cost_micros`,
  `duration_ms`.
- `/observability/*` — 14 diagnostic endpoints.
- An evidence ledger ([backend/insight/evidence.py](../backend/insight/evidence.py)) that records
  every SQL query and its typed rows, then **recomputes** the figures in the model's answer.

What is missing versus Langfuse: no span tree, no nested trace correlation across the delegate
boundary, no prompt/completion capture, no per-session cost rollup, no UI.

### 3.14 Enterprise connectors — `PARTIAL`

Real, working, first-party:

| Connector | Module | Endpoint |
|---|---|---|
| Gmail | [services/gmail.py](../backend/services/gmail.py) | `gmail.googleapis.com/gmail/v1` |
| Google Calendar | [services/gcalendar.py](../backend/services/gcalendar.py) | `googleapis.com/calendar/v3` |
| Google People | [services/gcontacts.py](../backend/services/gcontacts.py) | `people.googleapis.com/v1` |
| Microsoft Mail/Cal/Contacts | `msmail.py`, `mscalendar.py`, `mscontacts.py` | `graph.microsoft.com/v1.0` |
| Azure SQL (CORE-SHARE) | [dashboard/coreshare_db.py](../backend/dashboard/coreshare_db.py) | read-only, PII-blocked |
| SeaweedFS object store | [backend/storage/](../backend/storage/) | S3 + filer, running |
| SearXNG | [services/websearch.py](../backend/services/websearch.py) | self-hosted, no scraper fallback |
| Supabase | `auth/supabase_client.py` | identity |
| Telegram | [integrations/telegram_bot.py](../integrations/telegram_bot.py) | 1,048 lines |
| LiteLLM gateway | [orchestrator/router.py:33](../backend/orchestrator/router.py#L33) | all model traffic |

OAuth token handling is real: encrypted at rest (`services/token_crypto.py`), dual store
(`_token_pg_store.py` / `_token_file_store.py`), refresh flows for both providers,
`provider_connections` holds 2 Google + 1 Microsoft.

Missing: any ERP, CRM, ITSM, HRIS or ticketing connector; and — more structurally — **any
connector framework**. Each integration is a bespoke module with its own auth, retry and error
handling. There is no registry, no manifest, no capability declaration, no per-connector
rate/quota, and no uniform egress control.

---

## 4. Request traces

Full narrative traces with line-by-line call chains are in
[docs/orchestration-flow.md](orchestration-flow.md). Summary:

**Trace 1 — `POST /agent/chat` "summarise my inbox"**
`AuthEnforceMiddleware` (JWT → sub → `X-Auth-User`, IDOR normalization) → `RateLimitMiddleware` →
`_TraceASGIMiddleware` → `agent_chat` → `unified_stream` → `route()` → lane `primary` →
`agent_os._sse` → `_load_primary` (DB agent + permission-derived allowlist) → memory recall →
`graph.astream_turn` → `_agent_node` (LiteLLM via `router.complete`, fallback chain) →
`_route_agent` → `_tools_node` (`list_emails` → Gmail/Graph) → `_agent_node` → `END` → SSE `done` →
conversation persisted → async memory capture.

**Trace 2 — the approval branch, `"email Sam the Q3 numbers"`**
Identical until `_tools_node` sees `send_email.is_outbound` → **handler not called** →
`awaiting` set → `_route_tools` → `END` → `store.create_approval` (Postgres JSONB) → SSE
`approval_required`. Later `POST /agent/approvals/{id}/approve` → ownership check → CAS claim →
`graph.resume` → handler executes once → `POST /send_email` internal.

**Trace 3 — the live path, `POST /chat` "what's the weather"**
`AuthEnforceMiddleware` → `chat_endpoint` → `build_ranked_context` (Qdrant + Neo4j + calendar +
tasks, concurrent) → hand-rolled `MAX_TOOL_ROUNDS=4` loop → `dispatch_tool_call` →
`guardrails.decide` (deny-check only) → `get_weather` executes → SSE. **No LangGraph, no approval
gate, no registry.**

---

## 5. Authorization path and OPA

Full analysis: [docs/opa-audit.md](opa-audit.md). Headlines:

### 5.1 OPA does not participate in any runtime decision

Exhaustive negative evidence:

```
find . -name "*.rego"                                    → 0 files
grep -rn "OPA" --include=*.py .                          → 1 hit: the word "OPAQUE" in a comment
                                                            (config/settings.py:136)
grep -inE "opa|open-policy" requirements.txt             → 0
grep -niE "opa" docker-compose.yml                       → 0
docker ps -a | grep -i opa                               → none
```

`OPA` appears in `docs/architecture/poc_backlog.json` — i.e. **as a planned item**.

### 5.2 What actually authorizes

Five Python-native layers, in order:

1. **Authentication** — `AuthEnforceMiddleware` ([auth/enforce.py:93](../backend/auth/enforce.py#L93)).
   Supabase ES256 JWT or `X-Internal-Token`; fails closed; injects non-forgeable `X-Auth-User`;
   unconditionally overwrites `user_id` in query string and JSON body (IDOR defence).
2. **Rate limiting** — `RateLimitMiddleware` ([main.py:886](../backend/main.py#L886)).
3. **Per-agent tool allowlist** — `graph.py:84`, sourced from `AgentPermission` rows.
4. **Outbound approval gate** — `graph.py:86`, `Tool.is_outbound`.
5. **Resource ownership** — `_get_owned` / `_resume`'s dual `resolve_user` comparison.

Plus data-layer controls: `validate_select_only` + `validate_no_pii`
([coreshare_db.py:95,124](../backend/dashboard/coreshare_db.py#L95)), and RAG ACL scoping in
`search_corporate`.

### 5.3 `guardrails.py` is `CONFIGURED_BUT_NOT_ENFORCED`

`decide()` returns one of `"auto"`, `"approval"`, `"deny"`. The only enforcement site is
[backend/tools.py:956](../backend/tools.py#L956):

```python
from backend.guardrails import decide
if decide(name) == "deny":
    return f"⚠️ I'm not able to perform that action ('{name}')."
```

**Only `"deny"` is checked.** A verdict of `"approval"` falls through and the tool executes
immediately. So on Runtime A, `draft_email` and `schedule_meeting` — classified `comms`, verdict
`"approval"` under the default `AUTONOMY_LEVEL="standard"` — run without any human sign-off. The
`AUTONOMY_LEVEL` env var has **no effect on behaviour** beyond what `/guardrails` reports.

---

## 6. Confirmed security findings

These were verified against the running instance on port 8001.

| ID | Finding | Evidence |
|---|---|---|
| **S1** | `DELETE /auth/provider/{provider}?user_id=<victim>` is **unauthenticated** and honours a caller-supplied `user_id` — any anonymous caller can destroy any user's stored Google/Microsoft credentials. | `/auth/provider` is in `_PUBLIC_PREFIXES` ([enforce.py:67](../backend/auth/enforce.py#L67)); the public-prefix branch returns *before* auth **and before** `_forward`'s `user_id` rewrite ([enforce.py:104](../backend/auth/enforce.py#L104)); handler reads `user_id: str = Query(...)` ([provider_auth.py:338](../backend/routes/provider_auth.py#L338)). |
| **S2** | `GET /auth/provider/status?user_id=<victim>` is unauthenticated and discloses which providers a given user has connected, plus provider email and scopes. | Live: `curl -s ".../auth/provider/status?user_id=test"` → `200` with body. |
| **S3** | `GET /observability/sessions` enumerates **every** user's `session_id` + `user_id` with no ownership filter; `GET /observability/trace/{session_id}` then returns that session's chat content. Authenticated but not authorized → horizontal privilege escalation. | [observability.py:794](../backend/routes/observability.py#L794) `async def sessions(limit)` — no user parameter, `GROUP BY session_id, user_id` unfiltered; [observability.py:831](../backend/routes/observability.py#L831) `WHERE session_id = :s` with no ownership check. |
| **S4** | The approval gate is absent on the runtime that serves live traffic (§5.3). | `guardrails.decide` "approval" verdict never enforced. |
| **S5** | `web_search` egress is approval-gated on one runtime and auto-executed on the other. | `skills.py:409` `is_outbound=True` vs `guardrails.py:39` `"web_search": "read"`. |
| **S6** | `org_id` is written on ~25 tables and never used as a filter; there is no tenant concept. | `grep -rn "org_id ==" backend/` → 0 results. |
| **S7** (informational) | `run_python` shells out to `docker run` from the API process, which runs as a user in the `docker` group. The Docker arguments are fixed and the model controls only the script body, so the sandbox itself holds (`--network none --read-only --memory 256m --pids-limit 128`, 25 s timeout) — but the blast radius of any argument-injection regression here is host root. | [skills.py:175](../backend/orchestrator/skills.py#L175); `groups` → `matrix … docker`. |

---

## 7. Prioritized implementation gap list

### P0 — blocking architecture / security

| # | Gap | Action |
|---|---|---|
| P0-1 | **S1: unauthenticated credential destruction.** | Remove `/auth/provider` from `_PUBLIC_PREFIXES`; derive `user_id` from `X-Auth-User` only. Keep `/auth/{provider}/connect` and `/callback` public (they need to be). |
| P0-2 | **S3: cross-user chat exposure via `/observability/sessions` + `/trace/{id}`.** | Scope both to the caller, or gate the whole `/observability` router behind an admin role. |
| P0-3 | **S2: unauthenticated provider-status disclosure.** | Same fix as P0-1. |
| P0-4 | **Decide which agent runtime is production.** Until this is settled every other item is built twice and security behaviour is nondeterministic. Recommendation: **Runtime B (LangGraph) is the target**; Runtime A must be reduced to a compatibility shim or retired. | Produce a runtime-consolidation decision record; freeze new tools on Runtime A. |
| P0-5 | **S4/S5: the approval gate does not cover live traffic.** | Either route `/chat` through the orchestrator, or port the `is_outbound` gate into `dispatch_tool_call` and enforce the `"approval"` verdict. Reconcile `web_search` gating across both. |
| P0-6 | **No tenant boundary.** `org_id` written, never enforced. | Introduce a `TenantContext` resolved in middleware and a repository layer that cannot issue an unscoped query. This is a prerequisite for multi-customer, not cleanup. |
| P0-7 | **`required_permission` is dead** — the permission vocabulary is decorative. | Either enforce it in `_tools_node` against a resolved permission set, or delete the field so it stops implying a control that does not exist. |

### P1 — required for the multi-agent foundation

| # | Gap | Action |
|---|---|---|
| P1-1 | **Agent routing to the registry.** 6 registered specialists cannot execute. | Make `delegate` resolve targets from the `agents` table (scoped to org+user), with the in-code dict as seed data only. |
| P1-2 | **Reconcile the two `SPECIALISTS` dicts** (`agents.py` vs `templates.py`) — they disagree on tool sets including `send_email`. | One source of truth; `templates.py` seeds, `agents` table executes. |
| P1-3 | **LangGraph checkpointing.** No `PostgresSaver`, no `thread_id`. | Add a Postgres checkpointer keyed by `thread_id = session_id`; make `agent_runs` the run-of-record (it currently has 0 rows and no writer). |
| P1-4 | **Supervisor.** Regex lane-routing cannot select agents, decompose tasks, or re-route. | Introduce a supervisor node that produces a plan and selects registered agents; keep the regex router as a cheap fast path. |
| P1-5 | **A2A beyond one synchronous hop.** `agent_messages` has 0 rows; `delegations` is SQLite-only. | Pick one transport. Retire the other three. |
| P1-6 | **Unify the tool registries.** Two registries, ~10 colliding names, divergent authorization. | Single registry; single gate; delete `backend/tools.py`'s parallel catalogue once P0-4 lands. |
| P1-7 | **Neo4j is invisible to the LangGraph runtime.** | Expose graph retrieval as a registry tool, or move `build_ranked_context` into the orchestrator's pre-turn context assembly. |
| P1-8 | **Redis is unused.** | Decide: enable it for transient state / rate limiting / SSE fan-out, or remove the settings block and the container so it stops reading as capability. |
| P1-9 | **Customer logic compiled into core** (`_DOMAIN` Arabic charity nouns, `backend/dashboard/` bound to one Azure SQL schema, `DEFAULT_ORG_SLUG`). | Extract to per-tenant configuration. Blocks any second customer. |
| P1-10 | **Tracing.** No span tree; nested delegate runs are not correlated. | Add OpenTelemetry spans (or Langfuse) around `_agent_node`, `_tools_node` and `_delegate`, carrying the existing `X-Trace-Id`. |

### P2 — required for browser-agent integration *(build the substrate, not the browser)*

| # | Gap | Action |
|---|---|---|
| P2-1 | **No tool execution gateway.** Handlers are awaited in-process; no timeout budget per tool, no quota, no egress policy, no isolation. A browser agent is a long-running, high-risk tool and cannot be hung off this. | Introduce a gateway layer between `_tools_node` and handlers: per-tool timeout, concurrency cap, egress allowlist, structured result envelope. |
| P2-2 | **No long-running / async tool contract.** `STEP_BUDGET=8`, 90 s LLM timeout, 25 s sandbox cap, SSE-bound turn. A browser session outlives all of them. | Define an async job contract: tool returns a handle, run is checkpointed, results arrive out-of-band. Depends on P1-3. |
| P2-3 | **No MCP.** `mcp`/`fastmcp` are in the venv but unpinned and unused. Browser tooling is most naturally an MCP server. | Decide MCP in or out. If in: pin it, add a client in the orchestrator, and expose MCP servers as registry tools behind the same gate. |
| P2-4 | **No policy engine at the tool boundary.** With OPA absent and `required_permission` dead, there is nowhere to express "this agent may drive a browser to these domains". | Fold into P0-7 / P3-1. |
| P2-5 | **No per-tool audit granularity.** `events.tool_called` records name+success+ms and an empty `meta`. | Record arguments (redacted), target, and outcome — mandatory before an agent drives a browser. |
| P2-6 | **No credential brokering for tools.** Provider tokens are fetched per-service inside handlers. | A broker that issues scoped, short-lived credentials per tool invocation. |

### P3 — later enhancements

| # | Gap |
|---|---|
| P3-1 | OPA (or Cedar/Casbin) as the policy engine, with `.rego` for tool/agent/data authorization and enforcement points in `_tools_node`, the approval route, and the SQL layer. Only worth doing after P0-7 defines the permission model. |
| P3-2 | Retire SQLite entirely (`tasks/tasks.db` still authoritative for `delegation.py`, parts of `events.py`, `store.py`, `tools.py`). |
| P3-3 | Consolidate the two Postgres databases (`aganeti`, `aganeti_genesis`). |
| P3-4 | Connector framework: manifest, capability declaration, uniform auth/retry/quota; then ERP/CRM/ITSM connectors. |
| P3-5 | Remove the dead `POST /delegate` toy graph and `email_workflow` from `main.py`. |
| P3-6 | Message bus (NATS / Redis Streams) for async multi-agent choreography. |
| P3-7 | Prompt/model registry with versioning — prompts are currently string constants in `templates.py` and `ask.py`. |
| P3-8 | Cost governance: `cost_micros` is computed and logged but no budget is enforced. |
| P3-9 | Harden `run_python` (S7): run the sandbox via a rootless/remote Docker socket rather than the host group. |

---

## 8. Explicitly out of scope for this audit

Per instruction, **no Playwright, Agent Reach, or browser-automation work was designed or
implemented.** §P2 lists only the substrate gaps that must close *before* such work can be
attached safely.

---

## 9. Audit provenance

| Check | Command |
|---|---|
| Container inventory | `docker ps -a` |
| Postgres schema + counts | `docker exec postgres psql -U postgres -d aganeti -c "\dt"` and per-table `count(*)` |
| Event provenance | `SELECT kind, name, count(*), max(ts) FROM events GROUP BY 1,2` |
| Qdrant | `curl -s localhost:6333/collections` , `/collections/corporate_memory` |
| Neo4j | Bolt session: `MATCH (n) RETURN count(n)`, `CALL db.labels()` |
| Live tool registry | `python -c "from backend.orchestrator import registry; registry.all_names()"` (35 with dashboard tools imported) |
| Legacy tool registry | `python -c "from backend.tools import tools_for; tools_for('u')"` (34) |
| Auth behaviour | `curl` against `127.0.0.1:8001` for `/agent/chat`, `/agent/tools`, `/guardrails`, `/auth/provider/status`, `/observability/sessions` |
| OPA / Langfuse / MCP absence | `find -name "*.rego"`, `grep -rn` over `*.py`/`*.yml`/`requirements.txt`, venv listing |

No production code, configuration, or data was modified.
