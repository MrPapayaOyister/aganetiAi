# Runtime B Readiness Assessment

**Date:** 2026-08-14 · **Phase 4 of the Runtime B migration**
**Companions:** [runtime-tool-reconciliation.md](runtime-tool-reconciliation.md) · [runtime-migration.md](runtime-migration.md) · [authorization-model.md](authorization-model.md) · [p0-remediation.md](p0-remediation.md)

---

## Verdict

# READY FOR CANARY — NOT READY FOR GENERAL TRAFFIC

The completion criterion is **met and demonstrated on the live stack**: a real HTTP
request executes entirely through Runtime B — API → TenantContext → LangGraph →
agent → tool registry → authorization boundary → tool → result — including Qdrant
and Neo4j, with no path around the authorization boundary.

It is **not** ready for general traffic, and the blocker is not the runtime. It is
that **every seeded agent lacks the 14 new tool grants** (§7 G1), so a user moved
today would silently lose the media capabilities that account for most live usage.
That is a one-statement migration, but it must be run and verified per cohort
before the flag is widened beyond a canary.

Recommended next action: **stage 1 (one pinned session)**, after running the grant
in §6.

---

## 1. The completion criterion, demonstrated

Run against the live stack (LiteLLM, Postgres, Qdrant, Neo4j) on a canary instance
with `RUNTIME_B_USERS` set to one internal user. Production was not touched.

```
POST /chat   {"message":"Use graph_search to find what the Agentic AI project is connected to."}
             X-Internal-Token / X-Internal-User: 84541ce0-…

HTTP/1.1 200 OK
x-runtime: B
x-runtime-reason: pinned_user
{"reply":"The Agentic AI project is connected to the following:
          1. People: Rohit Menon, Fatima Al Marzooqi, Akshay, Sara Haddad, …
          2. Technologies: TypeScript, PostgreSQL, Neo4j, FastAPI, LangGraph, Qdrant, …
          3. Documents/Meetings: Sprint Planning: Agentic AI (W01), … "}
```

Each hop, with the code that performs it:

| Hop | Component | Evidence |
|---|---|---|
| API | `POST /chat` → `chat_endpoint` | `x-runtime: B` header present |
| Runtime selection | `runtime_flag.choose` → `_serve_via_runtime_b` | `x-runtime-reason: pinned_user`; a **non-pinned** user on the same instance returned **no** `x-runtime` header (stayed on Runtime A) |
| TenantContext | `agent_os._load_primary` → `repo.resolve_user` → `agent["tenant_id"]` | tenant `c17a0942…` threaded into `AgentState` |
| LangGraph | `graph.run_turn` → `GRAPH.ainvoke` | cyclic agent⇄tools loop |
| Agent | DB primary agent + permission-derived allowlist | grant required before the tool resolved |
| Registry | `registry.get("graph_search")` | 43 tools registered on package import |
| **Authorization** | `authz.authorize_call` in `_tools_node` | ALLOW `rule=grant:name`; a call with no tenant is DENY `rule=no_tenant` |
| Tool | `GraphService.tenant_*` → **Neo4j** | 3 entity classes + relationships returned |
| Result | bridge → `{"reply": …}` | Runtime A's response shape preserved |

**Qdrant, same path:**
```
{"reply":"The Evaluation Framework is an automated analysis pipeline designed for
          Dar Al Ber Society, covering design, build, and integration…"}     x-runtime: B
```

**Approval gate, same path:**
```
{"reply":"I need your approval first: Search the web for: 'latest LangGraph release notes' (top 5 results)",
 "approval":{"id":"e376afc6-…","action_type":"web_search"}}                   x-runtime: B
```
and the row was durably persisted:
```
tool_key   | action_type | status  | preview
web_search | web_search  | pending | Search the web for: 'latest LangGraph release notes' (top 5 …
```
**The search never executed.** The test approval and the canary grant were both
rolled back afterwards; the agent's permission set was verified byte-identical to
its pre-test backup.

---

## 2. Tools migrated

19 Runtime A capabilities → **13 Runtime B tools**. Full per-tool record in
[runtime-tool-reconciliation.md §3](runtime-tool-reconciliation.md).

| Family | Tools | Notes |
|---|---|---|
| YouTube | `play_youtube_video`, `get_youtube_video_info`, `search_youtube` | 1:1; `max_results` clamped 1–10 |
| Live TV | `watch_live_tv`, `search_tv_channels`, `add_tv_channels` | two add-tools merged |
| Radio | `play_radio`, `search_radio_stations`, `my_radio`, `manage_radio_stations` | **nine tools → four** |
| Utility | `get_weather`, `get_news`, `generate_qr_code` | 1:1 |

Plus **`graph_search`** (Phase 3), which has no Runtime A ancestor.

