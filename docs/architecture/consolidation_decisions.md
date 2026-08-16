# Consolidation Decisions

Companion to `phase0_implementation_plan.md`.

**Repository** `/home/matrix/aganetiAi` · **HEAD** `432fd5e`
**Nothing in this document has been implemented. Nothing is deleted in Phase 0.**

---

## The governing rule

> **The survivor is the twin that already carries a governance primitive or a tenancy seam**,
> because those are the two things that cannot be retrofitted cheaply.

On that rule: the LangGraph runtime beats the NATIVE_TOOLS loop, the typed `Tool` registry beats
`TOOL_SCHEMAS`, `services/llm.py` beats `orchestrator/router.py`'s client, the Fernet token stack
beats the plaintext one, and Postgres beats SQLite everywhere.

**Verdict vocabulary**

| Verdict | Meaning |
|---|---|
| **KEEP** | The survivor. Extend this. |
| **DEPRECATE** | Marked, banner-commented, guarded by CI. Still runs. Still tested. |
| **MIGRATE** | Its *logic* moves onto the survivor. The module may then be deprecated. |
| **DELETE-LATER** | Deletable **only** once its stated precondition holds — in a later phase. |

---

## Register

### 1. Agent runtime

| | |
|---|---|
| **KEEP** | `backend/orchestrator/graph.py`, surfaced by the already-mounted `/agent/chat` (`routes/agent_os.py:95-145`) |
| **DEPRECATE** | The `NATIVE_TOOLS` loop at `main.py:2266` (`@app.post("/chat")`, `MAX_TOOL_ROUNDS=4`) over `backend/tools.py` |
| **DELETE-LATER** | Yes |

**Why the survivor is correct — four properties only it has.**
1. It is the **only** path that enforces approval: `graph.py:86-92` never calls an outbound tool's
   handler; it records `pending` and answers the tool-call protocol with a placeholder.
2. It is the only path with **persisted, exactly-once** approvals (`store.create_approval` →
   compare-and-swap `store.decide` → `graph.resume`).
3. It is the only path with **typed per-tool authorization** (`Tool.required_permission`,
   `Tool.is_outbound`).
4. **Decisive for tenancy:** it is the only chat path that already carries `org_id` into a write —
   `agent_os.py:169 repo.upsert_and_touch_chat_session(..., org_id=user.org_id)`. The losing path
   writes no org anywhere.

**`/agent/chat` is not a stub.** It already does DB-backed primary-agent load with
persona/prompt/model/permission-derived allowlist, conversation history with stale-marking,
long-term memory recall, approval persistence, session-registry upsert and post-turn memory capture.
**The parity gap is one endpoint** — the UI calls `/api/chat`, `/chat/history`, `/chat/tools`,
`/chat/suggestions`; the LangGraph lane already provides `/chat`, `/history` and `/tools`, and
additionally offers approvals, sessions and memory the legacy lane lacks. Only `/suggestions` is
missing.

> **DO NOT cut `/api/chat` over in Phase 0.** Two facts the audit did not connect:
> (a) the frozen GraphRAG pipeline's **only** production wiring is `build_ranked_context` at
> `main.py:2382`, which lives **exclusively on the losing runtime**; (b) **19 of the 24** tools that
> exist only on the losing side are the media/consumer surface **currently being written by another
> developer in the uncommitted tree**.
> Phase 0's job is to **freeze the boundary and publish a parity ledger**.

**BEFORE DELETION:** the parity ledger is empty; GraphRAG context assembly is available on the
survivor; the tool-toggle mechanism has migrated (§4); `/suggestions` exists; and no UI route calls
`/api/chat`.

---

### 2. Tool registries

| | |
|---|---|
| **KEEP** | `backend/orchestrator/registry.py` — typed `Tool` with `is_outbound`, `required_permission` |
| **DEPRECATE** | `backend/tools.py` `TOOL_SCHEMAS` |
| **DELETE-LATER** | Yes |

Ten tool names are registered in **both** registries with **different approval semantics** —
verified: `complete_task`, `create_task`, `draft_email`, `get_agenda`, `get_analytics`, `read_email`,
`remember_fact`, `resolve_contact`, `set_reminder`, `web_search`.

**Phase 0 deliverable:** a name-by-name **parity ledger** as a tracked artifact, plus `Tool` v1
field additions (`group`, `pack`).

