# Runtime Tool Reconciliation

**Date:** 2026-08-14 · **Phase 2 of the Runtime B migration**
**Companions:** [runtime-migration.md](runtime-migration.md) · [authorization-model.md](authorization-model.md) · [runtime-b-readiness.md](runtime-b-readiness.md)

Runtime B's registry (`backend/orchestrator/registry.py`) is now the **canonical**
registry. This document records every tool in both runtimes, what happened to it,
and why.

Inventories were taken at runtime, not read off source:

```bash
python -c "from backend.tools import tools_for; print(len(tools_for('u')))"                 # A: 34
python -c "import backend.orchestrator; from backend.orchestrator import registry; \
           print(len(registry.all_names()))"                                                # B: 43
```

---

## 1. Summary

| | Before Phase 2 | After |
|---|---|---|
| Runtime A tools | 34 | 34 (**unchanged — nothing added, nothing removed**) |
| Runtime B tools | 29 + 7 process-dependent | **43, all registered on package import** |
| Capabilities in A but not B | 24 | **0** |
| Runtime B tools with no `required_permission` | 3 (`current_time`, `calc`, `set_reminder`) | 3 (deliberate — no data access, no side effect) |
| Runtime B tools missing from the policy table | 2 | **0** |

Of the 24 A-only tools: **5 were already covered** by a Runtime B tool under a
different name, and **19 real capabilities were migrated** — implemented as
**13 tools**, because five radio tools and two TV tools were one capability each.

---

## 2. Migration decisions

### 2.1 Superseded — NOT migrated (5)

A tool with an equivalent already in Runtime B. Copying it would have created two
names for one capability and split the permission model.