Every one calls the *same* `backend/services/*` function Runtime A calls, so the
runtimes cannot drift.

---

## 3. Tools intentionally not migrated

| Runtime A tool | Reason | Runtime B equivalent |
|---|---|---|
| `get_emails` | superseded | `list_emails` (returns ids, so `read_email` can follow) |
| `get_contacts` | superseded | `resolve_contact` |
| `recall_memory` | superseded | `search_memory` |
| `search_knowledge` | superseded | `search_documents` + `knowledge_search` |
| `schedule_meeting` | superseded **and unsafe** | `create_calendar_event` — approval-gated; A's is not |

---

## 4. Parity

### 4.1 Byte-identical results at the same layer

Same tool, same arguments, both runtimes, compared at the post-split layer
(`execute_single_tool` on A, the registry handler + embed sink on B):

| Tool | A ms | B ms | A text | B text | A embeds | B embeds | identical text |
|---|---|---|---|---|---|---|---|
| `get_weather` | 1397 | 1222 | 206 | 206 | 1 | 1 | **yes** |
| `get_news` | 408 | 395 | 1160 | 1160 | 1 | 1 | **yes** |
| `generate_qr_code` | 46 | 4 | 57 | 57 | 1 | 1 | **yes** |

Identical model-facing text and identical embed counts — the shared service layer
doing its job.

### 4.2 Authorization: where the runtimes deliberately differ

| Tool | Runtime A | Runtime B | Assessment |
|---|---|---|---|
| `web_search` | `auto` — executes | `approval` — pauses | **B correct.** The query egresses |
| `draft_email` | `approval`, **not enforced** → executes | `auto` — executes | Equivalent in effect; B's classification is honest (a draft *is* the approval step) |
| `schedule_meeting` / `create_calendar_event` | `approval`, **not enforced** → creates a real event | `approval` — pauses | **B correct.** This is the strongest single argument for migrating |
| `get_weather` | `auto` | `auto` | same |

### 4.3 Parity gaps closed since [runtime-migration.md §5](runtime-migration.md)

| # | Gap | Status |
|---|---|---|
| P1 | 24 consumer tools absent from B | **CLOSED** — 19 capabilities migrated, 5 superseded |
| P2 | No knowledge-graph path in B | **CLOSED** — `graph_search` (direct) + `knowledge_search` (fused Qdrant+Neo4j) |
| P4 | Approval pause is a visible UX change | **VERIFIED** — the bridge returns a structured `approval` object alongside `reply` |
| P6 | `web_search` gated on B, auto on A | **Intended and confirmed**; B is correct |

### 4.4 Parity gaps still open

| # | Gap | Severity | Effect on rollout |
|---|---|---|---|
| P3 | **SSE frame vocabulary differs.** A emits `thinking/token/action/error/done`; B emits `backend/chat/frames.py`. A streaming client will not understand the other's frames | **blocking for `stream: true`** | Enable streaming cohorts only after the client handles both. Non-streaming `/chat` is unaffected and is what was validated |
| P5 | **Passive task detection** and the task-confirmation state machine exist only on A | medium | A user on B loses the "I noticed a potential task…" prompt |
| P7 | **Telegram** posts to `/chat` and follows the flag | medium | Pin Telegram users explicitly, or leave them on A |
| P8 | **Widget embeds are dropped on every Runtime B path.** `consumer_tools` splits the HTML out correctly and offers a per-turn sink, but **no production code binds it** — `grep bind_embed_sink backend/` returns only the definition | medium | A user on B gets the text ("Now playing: …") with **no player, picker or QR image** |

**P8 is new and was found by this work; state it precisely.** The splitter is
correct and tested — widget markup never reaches the model, which was the risk
being designed against. What does not exist yet is the *delivery* half: neither
`_serve_via_runtime_b` (whose `{"reply": …}` shape has no field for embeds) nor
`agent_os._sse` (which has an `artifact` frame but does not bind the sink) hands
the HTML to a client. Both need wiring before any cohort that uses media tools.
The tests assert the sink behaviour when bound, not that anything binds it.

---

## 5. Results by workflow

`scripts/validate_runtime_b.py`, live stack, no stubs:

```
Runtime B validation — 43 tools registered

[PASS] registry_completeness         0ms  tools=43 unclassified=[] missing_permission=[]
[PASS] cross_tenant_isolation        0ms  tenantA=c17a0942 tenantB=ef75b93a distinct=True
                                          no_tenant=no_tenant with_tenant=allow
[PASS] simple_chat                 314ms  status=done final='ready'
[PASS] enterprise_lookup           783ms  tools=['list_tasks']
[PASS] qdrant_search              4580ms  tools=['search_documents'] bytes=3512
[PASS] neo4j_graph_search         7016ms  tools=['graph_search'] entities_or_rels=True
[PASS] web_search_approval         643ms  status=awaiting_approval rule=outbound
[PASS] approval_required           837ms  status=awaiting_approval tool=send_email placeholder=True
[PASS] denied_tool                1496ms  never_offered=True

9/9 workflows passed
```