**BEFORE DELETION:** the ledger shows zero unmatched names; `GROUP_IDS` has moved out of
`backend/tools.py` (it currently lives inside the module being deprecated, and
`services/tool_prefs.py:53,95` imports it from there — **the loser cannot be deleted while it owns
the group vocabulary**).

---

### 3. Security divergences between the two runtimes

Not a consolidation, but the reason the consolidation is safety-relevant. **One of these is a
standalone Phase 0 fix, independent of and prior to any migration:**

> **`schedule_meeting` (`tools.py:1258`) performs a real provider calendar write while
> `guardrails.decide()` classifies it `"approval"` and `tools.py:957` only refuses `"deny"`.**
> The ungated path does not merely lack a gate — **it actively overrides a policy verdict.**

Fix `tools.py` to honour the `"approval"` verdict in Phase 0. The remaining divergences are
resolved by the migration itself.

---

### 4. Outbound-ness — three disagreeing sources of truth *inside the survivor*

| | |
|---|---|
| **KEEP** | `registry.Tool.is_outbound` — the executor's authority, read at `graph.py:86` |
| **DEPRECATE** | `templates.py:52 OUTBOUND` / `templates.is_outbound()` — redefine as a **derived read** of the registry |
| **MIGRATE** | `agent_permissions.is_outbound` — one writer, from the registry |

The three disagree on `web_search`:
- `registry` says outbound (`skills.py:409`).
- `templates.OUTBOUND` **omits it** — even though the same file's own prompt text at
  `templates.py:28` tells the model web_search **is** outbound, and its comment at `:50-51` claims
  it "mirrors" the registry. It does not.
- The persisted column has **two writers that disagree**: `onboarding.py:50` seeds it from
  `templates` (→ web_search False) while `repo.py:201` writes it from the registry (→ True).

**Consequence:** for any user provisioned through onboarding, the persisted row says web_search is
not outbound while the executor gates it anyway, and the UI (`agent_os.py:479`) shows an incorrect
outbound list. **Not currently exploitable** — the gate reads the registry — but it is **corrupted
security metadata that a future PolicyInterface would consume as truth.**

> **Collapse this BEFORE PolicyInterface is defined.** Wiring a policy engine to this column first
> encodes the wrong answer into the new abstraction.

---

### 5. Tool authorization mechanisms

| | |
|---|---|
| **KEEP BOTH SEMANTICS** | per-agent allowlist (`agent_permissions` → `graph.py:84`) **and** per-user/per-session group toggles (`services/tool_prefs.py`) |
| **MIGRATE** | the toggle mechanism onto the survivor |
| **DEPRECATE** | `main.py:1366 _offered_tools`, `tools.py:966-973` |

These answer **different questions** — "is this agent permitted?" vs "has this user switched this
group off for this chat?" — and both are legitimate. The toggles are a shipped, user-visible feature
with 16 tests pinning "absent means enabled", subtract-only semantics, and "a disabled group is
absent from the payload, not merely refused at dispatch".

Survivor computes: `agent_allowlist ∩ (all_tools − disabled_groups(user, session))`, preserving
subtract-only ordering so **a toggle can never grant a tool the agent lacks**.

> **Order matters:** if the `/api/chat` cutover happens before the toggles move, the cutover
> **silently deletes a shipped feature and breaks 16 tests.** That is the most likely way this
> migration fails in an unrecoverable, user-visible way.

**BEFORE DELETION:** `tests/test_tool_reachability.py` passes **unmodified** against the survivor's
payload builder, and `GROUP_IDS` has moved.

---

### 6. Model gateway

| Component | Verdict |
|---|---|
| `services/llm.py` transport, typed errors, `strip_think`/`ThinkFilter` | **KEEP** |
| `router.py` `MODELS` registry, `plan()`, fallback chain, `_log()` cost/token accounting | **MIGRATE onto the survivor** |
| `router._client` / `_clients` pool | **DEPRECATE** |
| `AsyncAzureOpenAI` branch, `dashboard_model()` | **DELETE-LATER** — deleted, not moved |

`router._log()` is currently the **only** token/cost telemetry in the system.

**The survivor has a real gap:** the shipped React chat already uses `llm.py`, but through raw
transport primitives with a **hand-rolled streaming tool-call SSE parser at `main.py:1412-1496`**.
`llm.py` has **no streaming-with-tools API**. Any plan that ignores this will fork `main.py` again
or regress the shipped path.

