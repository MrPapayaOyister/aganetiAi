# POC Execution Plan
### "AI Application Processing Agent" — one integrated vertical slice

| | |
|---|---|
| **Repository** | `/home/matrix/aganetiAi` · branch `feat/enterprise-agentic-os` · HEAD `432fd5e` |
| **Inputs** | `target_architecture_v1_gap_analysis.md`, `target_architecture_v1_gap_matrix.json` (268 records), `current_platform_inventory.md`, `phase0_implementation_plan.md`, `phase0_dependency_graph.md`, `messaging_decision_spike.md`, `consolidation_decisions.md` |
| **Strategy change** | From "implement all P0 gaps" → **POC-first**. Target Architecture v1.0 remains frozen. |
| **Status** | PLAN ONLY. No source, configuration, database, Docker or package change. Nothing staged, nothing committed. |
| **Backlog** | `poc_backlog.json` — 30 items |

---

## 0. Five grounding findings that change the POC shape

Verified directly against the working tree during this planning pass. Each one moves work between
"build" and "reuse", so each one changes the backlog.

| # | Finding | Evidence | Effect on the POC |
|---|---|---|---|
| **F-1** | **OCR already exists, is VLM-based, and is multilingual by construction.** `_ocr_pdf_via_vision` renders each PDF page at 150 dpi and asks the vision model to transcribe verbatim (`temperature=0`, `max_pages=6`). It already flows through the Model Gateway. Its own docstring cites *"the Chinese business-licence case"* and *"a photographed business licence"* | `backend/ingest.py:141-180`; falls back from pdfplumber at `:113-116` | **SEE is reuse, not build.** Arabic is *plausible but unproven* — one spike, not a subsystem (POC-24) |
| **F-2** | **GraphRAG is not exposed as a tool.** The registered `search_documents` tool calls `ingest.search_corporate` — **plain Qdrant vector search, top 6, no graph, no fusion**. The frozen pipeline `build_ranked_context` is wired only into legacy chat (`main.py:2382`), the observability probes and the eval harness | `backend/orchestrator/registry.py:140-155`, `:237`; `backend/context/builder.py:294` | **RETRIEVE is one tool wrapper**, not a port. And the six frozen parameters are the **defaults** (`providers/graph.py:35` `top_k=8`, depth → `GRAPH_RETRIEVAL_DEPTH`=1), so a wrapper that passes nothing preserves the freeze |
| **F-3** | **The ingestion loop does not populate Neo4j.** `backend/ingest.py` never imports `backend.knowledge_graph`. Documents land in Qdrant only; the graph's 522 entities / 3414 relationships came from elsewhere | `grep knowledge_graph backend/ingest.py` → nothing; consumers are `context/providers/graph.py`, observability, and `main.py:553` (bootstrap only) | **The biggest hidden POC dependency.** A *newly submitted* application would be searchable in Qdrant and **invisible to the graph** — so "RETRIEVE = Qdrant + Neo4j" is false for new documents until POC-20 closes the write side |
| **F-4** | **The approvals backend is complete.** `GET /agent/approvals`, `POST /agent/approvals/{aid}/approve`, `POST /agent/approvals/{aid}/reject` all exist and are mounted. The frontend has **no approvals page** — `frontend/src/pages/` contains nine pages, none of them approvals | `routes/agent_os.py:378, 419, 424`; `ls frontend/src/pages/` | **P0-10 is mostly a UI task.** The gate, the persistence and the compare-and-swap already work |
| **F-5** | **Langfuse is absent entirely** — zero references in any `.py`, `.txt`, `.ts` or `.tsx` file | repo-wide grep | P1 is genuinely new integration work, not configuration |

### The migration surface, verified as P0-1 asks

The brief says *"4 frontend calls, /suggestions endpoint, approvals UI"*. Confirmed exactly — and
there is **a fifth item the brief does not name**:

