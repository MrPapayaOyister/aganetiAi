# Tool Inventory

**Audit date:** 2026-08-13 · **Companion to:** [architecture-audit.md](architecture-audit.md)

Enumerated at runtime, not read off documentation:

```bash
python -c "from backend.orchestrator import registry; import backend.dashboard.ask; print(registry.all_names())"   # 35
python -c "from backend.tools import tools_for; print([t['function']['name'] for t in tools_for('u')])"            # 34
```

---

## 0. Headline

**There are two tool registries.** They are separate Python modules, separate data structures,
separate authorization models, and they are consumed by two different agent runtimes.

| | Registry B — orchestrator | Registry A — legacy |
|---|---|---|
| Module | [backend/orchestrator/registry.py](../backend/orchestrator/registry.py) | [backend/tools.py](../backend/tools.py) |
| Structure | `Tool` dataclass in `_REGISTRY: dict[str, Tool]` | `TOOL_SCHEMAS` list + `dispatch_tool_call` if/elif chain |
| Count | **35** | **34** |
| Consumed by | LangGraph `_tools_node` | hand-rolled loop, `MAX_TOOL_ROUNDS = 4` |
| Authorization | per-agent tool-name allowlist | `guardrails.decide()` (deny only) + user tool-group toggles |
| Approval gate | `is_outbound` → hard pause | **none enforced** |
| Live traffic | last `primary` call 2026-08-06 | tool calls through 2026-08-13 |

**10 names exist in both with different implementations and different gating.** The most
security-relevant divergence is `web_search`.

---

## 1. Registry B — orchestrator (35 tools)

`Tool(name, description, parameters: JSON-Schema, handler: async, required_permission, is_outbound)`

> ⚠️ **`required_permission` is never read by any code path.** See §4.

### 1.1 Core tools — `backend/orchestrator/registry.py` (10)

| Tool | Outbound | `required_permission` | Handler | Effect |
|---|---|---|---|---|
| `list_tasks` | – | `tasks.read` | `_list_tasks` L100 | `tasks.store.get_all_tasks`, ≤25 rows |
| `get_agenda` | – | `calendar.read` | `_get_agenda` L109 | `services.mailbox.agenda` → Google/MS Calendar |
| `list_emails` | – | `email.read` | `_list_emails` L121 | `services.mailbox.inbox` → Gmail/Graph |
| `search_documents` | – | `documents.read` | `_search_documents` L140 | `backend.ingest.search_corporate` → **Qdrant**, ACL-scoped to caller + org corpus |
| `search_memory` | – | `memory.read` | `_search_memory` L158 | `memory.long_term.search_memory` |
| `draft_email` | – | `email.read` | `_draft_email` L167 | Pure formatting. **Nothing is sent** |
| `current_time` | – | *(none)* | `_now` L172 | local clock |
| `calc` | – | *(none)* | `_calc` L176 | AST-restricted arithmetic (`Add/Sub/Mult/Div/Pow/Mod/USub` only) — no `eval` |
| `set_reminder` | – | *(none)* | `_set_reminder` L195 | `backend.reminders.add`, NL time parsing |
| **`send_email`** | ✅ | `email.send` | `_send_email` L209 | `_internal_post("/send_email")` with `X-Internal-Token` |
| **`create_calendar_event`** | ✅ | `calendar.write` | `_create_calendar_event` L216 | `_internal_post("/schedule_meeting")` |

### 1.2 Skills — `backend/orchestrator/skills.py` (17)

| Tool | Outbound | `required_permission` | Handler | Effect |
|---|---|---|---|---|
| `read_email` | – | `email.read` | `_read_email` L33 | full body by message id |
| `email_digest` | – | `email.read` | `_email_digest` L43 | inbox summary |
| `create_task` | – | `tasks.write` | `_create_task` L61 | writes task |
| `complete_task` | – | `tasks.write` | `_complete_task` L74 | keyword-matched close |
| `remember_fact` | – | `memory.write` | `_remember_fact` L93 | long-term memory write |
| `resolve_contact` | – | `contacts.read` | `_resolve_contact` L105 | Google People / Graph |
| `next_event` | – | `calendar.read` | `_next_event` L120 | single next event |
| `get_analytics` | – | `analytics.read` | `_get_analytics` L133 | events-spine metrics |
| **`web_search`** | ✅ | `web.search` | `_web_search` L145 | **self-hosted SearXNG only** — no scraper fallback by design |
| `run_python` | – | `code.run` | `_run_python` L175 | **Docker sandbox** (below) |
| `predict_task_slippage` | – | `predictions.read` | L210 | computed from real task data |
| `predict_followups` | – | `predictions.read` | L250 | |
| `predict_relationship_value` | – | `predictions.read` | L269 | |
| `create_opportunity` | – | `deals.write` | L316 | writes `opportunities` |
| `list_opportunities` | – | `deals.read` | L340 | |
| `predict_deal_outcome` | – | `predictions.read` | L352 | expected value + win odds |