**Payload divergence, not just plumbing:** `router.py:161` skips the
`chat_template_kwargs enable_thinking=False` suppression when `provider=="azure"`, and has no
`ThinkFilter` — while `llm.py:16-18` explicitly documents that the flag alone cannot be trusted. So
**reasoning tokens can surface on the router path and not the gateway path.** The Azure branch is
also the only place an external provider API key lives inside the app process.

**Ordered migration:** (1) bring LiteLLM's `model_list` into the repo as a versioned artifact —
without it the aliases cannot be validated; (2) add `plan()`/fallback/cost-logging to `llm.py` as
**additive** API, no call-site change; (3) repoint `orchestrator/llm.py:8` (the single indirection
`graph.py:64` uses); (4) the Azure removal is an **alias swap** — `gpt-4.1` is already registered in
LiteLLM as `azure/gpt-4.1` at the same endpoint.

**DO NOT move embeddings behind the gateway.** The gateway hosts no embedding model at all, and the
frozen 174-case baseline and 993-point collection were produced by bge-small-en-v1.5 @ 384 dims.

---

### 7. Storage backend selector — freeze it first

| | |
|---|---|
| **KEEP** | the `postgres` branch |
| **DEPRECATE** | the `sqlite` branch |
| **DELETE-LATER** | Yes |

Five modules select their system of record from a bare `os.getenv("AGANETI_DATA_BACKEND", "sqlite")`
— `events.py:21`, `tasks/store.py:10`, `orchestrator/store.py:26`, `chat/store.py`, `db/sync.py`.
Production `.env:40` sets `postgres`. It appears in **neither** `.env.example` **nor**
`config/settings.py`.

**Consequences.**
1. A fresh clone or a deploy built from `.env.example` runs **SQLite** for events, tasks, approvals
   and chat — a different system of record, with different isolation properties (the SQLite branches
   have no `org_id`). *"The same signed artifact deploys to Customer #1 and #2"* **is not currently
   true even for one customer.**
2. **CI runs the SQLite branch.** Any test written to prove a consolidation ("approvals persist",
   "events carry org_id") passes against a branch production does not use. **This silently weakens
   every migration test in this plan** unless the gate pins the backend.
3. It is the concrete instance of the missing `ConfigSchema` contract.

**Phase 0, all non-behavioural:** declare it as a typed validated setting; add it to `.env.example`
with the production value and a deprecation comment; have the five modules read the typed setting;
add a loud startup assertion when the SQLite branch is active. **Do not flip the default in
Phase 0** — that changes which database a fresh deploy writes to.

---

### 8. PostgreSQL / SQLite paths

| | |
|---|---|
| **KEEP** | Postgres as system of record |
| **MIGRATE** | six SQLite-only islands |
| **DEPRECATE** | the SQLite branches |
| **DELETE-LATER** | `tasks/tasks.db` |

The cutover is **~60% done** — this is a split brain, not an unstarted migration.

- **Already dual-backend, Postgres live:** `events.py`, `tasks/store.py`, `orchestrator/store.py`,
  `chat/store.py`, `db/sync.py`.
- **SQLite-only islands:** `delegation.py` (`delegations`), `integrations/agent_inbox.py`
  (`agent_messages` — **the only agent-to-agent transport**), `activity.py`, `initiatives.py`,
  `integrations/contacts.py`, `scheduler/schedule_manager.py`.
- **Vestigial, no data dependency:** `registry.py:24-26` and `router.py:25-27` both compute a `_DB`
  path that no statement in those files uses.
- **Second SQLite file:** `backend/dashboard/dashboard_configs.db` — a **pack-boundary** question,
  not a core migration.

> **Why the split brain matters more than the migration:** with production on `postgres`, a
> delegation row is written to SQLite while the approval, event and task rows for the same user go
> to Postgres. **Nothing joins them, and only the Postgres rows carry `org_id`.** Any tenancy work
> filtering on `org_id` will be correct for four tables and **silently blind** to delegation and
> agent-messaging state.

**Priority within the islands:** `agent_messages` first — it is the only existing agent-to-agent
transport and the address-space repair depends on it.

---

### 9. OAuth / token stacks — a latent credential-exposure bug, not just dead code

| | |
|---|---|
| **KEEP** | `backend/services/provider_tokens.py` — Fernet at rest, consumed by 14 modules including the **mounted** `routes/provider_auth.py` |
| **DEPRECATE** | `backend/auth/providers/registry.py` token functions |