| # | Frontend call | Call site | LangGraph equivalent | Status |
|---|---|---|---|---|
| 1 | `POST /api/chat` (SSE) | `hooks/useStream.ts:42` | `POST /agent/chat` (`agent_os.py:354`) | ✅ exists |
| 2 | `GET /api/chat/history` | `pages/AssistantPage.tsx:208` | `GET /agent/history` (`:175`) | ✅ exists |
| 3 | `GET /api/chat/tools` | `components/ToolToggles.tsx:37` | `GET /agent/tools` (`:429`) | ✅ exists |
| 4 | `GET /api/chat/suggestions` | `pages/AssistantPage.tsx:257` | — | ❌ **missing** |
| **5** | **`POST /api/chat/tools`** (writes toggles) | `components/ToolToggles.tsx:68` | `agent_os.py` has **`@router.get("/tools")` only** | ❌ **missing** |

> Item 5 is the tool-toggle **write** path. The consolidation register already flagged that cutting
> `/api/chat` over before the toggles migrate *"silently deletes a shipped feature and breaks 16
> tests"* (`tests/test_tool_reachability.py`). It is a parity item, not a nice-to-have.

A sixth call site, `frontend/src/api/client.ts:71`, posts to a bare `/chat` — a separate client that
must be inventoried before any repoint.

---

## 1. POC architecture

One platform, one runtime, one policy point, one trace. The nine demonstrated stages map onto
existing components wherever they exist.