**`run_python` sandbox** — fixed arguments, model controls only the script body:
```
docker run --rm --network none --memory 256m --cpus 1 --pids-limit 128
           --read-only --tmpfs /tmp:size=32m -v <mkdtemp>:/work:ro -w /work
           python:3.12-slim python main.py          # 25 s timeout, temp dir always removed
```
Caveat (informational, §S7 of the audit): the API process invokes `docker` as a host user in the
`docker` group.

### 1.3 Delegation — `backend/orchestrator/agents.py` (1)

| Tool | `required_permission` | Handler | Effect |
|---|---|---|---|
| `delegate` | `delegate` | `_delegate` L67 | **Nested LangGraph run** — `graph.run_turn` with the specialist's persona and tool allowlist |

`enum` for `to_agent` = `list(SPECIALISTS.keys())` (L92) — fixed at import time:

| Specialist | Tools granted |
|---|---|
| `calendar_agent` | `get_agenda`, `next_event`, `current_time` |
| `research_agent` | `search_memory`, `search_documents`, `get_analytics` |
| `task_agent` | `list_tasks`, `create_task`, `complete_task` |
| `email_agent` | `list_emails`, `read_email`, `email_digest`, `draft_email` |
| `analyst_agent` | `run_python`, `search_documents` |
| `predictive_agent` | `predict_task_slippage`, `predict_followups`, `predict_relationship_value`, `predict_deal_outcome`, `create_opportunity`, `list_opportunities` |

No specialist has an outbound tool (deliberate — outbound stays on the primary where the gate
applies) and none has `delegate` (no recursion).