**Both stacks read and write the SAME Supabase table, `provider_connections`.** The survivor
encrypts before any store. The loser reads `access_token`/`refresh_token` **raw** (`registry.py:37-47`)
and, on refresh, **writes them back in plaintext** (`registry.py:72-77`).

> So the moment `auth/routes.py` is ever mounted — by anyone, for any reason, including someone
> "reviving" `UserContext` — two things happen: the loser treats the survivor's Fernet ciphertext as
> a bearer token (**every provider call 401s**), and on refresh it **overwrites the encrypted columns
> with plaintext credentials**. That is **one line of wiring** away from silently de-encrypting
> every user's OAuth tokens at rest.

**Phase 0 (both trivial, both high value):** a module-level deprecation banner, and a CI guard
asserting `backend/auth/routes.py` is never passed to `include_router`.

**Note:** `auth/providers/microsoft.py` already imports `provider_tokens`, so the provider
**classes** are not uniformly dead and must be triaged separately from `registry.get_provider_token`.

---

### 10. Approval persistence

| | |
|---|---|
| **KEEP** | `orchestrator/store.py` `_pg_*` |
| **DEPRECATE** | `_sq_*` |

Three properties the SQLite branch lacks: **(1)** `org_id` — only the Postgres branch records which
tenant an outbound action was approved for, and the approval gate is the platform's strongest
governance primitive; **(2)** `decided_by` — on SQLite an approval audit trail **cannot answer "who
approved this"**; **(3)** the compare-and-swap that makes execution exactly-once is a database-level
guarantee, and the SQLite file is opened per-call from a single process — acceptable **only** because
there is exactly one uvicorn worker. **The moment a worker tier exists, the exactly-once property is
gone.**

This is why §7 is blocking: today CI would exercise `_sq_decide` while production exercises
`_pg_decide`, so a test asserting "an approved outbound action executes exactly once" tests the
branch that does not ship.

**BEFORE DELETION:** default is `postgres`; the approvals table carries `org_id` and `decided_by` for
all historical rows; the exactly-once test runs against Postgres.

---

### 11. Event-write paths

| | |
|---|---|
| **KEEP** | `repo.log_event`'s **signature** (async, session-scoped, explicit `org_id`, `agent_id`, `run_id`, `cost_micros`) as the target shape |
| **KEEP** | `events.log_event`'s **call ergonomics** — 20 call sites will not be rewritten to pass an `AsyncSession` |
| **MIGRATE** | `events.log_event` to delegate to `repo.log_event` internally |
| **DEPRECATE** | the SQLite writer |

**This is where `EventEnvelope` is introduced — as the SHAPE written to the existing `events` table,
with no broker.** Mapped to what exists: `event_type` ← the existing 14-kind `kind` vocabulary
(preserve it); `tenant_id` ← `org_id`, **which the Postgres branch already resolves and writes, so
this field costs nothing at the write site**; `trace_id` ← the `x-trace-id` already stamped by
`main.py:906` and never joined; `timestamp` ← `ts`; `payload` ← `meta`; `event_version`,
`causation_id`, `correlation_id`, `producer` are new and nullable.

**DO NOT change `events.py` into a bus.** It stays the audit sink.

---

### 12. Delegation

| Implementation | Verdict |
|---|---|
| (A) the `delegate` tool (`agents.py:67-96`) — runs a specialist as a **nested `graph.run_turn`** | **KEEP** — the only real agent, and the only one inside the approval gate's blast radius |
| (B) `backend/delegation.py` — tracked SQLite lifecycle + status pills | **MIGRATE** the lifecycle and persistence (its sub-agents are thin wrappers, not agents) |
| (C) `main.py:1530-1555` two-node mesh behind `POST /delegate` | **DELETE-LATER** |

(A) returns a string and leaves **no record**; (B)'s value is precisely the persisted lifecycle and
the user-visible status.

**(C) is not merely dead — it is mounted and reachable**, its request model has **no user field**,
and it calls `retrieve_corporate_context(..., owner=None)` at `main.py:1532` — **reading the
org-wide corpus with the caller's identity discarded.** Deprecate the route immediately (mark + log
line; do not delete).

> **A governance hole in the survivor, worth encoding as an invariant now:** `agents.py:74-77`
> handles a nested run returning `awaiting_approval` by **formatting it into a string** for the
> parent. The approval is never persisted. **A delegated outbound action is neither executed nor
> approvable — it is silently dropped.** Currently unreachable because no specialist roster grants
> an outbound tool. **A Phase 5 planner that delegates makes it reachable.**