```
                          ┌───────────────────────────────────────────┐
                          │        Frontend — Application Console      │
                          │  submit · progress · explain · APPROVE     │
                          └───────────────────┬───────────────────────┘
                                              │  SSE
┌─────────────────────────────────────────────▼─────────────────────────────────────────────┐
│                          LangGraph runtime  (AUTHORITATIVE — reuse)                        │
│                          backend/orchestrator/graph.py · /agent/chat                       │
│                                                                                            │
│   UNDERSTAND ──► PLAN ──► COLLABORATE ──► RETRIEVE ──► SEE ──► DECIDE ──► ACT ──► VERIFY   │
│      reuse       NEW        reuse mech      wrap      reuse     NEW       NEW      NEW     │
└───┬──────────────┬─────────────┬──────────────┬─────────┬────────┬────────┬─────────┬──────┘
    │              │             │              │         │        │        │         │
    │         ┌────▼─────┐  ┌────▼──────────────▼─────────▼───┐    │        │         │
    │         │ Planner  │  │        Agent fleet (4)          │    │        │         │
    │         │  Agent   │  │  Document · Knowledge ·         │    │        │         │
    │         │  (NEW)   │  │  Verification · [Enterprise]    │    │        │         │
    │         └────┬─────┘  └────┬────────────────────────────┘    │        │         │
    │              │             │                                  │        │         │
    │              └──────┬──────┘   EventEnvelope over an          │        │         │
    │                     │          IN-PROCESS BUS                 │        │         │
    │                     │          ◄── REPLACEABLE BOUNDARY ──►   │        │         │
    │                     ▼                                          ▼        ▼         ▼
    │       ┌─────────────────────────┐              ┌──────────────────────────────────────┐
    │       │   PolicyInterface       │◄─────────────┤  Tool Gateway (typed Tool registry)  │
    │       │  tool · tenant · risk   │   every call │  backend/orchestrator/registry.py    │
    │       │  · human approval (NEW) │              └───────────────┬──────────────────────┘
    │       └───────────┬─────────────┘                              │
    │                   │ approval required                          │ ConnectorInterface (NEW)
    │                   ▼                                            ▼
    │       ┌─────────────────────────┐         ┌───────────────────────────────────────────┐
    │       │  Approval store (REUSE) │         │  1. Knowledge/Docs   2. M365   3. Nazo    │
    │       │  agent_os.py:378/419/424│         │     (reuse)          (reuse)   MOET mock  │
    │       └─────────────────────────┘         └───────────────────────────────────────────┘
    │
    ▼  every call carries TenantContext
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│  Model Gateway (services/llm.py → LiteLLM :4000 → vLLM :9002)  — ALL model calls, incl. OCR │
├────────────────────────────────────────────────────────────────────────────────────────────┤
│  Qdrant (corporate_memory)  ·  Neo4j (GraphRAG, FROZEN)  ·  PostgreSQL  ·  SeaweedFS        │
├────────────────────────────────────────────────────────────────────────────────────────────┤
│  OBSERVE — existing observability dashboard (24 endpoints, reuse) + Langfuse trace (P1, new)│
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Stage-by-stage: what is reused, what is new

| Stage | Existing component reused | New work | Notes |
|---|---|---|---|
| **UNDERSTAND** | LangGraph cyclic executor; Model Gateway; `_load_primary` persona/prompt/model/allowlist | — | Zero new work |
| **PLAN** | LangGraph state machine as the substrate | **Planner Agent** — decomposition, agent/tool/model selection, dependency ordering, approval detection, retry | **Bounded**: fixed step cap, fixed depth, no self-modification, no uncontrolled loop |
| **COLLABORATE** | The `delegate` tool — a **nested `graph.run_turn`** with its own persona and allowlist | 4 agent definitions; Agent interface; EventEnvelope | **Mechanism exists.** ⚠️ Fix the governance hole first — see §7 R-2 |
| **RETRIEVE** | `build_ranked_context` — the frozen GraphRAG pipeline, already tested by 174 golden cases | **One typed Tool wrapper** + tenant filter passthrough | F-2. Frozen params are the defaults; pass nothing |
| **SEE** | `_ocr_pdf_via_vision` — VLM OCR through the Model Gateway, multilingual | Structured **extraction** (OCR gives verbatim text; POC needs typed fields) + tenant-scoped intake | F-1. ⚠️ Do **not** reuse `ingest_file` wholesale — see §7 R-1 |
| **DECIDE** | `guardrails.py`; `Tool.is_outbound`; `agent_permissions` | **PolicyInterface** (minimal) + a small declarative **rules** set | Composite, most-restrictive. Enforcing for the POC path only |
| **ACT** | Typed `Tool` registry (27 tools); outbound approval gate | **ConnectorInterface** + Nazo/MOET mock connector | `Agent → Tool Gateway → Connector` |
| **VERIFY** | GraphRAG citations; the dashboard's evidence-verification ledger pattern | **Verification Agent** | Checks the decision against retrieved evidence |
| **OBSERVE** | 24-endpoint observability dashboard; `x-trace-id` stamped at `main.py:906` | **Langfuse** (P1) + explainability panel | F-5. Do not duplicate the existing dashboard |

### The messaging boundary — explicitly replaceable

Per the completed decision spike: **no Redis, no NATS, no broker of any kind for the POC.**

- Agent collaboration uses an **in-process bus** — a dict of `asyncio` queues, which is what the
  single-uvicorn-process deployment already effectively is.
- Every message is an **`EventEnvelope`** with `tenant_id` **required and non-Optional**.
- Every transport address is composed by **one function**, `subject_for(event_type, tenant_key, env)`.
- The bus is behind two Protocols — `MessageBus` and `WorkQueue` — so swapping in JetStream later
  touches **one adapter module**.

> **Marked boundary:** `backend/messaging/` is infrastructure, not domain. Nothing outside its
> adapter package may import a transport client. A CI rule enforces this from week 1.

---

## 2. POC vs Enterprise matrix

| Capability | POC implementation | Production implementation | Deferred? | Reason |
|---|---|---|---|---|
| **MUST HAVE NOW** |
| LangGraph authoritative | Repoint 4 frontend calls; add `/suggestions` + `POST /tools`; approvals UI | Legacy runtime deleted; single executor | No | Two runtimes = two security models. Only one enforces approval |
| TenantContext | 7-field type; stop discarding `org_id`; filter Postgres + Qdrant on the POC path | All 11 surfaces; app-Postgres RLS; Gate 8 enforcing | Partial | POC needs a **real** boundary, not all surfaces. A→B denial is the acceptance test |
| Model Gateway | All calls via `services/llm.py`; Azure bypass **redirected** | `plan()`/fallback/cost ported; `ModelProfile` per tenant | Partial | C-8: `gpt-4.1` is already `azure/gpt-4.1` in LiteLLM — an **alias swap**, not an integration |
| Tool Gateway | Typed registry as the single dispatch point; `Agent → Gateway → Connector` | `PluginManifest`; per-tenant tool entitlement; marketplace | Partial | The registry already is the seam; POC formalises the boundary |
| PolicyInterface | Minimal composite: tool + tenant + risk + human approval, **enforcing on the POC path** | Full OPA/Rego, externalised per tenant | Partial | Phase 0 said shadow-mode; the POC needs a **real** decision for the demo |
| Agent Planner | Bounded decomposition, selection, ordering, approval detection, retry | Learned routing, cost optimisation, replanning | Partial | "No uncontrolled autonomous loop" is a hard constraint |
| 4 agents + EventEnvelope | Planner, Document, Knowledge, Verification | Full agent catalog, `AgentManifest`-driven | Partial | Four is enough to prove collaboration |
| GraphRAG retrieval tool | Wrap `build_ranked_context`, frozen params | Tenant-scoped graph reads with an equivalence test | No | The demo has no RETRIEVE without it |
| Document intake + OCR | Reuse VLM OCR; **tenant-scoped** intake path | Durable pipeline, page-level jobs, dedicated OCR worker | Partial | F-1 reuse; the tenant path is new |
| KG write-side for new docs | Extract entities from submitted docs into Neo4j | Full ingestion loop, freshness, provenance | Partial | **F-3** — without it the graph cannot see the application |
| Human approval + audit | Approvals UI over the existing backend; decision record | Expiry sweep, assignment, bulk decisions, delegation | Partial | **F-4** — backend is done |
| Explainability | Panel: understood / planned / agents / documents / rules / why approval / action / trace | Full audit product | No | *"The system must visibly explain"* — this **is** the demo |
| **SHOULD HAVE NOW** |
| Connector Gateway | `ConnectorInterface` + 3 connectors (docs, M365, Nazo mock) | 10+ connectors; uniform auth/retry/audit | Partial | Three proves the contract |
| Nazo/MOET mock | In-repo mock service behind the connector | Real integration | No | The ACT step needs a real external boundary |
| Rules | Small declarative set, versioned, fires with citations | Rules service, authoring UI | Partial | Rules must be *visible* in the demo |
| Langfuse (P1) | Trace user→planner→agent→model→retrieval→tool→approval→result | Full OTel + Prometheus + Grafana | Partial | Do not duplicate the existing dashboard |
| Arabic OCR | **Spike first**, then sample docs | Tuned per-script pipeline | Partial | F-1 says plausible, not proven |
| **DEFINE NOW / IMPLEMENT LATER** |
| `AgentManifest`, `WorkflowManifest`, `PromptTemplate`, `PackManifest`, `PluginManifest`, `ConfigSchema`, `ModelProviderInterface`, `ConnectorInterface`, `PolicyInterface`, `TenantContext`, `EntitlementLicense`, `EventEnvelope` | **All 12 defined as types** in a dependency-free `backend/contracts/`; **only 5 implemented** on the POC path | All 12 implemented, versioned, validated in CI | Partial | Defining is cheap; the *shape* is what Phases 5–8 need. Implement only what the POC path traverses |
| Messaging | `EventEnvelope` + 2 Protocols + in-process adapter | Postgres queue/outbox → JetStream on trigger | Partial | Spike complete: transport reversible, contracts not |
| **DEFER** |
| Full OPA deployment, OpenMetadata, full Control Plane, Pack/Plugin marketplaces, 10+ connectors, SAP/Oracle/Jira/ServiceNow/Power BI, 50k distributed processing, production NATS, distributed workers, advanced learning, model cost optimisation, artifact signing | — | Roadmap | Yes | Named as deferred by the brief; none is on the demo path |
| **DO NOT BUILD** |
| GraphRAG re-tuning | — | — | — | **CLOSED.** 0.82 / depth 1 / top_k 8 / hop_decay 0.55 / fusion 0.60 / max_nodes 100 |
| Embeddings through the Model Gateway | — | — | — | The gateway hosts **no** embedding model; moving them invalidates the frozen 174-case baseline and the 993-point collection (bge-small-en-v1.5 @ 384 dims) |
| A second eval harness | — | — | — | Extend `backend/evals/`; do not create a third |
| Automatic self-modification | — | — | — | Out of scope, and incompatible with "no uncontrolled loop" |

---

## 3. POC execution sequence

Six stages. Each ends in something demonstrable.

### Stage A — Spine (week 1)
Make the authoritative runtime able to retrieve and be tenant-aware.
```
POC-1  GraphRAG tool on LangGraph  ──►  POC-11 Agent interface + EventEnvelope
POC-2  TenantContext (minimum)     ──►  POC-3  tenant-scoped document intake
POC-4  Model Gateway: redirect the Azure bypass
POC-28 Define all 12 contracts as types
```
**Demonstrable:** the LangGraph runtime answers a question using Qdrant **+** Neo4j, under a tenant.

### Stage B — Boundaries (week 2)
```
POC-5  Tool Gateway boundary   ──►  POC-15 ConnectorInterface
POC-6  PolicyInterface (minimal, enforcing on the POC path)
POC-20 KG write-side for submitted documents      ← F-3, the hidden dependency
POC-29 Messaging boundary: in-process bus behind Protocols
```
**Demonstrable:** a tool call is refused by policy; a submitted document appears in the graph.

### Stage C — Agents (week 3)
```
POC-10 Planner Agent (bounded)
POC-12 Document Agent   POC-13 Knowledge Agent   POC-14 Verification Agent
POC-18 Structured extraction   POC-19 Rules
```
**Demonstrable:** a plan is produced, three agents run, rules fire with citations.

### Stage D — Act & approve (week 4)
```
POC-16 Nazo/MOET mock connector   POC-17 M365 connector conformance
POC-7  Approvals UI               POC-21 Audit / decision record
POC-8  /suggestions + POST /tools parity
```
**Demonstrable:** high-risk application → approval → human approves → mock API called → audit row.

### Stage E — Make it one platform (week 5)
```
POC-9  Repoint the frontend to the LangGraph runtime
POC-23 Explainability panel
POC-22 Langfuse trace (P1)
```
**Demonstrable:** the full flagship flow in one UI, with one trace.

### Stage F — Scenarios & hardening (week 6)
```
POC-25 missing document   POC-26 conflicting information   POC-27 high-risk approval
POC-24 Arabic OCR spike → sample documents
POC-30 demo fixtures + reset script
```
**Demonstrable:** all three scenarios, repeatably, from a clean state.

---

## 4. Parallel workstreams

Five streams that share no files once Stage A's contracts land.

| Stream | Owner focus | Items | Shares nothing with |
|---|---|---|---|
| **W1 — Runtime & UI** | LangGraph parity, frontend repoint, approvals UI, explainability | POC-7, 8, 9, 23 | retrieval, connectors |
| **W2 — Retrieval & documents** | GraphRAG tool, intake, OCR, extraction, KG write-side | POC-1, 3, 18, 20, 24 | policy, connectors |
| **W3 — Governance** | TenantContext, PolicyInterface, audit, rules | POC-2, 6, 19, 21 | UI, connectors |
| **W4 — Agents & messaging** | Agent interface, EventEnvelope, 4 agents, planner, bus | POC-10, 11, 12, 13, 14, 29 | connectors, UI |
| **W5 — Connectors & gateway** | Tool Gateway boundary, ConnectorInterface, mock + M365 | POC-4, 5, 15, 16, 17 | retrieval, UI |

**Contention points — assign one owner each:**
- `backend/orchestrator/registry.py` — W4 and W5 both extend `Tool` (add `pack`, `group`). One change.
- `backend/auth/enforce.py` — W3 only. Never touched by two streams (Phase 0 COLLIDE-4).
- `backend/contracts/` — one skeleton owner, per Phase 0 COLLIDE-3, before any stream writes a type.

---

## 5. Demo scenarios

**Flagship:** *A business submits an application with Arabic/English documents.*

```
Submission → Document Agent → OCR → Extraction → Knowledge Agent → Qdrant + Neo4j
   → Rules → Planner → Verification → Human Approval → Nazo/MOET mock → Completion