> ⚠️ A **second, divergent** `SPECIALISTS` dict lives in
> [backend/orchestrator/templates.py:57](../backend/orchestrator/templates.py#L57) with 4 entries and
> different tool sets — notably its `email_agent` grants `send_email`. `templates.SPECIALISTS`
> seeds DB rows; `agents.SPECIALISTS` is what executes.

### 1.4 Dashboard / analytics tools (7)

Registered into the **same global `_REGISTRY`** by `register_dashboard_tools()`,
`register_forecast_tools()`, `register_compare_tools()` — imported as a side effect of
`backend.dashboard.ask`.

| Tool | `required_permission` | Module | Effect |
|---|---|---|---|
| `get_database_schema` | `charts.read` | [dashboard/tools.py](../backend/dashboard/tools.py) | every table/view + columns; **PII columns hidden** |
| `query_data` | `charts.read` | `dashboard/tools.py` L218 | **arbitrary SELECT against the customer's Azure SQL**, via `coreshare_db.run_query_cached` |
| `save_chart` | `charts.write` | `dashboard/tools.py` L253 | persists a chart config to the board |
| `delete_chart` | `charts.write` | `dashboard/tools.py` | removes a chart |
| `list_charts` | `charts.read` | `dashboard/tools.py` | board contents |
| `forecast_metric` | `charts.read` | `dashboard/forecast_tools.py` | linear trend + 95 % prediction band |
| `compare_periods` | `charts.read` | `dashboard/compare_tools.py` | two-period delta + % change |

**`query_data` is the highest-privilege tool in the system.** Its safety rests on two Python
validators in [backend/dashboard/coreshare_db.py](../backend/dashboard/coreshare_db.py) — **not** on
any policy engine:

```python
validate_select_only(sql)      # L95
    _SELECT_ONLY.match(...)              # must start SELECT
    _FORBIDDEN.search(...)               # keyword blocklist
    reject bare "SELECT *"               # forces explicit, non-personal columns
    reject ";"                           # no multi-statement

validate_no_pii(sql)           # L124
    _PII_COLUMNS = {arabicfullname, englishfullname, idnumber, phonenumber, email,
                    debitedfromaccount, currentsalary, netmonthlyincome,
                    totalsourcesofincome, employer, jobtitle}
```
Demographics (`Gender`, `Nationality`, `Religion`, `MaritalStatus`, `NumberOfFamilyMembers`) remain
queryable for aggregate analytics. Results are capped at 50 rows in the tool payload and recorded
in full to the evidence ledger for verification.

The `/ask` analytics agent restricts itself to
`ASK_TOOL_NAMES = ["get_database_schema", "query_data", "forecast_metric", "compare_periods", "current_time"]`
([dashboard/ask.py:36](../backend/dashboard/ask.py#L36)) — a per-lane allowlist, not a registry-level
boundary. Because registration is global, `query_data` is one `PUT /agent/agents/{id}/permissions`
away from any agent.

### 1.5 Default primary allowlist

[backend/orchestrator/templates.py:44](../backend/orchestrator/templates.py#L44) — 18 tools:
```
current_time, list_tasks, create_task, complete_task, get_agenda, list_emails, read_email,
draft_email, send_email, create_calendar_event, web_search, search_documents, search_memory,
remember_fact, resolve_contact, create_opportunity, predict_deal_outcome, delegate
```
`OUTBOUND = {send_email, create_calendar_event}` (L52) mirrors `Tool.is_outbound` for seeding
`AgentPermission.is_outbound`. Note it **omits `web_search`**, which *is* `is_outbound=True` in the
registry — the executor's `Tool.is_outbound` is authoritative, so `web_search` is still gated, but
the two declarations disagree.

`ALWAYS_ALLOWED = ["current_time", "calc"]` (L55) is declared and **never consulted** by the
executor — `_tools_node` checks only `state["allowed_tools"]`.

---

## 2. Registry A — legacy (34 tools)

**Module** [backend/tools.py](../backend/tools.py) (1,544 lines) ·
**Dispatcher** `dispatch_tool_call` (L944) · **Executor** `execute_single_tool` ·
**Schema builder** `tools_for(user_id)` (L1537, subtracts user-disabled groups)

```
add_radio_stations_bulk, add_tv_channel, add_tv_channels_bulk, arabic_radio, complete_task,
create_task, draft_email, generate_qr_code, get_agenda, get_analytics, get_contacts, get_emails,
get_news, get_weather, get_youtube_video_info, my_radio, play_youtube_video, quran_radio,
radio_by_genre, read_email, recall_memory, remember_fact, remove_radio_station, resolve_contact,
schedule_meeting, search_knowledge, search_radio, search_radio_stations, search_tv_channels,
search_youtube, set_reminder, uae_radio, watch_live_tv, web_search
```

**Groups** (`TOOL_GROUPS`, user-togglable via `services/tool_prefs.py`):
`email` · `calendar` · `contacts` · `tasks` · `memory` · `knowledge` · `web` · `media` ·
`weather` · `news` · `livetv` · `radio` · `qr`

**Live usage** (`events.kind='tool_called'`, all-time, all through 2026-08-13):
```
play_youtube_video 1975 · get_news 916 · get_weather 861 · search_youtube 679 · watch_live_tv 293
get_youtube_video_info 117 · uae_radio 21 · get_emails 15 · get_agenda 10 · arabic_radio 10
web_search 10 · search_radio 9 · read_email 9 · my_radio 8 · quran_radio 8 · search_tv_channels 8
generate_qr_code 8 · search_knowledge 3 · add_tv_channels_bulk 3
```

**Authorization** — [backend/tools.py:956](../backend/tools.py#L956):
```python
from backend.guardrails import decide
if decide(name) == "deny":
    return f"⚠️ I'm not able to perform that action ('{name}')."
```
followed by a user tool-group toggle check (L968) which the comment correctly describes as
subtract-only. **The `"approval"` verdict is never acted on.**

---

## 3. Divergence between the registries

### 3.1 Names present in both, with different implementations

| Tool | Registry B | Registry A |
|---|---|---|
| `web_search` | **`is_outbound=True` → approval-gated** (skills.py:409) | `ACTION_CATEGORY["web_search"] = "read"` → `decide()` = **`"auto"`** (guardrails.py:39) |
| `draft_email` | non-outbound, returns a formatted draft | category `comms` → `decide()` = `"approval"` → **not enforced** → executes |
| `create_task` / `complete_task` | `tasks.write`, orchestrator handler | category `task` → `"auto"` |
| `get_agenda` / `read_email` / `resolve_contact` / `get_analytics` / `remember_fact` / `set_reminder` | orchestrator handlers | separate legacy handlers |

### 3.2 Names unique to Registry B (25)
`list_tasks` `list_emails` `email_digest` `search_documents` `search_memory` `next_event`
`current_time` `calc` `send_email` `create_calendar_event` `run_python` `delegate`
`create_opportunity` `list_opportunities` `predict_task_slippage` `predict_followups`
`predict_relationship_value` `predict_deal_outcome` `get_database_schema` `query_data`
`save_chart` `delete_chart` `list_charts` `forecast_metric` `compare_periods`

### 3.3 Names unique to Registry A (24)
`get_emails` `get_contacts` `recall_memory` `search_knowledge` `schedule_meeting` `get_weather`
`get_news` `generate_qr_code` `play_youtube_video` `search_youtube` `get_youtube_video_info`
`watch_live_tv` `add_tv_channel` `add_tv_channels_bulk` `search_tv_channels` `uae_radio`
`arabic_radio` `quran_radio` `radio_by_genre` `my_radio` `search_radio` `search_radio_stations`
`add_radio_stations_bulk` `remove_radio_station`

Note the asymmetry: Registry A has **no** email-send tool at all (by design — `schedule_meeting`
and `draft_email` are its only `comms` entries), while Registry B has `send_email` behind a hard
gate. But Registry A's consumer-media surface (YouTube/TV/radio/QR — 3,400+ of its 4,980 recorded
calls) has **no counterpart** in the orchestrator, which is why consolidating on Registry B is not
a no-op.

---

## 4. The dead permission model

`Tool.required_permission` is populated for 33 of 35 orchestrator tools with a coherent vocabulary
(`email.read`, `email.send`, `calendar.write`, `tasks.write`, `code.run`, `charts.read`, …).

Repository-wide, the identifier appears at exactly **three** sites:

| Site | Use |
|---|---|
| [registry.py:44](../backend/orchestrator/registry.py#L44) | dataclass field declaration |
| [agents.py:95](../backend/orchestrator/agents.py#L95) | one assignment (`"delegate"`) |
| [routes/agent_os.py:433](../backend/routes/agent_os.py#L433) | echoed in the `GET /agent/tools` response |

**It is never compared against anything.** The actual check is by tool *name*:

```python
# backend/orchestrator/graph.py:84
elif name not in state["allowed_tools"]:
    content = f"error: this agent is not permitted to use '{name}'"
```

and `allowed_tools` comes from `AgentPermission.permission` rows, which store **tool names**
([routes/agent_os.py:600](../backend/routes/agent_os.py#L600) filters candidates against
`registry.all_names()`).

Consequences:
- The permission vocabulary is decorative. `GET /agent/tools` advertises a control that does not
  exist.
- Permissions are per-tool, not per-capability — granting "read email" means enumerating
  `list_emails`, `read_email`, `email_digest`, and remembering to add the next one.
- There is no scope, no resource qualifier, no condition. `query_data` is granted or not; there is
  no way to say "only these tables", "only aggregates", or "only this tenant's rows".
- This is the concrete reason there is nowhere to attach a policy engine today — the gap that
  P0-7 must close before P3-1 (OPA) is meaningful.

---

## 5. Approval-gated tools (the complete set)

Only `Tool.is_outbound` gates. Exactly **three** tools carry it:

| Tool | Registered | Approval card (`registry.preview`, L70) |
|---|---|---|
| `send_email` | registry.py:265 | `Send email to {to} — subject: {subject!r}` |
| `create_calendar_event` | registry.py:270 | `Create calendar event {title!r} at {start} with {attendees}` |
| `web_search` | skills.py:405 | `Search the web for: {query!r} (top {max_results} results)` |

Anything else — including `query_data` against the customer database, `run_python`, `create_task`,
`remember_fact`, `create_opportunity`, `save_chart` and `delete_chart` — executes without human
review.

Notable: `delete_chart` mutates a shared board and is not gated; `query_data` reads production
customer data and is not gated (it relies on the SQL validators instead).

---

## 6. Tool-related settings that do not affect behaviour

| Setting | Declared | Effect |
|---|---|---|
| `AUTONOMY_LEVEL` | [guardrails.py:21](../backend/guardrails.py#L21) | **None.** Only alters what `GET /guardrails` reports; the `"approval"` verdict it produces is never enforced |
| `ALWAYS_ALLOWED` | [templates.py:55](../backend/orchestrator/templates.py#L55) | **None.** `_tools_node` consults only `allowed_tools` |
| `Tool.required_permission` | registry.py:44 | **None.** §4 |
| `AgentPermission.is_outbound` | [models.py:128](../backend/db/models.py#L128) | Display only — `_agent_json` surfaces it; the executor uses `registry.Tool.is_outbound` |
| `REDIS_*` | [settings.py:268](../config/settings.py#L268) | **None.** Health probe only |