---

### 13. Specialist agent rosters

| | |
|---|---|
| **KEEP** | the DB `agents` table as system of record; `templates.py:57 SPECIALISTS` as the **seed** |
| **DEPRECATE** | `agents.py:17-63 SPECIALISTS` (the duplicate) and `delegation.py:34 KNOWN_AGENTS` |

Three rosters, none authoritative, all overlapping: the `delegate` tool enumerates its allowed
values from `agents.py` (`"enum": list(SPECIALISTS.keys())`), the DB is seeded from `templates.py`,
and the SQLite lifecycle validates against a third five-name tuple that invents `memory_agent`,
`scheduler_agent`, `aria` and omits research/task/analyst/predictive.

**Same shape of bug as the `agent_messages` address split — three vocabularies for one address
space — and it is why `AgentManifest` cannot be defined until it is resolved.**

The `delegate` tool should resolve against the caller's DB agents with `templates` as pre-seed
fallback, **exactly as `_load_primary` already does for the primary agent**. That pattern exists and
works; reuse it rather than inventing a second resolution path.

---

### 14. Memory systems — and a verified broken bridge

| | |
|---|---|
| **KEEP** | Postgres `memory_items` as **system of record** (the only tier with `org_id`) |
| **KEEP** | Qdrant `user_memory_<id>` as **retrieval index** (per-user collections — the strongest isolation any store here has) |
| **DEPRECATE** | the idea that they are alternatives |

**VERIFIED DEFECT.** `chat/memory.py:68,73` call `long_term.store_memory(...)` and `:222` calls
`long_term.forget(...)`. `memory/long_term.py` defines exactly `_require_uid`, `_get_embedder`,
`get_vector_size`, `ensure_collection`, `embed_text`, `extract_facts_from_summary`, `upsert_facts`,
`extract_and_store`, `format_timestamp_for_search`, `search_memory`. **Neither `store_memory` nor
`forget` exists anywhere in the repo.** The call sites carry `# type: ignore[call-arg]` and
`# type: ignore[attr-defined]` — the author knew. **Every fact capture and every forget silently
raises `AttributeError`.**

**The bridge is the deliverable**, built on the existing `upsert_facts`/`embed_text`/
`ensure_collection` primitives.

> **Identity is the hard part and it is a blocking sub-decision.** The collection name is
> `f"user_memory_{user_id}"` and the id differs across at least five call sites (session_id,
> supabase_uid, internal uuid, `X-Auth-User`, config aliases). **Choose one before the bridge is
> written.** Recommendation: the internal Postgres user UUID — the only id that also keys
> `memory_items`, and therefore the only one that makes the record and the index joinable.

---

### 15. Qdrant clients and embedders — cheapest item, hides a real bug

| | |
|---|---|
| **KEEP** | one accessor each: `get_qdrant()` and `get_embedder()` |
| **DEPRECATE** | five other client constructions, two other embedder loads |

**Six** Qdrant instantiations in three configurations (`main.py:1285`, `ingest.py:71`,
`long_term.py:53,121,171`, `memory_admin.py:26`, `observability.py:138`,
`observability_explorer.py:480`) — no shared pool, timeout or retry, and **no shared place to add
the API key that a shared, keyless, network-reachable instance holding another application's data
urgently needs.**

**Three fastembed loaders — and `main.py:1298` HARD-CODES `BAAI/bge-small-en-v1.5`, ignoring
`EMBED_MODEL_NAME`.** Not a style problem: if `EMBED_MODEL_NAME` ever changes, two loaders follow it
and one does not, and **vectors written by the third become silently incomparable with the
collection they are written into.** It is also three multi-hundred-megabyte model loads in one
process.

Fixed at **one** place if this happens first, at **six** if it happens after. Lowest-risk item in the
programme — pure indirection, no behaviour change. Good first landing.

---

### 16. Evaluation entrypoints

| | |
|---|---|
| **KEEP** | `backend/evals/` — 174-case frozen v3.1 dataset, nine stages, six metric families, 43 self-tests, artifact manifests |
| **MIGRATE** | `evals/tool_calling_eval.py`'s 23 cases and its 0.85 threshold **into** it as a tool-selection suite |
| **DEPRECATE** | the standalone entrypoint, **after** the fold-in |