```

### Scenario 1 — Missing document
- **Setup:** application with trade licence + ID, **no** audited financial statement.
- **Expected:** Document Agent extracts what is present. Knowledge Agent retrieves the requirement
  from the corpus **with a citation**. Rule `REQ-DOC-003` fires. Planner marks the application
  `incomplete` and produces a **specific** request for the named missing document.
- **Proves:** understanding, retrieval grounding, rule firing, graceful incompleteness —
  **no hallucinated approval.**

### Scenario 2 — Conflicting information
- **Setup:** the trade licence states one legal entity name/date; the application form states another.
- **Expected:** Verification Agent detects the conflict **field by field**, cites **both** sources with
  page/chunk provenance, and refuses to proceed. Planner routes to human review.
- **Proves:** multi-document reasoning, verification as a distinct agent, evidence-backed refusal.
- **This is the scenario that most convinces a sceptical reviewer** — the system says *"I don't know,
  and here is exactly why."*

### Scenario 3 — High-risk application requiring approval
- **Setup:** application value above the risk threshold; all documents present and consistent.
- **Expected:** Planner produces a complete plan and a recommendation. PolicyInterface classifies the
  submission action as **high-risk** → approval required. **The outbound call does not happen.**
  A human sees the full explanation and approves. Only then is the Nazo/MOET mock called. An audit
  record links decision → policy → approver → action → result.
- **Proves:** the approval gate is real, it is enforced at the executor, and the action is
  **exactly-once**.

### What the UI must visibly show, for all three

| Question | Source |
|---|---|
| What did it understand? | Extracted fields, with the OCR span each came from |
| What did it plan? | The plan tree: steps, agents, tools, models, dependencies |
| Which agents ran? | Agent timeline from `EventEnvelope` |
| Which documents were retrieved? | GraphRAG citations — Qdrant chunks **and** graph entities |
| Which rules fired? | Rule id, version, inputs, outcome |
| Why was approval required? | The policy decision record, with the deciding rule |
| What action was taken? | Connector request/response, redacted |
| Complete trace | One `trace_id`, end to end, in Langfuse and the existing dashboard |

---

## 6. Acceptance criteria

**Gate 1 — Spine**
1. `/agent/chat` answers using Qdrant **and** Neo4j via the wrapped tool, with citations from both.
2. The six frozen GraphRAG parameters are **unchanged**, asserted by test.
3. `TenantContext` is constructed on every POC request; `enforce.py` no longer discards `org_id`.
4. **Tenant A cannot access Tenant B data** — proven for Postgres rows, Qdrant points and uploaded
   documents, including a variant authenticated by the internal-token path.
5. No model call reaches a provider except through `services/llm.py`, proven by a report-only egress
   scan with a baseline of **zero** on the POC path.

**Gate 2 — Governance**
6. Every tool invocation on the POC path passes through the Tool Gateway and a `PolicyInterface`
   decision; a denied call is refused at the executor, not at the UI.
7. A high-risk action creates a pending approval and **does not execute**; approving it executes it
   **exactly once**; rejecting it never executes.
8. Every decision produces an audit record linking tenant, user, plan, policy, approver and result.

**Gate 3 — Agents**
9. The Planner produces a bounded plan (step cap, depth cap enforced by test); a step failure retries
   per policy and terminates.
10. Four agents run within one turn, exchanging `EventEnvelope`s with a non-null `tenant_id`.
11. The Verification Agent detects the Scenario-2 conflict and blocks completion.

**Gate 4 — End-to-end**
12. All three scenarios run from a clean state, repeatably, in one UI.
13. Each produces one `trace_id` spanning user → planner → agent → model → retrieval → tool →
    approval → result.
14. The explainability panel answers all eight questions in §5 without a developer console.
15. A submitted document is retrievable in **both** Qdrant and Neo4j (closes F-3).
16. An Arabic document produces usable extracted fields, **or** the spike's negative result is
    recorded and the demo uses English with the limitation stated.

---

## 7. Risks

| # | Risk | Severity | Evidence | Mitigation |
|---|---|---|---|---|
| **R-1** | **Document intake silently writes into the shared org corpus.** `ingest_file` defaults to `owner=__org__, org_id=None`, so reusing it wholesale puts Tenant A's application into a corpus Tenant B can search — **failing the single stated P0-2 acceptance test via a reuse decision** | **Critical** | `backend/ingest.py:231`; Phase 0 tenancy trap | POC-3 builds a tenant-scoped intake path that reuses `extract_file_text`/`_ocr_pdf_via_vision` as **primitives** but never `ingest_file` as a pipeline |
| **R-2** | **A delegated outbound action is silently dropped.** `agents.py:74-77` formats a nested run's `awaiting_approval` into a **string** for the parent; the approval is never persisted. Unreachable today only because no specialist has an outbound tool — **the POC's Enterprise Action Agent makes it reachable** | **Critical** | `orchestrator/agents.py:74-77` | Fix as an invariant **before** the Enterprise Action Agent exists; add a test asserting a delegated outbound action creates a persisted approval |
| **R-3** | **Neo4j cannot see new documents** (F-3), so the demo's RETRIEVE looks like Qdrant-only | High | `backend/ingest.py` never imports `knowledge_graph` | POC-20 in Stage B, **before** the agents that depend on it |
| **R-4** | **Arabic OCR is unproven.** The VLM handles CJK per its docstring; Arabic is RTL with different failure modes | High | `ingest.py:141-144` | POC-24 is a **spike in Stage A**, not a Stage-F discovery. Fallback stated in acceptance #16 |
| **R-5** | **CI cannot run any of these tests.** 33 of 35 modules import `backend`; undeclared deps; `import backend.main` transitively pulls **torch (921 MB)** | High | Phase 0 §4 | Break the torch import edge + pinned `requirements-test.txt` before Gate 1 is claimed |
| **R-6** | **`AGANETI_DATA_BACKEND` defaults to `sqlite` while production runs `postgres`**, so POC tests may prove nothing about the shipped system | High | Five modules read a bare getenv; `.env:40` | Freeze it as a declared setting in Stage A; pin the backend in CI |
| **R-7** | Frontend repoint regresses shipped chat | High | 19 of 24 loser-only tools are another developer's uncommitted work | Repoint in **Stage E**, behind a flag, after parity items 4 and 5 land |
| **R-8** | Planner produces an unbounded loop | High | New component | Hard step/depth/token caps enforced by test; **no self-modification**; every tool call still passes policy |
| **R-9** | The evidence base is one `git clean` from deletion — `docs/` untracked, 22 of 35 test modules untracked, 58 of 174 golden cases untracked | High | Phase 0 §3 | The preservation commit precedes POC work, exactly as Phase 0 sequences it |
| **R-10** | Policy enforced for the demo path only, creating a false sense of coverage | Medium | Deliberate scope | State the enforced surface explicitly in the demo script and the audit record |
| **R-11** | OCR is capped at **6 pages** and is **synchronous** (`_llm.complete` inside `extract_file_text`) | Medium | `ingest.py:141`, `:169` | Keep demo documents ≤6 pages, or raise the cap for the POC path only and move the call off the request path |
| **R-12** | Four demo scenarios depend on LLM determinism, and Step 13 measured **62/174 answers differing between identical runs**, session-state dependent | Medium | GraphRAG Step 13 | Pin `temperature=0` on decision paths; make rules/policy **deterministic code**, never model judgement; rehearse from a clean state |

---

## 8. Deferred roadmap

| Horizon | Capability | Trigger to start |
|---|---|---|
| **Right after POC** | Full 12-contract implementation; Gate 8 tenant propagation across all 11 surfaces; app-Postgres RLS | POC accepted |
| | Legacy runtime deletion | Parity ledger empty; GraphRAG on survivor; toggles migrated |
| | Postgres queue + outbox (messaging option D) | Trigger T2/T3/T5, or the SQLite→Postgres completion |
| **Next** | Full OPA/Rego externalised policy | More than one tenant with divergent rules |
| | Pack extraction — Dar Al Ber as a pack; the two-customer proof | Contracts landed; Gate 1 ratcheting |
| | Plugin marketplace; 10+ connectors; SAP/Oracle/Jira/ServiceNow/Power BI | `PluginManifest` + compatibility validation |
| | NATS JetStream | **T1 + T4 together** (a second process **and** cross-process fan-out) |
| **Later** | OpenMetadata; full Control Plane; artifact signing / air-gapped packaging | Phase 8 |
| | 50,000-application distributed processing; advanced distributed workers | Measured backlog, not anticipation |
| | Advanced learning; model cost optimisation | Post-production baseline exists |
| **Never (as scoped)** | Automatic self-modification | Incompatible with "no uncontrolled autonomous loop" |

---

## 9. Exact first implementation task

> ### POC-1 — Expose the frozen GraphRAG pipeline to the LangGraph runtime as a typed Tool
>
> **File:** `backend/orchestrator/skills.py` (registration) — a new handler alongside the existing 27 tools.
>
> **What:** register `knowledge_search` as a typed `Tool` whose handler calls
> `backend.context.build_ranked_context(user_id, query, session_id)` and renders the returned
> `RankedContextBundle` as cited excerpts — Qdrant chunks **and** graph entities, each with its source.
>
> **Explicitly NOT:**
> - **Pass no retrieval parameters.** The six frozen values are already the defaults
>   (`providers/graph.py:35` `top_k=8`; depth → `GRAPH_RETRIEVAL_DEPTH`=1). Passing them explicitly is
>   how a freeze gets accidentally re-tuned.
> - Do **not** modify `search_documents`. It stays as it is (Qdrant-only) — this is additive.
> - Do **not** touch `main.py:2382` or any GraphRAG internals.
>
> **Why this one first:**
> - It is the **only** thing standing between the authoritative runtime and the demo's RETRIEVE step
>   (F-2), and it is a wrapper, not a port.
> - It is **purely additive** — no shipped path changes, so it carries near-zero regression risk.
> - It **unblocks the most**: the Knowledge Agent, the Verification Agent, the rules step and the
>   citation panel all consume it.
> - It is **provable today** against the existing frozen 174-case eval harness.
>
> **Acceptance:**
> 1. `/agent/chat` answers a corpus question using the tool and returns citations from **both**
>    Qdrant and Neo4j.
> 2. A test asserts the six frozen parameters are unchanged at the call site.
> 3. The user-scoped ACL that `search_documents` already honours (`registry.py:143` — *"scope to the
>    caller's own docs + the shared org corpus, never other users'"*) is preserved, and the handler
>    takes the tenant filter as an argument it will pass once POC-2 lands.
> 4. `backend/evals/` re-runs with an unchanged score band.
>
> **Estimated complexity:** S — one handler, one registration, one test. Roughly a day including the
> eval re-run.

---

*Plan only. No source, configuration, database, Docker or package change has been made.
Nothing staged, nothing committed.*