### Authorization results

| Case | Expected | Observed |
|---|---|---|
| granted tool | ALLOW, executes | ✅ `rule=grant:name` |
| permission-string grant | ALLOW | ✅ `rule=grant:permission` |
| not granted | DENY, handler never runs | ✅ `rule=not_granted`; graph never queried |
| no tenant | DENY | ✅ `rule=no_tenant`, **before** any tool-specific logic |
| no subject | DENY | ✅ `rule=no_subject` |
| kill switch | DENY, beats the grant | ✅ `rule=kill_switch` |
| cross-tenant (two **real** users, different orgs) | isolated | ✅ `c17a0942` ≠ `ef75b93a`; neither borrows the other |

### HITL results

| Case | Observed |
|---|---|
| `send_email` | paused; `[AWAITING USER APPROVAL]` placeholder in the tool message; handler not called |
| `web_search` | paused; approval row persisted `status=pending`; search not executed |
| approval carries its reason | `rule=outbound`, `risk_level=outbound` |
| resume re-authorizes | revoked grant → refused at resume (`tests/test_executor_enforcement.py`) |

### Performance

| Workflow | Latency | Note |
|---|---|---|
| simple chat | 314 ms | one LLM call |
| enterprise lookup | 783 ms | 2 LLM calls + Postgres |
| approval pause | 643–837 ms | one LLM call, no tool execution |
| Qdrant search | 4,580 ms | **includes one-off embedding-model load**; warm ≈ 1.2 s |
| Neo4j graph search | 7,016 ms | 2 LLM calls + 3 Cypher passes; the graph itself is ~50 ms |
| Runtime A vs B, same tool | within ±15 % | §4.1 |

No latency regression attributable to the authorization boundary: `authorize()` is
pure and in-process, and the deny path never reaches a service.

---

## 6. Required migration step before any cohort

**Existing agents have 18 permissions and none of the 14 new tools.** A user moved
today would lose YouTube, TV, radio, weather, news, QR and the graph.

```sql
-- Grant the migrated consumer + graph tools to ONE canary agent.
-- Verify with the SELECT first; keep its output as the rollback baseline.
SELECT string_agg(permission, ',' ORDER BY permission)
  FROM agent_permissions WHERE agent_id = '<agent-uuid>';

INSERT INTO agent_permissions (org_id, agent_id, permission, is_outbound)
SELECT a.org_id, a.id, p, false
  FROM agents a
  CROSS JOIN unnest(ARRAY[
    'play_youtube_video','get_youtube_video_info','search_youtube',
    'watch_live_tv','search_tv_channels','add_tv_channels',
    'play_radio','search_radio_stations','my_radio','manage_radio_stations',
    'get_weather','get_news','generate_qr_code','graph_search'
  ]) AS p
 WHERE a.id = '<agent-uuid>'
ON CONFLICT DO NOTHING;
```

Rollback: `DELETE FROM agent_permissions WHERE agent_id = '<agent-uuid>' AND permission = ANY(ARRAY[...]);`

This exact sequence was executed and reversed during validation; the permission set
was verified identical to its backup afterwards.

Alternatively grant by **permission string** — `media.radio.read` covers
`play_radio`, `search_radio_stations` and `my_radio` in one row.

---

## 7. Remaining risks

| # | Risk | Severity |
|---|---|---|
| **G1** | Seeded agents lack the new grants (§6). Silent capability loss if a cohort is moved first | **blocking for rollout** |
| **G2** | **No Runtime B path binds the embed sink**, so players/pickers/QR images are dropped on both the bridge and the SSE route (parity P8) | **blocking for media cohorts** |
| **G3** | SSE frame vocabularies differ (parity P3) | **blocking for streaming cohorts** |
| **G4** | The graph carries **no tenant property**. `graph_search` filters on a configurable key in lenient mode, so the predicate is a no-op on today's data. `GRAPH_TENANT_STRICT=true` is exclusive and returns nothing on this graph. **The graph is single-tenant until ingestion stamps the property** | P1 |
| **G5** | `knowledge_search` carries the tenant on its result but passes it to no retrieval filter; its Neo4j half is unscoped | P1 |
| **G6** | `query_data` reads the customer's Azure SQL with no row-level tenant predicate — protected by SELECT-only + a PII blocklist, not by tenancy | P1 |
| **G7** | No LangGraph checkpointer; `agent_runs` still has no writer. An interrupted Runtime B turn is lost | P1 — blocks stage 5 |
| **G8** | Six registered specialist agents still cannot execute; `delegate` routes to a hardcoded dict | P1 |
| **G9** | Runtime A's `schedule_meeting` still creates real calendar events unapproved. Mitigated by `AGANETI_DENIED_TOOLS=schedule_meeting`, verified in test; **not enabled by default** | P0 until the flag is set or traffic moves |
| **G10** | Runtime B lacks passive task detection (parity P5) | P2 |
| **G11** | `tests/test_insight_evidence.py` and `tests/test_routing.py` `sys.exit()` at import and crash pytest collection for the whole run. Both pre-existing and unmodified; excluded to obtain a suite result | P1 — any CI gate on this suite is unreliable |
| **G12** | 4 pre-existing `tests/test_analytics.py` failures (`backend.analytics` has no `DB_PATH`). Confirmed absent at `HEAD`; not touched by this work | P2 |