**Do not deprecate it first.** `tool_calling_eval.py` is the **only executable coverage of the
surviving tool registry and the surviving model router** — "no test imports
`backend.orchestrator`" is true of `tests/` but **not** of `evals/`. Deprecating first deletes the
registry's only coverage.

**Two sequencing traps:** (a) it calls `router.complete` — the **losing** gateway, so the gateway
consolidation must repoint it or the suite dies; (b) it is imported at module scope with
`# registers skills into the registry` — a registration-by-import-side-effect that the
`PluginManifest` work will change.

**The preservation action is the urgent one and it is broader than the audit said** — see §3 of the
implementation plan. `git ls-files docs` → 0; **22 of 35 test modules are untracked**, including the
two the audit cites as the entire basis for rating Gate 2 PARTIAL.

---

### 17. Customer-coupled tools in the CORE registry

| | |
|---|---|
| **KEEP** | the registration mechanism (it is the right seam) and the allowlist as the enforcement point |
| **MIGRATE** | ownership **into data** — add `pack: str = "core"` to `Tool` v1 |
| **MOVE NO CODE IN PHASE 0** | extraction is Phase 6 |

Four customer modules register tools into the **shared core registry** by import side effect
(`dashboard/tools.py:22,347`, `analytics_tools.py:18,82`, `forecast_tools.py:9,84`,
`compare_tools.py:9,104`). `main.py:858-866` mounts the dashboard router inside a `try/except`, so
**the count of tools in the "core" registry is customer-dependent AND driver-dependent** — it
depends on whether an Azure SQL driver imported successfully.

`main.py:860`'s comment says they are "kept out of the primary agent's allow-list" — true, and
exactly the right instinct: **the per-agent allowlist is already functioning as a pack boundary.**
What is missing is that a `Tool` carries no ownership metadata, so **CI Gate 1 cannot be written
against a registry where core and customer tools are indistinguishable.**

Same decision shape: `backend/dashboard/dashboard_configs.db` should be **labelled pack-owned**, not
migrated into the core Postgres schema, or the extraction will have to unpick it later.

---

## Summary table

| # | Duplicate | KEEP | DEPRECATE / MIGRATE | Delete precondition |
|---|---|---|---|---|
| 1 | Agent runtime | `graph.py` | NATIVE_TOOLS loop | Parity ledger empty; GraphRAG on survivor; toggles migrated; `/suggestions` exists |
| 2 | Tool registries | typed `registry.py` | `TOOL_SCHEMAS` | Ledger shows zero unmatched; `GROUP_IDS` relocated |
| 4 | Outbound-ness | `registry.Tool.is_outbound` | `templates.OUTBOUND` → derived read | Single writer for the persisted column |
| 5 | Tool authorization | **both semantics** | `_offered_tools` | 16 toggle tests pass unmodified against the survivor |
| 6 | Model gateway | `services/llm.py` | `router._client`; Azure branch | `model_list` versioned; `plan()` ported; telemetry ported |
| 7 | Backend selector | `postgres` | `sqlite` | Default flipped; CI on Postgres |
| 8 | Postgres/SQLite | Postgres | six islands | Islands migrated; `org_id` present |
| 9 | Token stacks | Fernet `provider_tokens` | plaintext `auth/providers/registry` | Not-mounted CI guard green; provider classes triaged |
| 10 | Approvals | `_pg_*` | `_sq_*` | Default postgres; historical rows carry `org_id`+`decided_by` |
| 11 | Event writers | `repo.log_event` shape | SQLite writer | Envelope shape landed; SQLite `events` retired |
| 12 | Delegation | `delegate` tool | `delegation.py` lifecycle → migrate; `main.py` mesh → delete-later | Lifecycle on Postgres; approval propagation invariant tested |
| 13 | Agent rosters | DB `agents` (seed: `templates`) | `agents.SPECIALISTS`, `KNOWN_AGENTS` | `delegate` resolves against DB agents |
| 14 | Memory | Postgres record + Qdrant index | — | Bridge implemented; **one** collection identity chosen |
| 15 | Qdrant/embedders | one accessor each | 5 clients, 2 loaders | All call sites repointed |
| 16 | Eval entrypoints | `backend/evals/` | `tool_calling_eval.py` **after** fold-in | 23 cases folded in; router repointed |
| 17 | Customer tools | registration + allowlist | — (label only) | Every registration carries a non-core pack id; Gate 1 enforcing |

---

*Decision register only. Nothing implemented, nothing deleted, nothing staged or committed.*
