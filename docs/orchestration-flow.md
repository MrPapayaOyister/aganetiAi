# Orchestration Flow — Traced Requests

**Audit date:** 2026-08-13 · **Companion to:** [architecture-audit.md](architecture-audit.md)

Four traces, each following a real request from HTTP entry through model selection, agent
selection, tool selection, tool execution and response. Every step cites the file and line that
performs it.

**Traces 1–3 run on the LangGraph orchestrator (Runtime B). Trace 4 runs on the legacy runtime
(Runtime A) — the one carrying live traffic.**

---

## Trace 1 — `POST /agent/chat` · "summarise my inbox"

The canonical happy path: authentication → supervisor → agent selection → tool selection → tool
execution → second LLM turn → response.

### Stage 0 · Middleware (outermost → innermost)

Registration order in [backend/main.py](../backend/main.py) L879–944; Starlette applies them in
reverse, so `_TraceASGIMiddleware` sees the request first.

| # | Middleware | File | Action |
|---|---|---|---|
| 1 | `_TraceASGIMiddleware` | [main.py:906](../backend/main.py#L906) | reads/mints `X-Trace-Id`, `set_trace_id(tid)`, wraps `send` to echo the header. Pure ASGI — a `BaseHTTPMiddleware` here buffered SSE and broke streaming |
| 2 | `RateLimitMiddleware` | [main.py:886](../backend/main.py#L886) | per-caller throttle |
| 3 | `AuthEnforceMiddleware` | [auth/enforce.py:93](../backend/auth/enforce.py#L93) | see below |
| 4 | CORS | [main.py:961](../backend/main.py#L961) | `DASHBOARD_ORIGINS` allowlist |

**Authentication** (`enforce.py`):
```
path "/agent/chat" not in _PUBLIC_PREFIXES                              L104
Authorization: Bearer <jwt>                                             L135
verify_supabase_jwt(token)              → claims{sub, email, …}          L140  (ES256 via JWKS)
identity.resolve_or_provision(sub, email, name)                         L159  (domain allowlist,
                                                                               disabled accounts,
                                                                               first-login provisioning)
effective_user = caller_sub                                             L160
_forward(...)                                                           L168
   strip client x-auth-user; inject resolved                            L183   ← non-forgeable
   query_string["user_id"] = [effective_user]     (unconditional)       L199   ← IDOR defence
   path segments equal to sub rewritten                                 L203
   JSON body["user_id"] = effective_user                                L229
   _receive shim: first read returns the rewritten body, then delegates L241   ← keeps SSE
                                                                                 disconnect
                                                                                 detection alive
```

### Stage 1 · Route handler

[backend/routes/agent_os.py:354](../backend/routes/agent_os.py#L354)
```python
@router.post("/chat")
async def agent_chat(request: Request):
    user_id = _uid(request)                       # L364 — header ONLY; missing → 401
    body    = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "sess")
    images  = _clean_images(body.get("images"))   # L368 — data: URLs only, capped at MAX_IMAGES=4
    gen = unified_stream(user_id, message, session_id, images, dashboard_model())   # L371
    return StreamingResponse(gen, media_type="text/event-stream")
```
If `unified_stream` cannot be imported the handler falls back to `_sse` (primary agent only,
L373) — a deliberate degrade-not-fail.

### Stage 2 · Supervisor / lane selection

[backend/chat/unified.py:132](../backend/chat/unified.py#L132) `unified_stream`
```python
lane = "primary" if images else route(message)          # L135 — a vision turn is never analytical
yield F.sse(F.start(session_id, agent=lane))            # L138
yield F.sse(F.stage("router", "done", summary=lane))    # L139
```
`route()` ([L71](../backend/chat/unified.py#L71)) — pure regex, no LLM call:

| Test | Result |
|---|---|
| `_PERSONAL` ∧ ¬`_CHART` | `"primary"` |
| `_CHART` | `"chart"` |
| `_DATA` ∧ `_DOMAIN` | `"data"` |
| otherwise | `"primary"` |

`"summarise my inbox"` matches `_PERSONAL` (`inbox`) and not `_CHART` → **lane `primary`**, so:
```python
if lane == "primary":                               # L142
    from backend.routes import agent_os
    async for chunk in agent_os._sse(user_id, message, session_id, images):
        yield chunk
    return
```

### Stage 3 · Agent selection

[routes/agent_os.py:95](../backend/routes/agent_os.py#L95) `_sse` → `_load_primary(user_id)` (L50):
```
repo.resolve_user(s, user_id)          → internal User (uuid, org_id)
repo.get_primary_agent(s, user.id)     → Agent row       ← the ONLY registry read on any
repo.allowed_tools(s, ag.id)           → permission rows    execution path
tools = [t for t in perms if t in registry.all_names()]     ← unknown names dropped
agent = {"id":"primary", "tools":tools,
         "model_key": ag.model_key, "fallback_models": ag.fallback_models or []}
prompt = ag.system_prompt or PRIMARY_PROMPT   (+ "\n\n" + ag.persona if set)
```
An **empty** `tools` list is honoured verbatim as a deliberate lockdown (L66–69). If the user has
no DB agent yet, falls back to `DEFAULT_PRIMARY` + `templates.PRIMARY_PROMPT` (L78).

> **Agent selection ends here.** No non-primary registered agent can be selected — see
> [agent-capability-matrix.md §4](agent-capability-matrix.md).

### Stage 4 · Context assembly

```python
history = convo.load(user_id, session_id, mark_stale=True)      # L101
_recalled = await _mem.recall(user_id, message)                 # L106 — cross-thread facts,
if _recalled: prompt = f"{prompt}\n\n{_recalled}"               #        time-boxed, fail-soft
```
`mark_stale` ([backend/chat/stale.py](../backend/chat/stale.py)) annotates prior turns whose data has
gone stale (`VOLATILE_TOOLS`) so the model re-calls instead of quoting the transcript.

> Note: **no** Qdrant corporate retrieval and **no** Neo4j graph retrieval happen here.
> `build_ranked_context` is not called on this path — see [Trace 4](#trace-4).

### Stage 5 · Graph invocation

```python
async for ev in graph.astream_turn(user_id=user_id, agent=agent,
                                   user_message=message, session_id=session_id,
                                   system_prompt=prompt, history=history, images=images):   # L113
```
[graph.py:190](../backend/orchestrator/graph.py#L190) assembles `[system] + history + [user]`,
calls `_init` (L128) to build `AgentState`, then:
```python
async for mode, chunk in GRAPH.astream(state, {"recursion_limit": 24}, stream_mode=["updates"])
```

### Stage 6 · `_agent_node` — model routing + tool selection

[graph.py:44](../backend/orchestrator/graph.py#L44)
```python
schemas = registry.openai_schemas(state["allowed_tools"])   # L46 — only permitted tools offered
_temp   = 0.0 if agent_id in ("dashboard","analytics") else 0.2          # L58
_tc     = "required" if (agent_id=="dashboard" and step==0 and not has_image) else "auto"   # L59
_maxtok = 4096 if agent_id in ("dashboard","analytics") else 1024        # L63
msg = await llm.chat(messages, tools=(None if has_image else schemas), …) # L64
return {"messages": [msg], "step": state["step"] + 1}                    # L67
```
Vision turns send **no** tools — the deployed `vision-vl` vLLM was launched without
`--enable-auto-tool-choice`, so a `tool_choice="auto"` request 400s (L51–55).

**Model routing** — `llm.chat` → `router.complete`
([router.py:147](../backend/orchestrator/router.py#L147)):
```
plan(agent, need_tools=True, need_vision=False, tier=None)      L152
   per-agent model_key + fallback_models (capability-filtered)
   → vision-vl if need_vision
   → gateway-fast, gateway if tier=="fast" and not need_tools
   → DEFAULT_CHAIN = ["gateway", "gateway-fast"]
for mk in chain:                                                 L155
    AsyncOpenAI(base_url=LLM_BASE_URL) .chat.completions.create(timeout=90, …)
    extra_body={"chat_template_kwargs": {"enable_thinking": False}}   # Qwen3 reasoning off
    on success: _normalise(msg) → {role, content, tool_calls[]}       L172
                _log(...) → events.log_event("llm_call", meta={agent_id, tokens_in,
                                                               tokens_out, cost_micros})
    on failure: log failure, continue to next model
all fail → RuntimeError(f"all models failed (chain={chain})")    L178
```
Every entry in `MODELS` (L35) addresses the **LiteLLM gateway** (`:4000`) — never a vLLM port
directly — so the gateway owns keys, routing and its own fallbacks.

**Tool selection** is the model's: it returns `tool_calls=[{id, function:{name:"list_emails",
arguments:"{\"max_results\":10}"}}]`.

### Stage 7 · Routing decision

```python
# graph.py:105
def _route_agent(state) -> Literal["tools","end"]:
    if state["step"] >= STEP_BUDGET:   # 8
        return "end"
    return "tools" if state["messages"][-1].get("tool_calls") else "end"
```
→ `"tools"`.

### Stage 8 · `_tools_node` — authorization + execution

[graph.py:70](../backend/orchestrator/graph.py#L70)
```python
for tc in last["tool_calls"]:
    name = tc["function"]["name"]                     # "list_emails"
    args = json.loads(tc["function"]["arguments"] or "{}")   # {} on parse failure — L78
    tool = registry.get(name)                          # L81
    if tool is None:                    → "error: unknown tool"
    elif name not in allowed_tools:     → "error: not permitted"          ← AUTHORIZATION
    elif tool.is_outbound:              → PAUSE (Trace 2)                 ← APPROVAL GATE
    else:                               → content = await tool.handler(ctx, **args)   # L95
outs.append({"role":"tool","tool_call_id":tc["id"],"name":name,
             "content": str(content)[:6000]})                              # MAX_TOOL_OUTPUT
```
`list_emails` is non-outbound → executes inline:
```
registry._list_emails(ctx, max_results=10)                 registry.py:121
  → services.mailbox.inbox(user_id, max_results=10)
      → provider_tokens: decrypt + refresh                  services/provider_tokens.py
      → Gmail  gmail.googleapis.com/gmail/v1/users/me/messages
        or MS  graph.microsoft.com/v1.0/me/messages
  → "10 recent email(s) (use the [id] with read_email):\n• [id] Subject — Sender…"
```
Exceptions are caught and returned as tool content (`TypeError` → bad-arguments message; anything
else → `error: {name} failed: {e}`) so the loop never dies on a tool failure.

### Stage 9 · Loop back

`_route_tools` (L111) returns `"agent"` (no `awaiting`) → `_agent_node` runs again with the tool
result in context → produces a final assistant message with no `tool_calls` → `_route_agent`
returns `"end"` → `END`.

### Stage 10 · SSE translation + persistence

`astream_turn` (L205–227) converts node updates into UI events:
`tool_call` → `token` → `final`. `_sse` then:
```python
convo.append(user_id, session_id, "user", message)              # L123
convo.append(user_id, session_id, "assistant", fin)             # L132
yield {"type":"done","final":fin}                               # L133
await _record_session(user_id, session_id, message)             # L136 — chat_sessions upsert
_aio.create_task(_mem.capture_turn(...))                        # L142 — AFTER the answer,
yield "data: [DONE]\n\n"                                        #        never on the critical path
```

### Trace 1 summary

```
HTTP POST /agent/chat
 └ _TraceASGIMiddleware      X-Trace-Id
 └ RateLimitMiddleware
 └ AuthEnforceMiddleware     JWT → sub → X-Auth-User; user_id normalized in qs/body/path
 └ agent_chat                 routes/agent_os.py:354
   └ unified_stream           chat/unified.py:132
     └ route()                → "primary"                       [SUPERVISOR]
     └ agent_os._sse          routes/agent_os.py:95
       └ _load_primary        → DB Agent + permission allowlist  [AGENT SELECTION]
       └ convo.load + memory.recall                              [CONTEXT]
       └ graph.astream_turn   orchestrator/graph.py:190
         └ GRAPH.astream
           ├ _agent_node      → router.plan → LiteLLM :4000      [MODEL ROUTING]
           │                  → tool_calls[list_emails]          [TOOL SELECTION]
           ├ _route_agent     → "tools"
           ├ _tools_node      → allowlist check → handler        [AUTHZ + EXECUTION]
           │                  → Gmail / MS Graph
           ├ _route_tools     → "agent"
           ├ _agent_node      → final text
           └ _route_agent     → END
       └ convo.append ×2, chat_sessions upsert, async memory capture
 └ SSE: start → stage(router) → tool_call → token → done → [DONE]
```

---

## Trace 2 — the approval branch · "email Sam the Q3 numbers"

Identical through Stage 7. Diverges inside `_tools_node`.

### Stage 8′ · The hard gate

```python
# graph.py:86
elif tool.is_outbound:
    # HARD GATE: never execute here. Record for approval; answer the call
    # with a placeholder so the tool-call protocol stays valid.
    prev = registry.preview(name, args)                 # "Send email to sam@… — subject: 'Q3'"
    pending.append({"tool_call_id": tc["id"], "name": name, "args": args,
                    "action_type": name, "preview": prev})
    content = f"[AWAITING USER APPROVAL] {prev}"
```
`_send_email`'s handler is **not** invoked. `_tools_node` returns
`{"messages": outs, "awaiting": pending[0]}` (L102).

### Stage 9′ · Terminate

```python
# graph.py:111
def _route_tools(state) -> Literal["agent","end"]:
    return "end" if state.get("awaiting") else "agent"
```
→ `END`. `_result` (L144) returns `{"status":"awaiting_approval","approval":{…}}`.

### Stage 10′ · Persist the paused run

[routes/agent_os.py:124](../backend/routes/agent_os.py#L124)
```python
if final.get("status") == "awaiting_approval":
    aid = await store.create_approval(user_id, agent, _strip_images(final["messages"]),
                                      final["approval"])
    convo.append(user_id, session_id, "assistant",
                 f"(Prepared an action for your approval: {preview})")
    yield {"type":"approval_required","approval":…, "approval_id": aid}
```
`_strip_images` (L81) replaces multimodal content with its text so the blob stays small and images
are not re-sent on resume.

`store.create_approval` → `_pg_create` ([store.py:128](../backend/orchestrator/store.py#L128)) writes
an `Approval` row keyed by internal `user_id` + `org_id`, with
`payload = {agent, messages, approval, supabase_uid}`. The **original supabase uid is preserved in
the blob** so resume can still execute against Gmail/Calendar, while the row lists per internal
user.

> The run is now fully durable. Nothing is held in process memory. A restart loses nothing.

### Stage 11′ · The decision — `POST /agent/approvals/{aid}/approve`

[routes/agent_os.py:383](../backend/routes/agent_os.py#L383) `_resume`:
```
caller = _uid(request)
rec = await store.get_approval(aid)                      → 404 if missing
resolve_user(caller) vs resolve_user(rec["user_id"])     → 404 if different  ← don't leak existence
rec["status"] != "pending"                               → 409
won = await store.decide(aid, "approved", decided_by=cu.id)     ← CAS, BEFORE execution
    UPDATE approvals SET status=…, decided_at=now(), decided_by=…
    WHERE id=:id AND status='pending'                    → rowcount > 0
    + repo.log_event(kind="approval", name=status, meta={approval_id, tool_key,
                                                         action_type, decided_by})
if not won: → 409 "already decided"                      ← double-click / concurrent
                                                            approve+reject cannot double-execute
res = await graph.resume(user_id=rec["user_id"], agent=…, messages=…,
                         approval=…, approved=True)
await store.set_result(aid, res["final"])
```

### Stage 12′ · Resume

[graph.py:167](../backend/orchestrator/graph.py#L167)
```python
if approved:
    result = await tool.handler(ctx, **approval["args"])     # ← the REAL send, exactly once
else:
    result = (f"User REJECTED this action: {approval['preview']}. "
              f"Do not retry it; continue without it.")
# replace the placeholder tool message with the outcome
for m in msgs:
    if m["role"]=="tool" and m["tool_call_id"]==approval["tool_call_id"]:
        m["content"] = result; break
out = await GRAPH.ainvoke(_init(user_id, agent, msgs, step=step), cfg)   # graph resumes
```
`_send_email` ([registry.py:209](../backend/orchestrator/registry.py#L209)) posts to the app's own
`/send_email` with `X-Internal-Token` + `X-Internal-User`, which the auth middleware accepts as
path (b).

A rejection is fed back as a **tool result**, not a hard stop, so the model can continue the turn
without the action.

### Trace 2 summary

```
… _tools_node
     tool.is_outbound → handler NOT called
     awaiting = {tool_call_id, name, args, action_type, preview}
   _route_tools → END
   store.create_approval → approvals.payload JSONB (durable, restart-safe)
   SSE approval_required {approval, approval_id}

── later HTTP request ──────────────────────────────────────────────
POST /agent/approvals/{aid}/approve
   ownership: resolve_user(caller) == resolve_user(record)   else 404
   status == "pending"                                       else 409
   CAS decide()  ── audit event kind='approval'              else 409
   graph.resume → tool.handler(**args)   ← executes EXACTLY ONCE
   GRAPH.ainvoke → model narrates the outcome
   store.set_result
```

---

## Trace 3 — the analytics lane · "how many requests were approved by emirate?"

Same Stages 0–2. `route()` matches `_DATA` (`how many`, `by emirate`) **and** `_DOMAIN`
(`request`, `approv`, `emirate`) → **lane `"data"`**.

```python
# unified.py:171
from backend.dashboard import ask as dash_ask
gen = dash_ask.ask_stream(user_id, message, None, model_key, session_id, persist=False)
```

### Agent + tool selection

[backend/dashboard/ask.py](../backend/dashboard/ask.py) — a **different agent identity** on the
**same** LangGraph:
```
agent_id      = "analytics"
system_prompt = ask.system_prompt()        ← customer-specific: Dar Al Ber schema, table names,
                                             RequestStatusName enum, forecast context
ASK_TOOL_NAMES = ["get_database_schema","query_data","forecast_metric",
                  "compare_periods","current_time"]                      # L36
model_key      = dashboard_model()         ← "azure" when DASHBOARD_LLM=azure, else local chain
temperature    = 0.0                       ← graph.py:58, deterministic SQL
max_tokens     = 4096                      ← graph.py:63, wide markdown tables
```
Then `graph.GRAPH.astream(st, cfg, stream_mode=["updates"])` — the **same compiled graph**,
different state.

### Tool execution — `query_data`

```
_query_data(ctx, sql)                              dashboard/tools.py:218
  → coreshare_db.run_query_cached(sql, ttl=60, stale=60)
       validate_select_only(sql)                   coreshare_db.py:95
            SELECT-only · keyword blocklist · no bare "SELECT *" · no ";"
       validate_no_pii(sql)                        coreshare_db.py:124
            10-column beneficiary-PII blocklist → ValueError
       engine.connect() → Azure SQL (MS SQL / T-SQL), read-only
  → evidence ledger: _led.record(sql, rows, ms)    insight/evidence.py
  → returns {"evidence_id": …, "row_count": N, "rows": rows[:50]}
```

### The verification pass — unique to this lane

After the graph settles ([ask.py:315–380](../backend/dashboard/ask.py#L315)):

1. **Retry guard** — if the answer states figures but the ledger is empty (`query_data` never ran),
   re-ask once with a system message insisting on a query. Emits
   `stage{critic, status:"retry"}`.
2. **Deterministic verification** — `evidence.verify(prose, figures, ledger)` **recomputes** each
   stated figure against the recorded typed rows. Emits `stage{critic, …}` and sets
   `done.verified ∈ {true, false, null}` plus a `verification` detail object.
   `null` means "nothing numeric to check" and is explicitly **not** an assurance.
3. On unreconciled figures with `_pmode == "on"`, appends a provisional-figures caveat to the
   answer.

The `chart` lane is the same shape via `dashboard/stream.stream_dashboard`, plus artifact
emission: `_board_chart_ids` before → `_render_new_charts` after → `chat_store.add_artifact` →
SSE `artifact` frames ([unified.py:233](../backend/chat/unified.py#L233)).

### Trace 3 summary

```
POST /agent/chat → unified_stream → route() → "data"
  dash_ask.ask_stream
    agent_id="analytics", 5-tool allowlist, temp=0.0, max_tokens=4096
    graph.GRAPH.astream          ← SAME LangGraph, different state
      _agent_node  → get_database_schema
      _tools_node  → schema (PII columns hidden)
      _agent_node  → query_data(SELECT …)
      _tools_node  → validate_select_only + validate_no_pii → Azure SQL
                   → evidence ledger record
      _agent_node  → prose answer with figures
    retry guard (figures stated but ledger empty → re-ask once)
    evidence.verify → recompute every figure → verified: true|false|null
  SSE: start → stage(router) → tool_call → tool_result(rows, ms) → stage(query)
       → token → stage(critic) → done{final, verified, verification} → [DONE]
```

---

## Trace 4 — the runtime that actually serves users · `POST /chat` "what's the weather in Dubai"

This is the path taken by this repo's Vite frontend
([frontend/src/api/client.ts:71](../frontend/src/api/client.ts#L71)) and the Telegram bot
([integrations/telegram_bot.py:274](../integrations/telegram_bot.py#L274)). It shares the auth
middleware and **nothing else**.

```
POST /chat                                          backend/main.py:2266
  _persist_turn(session_id, user_id, "user", msg)
  events.log_event("message_user", …)                        main.py:2274
  load_session_state(session_id)                             ← awaiting_task_confirmation, etc.

  [CONTEXT — this is where Qdrant AND Neo4j are used]
  build_ranked_context(user_id, message, session_id,          main.py:2377
                       only=("corporate","memory","graph","calendar","tasks"))
      backend/context/providers/  run CONCURRENTLY, per-provider timeout, failure-isolated
        corporate → Qdrant corporate_memory
        memory    → Qdrant user_memory_<uuid>
        graph     → Neo4j  (context/providers/graph.py:53 → GraphRetrievalAPI)
        calendar  → Google/MS Calendar
        tasks     → task store
      → fusion (cross-source dedup) → ranking → compression → token budget

  [LOOP — hand-rolled, no LangGraph]
  if NATIVE_TOOLS:                                            main.py:2604
      MAX_TOOL_ROUNDS = 4                                     main.py:2612
      tools_for(user_id)              ← 34 legacy schemas, minus user-disabled groups
      → model returns tool_calls
      → execute_single_tool → dispatch_tool_call              backend/tools.py:944
             decide(name) == "deny" → refuse                  backend/tools.py:956
                 ⚠ ONLY "deny" is checked — "approval" falls through and EXECUTES
             tool_prefs group toggle (subtract-only)          backend/tools.py:968
             if/elif chain → get_weather → Open-Meteo
             events.log_event("tool_called", …)               backend/tools.py:1322
      → feed result back, next round
  events.log_event("message_agent", …)                        main.py:2834 / 2903
  SSE: thinking | token | action | error | done
```

### What this path has that Runtime B does not

- **Full context engine** — Qdrant corporate + user memory, **Neo4j knowledge graph**, calendar and
  tasks, fused, ranked, compressed, budgeted.
- 24 consumer-media tools (YouTube, live TV, radio, QR, weather, news) with no counterpart in the
  orchestrator registry.
- Passive task detection, task-confirmation state machine, Telegram integration.

### What it lacks that Runtime B has

- **No LangGraph** — a fixed 4-round loop, no state machine, no step budget, no conditional edges.
- **No approval gate** — `is_outbound` does not exist here; `guardrails.decide()`'s `"approval"`
  verdict is computed and discarded ([opa-audit.md §4](opa-audit.md)).
- **No typed tool registry** — an if/elif dispatcher.
- **No agent registry, no delegation, no per-agent permissions, no model fallback chain.**
- **No `llm_call` cost/token accounting** (those events come only from `orchestrator/router.py`).

### Runtime evidence that this is the live path

```
events kind='tool_called'   (emitted only by backend/tools.py:1322)
  play_youtube_video 1975 · get_news 916 · get_weather 861 …   last 2026-08-13

events kind='llm_call'      (emitted only by orchestrator/router.py:141)
  agent_id='primary'  115 calls                                last 2026-08-06
```

---

## Cross-cutting: state, at every point in a turn

| Moment | Where the state lives | Survives restart |
|---|---|---|
| Between `_agent_node` and `_tools_node` | `AgentState` in process | ❌ |
| Between graph steps | `AgentState` in process | ❌ |
| Awaiting approval | `approvals.payload` JSONB (Postgres) or `agent_approvals` (SQLite) | ✅ |
| Between turns | `conversation.py` JSON file store + `chat_messages` | ✅ |
| Across threads | `memory_items` + per-user Qdrant collection | ✅ |
| Charts / artifacts | `chat_artifacts` + `config_db` | ✅ |
| **LangGraph checkpoints** | — | **none exist** |
| **Run of record** | `agent_runs` table | **0 rows, no writer** |

The design trade documented at [graph.py:8–11](../backend/orchestrator/graph.py#L8) is explicit:
*"State == the message list, so a paused run is fully captured by its messages + the `awaiting`
record… no checkpointer gymnastics, survives process restarts."*

That reasoning holds **for the approval case only**. Any other interruption — a crash mid-turn, a
deploy, a tool that outlives the request, a run that should be resumable by id — has no
representation. This is the concrete blocker behind gap **P1-3** (add a Postgres checkpointer and
make `agent_runs` the run-of-record) and, transitively, behind **P2-2** (the async/long-running
tool contract a browser agent would require).

---

## Cross-cutting: the SSE frame vocabulary

All three orchestrator lanes emit one vocabulary
([backend/chat/frames.py](../backend/chat/frames.py)) so the client is lane-agnostic:

| Frame | Builder | Carries |
|---|---|---|
| `start` | `F.start` L37 | `session_id`, `agent` (the chosen lane), `message_id` |
| `stage` | `F.stage` L44 | `name`, `status` (`running`/`done`/`failed`/`retry`/`skipped`), `label`, `ms`, `summary`, `detail` |
| `tool_call` | `F.tool_call` L58 | `name`, `call_id`, `stage_name` |
| `tool_result` | `F.tool_result` L70 | `name`, `ok`, `rows`, `ms` |
| `token` | `F.token` L80 | `content` — **replace semantics** (the data lane yields the complete message each time; the chart lane yields deltas; both are normalized to replace) |
| `artifact` | `F.artifact` L85 | `id`, `kind`, `title`, `spec`, `data` |
| `done` | `F.done` L93 | `final`, `message_id`, `artifacts[]`, and on the data lane `verified` + `verification` |
| `error` | `F.error` L107 | `message`, `stage_name`, `recoverable` |
| — | `F.DONE_SENTINEL` | `data: [DONE]` |