| Runtime A | Runtime B equivalent | Why the B version is canonical |
|---|---|---|
| `get_emails` | `list_emails` | Same `services.mailbox.inbox` call. B's returns message **ids**, so `read_email` can then open one — A's could not |
| `get_contacts` | `resolve_contact` | Same Google People / Graph lookup; B's returns a structured single contact rather than a list blob |
| `recall_memory` | `search_memory` | Same `memory.long_term.search_memory` |
| `search_knowledge` | `search_documents` **and** `knowledge_search` | Both hit the same Qdrant corpus. `knowledge_search` additionally federates Neo4j and returns citations |
| `schedule_meeting` | `create_calendar_event` | **Materially better**: B's is `is_outbound=True` and pauses for approval. A's creates a real calendar event with no sign-off — see [§5](#5-schedule_meeting) |

### 2.2 Migrated (19 capabilities → 13 tools)

| Runtime A tool(s) | Runtime B tool | Decision |
|---|---|---|
| `play_youtube_video` | `play_youtube_video` | 1:1 |
| `get_youtube_video_info` | `get_youtube_video_info` | 1:1 |
| `search_youtube` | `search_youtube` | 1:1, `max_results` now clamped 1–10 |
| `watch_live_tv` | `watch_live_tv` | 1:1 |
| `search_tv_channels` | `search_tv_channels` | 1:1 |
| `add_tv_channel` + `add_tv_channels_bulk` | **`add_tv_channels`** | **merged** — one verb, two argument shapes |
| `uae_radio` + `arabic_radio` + `quran_radio` + `radio_by_genre` + `search_radio` | **`play_radio`** | **merged** — five schemas for "play a station matching X" |
| `search_radio_stations` | `search_radio_stations` | 1:1 (browse, no player) |
| `my_radio` | `my_radio` | 1:1 |
| `add_radio_stations_bulk` + `remove_radio_station` | **`manage_radio_stations`** | **merged** — `action: add\|remove` |
| `get_weather` | `get_weather` | 1:1 |
| `get_news` | `get_news` | 1:1 |
| `generate_qr_code` | `generate_qr_code` | 1:1 |

**Why merge.** Handing a model five near-identical schemas measurably degrades tool
selection, and a migration is the moment to fix that rather than carry it forward.
Each merged handler still calls the *same* service function Runtime A calls, and a
test asserts each branch lands on the right one
(`test_play_radio_preset_routes_to_the_right_service_fn`).

### 2.3 Added in Phase 3, no Runtime A ancestor (1)

| Tool | Purpose |
|---|---|
| `graph_search` | Direct, tenant-scoped Neo4j entity + relationship lookup. See [§6](#6-the-knowledge-graph-path) |

---

## 3. Full tool register

Legend — **A**: in Runtime A · **B**: in Runtime B · **Appr**: approval required at
`AUTONOMY_LEVEL=standard` · **Tenant**: how the tenant constrains the call.

Every Runtime B row passes `authz.authorize_call` in `graph._tools_node`; a call
with no tenant is DENIED (`rule=no_tenant`) before the handler runs. That is what
"required" means in the Tenant column and it is uniform, so the column records the
*additional* constraint each tool applies.

### 3.1 Communication and calendar

| Tool | A | B | Implementation | Arguments | Result | Side effects | Permission | Risk | Appr | Tenant | Canonical |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `list_emails` | as `get_emails` | ✅ | `services.mailbox.inbox` → Gmail/Graph | `max_results:int` | text list with `[id]` per message | none | `email.read` | read | no | required; provider token is per-user | **B** |
| `read_email` | ✅ | ✅ | `services.mailbox` | `message_id:str` | message body text | none | `email.read` | read | no | required | **B** |
| `email_digest` | — | ✅ | `services.mailbox` | — | summary text | none | `email.read` | read | no | required | **B** |
| `draft_email` | ✅ | ✅ | pure formatting | `to,subject,body` | draft text | **none — nothing is sent** | `email.read` | read | no | required | **B** |
| `send_email` | — | ✅ | `_internal_post("/send_email")` | `to,subject,body` | "Email sent." | **sends mail** | `email.send` | outbound | **YES** | required | **B** |
| `get_agenda` | ✅ | ✅ | `services.mailbox.agenda` | `days_ahead:int` | event list text | none | `calendar.read` | read | no | required | **B** |
| `next_event` | — | ✅ | `services.mailbox` | — | one event | none | `calendar.read` | read | no | required | **B** |
| `create_calendar_event` | as `schedule_meeting` | ✅ | `_internal_post("/schedule_meeting")` | `title,start,attendees` | confirmation | **creates a real event** | `calendar.write` | outbound | **YES** | required | **B** |
| `resolve_contact` | as `get_contacts` | ✅ | Google People / Graph | `name_or_email` | contact line | none | `contacts.read` | read | no | required | **B** |

### 3.2 Tasks, memory, deals

| Tool | A | B | Implementation | Arguments | Result | Side effects | Permission | Risk | Appr | Tenant | Canonical |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `list_tasks` | — | ✅ | `tasks.store.get_all_tasks` | `status?` | ≤25 tasks | none | `tasks.read` | read | no | required; rows scoped by user | **B** |
| `create_task` | ✅ | ✅ | task store | `title,priority,due_date` | confirmation | writes a task | `tasks.write` | write | no | required | **B** |
| `complete_task` | ✅ | ✅ | task store | `title` | confirmation | closes a task | `tasks.write` | write | no | required | **B** |
| `set_reminder` | ✅ | ✅ | `backend.reminders.add` | `message,remind_at` | confirmation | schedules a notification | *(none)* | write | no | required | **B** |
| `remember_fact` | ✅ | ✅ | long-term memory | `fact` | confirmation | writes memory + vector | `memory.write` | write | no | required | **B** |
| `search_memory` | as `recall_memory` | ✅ | `memory.long_term.search_memory` | `query` | recalled facts | none | `memory.read` | read | no | required; per-user Qdrant collection | **B** |
| `create_opportunity` | — | ✅ | Postgres `opportunities` | `title,value_amount,…` | confirmation | writes a deal | `deals.write` | write | no | required | **B** |
| `list_opportunities` | — | ✅ | Postgres | — | deal list | none | `deals.read` | read | no | required | **B** |
| `predict_task_slippage` / `predict_followups` / `predict_relationship_value` / `predict_deal_outcome` | — | ✅ | computed in Python from user data | varies | forecast + evidence | none | `predictions.read` | read | no | required | **B** |

### 3.3 Knowledge and data

| Tool | A | B | Implementation | Arguments | Result | Side effects | Permission | Risk | Appr | Tenant | Canonical |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `search_documents` | as `search_knowledge` | ✅ | `ingest.search_corporate` → **Qdrant** | `query` | cited excerpts | none | `documents.read` | read | no | required; ACL = own docs + org corpus | **B** |
| `knowledge_search` | — | ✅ | `context.build_ranked_context` → **Qdrant + Neo4j**, fused | `query` | `[D#]`/`[G#]` citations | none | `documents.read` | read | no | required; carried, not yet a graph filter | **B** |
| `graph_search` | — | ✅ | `GraphService.tenant_*` → **Neo4j** | `query,limit,expand` | entities + relationships | none | `knowledge.graph.read` | read | no | **required and applied as a Cypher predicate** | **B** |
| `get_database_schema` | — | ✅ | `coreshare_db` (Azure SQL) | — | tables/columns, PII hidden | none | `charts.read` | data | no | required | **B** |
| `query_data` | — | ✅ | `coreshare_db.run_query_cached` | `sql` | ≤50 rows JSON | none (SELECT-only) | `charts.read` | data | no | required; **not** row-level scoped — see [§7](#7-known-gaps) | **B** |
| `forecast_metric` / `compare_periods` / `list_metrics` / `run_metric` | — | ✅ | dashboard metric layer | varies | numbers | none | `charts.read` | data | no | required | **B** |
| `save_chart` / `delete_chart` / `list_charts` | — | ✅ | `dashboard.config_db` | varies | confirmation / list | writes a board | `charts.write` / `charts.read` | write / read | no | required; board is session-scoped | **B** |
| `get_analytics` | ✅ | ✅ | events spine | `metric,days` | usage stats | none | `analytics.read` | read | no | required | **B** |

### 3.4 Consumer capabilities — migrated in Phase 2

| Tool | A | B | Implementation (shared service) | Arguments | Result | Side effects | Permission | Risk | Appr | Tenant | Canonical |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `play_youtube_video` | ✅ | ✅ | `services.youtube.play_youtube_video` | `video` | text + **player embed** | none | `media.youtube.read` | egress | no | required | **B** |
| `get_youtube_video_info` | ✅ | ✅ | `services.youtube.get_youtube_video_info` | `video` | title/channel text | none | `media.youtube.read` | egress | no | required | **B** |
| `search_youtube` | ✅ | ✅ | `services.youtube.search_youtube` | `query,mode,max_results` | player or list + embed | none | `media.youtube.read` | egress | no | required | **B** |
| `watch_live_tv` | ✅ | ✅ | `services.livetv.watch_live_tv` | `channel?` | player embed | none | `media.tv.read` | egress | no | required; library is per-user | **B** |
| `search_tv_channels` | ✅ | ✅ | `services.livetv.search_tv_channels` | `name,category,country` | picker embed | none | `media.tv.read` | egress | no | required | **B** |
| `add_tv_channels` | as 2 tools | ✅ | `livetv.add_tv_channels_bulk` / `add_tv_channel` | `names[]` \| `name+url+category` | confirmation | **writes the user's TV library** | `media.tv.write` | write | no | required; per-user library | **B** |
| `play_radio` | as 5 tools | ✅ | `radio.uae_radio` / `arabic_radio` / `quran_radio` / `my_radio` / `radio_by_genre` / `search_radio` | `preset\|genre\|query,country_code` | player embed | none | `media.radio.read` | egress | no | required | **B** |
| `search_radio_stations` | ✅ | ✅ | `radio.search_radio_stations` | `name,country,genre` | picker embed | none | `media.radio.read` | egress | no | required | **B** |
| `my_radio` | ✅ | ✅ | `radio.my_radio` | — | player embed | none | `media.radio.read` | egress | no | required; per-user list | **B** |
| `manage_radio_stations` | as 2 tools | ✅ | `radio.add_radio_stations_bulk` / `remove_radio_station` | `action,names[],name` | confirmation | **writes the user's station list** | `media.radio.write` | write | no | required | **B** |
| `get_weather` | ✅ | ✅ | `services.weather.get_weather` | `location,units` | forecast text | none | `web.weather` | egress | no | required | **B** |
| `get_news` | ✅ | ✅ | `services.news.get_news` | `category` | headlines + article embed | none | `web.news` | egress | no | required | **B** |
| `generate_qr_code` | ✅ | ✅ | `services.qr.generate_qr_code` | `content` | QR embed | none (**pure CPU, no network**) | `utility.qr` | read | no | required | **B** |

### 3.5 Web, code, delegation, utility

| Tool | A | B | Implementation | Arguments | Result | Side effects | Permission | Risk | Appr | Tenant | Canonical |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `web_search` | ✅ | ✅ | self-hosted SearXNG | `query,max_results` | results text | **query egresses** | `web.search` | outbound | **YES** (B) / **no** (A) | required | **B** |
| `run_python` | — | ✅ | Docker `--network none --read-only` | `code` | stdout/stderr | sandboxed execution | `code.run` | code | no | required | **B** |
| `delegate` | — | ✅ | nested `graph.run_turn` | `to_agent,task` | specialist answer | sub-agent runs | `delegate` | read | no | **inherited by the sub-agent** | **B** |
| `current_time` | — | ✅ | local clock | — | timestamp | none | *(none)* | read | no | required | **B** |
| `calc` | — | ✅ | AST-restricted arithmetic | `expr` | number | none | *(none)* | read | no | required | **B** |

### 3.6 Runtime A only — deliberately not migrated

Already covered in §2.1: `get_emails`, `get_contacts`, `recall_memory`,
`search_knowledge`, `schedule_meeting`.

---

## 4. What "not blindly copied" meant in practice

1. **Service layer reused, dispatcher not.** Every migrated handler calls the same
   `backend/services/*` function Runtime A calls. Nothing was reimplemented, so the
   two runtimes cannot drift and the fixes already in those services (yt-dlp
   fallbacks, station dedup, the directory cache) are inherited.
2. **Nine tools became four; four became three.** §2.2.
3. **Widget HTML never reaches the model.** Nine of these return
   `(HTMLResponse, context)`. Runtime B stringifies a handler's return value into
   the context window, so a naive port would have put hundreds of lines of markup
   in front of the LLM. Each handler runs its result through
   `tool_result.process_tool_result` — Runtime A's own splitter — and returns only
   the short context string; the HTML travels on a per-turn `ContextVar` sink.
4. **Permissions split read from write.** `media.radio.read` does not confer
   `manage_radio_stations`. Runtime A had no permission model at all here.
5. **Inputs bounded.** `search_youtube.max_results` is clamped to 1–10;
   `graph_search.limit` to 1–25. Runtime A passed them through.
6. **Empty input asks instead of calling.** An empty video id or QR payload returns
   a question without touching the service.

---

## 5. `schedule_meeting`

The single most consequential difference between the runtimes.

| | Runtime A `schedule_meeting` | Runtime B `create_calendar_event` |
|---|---|---|
| Effect | creates a real event on Google/Microsoft | same |
| Policy verdict | `"approval"` | `"approval"` |
| Enforced? | **no** — `backend/tools.py:956` checks only `"deny"` | **yes** — `is_outbound` → pause |

Runtime A has no HITL mechanism and is not getting one. The interim control is the
kill switch, verified end to end in `tests/test_runtime_a_killswitch.py`:

```bash
AGANETI_DENIED_TOOLS=schedule_meeting   # → dispatcher refuses before the provider call
```

This is a **temporary migration safety control**. It is removed when Runtime B
serves the traffic, at which point the tool is gated rather than blocked.

---

## 6. The knowledge-graph path

Two complementary tools, both registered:

| | `knowledge_search` | `graph_search` |
|---|---|---|
| Question | "what does the company know about X" | "what is X connected to" |
| Path | `build_ranked_context` → Qdrant **+** Neo4j, fused/ranked | `GraphService.tenant_*` → Neo4j |
| Returns | `[D#]`/`[G#]` cited prose | entities + typed relationships |
| Tenant | carried on the result; **not yet a retrieval filter** | **applied as a Cypher predicate** |

`graph_search` resolves in three passes, tightest first — exact name/alias, then
substring, then any-significant-token. The third exists because live validation
caught the model asking for "the Agentic AI project" when the node is called
"Agentic AI": the first two passes missed and the tool answered "nothing is
recorded", which reads as an authoritative absence. Ranked by hit count then
degree, so loosening cannot promote a one-token coincidence.

**No Cypher is exposed to the model.** The schema offers `query`, `limit`, `expand`
and nothing else; `graph_tools.py` authors no Cypher and never imports
`GraphService.run_query`. Both are asserted structurally in
`tests/test_graph_search_tool.py`.

---

## 7. Known gaps

| # | Gap | Severity |
|---|---|---|
| G1 | The graph carries **no tenant property**. `graph_search` filters on a configurable key (`GRAPH_TENANT_PROPERTY`, default `org_id`) in lenient mode, where an unstamped node is shared — so the filter is a no-op on today's data. `GRAPH_TENANT_STRICT=true` makes it exclusive and, on this graph, returns nothing. **The graph is single-tenant until ingestion stamps the property.** | P1 |
| G2 | `knowledge_search` carries the tenant on its result but passes it to no retrieval filter — no provider accepts one yet. Its Neo4j half is therefore unscoped. | P1 |
| G3 | `query_data` reads the customer's Azure SQL with no row-level tenant predicate. Protected by SELECT-only + a PII column blocklist, not by tenancy. | P1 |
| G4 | Existing seeded agents have **18 permissions** and none of the 14 new tools. They must be granted before a canary can exercise them — see [runtime-b-readiness.md](runtime-b-readiness.md). | **blocking for rollout** |
| G5 | `egress` is auto at `standard`. A tenant wanting every outbound byte reviewed sets `AUTONOMY_LEVEL=assist`; there is no per-tool egress switch short of the kill switch. | P2 |
| G6 | Runtime B has no equivalent of Runtime A's passive task detection or task-confirmation state machine. | P2 |

---

## 8. Test coverage

| File | Cases | Covers |
|---|---|---|
| `tests/test_consumer_tools_migration.py` | 108 | registration, schemas, permissions, authorization for all 13; each handler reaching the right service; the merges; embed splitting |
| `tests/test_graph_search_tool.py` | 35 | callable through the real executor; unauthorized rejected; tenant in every query; GraphService the sole Cypher executor; three-pass resolution |
| `tests/test_runtime_a_killswitch.py` | 10 | `schedule_meeting` blocked at Runtime A's dispatcher, before the provider call |
| `tests/test_oauth_connect_idor.py` | 23 | connect authenticated + self-scoped; state signed, tamper/expiry rejected |

All pass. Full suite: **802 passed**, 4 pre-existing failures unrelated to this work.
