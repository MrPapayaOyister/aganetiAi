# Browser Automation — Repository Verification Audit

**Date:** 2026-08-14 · **Method:** repository inspection only · **Scope:** read-only
**Design under audit:** `docs/automation-architecture.md` §15 (14 items + `[DECIDE]` set)

No code was written, modified, or refactored. This file is the only artifact created.

> **Filename note.** The task names `docs/browser-automation-architecture.md`. That
> file does not exist. The audited document is **`docs/automation-architecture.md`**
> — H1 `# Browser Automation Architecture (Runtime B)`, §15 `Repo verification
> checklist (blocking)`, 14 numbered items, `[DECIDE]` list. Identified by content,
> not guessed. If a different file was intended, this audit is against the wrong one.

**Evidence rule applied throughout:** every answer cites `file:line`. Where the
repository does not answer the question, the entry reads **NOT FOUND** and stops.
Nothing below is inferred from naming, comments, or plausibility.

---

# PART 1 — ITEM 9 (PRIORITY)

> *§15.9 — Non-streaming `/chat`: does it surface approval requests today? Can it
> carry a screenshot reference given G2? **This is the most likely blocker for the
> §19 demo.***

## 9.1 Does non-streaming `/chat` surface an approval request?

**YES.** `backend/main.py:2321-2335`:

```python
if res.get("status") == "awaiting_approval":
    ap = res.get("approval") or {}
    aid = None
    try:
        from backend.orchestrator import store as _store
        aid = await _store.create_approval(uid, agent, res["messages"], ap)   # 2329
    except Exception:
        log.exception("runtime B bridge: could not persist approval")
    return _JSON({"reply": f"I need your approval first: {ap.get('preview', '')}",
                  "approval": {"id": aid, "preview": ap.get("preview"),
                               "action_type": ap.get("action_type")}},
                 headers=hdrs)
```

Verified live during the Phase 4 validation (`docs/runtime-b-readiness.md` §1):

```
{"reply":"I need your approval first: Search the web for: 'latest LangGraph release notes' (top 5 results)",
 "approval":{"id":"e376afc6-…","preview":"Search the web for: …","action_type":"web_search"}}
x-runtime: B
```

**CONFIRMS** the design's §8.3 assumption that non-streaming surfaces approvals.

## 9.2 Does it accept a resume?

**YES, but NOT on `/chat`.** Resume is on a different router entirely:

- `POST /agent/approvals/{aid}/approve` — `backend/routes/agent_os.py:431`
- `POST /agent/approvals/{aid}/reject` — `backend/routes/agent_os.py:436`
- both delegate to `_resume` — `backend/routes/agent_os.py:393`

`/chat` has **no** resume path. `grep` for approval handling in `backend/main.py`
finds only the emit at 2321. The `id` returned by the bridge is a
`store.create_approval` id (`backend/main.py:2329`) and `_resume` loads it via
`store.get_approval(aid)` (`backend/routes/agent_os.py:395`), so the id **is**
usable — the client must simply call the other endpoint.

**PARTIALLY CONTRADICTS** §8.3, which reads as though a single transport handles
both halves. It does not: emit is `/chat`, resume is `/agent/approvals/{id}/…`.
A client doing the §19 demo must speak to both routers.

## 9.3 Can the approval payload carry a screenshot reference and a field-by-field summary?

**NO — not without a code change.** Three independent blockers, each with a distinct fix.

### Blocker A — the approval record is a fixed dict literal

`backend/orchestrator/graph.py:112-114`:

```python
pending.append({"tool_call_id": tc["id"], "name": name, "args": args,
                "action_type": name, "preview": prev,
                "rule": verdict.rule, "risk_level": verdict.risk_level.value})
```

Seven keys, constructed inline. **A tool handler cannot contribute anything to it.**
The handler is never called on the `APPROVAL_REQUIRED` branch (that is the gate's
whole point), so `browser_submit` has no execution moment in which to attach a
screenshot id or a field summary before the pause.

### Blocker B — the human-readable text is a hardcoded `if/elif` over three tool names

`backend/orchestrator/registry.py:89-97`:

```python
def preview(name: str, args: dict) -> str:
    if name == "send_email":            return f"Send email to {args.get('to')} — subject: …"
    if name == "create_calendar_event": return f"Create calendar event …"
    if name == "web_search":            return f"Search the web for: …"
    return f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
```