---

## 8. Failures encountered and fixed during validation

Recorded because each was a real defect that only live execution exposed.

| # | Failure | Fix |
|---|---|---|
| 1 | `graph_search` returned "nothing is recorded" for "Agentic AI **project**" — the node is "Agentic AI". Exact and substring both missed, and the answer read as an authoritative absence | Added a third resolution pass on significant tokens, ranked by hit count then degree. `tenant_search_entities_any_token` + `_terms()`, with tests |
| 2 | Four unrelated `test_graph_retrieval` tests failed **only in a full-suite run** | My module-level Neo4j probe built the process-wide driver singleton during pytest *collection*, before that suite set up its own. Made the skip decision lazy (`_require_graph()` inside the test) |
| 3 | My widget test fakes did not split | `process_tool_result` keys on `Content-Disposition: inline`, which the real services set and my fakes did not. Tests corrected to the real contract |
| 4 | Two `list_metrics`/`run_metric` tools escaped the policy table | Registration was route-import-dependent. All registration moved into `backend/orchestrator/__init__.py`, so the canonical registry is complete in every process |

---

## 9. Rollback

| Change | Rollback | Deploy needed |
|---|---|---|
| Runtime cohort | unset `RUNTIME_B_*`, or add the user to `RUNTIME_B_DENY_USERS` (beats every opt-in) | no |
| Tool grants (§6) | `DELETE FROM agent_permissions WHERE agent_id=… AND permission = ANY(…)` | no |
| A specific tool misbehaving | `AGANETI_DENIED_TOOLS=<name>` — refused ahead of any grant, on **both** runtimes | restart |
| Strict tenancy | `AUTHZ_STRICT_TENANT=false` — **emergency only**; reinstates pre-P0 unscoped tool calls | restart |
| Graph tenant filter | `GRAPH_TENANT_STRICT=false` (the default) | restart |
| OAuth connect fix / observability scoping | **no runtime toggle, by design** — these are the security fixes; reverting requires a code revert | — |

Runtime A is untouched and remains the default for everyone. No schema migration
was applied in any phase, so there is nothing to un-migrate.

---

## 10. Path to READY for general traffic

| # | Gate | Blocking |
|---|---|---|
| 1 | Grant the 14 new permissions per cohort (§6) | **yes** (G1) |
| 2 | Bind the embed sink and emit widgets — on the bridge (needs a response field) **and** the SSE route (`artifact` frame) | **yes** for media cohorts (G2) |
| 3 | Client understands both SSE vocabularies, or streaming stays on A | **yes** for streaming (G3) |
| 4 | Set `AGANETI_DENIED_TOOLS=schedule_meeting` until traffic moves | **yes** (G9) |
| 5 | Stamp the graph with a tenant property, then `GRAPH_TENANT_STRICT=true` | before a second customer (G4) |
| 6 | LangGraph checkpointer + `agent_runs` as run-of-record | before stage 5 (G7) |
| 7 | Decide passive task detection: port or drop | before general traffic (G10) |
| 8 | Fix the two collection-crashing test files so CI can gate | (G11) |

Stages 1–3 of [runtime-migration.md §4](runtime-migration.md) can begin as soon as
gates 1 and 4 are done.

---

## 11. Test evidence

| Suite | Cases | Result |
|---|---|---|
| `tests/test_oauth_connect_idor.py` | 23 | pass |
| `tests/test_runtime_a_killswitch.py` | 10 | pass |
| `tests/test_consumer_tools_migration.py` | 108 | pass |
| `tests/test_graph_search_tool.py` | 35 | pass |
| `tests/test_authz_boundary.py` | 24 | pass |
| `tests/test_executor_enforcement.py` | 16 | pass |
| `tests/test_tenant_isolation.py` | 20 | pass |
| `tests/test_runtime_flag.py` | 25 | pass |
| **Full suite** | — | **802 passed, 4 failed** (all four pre-existing, §7 G12) |
| `scripts/validate_runtime_b.py` | 9 workflows | **9/9 pass** |