`browser_submit` hits the generic fallback at line 97 and renders as
`browser_submit(browser_session_id='…', element_ref='e17')`. That is a repr of the
arguments, not a field-by-field summary of what will be submitted — and per §3.3 the
arguments are opaque refs, so **the fallback is structurally incapable of showing a
human what the form contains.** §8.2(c) ("a human approving a form submission needs
to see the form") is unreachable through `preview()`.

### Blocker C — the bridge re-projects to three fields

`backend/main.py:2333-2334` emits `{id, preview, action_type}` only. `args`, `rule`
and `risk_level` are already dropped today. `GET /agent/approvals` is no better —
`backend/orchestrator/store.py:184-188` returns a fixed six-field projection with no
`args` and no payload. There is **no `GET /agent/approvals/{id}`** returning the full
record (`backend/routes/agent_os.py:386,431,436` are the only three approval routes).

### What is *not* a blocker

Persistence is already extensible. `store.create_approval` →
`backend/orchestrator/store.py:139-141` writes the whole approval dict into JSONB:

```python
payload={"agent": agent, "messages": messages, "approval": approval,
         "supabase_uid": user.supabase_uid}
```

Arbitrary keys added to the approval dict **would** survive a round trip. The storage
layer is ready; the producer, the renderer and the projection are not.

### G2 confirmed, and worse than stated

G2 says artifacts are not exposed in non-streaming responses. Verified — and the
scope is wider than the design assumes:

- `bind_embed_sink` has **no production caller**:
  `grep -rn "bind_embed_sink" backend/` returns only the definition at
  `backend/orchestrator/consumer_tools.py:69`.
- The only artifact-read path is `GET /chat/history`
  (`backend/main.py:3173`), which rebuilds embeds from stored artifacts via
  `_embeds_from_artifacts` (`backend/main.py:3219,3224`). It is Runtime A's history
  endpoint and is keyed by `session_id`, not by approval id.
- `chat_store.add_artifact` (`backend/chat/store.py:187`) has exactly two callers —
  `backend/chat/unified.py:235` (chart lane, SSE) and `backend/main.py:2238`
  (Runtime A embeds). **Neither is on the Runtime B non-streaming path.**

So there is no mechanism today by which any Runtime B response — streaming or not —
delivers an image to a client.

## 9.4 Smallest change that makes §19 reachable

Four edits. None requires new infrastructure, a new table, or an SSE unification.

| # | Change | File | Size |
|---|---|---|---|
| 1 | Let a tool contribute approval detail. Add one optional registry hook (e.g. `Tool.approval_detail(args, ctx) -> dict`) called on the `needs_approval` branch, and merge its result under a single `detail` key in the `pending` dict. | `backend/orchestrator/registry.py`, `backend/orchestrator/graph.py:112-114` | ~15 lines |
| 2 | Pass `detail` through the bridge projection. | `backend/main.py:2333-2334` | ~2 lines |
| 3 | Add `GET /agent/approvals/{aid}` returning the full record for the owner (reusing `_resume`'s existing ownership check at `routes/agent_os.py:396-401`), so the client can fetch the summary and the screenshot reference out of band. | `backend/routes/agent_os.py` | ~20 lines |
| 4 | Store the screenshot as an artifact and put its **id** in `detail` (not the bytes). Requires binding the embed sink, or calling `chat_store.add_artifact` directly from the browser tool. | `backend/orchestrator/consumer_tools.py` sink, or direct | ~10 lines |

**Critical ordering constraint the design does not state.** Change 1 is not merely
plumbing. The screenshot and the field summary must be captured **before** the pause,
because after the pause there is no handler execution until resume. `browser_submit`
must therefore either (a) be preceded by a mandatory `browser_screenshot` +
`browser_extract` whose results the agent passes as arguments, or (b) have its
approval-detail hook perform a read against the live session at pause time. Option
(b) means the hook does I/O inside `_tools_node`, which is currently a pure-decision
path — that is a design decision §8.2(c) has not made.

**Screenshot reference, not inline.** Approval rows are JSONB in Postgres and the
bridge response is JSON; a base64 PNG inline would bloat both. §8.3's own suggestion
("carry a screenshot reference the frontend fetches separately") is the correct one
and is what change 4 assumes.

## 9.5 Verdict on the §19 demo

**Reachable, but not as designed, and not without changes to files §1 declares
unmodified.**

The design's §1 states: *"Everything between the Browser Agent and the Browser
Session Manager — registry lookup, authorization, approval pause/resume, audit — is
existing machinery, unmodified. If the implementation finds itself adding a branch to
any of those to special-case browser tools, that is a design failure."*

Changes 1–3 modify `graph.py`'s approval branch, `registry.py`'s preview, and the
approval route surface. They are **generic** (any tool gains reviewable approvals),
so they are not a browser special-case and do not violate the letter of §1 — but §1
must stop claiming this machinery is unmodified, and §8.1 must stop claiming
`browser_submit` uses the mechanism "unchanged".

---

# PART 2 — THE REMAINING 13 ITEMS

## Item 1 — LangGraph `StateGraph`: node registration and state typing

**CONFIRMS.**

- Graph built in `_build()`, `backend/orchestrator/graph.py:152-159`:
  `add_node("agent", _agent_node)` (154), `add_node("tools", _tools_node)` (155),
  `add_edge(START, "agent")` (156), two `add_conditional_edges` (157-158),
  `g.compile()` (159). Module-level singleton `GRAPH = _build()` at line 162.
- State is a `TypedDict`, `backend/orchestrator/graph.py:30-42`, 12 fields:
  `messages` (`Annotated[list, operator.add]`), `user_id`, `tenant_id`, `session_id`,
  `agent_id`, `board_id`, `allowed_tools`, `step`, `awaiting`, `model_key`,
  `fallback_models`, `has_image`.
- **No checkpointer.** `g.compile()` (line 159) takes no `checkpointer=` argument.

**Relevant to §7 (browser state):** `AgentState` has no browser field. Adding one is a
`TypedDict` edit; the three constructors that build state literals must all be
updated — `graph._init` (`graph.py:165`), `backend/dashboard/ask.py:189`,
`backend/dashboard/stream.py:89`. The design does not mention the latter two.

## Item 2 — Supervisor delegation

**CONFIRMS the dictionary. CONTRADICTS the placement.** Full sizing in Part 4.

The hardcoded dictionary is real: `SPECIALISTS` at
`backend/orchestrator/agents.py:17`, 6 entries (lines 18, 24, 31, 37, 44, 52).

**But "Supervisor" in this repo is not what §1/§2.3 describes.** Two distinct things
exist and the design conflates them:

| Design term | Repo reality | Evidence |
|---|---|---|
| "Supervisor" routing to agents | `route()` — a regex router returning one of **three lanes** (`'chart' \| 'data' \| 'primary'`), not an agent | `backend/chat/unified.py:71-82` |
| Delegation to specialists | the `delegate` **tool**, model-invoked, dictionary-backed | `backend/orchestrator/agents.py:65-104` |

`route()` cannot select an agent. There is no code path in which a supervisor
chooses "Browser Agent"; the only way a specialist runs is the model calling
`delegate`.

## Item 3 — Canonical tool registry

**MOSTLY CONFIRMS; one CONTRADICTION.**

- **Count is 43**, verified at runtime:
  `python -c "import backend.orchestrator; from backend.orchestrator import registry; print(len(registry.all_names()))"` → `43`. No `browser_*` tools present.
- Registration is `register(Tool(...))` at module import, plus three idempotent
  registrar functions called from `backend/orchestrator/__init__.py:22-23`
  (`consumer_tools.register_consumer_tools()`, `graph_tools.register_graph_tools()`).
  `backend/routes/dashboard.py:31,34` also registers on router import.
- Naming is **snake_case** throughout (`graph_search`, `knowledge_search`,
  `query_data`, `play_youtube_video`). **CONFIRMS §3.1** — use `browser_open`, not
  `browser.open`.
- **No namespace/prefix concept exists.** `grant_matches`
  (`backend/orchestrator/authz.py:186-191`) is exact set membership. §3.1's second
  `[VERIFY]` resolves to: *do not invent one.*

**CONTRADICTS §3.2's assumption** that *"the registry-completeness test almost
certainly asserts a count or a name set."* It does not. The only completeness test is
`test_every_registered_tool_has_a_risk_classification`
(`tests/test_authz_boundary.py:244-260`), which asserts **no unclassified tools**:

```python
unclassified = [n for n in registry.all_names()
                if n not in g.TOOL_CATEGORY and not n.startswith("_")]
assert unclassified == []
```

`grep -rn "43" tests/` finds no count assertion anywhere. **Consequence:** adding 14
browser tools will *not* fail a count test — but it **will** fail this test unless all
14 are added to `guardrails.TOOL_CATEGORY`. §16's Phase D exit criterion
("57 tools at import; completeness test updated deliberately") is therefore wrong
about *which* test and *what* the update is.

## Item 4 — Authorization boundary

**CONFIRMS.** It is an **explicit in-line call**, not middleware and not a decorator.

`backend/orchestrator/graph.py:92-97`:

```python
tool = registry.get(name)
verdict = authz.authorize_call(
    user_id=state["user_id"], tenant_id=state.get("tenant_id", ""),
    agent_id=state["agent_id"], session_id=state.get("session_id", ""),
    tool_name=name, arguments=args,
    granted=state["allowed_tools"], tool=tool)
```

Position relative to handler invocation: the verdict is computed at 92, and
`tool.handler` is invoked only in the `else` branch at `graph.py:120`. Deny (102) and
approval (106) both return before it. `authorize()` itself is **pure — no I/O**
(`backend/orchestrator/authz.py:195`, docstring and body).

**Consequence for §4.2's ordering requirement** ("ownership and domain checks must run
inside the authorization boundary, before the handler"): satisfiable, but *only* if
the boundary can see `target_domain` and `browser_session_id`. Because `authorize()`
is pure and takes no session store, an ownership check against a live Browser Session
Manager cannot happen inside it without breaking that purity — which 24 tests in
`tests/test_authz_boundary.py` depend on. **The design has not reckoned with this.**

Rule order (`backend/orchestrator/authz.py:207-249`): `unknown_tool` → `no_tenant` →
`no_subject` → `kill_switch` → `not_granted` → `guardrail_deny` → `outbound` →
`guardrail_approval` → ALLOW.

## Item 5 — `ToolRequest`: fixed or extensible

**CONTRADICTS §4.1 option (a) as written — but the fix is small.**

`backend/orchestrator/authz.py:73-90`: a **frozen dataclass** with exactly 8 fields —
`user_id`, `tenant_id`, `agent_id`, `session_id`, `tool_name`, `action`, `arguments`,
`risk_level`.

- Frozen ⇒ no attribute may be set after construction
  (`tests/test_authz_boundary.py:230-234` asserts this).
- Adding optional fields with defaults is **source-compatible**: every construction
  site is `build_request(...)` with keyword arguments
  (`backend/orchestrator/authz.py:143-158`), called from exactly one place,
  `authorize_call` (`authz.py:252`), itself called from two places —
  `graph.py:92` and `graph.py:239`.
- **But `as_dict()` (`authz.py:92-104`) enumerates fields explicitly** and would
  silently omit any new field from the audit record. §12 assumes `as_dict()` is the
  future OPA input document, so this is a real trap: adding `target_domain` without
  editing `as_dict()` produces a policy input missing the field the policy needs.

**§4.1(a) is feasible.** Cost: 2 dataclass fields + `build_request` params +
`as_dict()` entries ≈ 10 lines, 1 file. Existing tests do not enumerate the field
set, so nothing breaks. Recommendation (a) **stands** — with the `as_dict()` caveat
the design omits.

## Item 6 — `risk_level` enum values

**CONTRADICTS the design.**

`backend/orchestrator/authz.py:46-52` — `class RiskLevel(str, Enum)`:

```
READ = "read"   WRITE = "write"   DATA = "data"
CODE = "code"   OUTBOUND = "outbound"   UNKNOWN = "unknown"
```

**There is no `low`, `medium` or `high`.** The design uses all three:

- §4.1 sample `ToolRequest` shows `risk_level  high`
- §3.2's table classifies all 14 tools as `low` / `medium` / `high`

`risk_level` is also **derived, never supplied** — `risk_of()`
(`backend/orchestrator/authz.py:131-141`) maps `guardrails.TOOL_CATEGORY[name]`
through `_CATEGORY_RISK` (`authz.py:61-67`), with `is_outbound` overriding to
`OUTBOUND`. A tool author sets a **category**, not a risk.

**Unmapped-category trap:** `_CATEGORY_RISK` has no `egress` key, so the 9 `egress`
tools resolve via `.get(cat, RiskLevel.WRITE)` (`authz.py:141`) to **WRITE**. A
browser category added to `TOOL_CATEGORY` without a `_CATEGORY_RISK` entry will
silently be WRITE.

## Item 7 — Grant model and wildcards

**CONTRADICTS the wildcard option in §4.3 — the repo makes it impossible without new code.**

Two grant forms, exact match only (`backend/orchestrator/authz.py:186-191`):

```python
g = set(granted or ())
if tool_name in g:                                   return True, "grant:name"
if required_permission and required_permission in g: return True, "grant:permission"
return False, "not_granted"
```

No prefix, glob, or regex. Further, grants are **filtered at write time** against a
closed vocabulary at three routes —
`backend/routes/agent_os.py:65`, `:564`, `:616`:

```python
known = set(registry.all_names()) | registry.all_permissions()
```

`all_permissions()` (`backend/orchestrator/registry.py:70-78`) returns only the
`required_permission` strings tools actually declare. **A grant string `browser_*`
would be silently dropped and never stored.**

**§4.3's `[DECIDE]` is therefore settled by the code**, and settled *toward the
design's own recommendation:*

- **per-tool grants** — work today, zero change;
- **one shared permission string** (e.g. every browser tool declaring
  `required_permission="browser.use"`) — also works today, zero change, and is the
  existing idiom (`media.radio.read` covers 3 tools,
  `tests/test_consumer_tools_migration.py:150-161`);
- **wildcard `browser_*`** — needs new matching logic *and* a write-filter change.

The design's recommendation (per-tool, wildcard rejected) is **correct and free**. It
should also record that a shared permission string is the middle option, because the
design does not mention it and it is what every other tool family here uses.

**What a G1 grant looks like concretely** — `docs/runtime-b-readiness.md` §6 gives the
verified statement, executed and reversed during Phase 4 validation:

```sql
INSERT INTO agent_permissions (org_id, agent_id, permission, is_outbound)
SELECT a.org_id, a.id, p FROM agents a
CROSS JOIN unnest(ARRAY['play_youtube_video', …]) AS p
WHERE a.id = '<agent-uuid>' ON CONFLICT DO NOTHING;
```

## Item 8 — Approval pause/resume and payload extensibility

**CONFIRMS the mechanism. CONTRADICTS §8.2(c) extensibility** (detail in Part 1).

- Pause state = `{"messages": outs, "awaiting": pending[0] if pending else None}`
  (`backend/orchestrator/graph.py:124`). `_route_tools` returns `"end"` when
  `awaiting` is set (`graph.py:147-148`).
- Full run state persisted as JSONB: `store._pg_create`,
  `backend/orchestrator/store.py:139-141`.
- **Resume re-authorizes** — `backend/orchestrator/graph.py:239-247`: the verdict is
  recomputed and the handler runs only if `verdict.needs_approval` still holds;
  otherwise it refuses (`graph.py:245-247`). **CONFIRMS §8.1.**
- Single-execution guarantee: compare-and-swap before execution,
  `backend/orchestrator/store.py:207-213`.

**§8.2(b) re-validation on resume: NOT FOUND.** `resume()` re-authorizes but does not
re-read anything about the *target*. There is no snapshot-compare hook. This must be
built.

**§8.2(a) session pinning: NOT FOUND** — no TTL, lease, or session-lifetime concept
exists anywhere in the approval path.

## Item 10 — `TenantContext` propagation into services

**CONFIRMS to the handler boundary. NOT FOUND beyond it.**

- `TenantContext` — `backend/auth/tenant.py:52` (frozen dataclass: `user_id`,
  `tenant_id`, `supabase_uid`, `email`, `role`).
- Into graph state: `graph._init(..., tenant_id=...)` (`graph.py:165-178`).
- Into the handler `ctx` dict: `backend/orchestrator/graph.py:85-86`
  — `{"user_id", "agent_id", "board_id", "tenant_id"}`.
- Handlers read it: `backend/orchestrator/graph_tools.py:266`,
  `backend/orchestrator/knowledge.py:346`.
- Resume path: `backend/orchestrator/graph.py:235-236`.

**How it would reach a worker: NOT FOUND.** No RPC client, no outbound service call
carries a tenant today. The only cross-process pattern is
`registry._internal_post` (`backend/orchestrator/registry.py:100-105`), which sends
`X-Internal-Token` + `X-Internal-User` — **user, not tenant**. §5/§6 must specify this;
the repo offers no precedent to copy.

**Note for §4.1:** `_agent_node`'s ctx (`graph.py:53`) omits `tenant_id` — only
`_tools_node`'s has it. Not a defect today (the model node needs no tenant) but it
means "the ctx dict" is not uniform, which §5 should not assume.

## Item 11 — Audit sink

**CONFIRMS, with a material gap.**

`_audit()` — `backend/orchestrator/graph.py:128-139` — writes
`kind="authz_decision"` to the events spine with `meta=verdict.as_dict()`.

**Gap: it records only refusals.** `graph.py:131-132`:

```python
if verdict.allowed:
    return
```

**ALLOW decisions are not audited.** §11.6 assumes a complete audit trail; the trail
today contains denials and approvals-required, not permitted actions. For browser
work — where the interesting record is *what the agent did*, not only what it was
stopped from doing — this is a real hole. Approvals are separately audited at
`backend/orchestrator/store.py:215-218` (`kind="approval"`, with `decided_by`).

Arguments are **redacted to keys** by default in the audit record
(`backend/orchestrator/authz.py:92-104`), which §11.5 should note: a browser audit
entry will show `["browser_session_id", "element_ref"]`, not values.

## Item 12 — Test infrastructure

**CONFIRMS the two `sys.exit()` modules. CONTRADICTS the assumption of a test topology.**

- `tests/test_insight_evidence.py:136` — `sys.exit(1 if FAILS else 0)`
- `tests/test_routing.py:64` — `sys.exit(1 if FAILS else 0)`

Both execute at **import**, which crashes pytest **collection for the entire run**
(`INTERNALERROR … SystemExit`), not just their own module. Both are committed and
were not modified by prior phases.

- **Markers: NOT FOUND.** `pytest.ini` (5 lines) declares `testpaths`,
  `python_files`, `addopts = -q`, `filterwarnings`. **No `markers` section.** The only
  marks used anywhere are stdlib `parametrize` / `skipif` / `filterwarnings`, plus
  `asyncio`. There is no `integration` / `slow` / `docker` marker to hang browser
  tests on.
- **Fixtures: NOT FOUND at suite level.** Root `conftest.py` is 5 lines (`sys.path`
  insert only). **`tests/conftest.py` does not exist.** Every fixture is local to a
  test module.
- **No docker-compose test topology exists** (see item 13).
- Live-dependency tests skip **at run time**, not collection time — e.g.
  `_require_graph()` in `tests/test_graph_search_tool.py`. A module-level probe there
  previously corrupted four unrelated tests by building the Neo4j driver singleton
  during collection (`docs/runtime-b-readiness.md` §8, failure 2). **Browser tests
  must follow the run-time pattern.**

## Item 13 — Docker Compose topology

**CONTRADICTS the design's premise.**

`docker-compose.yml` is **48 lines with 4 services**: `qdrant` (3), `whisper_stt` (14),
`piper_tts` (22), `neo4j` (31).

- **No `networks:` section at all** — `grep -c networks docker-compose.yml` → `0`.
  Every service is on the default bridge. **There is no network segmentation to
  attach to**, so §11's egress-blocking and §6's container isolation have no existing
  structure to extend.
- **No `postgres`, no `redis`, no `litellm`, and no application service.** Those
  containers run (verified: `postgres`, `redis`, `litellm`, `nginx`, seaweed×4 are
  up) but are **not managed by this file**. Where they are defined: **NOT FOUND** in
  this repository.
- The app itself runs from a venv via uvicorn, not a container:
  `ps` shows `/home/matrix/aganetiAi/.venv/bin/uvicorn backend.main:app --port 8001`.

**Consequence:** §13's "where `browser-lab` and `playwright-worker` attach" has no
answer in-repo. Adding them to this file gives them a network the application is not
on, because the application is not containerised here.

## Item 14 — Runtime flag / cohort mechanism

**CONFIRMS.**

`backend/runtime_flag.py:90-109`, `choose()` — pure, env-only, first match wins:

| Order | Variable | Line | Reason emitted |
|---|---|---|---|
| 1 | `RUNTIME_B_DENY_USERS` | 98 | `deny_user` |
| 2 | `RUNTIME_B_SESSIONS` | 100 | `pinned_session` |
| 3 | `RUNTIME_B_USERS` | 102 | `pinned_user` |
| 4 | `RUNTIME_B_PERCENT` | 104-106 | `percent:N` |
| 5 | `RUNTIME_B_ENABLED` | 107 | `global` |
| — | default | 109 | `default` → Runtime A |

Bucketing is `blake2b` (`runtime_flag.py:80-88`) — stable across restarts. Consulted
as the first statement of `chat_endpoint` (`backend/main.py:2362-2369`). Live
configuration readable at `GET /runtime/flag` (`backend/main.py:2339`).

**Pinning one canary is `RUNTIME_B_SESSIONS=<session-id>` or
`RUNTIME_B_USERS=<supabase-uid>`.** Verified end-to-end in Phase 4
(`docs/runtime-b-readiness.md` §1): pinned user returned `x-runtime: B`, non-pinned
user on the same instance returned no header.

---

# PART 3 — `[DECIDE]` QUESTIONS THE REPO SETTLES

Only those the code answers. The rest remain human decisions.

| `[DECIDE]` | Repo verdict | Evidence |
|---|---|---|
| **§4.1 (a) vs (b)** — promote `target_domain`/`browser_session_id` to `ToolRequest` | **(a) is feasible; ~10 lines, 1 file.** Frozen dataclass, but all construction is keyword-based through one function. **Must also edit `as_dict()`** or the fields vanish from the audit/OPA input. | `authz.py:73-90`, `:143-158`, `:92-104`, `:252` |
| **§4.3 grant granularity** | **Wildcards are impossible without new code** — exact set membership, plus a write-time filter that drops unknown strings. Per-tool grants and a shared permission string both work today. Design's recommendation stands. | `authz.py:186-191`; `routes/agent_os.py:65,564,616`; `registry.py:70-78` |
| **§3.1 naming** (`browser.open` vs `browser_open`) | **snake_case, settled.** All 43 tools are snake_case; no namespace concept exists. | `registry.all_names()` = 43; `authz.py:186-191` |
| **`risk_level` values** | **`read\|write\|data\|code\|outbound\|unknown`.** No `low/medium/high`. Derived from category, never author-supplied. | `authz.py:46-52`, `:131-141`, `:61-67` |
| **§6.1 in-process Playwright fallback** | **Repo evidence points against it.** `run_python` already establishes the out-of-process precedent (`docker run --network none --read-only --memory 256m --pids-limit 128`, 25 s timeout) — `skills.py:175-200`. Note the app invokes `docker` as a host user in the `docker` group, flagged S7 in `architecture-audit.md`. | `backend/orchestrator/skills.py:175-200` |
| **§10.1 lab stack** | **NOT SETTLED**, but constrained: compose has no networks and no app service (item 13), so "attach the lab to the app network" is not currently expressible. | `docker-compose.yml` |
| §2.2 bundle-or-sequence | Sizing in Part 4; the decision remains human. | — |
| §3.3 element cap / semantic locators, §5.3, §5.4, §8.2(a) | **NOT FOUND** — no repo evidence either way. | — |

---

# PART 4 — SUPERVISOR DELEGATION: SIZE ESTIMATE

## Where it is

**`backend/orchestrator/agents.py:17`** — `SPECIALISTS: dict[str, dict]`, 6 entries at
lines 18, 24, 31, 37, 44, 52. Each entry is `{name, prompt, tools}`.

A **second, divergent** dictionary exists: `backend/orchestrator/templates.py:57`,
**4 entries** (58, 64, 70, 76). It seeds DB rows; `agents.SPECIALISTS` is what
executes. They disagree — `templates`' `email_agent` grants `send_email`,
`agents`' does not (`templates.py:80` vs `agents.py:42`).

## What depends on it — 9 call sites

| # | Site | Use | Coupling |
|---|---|---|---|
| 1 | `agents.py:68` | `SPECIALISTS.get(to_agent)` — the lookup | **the fix** |
| 2 | `agents.py:70` | error message lists valid keys | trivial |
| 3 | `agents.py:72` | `spec["tools"]` → sub-agent allowlist | **the fix** |
| 4 | `agents.py:74` | `spec["prompt"]` → system prompt | **the fix** |
| 5 | `agents.py:76,78` | `spec['name']` → result formatting | trivial |
| 6 | `agents.py:97` | **`"enum": list(SPECIALISTS.keys())` in the tool schema** | **hardest — see below** |
| 7 | `agents.py:105` | `specialist_names()` | trivial |
| 8 | `__init__.py:18,34` | package re-export | trivial |
| 9 | `tests/test_authz_boundary.py:189-190, 205-206` | two tests iterate it | rewrite |

Plus `templates.py:93` (`specialist_templates()`, seeding — separate concern).

## The hard part

**Call site 6 is a static enum baked into the tool schema at import time.** Registry
tools are registered once at module import (`agents.py:88-104`); the schema is handed
to the model by `registry.openai_schemas()` (`registry.py:60-67`). A DB-backed roster
is **per-user and dynamic**, so the enum cannot be resolved at import.

Three options:

- **(a) Drop the enum**, make `to_agent` a free string, validate at handler time
  against the registry. Smallest change; costs some model routing accuracy.
- **(b) Build the schema per turn.** `_agent_node` calls `openai_schemas` at
  `graph.py:46`; a per-turn override would need a state-carried roster. Touches the
  hot path.
- **(c) Keep a static enum of *template keys*, resolve the instance from the DB.**
  Hybrid; keeps model-facing stability, still requires the DB lookup.

## Second hard part: `_delegate` is sync-context, the registry is async

`repo.list_agents`/`get_agent`/`allowed_tools`
(`backend/db/repo.py:129,124,152`) are `async` and take an `AsyncSession`.
`_delegate` is already `async` (`agents.py:67`), so this is workable — but it
introduces a **DB round trip into every delegation**, where today it is a dict lookup.
No caching layer exists for agent rows (`tenant.resolve_tenant_id`,
`backend/auth/tenant.py`, has a 300 s TTL cache — a precedent, not a facility).

## Data problem the design does not mention

The live `agents` table cannot currently back this:

```
specialist | Calendar Agent    | archived  | calendar_agent
specialist | Finance Agent     | active    | demo_mesh
specialist | HR Compliance     | active    | demo_mesh
specialist | IT Security Agent | active    | demo_mesh
specialist | Legal Agent       | active    | demo_mesh
specialist | Research Agent    | archived  | research_agent
specialist | Research Agent    | archived  | research_agent
specialist | Research Agent    | active    | research_agent
specialist | Research Agent    | active    | demo_mesh
```

- Four of six executing specialists (`task_agent`, `email_agent`, `analyst_agent`,
  `predictive_agent`) **have no DB row at all** — switching to a registry lookup
  *removes* working capability unless they are seeded first.
- `template_key` is not unique: four rows are `demo_mesh`, and `Research Agent`
  appears 4× (2 active, 2 archived). **There is no stable key to route on.**
- The DB rows have `system_prompt` but the executor needs `tools` from
  `agent_permissions` — a second query per delegation.

## Estimate

| Work | Size |
|---|---|
| `_delegate` → registry lookup (sites 1, 3, 4) | ~40 lines, 1 file |
| Enum decision (site 6) — option (a) | ~10 lines |
| Trivial sites (2, 5, 7, 8) | ~10 lines |
| Seed the 4 missing specialists + stable routing key (migration or seed script) | ~60-100 lines, new file |
| Reconcile the two `SPECIALISTS` dicts | ~20 lines, 1 file |
| Rewrite 2 coupled tests + new registry-routing tests | ~120 lines |
| **Total** | **~1 day focused; 4-6 files; ~250-350 lines** |

**Risk: medium, and it is not in the code.** The code change is small and
well-bounded. The risk is the **data**: seeding four specialists, choosing a routing
key that is currently non-unique, and deciding what happens to the four `demo_mesh`
rows. That is a schema/data decision, not a refactor.

## Recommendation on §2.2 (bundle or sequence)

**Sequence it.** Three reasons from the evidence:

1. It is a **data migration** as much as a refactor. Bundling a migration with a new
   subsystem means one rollback for two unrelated failure modes.
2. **Phases A–F genuinely do not depend on it** — the design says so, and the repo
   confirms it: browser tools reach the executor through the registry
   (`graph.py:92`), not through `delegate`. A Browser Agent can be exercised in
   Phases A–F by granting the tools to a **primary** agent, exactly as Phase 4
   validation did for `graph_search` (`docs/runtime-b-readiness.md` §6).
3. The enum problem (site 6) is a model-facing behavioural change that deserves its
   own before/after routing measurement, which browser work would confound.

---

# PART 5 — DESIGN SECTIONS REQUIRING REVISION

| § | Required revision | Driver |
|---|---|---|
| **§1** | Remove "Supervisor" from the pipeline diagram for the non-streaming PoC — **`route()` is not on that path** (`main.py:2300-2336` calls `_load_primary` + `run_turn` directly; no `unified_stream`, no `route()`). Also stop asserting approval machinery is "unmodified": §19 needs changes to `graph.py`, `registry.py` and the approval routes. | Items 2, 9 |
| **§2.2, §2.3** | "Supervisor routes to Browser Agent" is not implementable as described. `route()` returns one of three **lanes**, not an agent (`unified.py:71-82`). Specify whether Browser Agent is reached by `delegate` (a tool the model calls) or whether a real agent-selecting supervisor is in scope. | Item 2 |
| **§3.1** | Confirm `browser_open` (snake_case). Delete the namespace/prefix `[VERIFY]` — none exists; do not invent one. | Item 3 |
| **§3.2** | Replace `low/medium/high` with the real enum (`read/write/data/code/outbound/unknown`), and note risk is **derived from `TOOL_CATEGORY`**, not declared. Add: every browser tool must be added to `guardrails.TOOL_CATEGORY`, and any new category must also get a `_CATEGORY_RISK` entry or it silently becomes WRITE. | Item 6 |
| **§3.2** (count) | Correct the claim about the completeness test: it asserts **classification**, not a count (`tests/test_authz_boundary.py:244-260`). No test asserts 43. | Item 3 |
| **§4.1** | Keep recommendation (a) but add: `ToolRequest.as_dict()` (`authz.py:92-104`) enumerates fields and must be edited too, or the new fields are absent from the audit record and the future OPA input. | Item 5 |
| **§4.2** | Resolve the conflict between "ownership checks must run inside the authorization boundary" and `authorize()` being **pure with no I/O**. A session-ownership check needs state the boundary cannot reach without breaking that property (and 24 tests). | Item 4 |
| **§4.3** | Record that wildcards are not merely inadvisable but **structurally impossible** without new matching logic *and* a write-filter change. Add the shared-permission-string option, which is the existing idiom. | Item 7 |
| **§8.1** | "uses that mechanism unchanged" is false for a reviewable payload. State which generic extensions are required. | Item 9 |
| **§8.2(c)** | Specify **when** the screenshot and field summary are captured. The handler never runs before the pause, so either the agent must pre-supply them as arguments, or an approval-detail hook must do I/O inside `_tools_node` — currently a pure-decision path. | Item 9 |
| **§8.3** | Correct: emit is `/chat`, resume is `POST /agent/approvals/{id}/approve\|reject`. Two routers. Also add the missing `GET /agent/approvals/{id}` needed to fetch a payload richer than the three fields the bridge projects. | Item 9 |
| **§11.6** | The audit sink records **refusals only** (`graph.py:131-132` returns early on ALLOW). A complete browser audit trail requires auditing permitted actions too. | Item 11 |
| **§13 / §16 Phase A** | No compose network segmentation exists (0 `networks:` blocks) and the app is **not containerised** in this repo. Restate what `browser-lab` and `playwright-worker` attach to. | Item 13 |
| **§16 Phase D** | Exit criterion "57 tools at import; completeness test updated" — the test to update is the **classification** test, and the count is not asserted anywhere. | Item 3 |
| **§16 Phase G** | Add the delegation prerequisite the design omits: four specialists have **no DB row**, and `template_key` is **not unique**. | Part 4 |
| **§15** | Item 3's phrasing ("the completeness test that asserts 43") presumes a test that does not exist. | Item 3 |
| **Doc-wide** | §15 references **§19** (HITL demo) and **§24** (acceptance); §16 references **§28**, **§29**. **The document ends at §16.** Those sections do not exist in this file. | — |

---

# PART 6 — WHAT THE DESIGN MISSED ENTIRELY

Findings with no corresponding `[VERIFY]` or `[DECIDE]`.

1. **The non-streaming PoC bypasses the Supervisor completely.** `_serve_via_runtime_b`
   (`main.py:2300-2336`) calls `_load_primary` + `run_turn`; it never touches
   `unified_stream` or `route()`. The design's §1 pipeline shows Supervisor in the
   path and §8.3 chooses non-streaming. **These two choices are mutually exclusive
   as the code stands.** This is arguably more blocking than item 9.

2. **`AgentState` is constructed in three places, not one.** `graph._init`
   (`graph.py:165`), `dashboard/ask.py:189`, `dashboard/stream.py:89`. Any §7 browser
   state field must be added to all three or the analytics/chart lanes break on a
   missing key.

3. **No LangGraph checkpointer exists** (`graph.py:159`, bare `compile()`), and
   `agent_runs` has no writer. §16 Phase I ("resumes across process restart") has no
   foundation — this is P1/G7 in `runtime-b-readiness.md`, unmentioned in the design.

4. **ALLOW decisions are never audited** (`graph.py:131-132`). §11.6 assumes a
   complete trail.

5. **Audit arguments are redacted to keys by default** (`authz.py:92-104`). A browser
   audit record shows `["browser_session_id","element_ref"]`, never values. Relevant
   to §11.5 and to any post-hoc investigation.

6. **`_agent_node`'s ctx omits `tenant_id`** (`graph.py:53` vs `graph.py:85-86`). The
   handler ctx is not uniform across nodes.

7. **No pytest markers and no `tests/conftest.py`.** Nothing exists to gate
   browser/Docker tests on; every fixture is module-local. §13's test strategy assumes
   infrastructure that is absent.

8. **Two committed test modules `sys.exit()` at import** and crash collection for the
   whole suite. Any CI gate on the browser tests is unreliable until fixed.

9. **`registry.preview()` is a hardcoded three-branch function** (`registry.py:89-97`).
   Every future approval-gated tool degrades to an argument repr. This is a generic
   defect the browser work merely exposes first.

10. **Postgres, Redis and LiteLLM are not in `docker-compose.yml`** and their
    definitions are **NOT FOUND** in this repository. Any topology change assumes
    files that are not here.

11. **The two `SPECIALISTS` dicts disagree on `send_email`** (`templates.py:80` grants
    it, `agents.py:42` does not). A registry-backed delegation must pick one, and one
    of them is a privilege change.

12. **`egress` has no `_CATEGORY_RISK` entry** (`authz.py:61-67`), so 9 existing tools
    silently resolve to `WRITE`. A browser category would inherit the same trap.

13. **`run_python` is the existing out-of-process precedent** (`skills.py:175-200`) and
    the design does not cite it — including its known caveat that the app invokes
    `docker` as a host user in the `docker` group (S7, `architecture-audit.md`).
    §6/§11 should either follow that pattern or say why not.

14. **`GET /agent/approvals` returns a fixed six-field projection**
    (`store.py:184-188`) with no `args`. Even with a richer stored payload, the list
    endpoint would not show it.
