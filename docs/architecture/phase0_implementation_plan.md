# Phase 0 Implementation Plan
### Target Architecture v1.0 — dependency-aware plan to unblock Phases 5–8

| | |
|---|---|
| **Repository** | `/home/matrix/aganetiAi` |
| **Branch / HEAD** | `feat/enterprise-agentic-os` @ `432fd5e` |
| **Inputs** | `target_architecture_v1_gap_analysis.md`, `target_architecture_v1_gap_matrix.json` (268 records), `current_platform_inventory.md` |
| **Method** | 10 parallel read-only design agents + 1 adversarial critique agent, then orchestrator re-verification of every decision-changing claim |
| **Status of this document** | PLAN ONLY. No source, config, database, Docker or package change has been made. Nothing staged, nothing committed. |
| **Companion documents** | `phase0_dependency_graph.md`, `messaging_decision_spike.md`, `consolidation_decisions.md` |

---

## 0. How to read this plan

Phase 0 is **not** "start building the target architecture". Phase 0 is the minimum set of
decisions and foundations without which Phases 5–8 would each independently re-derive the same
boundary and then have to reconcile four different guesses.

Three rules govern every item below:

1. **Nothing is deleted.** Every consolidation resolves to KEEP / DEPRECATE / MIGRATE /
   DELETE-LATER, each with an explicit delete-precondition for a later phase.
2. **No behaviour changes** except where an item is explicitly a security fix.
3. **Every "safe" fix must be proven safe at the chokepoint, not at the route.** Two items in the
   first draft of this plan were closed on paper and open in production. See §2.

### Phase numbering — a flagged assumption

The frozen artifacts number phases 0–3; the programme brief asks for Phases 5–8. No frozen
document defines 5–8. This plan uses the reading the design agents converged on, and **this
assumption needs confirmation** (open question OQ-8):

| Phase | Assumed scope |
|---|---|
| **Phase 5** | Agent planning / runtime — the planner, the consolidated executor, agent lifecycle |
| **Phase 6** | Pack extraction — Dar Al Ber becomes a pack; the two-customer proof |
| **Phase 7** | Plugins & connectors — external capability as a versioned, signed, compatibility-checked unit |
| **Phase 8** | Control plane & packaging — signed artifact, per-tenant configuration, deployment profiles |

If this mapping is wrong, the last four sections of this document are the only part that changes;
the work itself is derived from the gap matrix, not from the numbering.

---

## 1. Corrections to the audit established during planning

These are re-verified by the orchestrator against the working tree. Each one changes the plan.

| # | Audit said | Verified reality | Consequence |
|---|---|---|---|
| C-1 | The LangGraph lane reaches production only through `/dashboard/chat` and `/dashboard/ask` | `/agent/chat` calls `graph.astream_turn` at `routes/agent_os.py:113`, is mounted, streams SSE, and has **zero UI callers** | Runtime consolidation is a *traffic* exercise, not a rewrite. Parity gap is **one endpoint** (`/suggestions`) |
| C-2 | Evaluation artifacts are safe because `reports/` is reproducible | `.gitignore:61-65` justifies the rule as "reproduced byte-identically by their scripts" — **Step 13 disproved this** (62/174 answers differed between identical runs) | The ignore rule rests on an assumption its own workstream falsified |
| C-3 | GraphRAG evidence is at risk from `git clean` | `git ls-files docs` → **0**. `docs/` has *never been tracked*. `git status --porcelain docs` → `?? docs/` | Broader and cheaper to fix than assumed: no ignore surgery needed, only `git add` |
| C-4 | CI Gate 2 is PARTIAL, on the strength of `test_provider_isolation.py` + `test_identity_normalization.py` | **13 of 35 test modules are tracked.** Both of those files are **untracked**, as is `test_tool_reachability.py` | **In a fresh clone, Gate 2 is MISSING, not PARTIAL.** The audit's own evidence base does not survive a clone |
| C-5 | Two personas: "Aria" (`auth/context.py:125`) and "Kannan Kuttan" | `auth/context.py` is **dead** (router never mounted). The live pair is `main.py:2089` (shipped React path) and `templates.py:13` (`/agent/chat`) | Both live personas are on shipped paths; the dead one is a distraction |
| C-6 | Six stale `main.py` copies are gitignored | **Four of six are tracked in git**, alongside 28 other tracked `.bak` files | Quarantine is a `git mv`, not a cleanup |
| C-7 | `docker-compose.yml` has 3 services | It has **4** (`neo4j:31` was missed by a truncated read) | Corrected in the gap analysis §8.1 |
| C-8 | `gpt-4.1` requires an Azure integration to remove the bypass | A read-only probe of `GET :4000/model/info` shows `gpt-4.1` is **already registered in LiteLLM as `azure/gpt-4.1`** at the same endpoint the bypass targets | Removing the Azure bypass is an **alias swap**, not an integration |
| C-9 | Embeddings should move behind the Model Gateway (P1) | The gateway hosts **no embedding model at all**; the frozen 174-case baseline and the 993-point `corporate_memory` collection were produced by bge-small-en-v1.5 @ 384 dims | Embeddings must **not** move. Recorded as a do-not-do |

### Defects found during planning that were in no audit record

| # | Defect | Evidence |
|---|---|---|
| D-1 | The memory bridge calls two functions that **do not exist** — every fact capture and every forget raises `AttributeError` | `chat/memory.py:68,73` call `long_term.store_memory`; `:222` calls `long_term.forget`. `memory/long_term.py` defines neither. The call sites carry `# type: ignore[call-arg]` — the author knew |
| D-2 | `POST /agent/message` defaults the **recipient** to a literal account | `main.py:1889` — `to_user = payload.get("to_user_id", "user_2")`. Invisible to the existing structural guard, which inspects argument defaults only, not `dict.get` defaults |
| D-3 | Board ids are **not** unguessable on the shipped path | `chat/unified.py:265-271 _board_id_for` — docstring reads *"A session id IS the board id."* `_NS` is a hard-coded constant at `chat/store.py:36` |
| D-4 | `/observability/providers` is **not a read** — `deep: bool = Query(True)` forces an OAuth refresh across every stored connection of every user | `routes/observability_explorer.py:552-560`, docstring: *"deep=true exercises a real refresh"* |
| D-5 | Outbound-ness has **three disagreeing sources of truth inside the surviving runtime**; the persisted column is seeded from the wrong one | `registry.Tool.is_outbound` (web_search=True) vs `templates.OUTBOUND` (omits web_search) vs `agent_permissions.is_outbound`, written by `onboarding.py:50` from the wrong source and `repo.py:201` from the right one |
| D-6 | `AGANETI_DATA_BACKEND` is an undeclared bare-getenv switch defaulting to `sqlite`, while production `.env:40` sets `postgres` | Five modules read it; it appears in neither `.env.example` nor `config/settings.py`. **CI exercises a different system of record than production** |
| D-7 | `schedule_meeting` performs a real provider calendar write while `guardrails.decide()` classifies it "approval" and `tools.py:957` only refuses "deny" | The ungated runtime does not merely lack a gate — it **overrides a policy verdict** |

---

## 2. The four programme-level collisions — resolve these before any track starts

The adversarial critique found that the plan was well-grounded on individual facts but failed as a
*programme*. Four collisions sit at the head of two or more critical paths each. Each is a
short decision; none is expensive; all are expensive if skipped.

### COLLIDE-1 — Two incompatible `TenantContext` definitions, `user_id` meaning opposite things

Two tracks each defined the type, in different modules, with **the same field name carrying the
opposite value**:

| | Track TENANCY | Track CONTRACTS-B |
|---|---|---|
| Module | `backend/tenancy/context.py` | `backend/contracts/tenancy.py` |
| `user_id` | the Supabase **sub** | the internal **`users.id` UUID** |
| Tenant id | `tenant_slug` | `tenant_key` |
| Import rule | may import `backend.services` | **may import nothing from `backend.*`** |

The codebase already uses this identifier ambiguously in production: `storage/indexing.py:160`
indexes under `users.id` while `context/providers/corporate.py:36-37` searches under the sub, and
`auth/identity.py:80` returns a *third* naming (`uid` for the internal UUID, `supabase_uid` for
the sub).

**RULING — adopt before anything else.** One owner, one module, one glossary:

| Concept | Canonical name | Backing value |
|---|---|---|
| External IdP subject | `subject_id` | Supabase `sub` = `users.supabase_uid` = the `x-auth-user` header |
| Internal user row | `user_uuid` | `users.id` UUID — what every FK points at |
| Tenant (UUID) | `tenant_id` | `organizations.id` — for joins |
| Tenant (slug) | `tenant_key` | `organizations.slug` — for subjects, collections, prefixes |

- The bare name **`user_id` is forbidden on the contract**. It is precisely the ambiguous
  identifier, and the ban is what stops the ambiguity being re-imported.
- Location: `backend/contracts/tenancy.py` (dependency-free, so its tests run under today's
  broken CI). `backend/__init__.py` does not exist, so `backend` is a namespace package and the
  zero-import rule is achievable — verified.
- The three factory constructors move to `backend/tenancy/factories.py` so the zero-import rule
  survives.

### COLLIDE-2 — Three tracks each build the single caller-identity chokepoint, in three modules

`backend/main.py:817 _authed_user`, `routes/agent_os.py:40 _uid` and `routes/dashboard.py:39 _uid`
are byte-similar (all read only `x-auth-user`, all 401 on absent). SECURITY RC-0 collapses them
into `backend/auth/caller.py`; TENANCY T5 collapses them into `backend/tenancy/deps.py`. Both are
BLOCKING, both claim to be where PolicyInterface later plugs in.

**RULING.** One item, one owner. Build `backend/auth/caller.py::require_caller(request) -> Caller`
once, where `Caller` starts as `{subject_id}` and **gains `tenant_id` / `user_uuid` additively**
the moment `enforce.py:159` stops discarding `resolve_or_provision`'s return.
`require_tenant()` becomes a thin `require_caller(...).tenant` accessor, not a second collapse.

### COLLIDE-3 — `backend/contracts/` defined twice with contradictory policies

Both contract tracks create the package with incompatible rules — two-axis versioning
(integer `schema_version` + instance semver + upgrader chain) vs one package-level SemVer;
pydantic-for-all-six vs pydantic-for-EventEnvelope-only; and two different baseline filenames.

**RULING.** A single **contracts package skeleton** item (base types, exception hierarchy,
version policy, harness, ONE baseline file) precedes both tracks. On the merits the two rulings
are not actually in conflict: adopt the **dependency-free package rule** and the **two-axis
versioning**. The conflict was entirely in who writes `base.py`.

### COLLIDE-4 — Three tracks edit the same 40 lines of `enforce.py`, independently sequenced

`backend/auth/enforce.py` is the most security-sensitive file in the repository and the only one
with a dedicated structural test suite. Four BLOCKING items across two tracks modify it, none
sequenced against the others.

**RULING.** One Phase-0 change, one owner, one review, in this internal order:
1. Strip the client-supplied `x-auth-user` **unconditionally**, before the public-path branch
2. Narrow the public-prefix allowlist
3. Bind the `resolve_or_provision` return at `:159`
4. Inject the org / uid headers
5. Set the tenancy `ContextVar` inside `_forward`

**And a factual correction to the proposed edit.** The current match, verified verbatim at
`enforce.py:103-104`, is:

```python
if method == "OPTIONS" or any(path == p or path.startswith(p + "/") or path.startswith(p)
                              for p in _PUBLIC_PREFIXES):
```

It **already contains** `path == p or path.startswith(p + "/")`. The defect is the **third
disjunct**, `or path.startswith(p)`, which must be **removed**. The security agent described the
change as *"from `path.startswith(p)` to `path == p or path.startswith(p + "/")`"*, which reads as
an addition — implemented literally, the `/healthz-anything` hole survives its own fix. The
acceptance test must assert `/healthz-anything` and `/auth/providerXYZ` are **not** public.

---

## 3. P-0 — Evidence preservation: the ordering constraint ahead of every workstream

This is the first action of Phase 0, before any other track, in any workstream.

**What is at risk, verified:**

| Asset | State | Consequence of loss |
|---|---|---|
| `docs/` (all three audit artifacts, 13 evaluation manifests) | **never tracked** — `git ls-files docs` → 0 | This plan's own inputs |
| `backend/evals/cases/discrimination.json` | untracked | **58 of the 174 golden cases** |
| `tests/` — 22 of 35 modules | untracked, incl. `test_provider_isolation.py`, `test_identity_normalization.py`, `test_tool_reachability.py` | The entire basis for rating Gate 2 PARTIAL, and the 16 tests pinning the tool-toggle contract |
| `reports/evals/` | **gitignored** at `.gitignore:65` | The frozen 0.5867 baseline |
| Working tree | 82 uncommitted files | All of the above is one `git checkout .` from gone |

**The manifests do not reproduce.** The 13 evaluation manifests name `git_commit 432fd5eb…`
(= current HEAD) and dataset hashes `cc9e9f2ddf118785` / `26435e37ec9f4d71` /
`ce0cb2992228971f`. At that commit, `core_retrieval.json` hashes `c5e17816fde39099`,
`enterprise.json` hashes `189475d7ce243b14`, and `discrimination.json` **does not exist**. All
three golden files diverge from the commit their own manifests point at, so the frozen result is
**currently unreproducible from git by construction**.

**P-0 actions, in order:**

1. `.gitattributes` staged **first**, so normalisation rules apply at add time and the dataset
   hashes are verifiable on any platform.
2. A **path-scoped** commit — `docs/`, `backend/evals/cases/`, the untracked `tests/`,
   `backend/tool_result.py`. Path-scoped because the working tree holds 82 files including
   another developer's in-flight work and a staged deletion of `config/users.py` that must not
   be touched.
3. Replace the blanket `reports/` ignore rule. It sits over a **mixed namespace** —
   `reports/email_digest.py` and `reports/pdf_generator.py` are live imports at
   `backend/main.py:361,491` and `backend/documents.py:22`. Ignore the generated subtree, not the
   package.
4. `docs/evaluation/PROVENANCE.md` — repair the manifest↔commit chain by *documenting* it against
   the new commit sha. **Do not edit the frozen manifests.**
5. Snapshot the Neo4j / Qdrant substrate — the half of the evidence base git cannot hold.

**Why this blocks everything:** four ratcheting baselines are specified to live under `docs/`;
the tenancy track's Neo4j change has exactly one acceptance test (a golden-set re-run) and it
is unrunnable until this lands; and CI Gate 2's status is a fiction in a fresh clone until the
tests are tracked.

> **OQ-5 — needs an answer before P-0 executes:** is there a reason `docs/` and the golden set are
> untracked (e.g. customer data in the eval fixtures)? If yes, the ratchets need a different home
> and the Neo4j change needs a different acceptance criterion. Nothing in the design asked.

---

## 4. P-1 — CI repair: without this, every test below is decoration

The CI job is non-functional for **three** independent reasons, not the two the audit found.

1. It installs only `requirements-dev.txt` (pytest, pytest-asyncio, httpx) while **33 of 35 test
   modules import `backend`/`config`**.
2. `sqlalchemy`, `alembic`, `supabase`, `PyJWT`, `cryptography`, `numpy`, `psutil`, `PyMuPDF` and
   `pyodbc` are imported but declared in **no** requirements file.
3. **Newly found:** `import backend.main` pulls `integrations/telegram_bot.py:18-19`, which imports
   `faster_whisper` and `kokoro` at module level — so six test modules transitively require
   **torch (921 MB locally, plus the CUDA wheels)**.

Naively "fixing requirements" produces a ~12-minute multi-gigabyte install on every PR. That is
how gate jobs get switched off.

**Ordered fix:**

| Step | Action | Note |
|---|---|---|
| CI-0.1 | Defer the two heavy imports into their handlers | Behaviour-preserving; `backend/main.py` already uses this pattern at lines 777/975/1084. Must precede CI-0.2 — the dependency closure cannot be computed correctly until this edge is cut |
| CI-0.2 | Pinned `requirements-test.txt` declaring the real closure | Blocks the `collect` and `test` jobs |
| CI-0.3 | Three jobs: `gates` (installs nothing), `collect`, `test` | **The gate runner must not import the application** |
| CI-0.4 | Preservation commit (P-0) lands **before** baseline freeze | A baseline frozen over an untracked tree is meaningless |

**The decisive architectural choice:** gates run on pure-stdlib `ast` / `re` / `git ls-files` over
220 tracked `.py` and 68 tracked `.ts/.tsx` files — **under 30 seconds, zero pip install**. That
is why gates 1/3/7/4a can be trusted and enforced on new code from day one *while the test job is
still being repaired*.

---

## 5. Security remediation — 8 named exposures + the Azure bypass

The eight exposures are **four root causes**. Planning against causes changes both the fix and the
order.

| RC | Root cause | Consequence |
|---|---|---|
| **RC-1** | The public-prefix allowlist is prefix-matched and returns **before** `_forward()` | `/auth/provider/*` runs with no token at all; **no public path ever gets the client-supplied `x-auth-user` stripped** |
| **RC-2** | Identity normalization covers the query-string `user_id` and the JSON body key `user_id` — **not** path parameters, other body keys, or resource ids | Four authenticated routes are open regardless |
| **RC-3** | There is **no authorization decision point**, so "operator-only" is currently inexpressible | The entire 24-endpoint `/observability/*` surface is readable by every authenticated user |
| **RC-4** | Ownership is **not modelled** in the dashboard store | `dashboard_configs` has no owner column; `created_by` holds `'system'`/`'agent'` |

**The decisive planning distinction:** a user-scoped owner predicate exists **today** for
observability, initiatives and agent messages (those tables carry `user_id`); it must be **created
by migration** for `/dashboard/*`; and it does not exist in any form for a tenant. So every fix
here is deliberately **user-scoped** — a strict subset of the eventual `tenant_id AND user_uuid`
rule, and therefore **additive rather than rework**.

### RC-0 — The internal-token bypass makes every predicate below conditional

**This must be stated before the table, because it changes what the table means.**

`enforce.py:118-131` accepts any request presenting `X-Internal-Token == INTERNAL_API_TOKEN` and
sets `effective_user` from the `X-Internal-User` header **verbatim** — no `resolve_or_provision`,
no existence check, no status check, and — unlike the dev-bypass branch at `:112`, which **is**
gated on `_LOOPBACK` — **no loopback restriction whatsoever** (verified). `_forward` then injects
that string as `x-auth-user`.

So a holder of the shared token, from anywhere with network reach, **is any user**, and every
predicate below filters on an attacker-chosen value.

- The tenancy track identified this; the security track — the one actually shipping the
  predicates — did not, and its `caller_uid` docstring asserts the opposite.
- **Fix (Step 1, with the allowlist narrowing — same file, same edit):** add the loopback
  restriction the dev branch already has (free, closes the remote case), and resolve
  `x-internal-user` through `resolve_or_provision`, refusing if it does not resolve to an active
  user.
- **Every item below gains a second test case** authenticated via `X-Internal-Token` + a forged
  `X-Internal-User`.
- Honest docstring: *"the identity the middleware resolved; on the internal-service path this is
  asserted by the caller, not verified."*

### The remediation table

| # | Endpoint | Current behaviour | Risk | Minimal safe fix | Test | Depends on | Blocks Phase 0? |
|---|---|---|---|---|---|---|---|
| S-1 | `/auth/provider/status` | **Unauthenticated** (allowlist prefix hole); reads a caller-supplied `user_id` | Leaks who has which mailbox connected | Remove the third disjunct in the allowlist match; route through `require_caller` | `/healthz-anything` and `/auth/providerXYZ` are NOT public | COLLIDE-4 | **YES** — reachable with **no credential** |
| S-2 | `DELETE /auth/provider/{provider}` | Same allowlist hole | **Unauthenticated disconnect** of any user's provider | Same one-line allowlist fix closes both; then sign the OAuth state | Two-user disconnect test | COLLIDE-4 | **YES** |
| S-3 | `/observability/sessions` | No `Request` param, no identity of any kind. Returns `user_id` + `session_id` for **every user** | Enumerates the entire user population **and** the session ids that are the input to S-4 and to the board-id chain | `WHERE user_id = :uid`; drop `user_id` from GROUP BY and response | Two-user scoping + an **AST structural test** over `observability*.py` | RC-0, RC-3 ruling | **YES** |
| S-4 | `/observability/trace/{session_id}` | No owner predicate | Returns **any user's chat content** | Owner predicate via `require_caller` | Two-user content test | RC-0 | **YES** |
| S-5 | `PATCH /agent/message/{id}/resolve` | No identity at all | Resolve/reject another user's agent inbox | Push the predicate **into the store**, not the route | See caller-inventory warning below | RC-0 | **YES** |
| S-6 | `POST /initiatives/{id}/ack` + `GET /initiatives/{user_id}` | No owner predicate; the GET is worse | Read and acknowledge another user's initiatives | Owner predicate; both in the same change | Two-user test on both | RC-0 | **YES** |
| S-7 | `/dashboard/*` | Authenticated then **discarded**; no owner column exists | Cross-user chart access → **arbitrary SQL against a third party's production database** | See the two corrections below | Chokepoint test, not a route test | RC-0, S-3, migration | **YES** |
| S-8 | Direct Azure OpenAI bypass | `DASHBOARD_LLM=azure` is **live** in `.env:55`; reachable from the **shipped `/agent/chat`** surface via `agent_os.py:371 → unified.py:169,172` | An external provider key inside the app process, invisible to gateway rate-limiting, rotation and cost accounting | **Gate now**; removal is an alias swap once `model_list` is versioned (C-8) | Report-only egress ratchet | MG-0 | Gate **YES**; removal **no** |
| S-9 | `/observability/providers` *(found in planning)* | `deep: bool = Query(True)` — forces a **real OAuth refresh** across every stored connection of every user | A cross-user **write** (token rotation); a better DoS primitive than S-2. Providers that rotate refresh tokens on use make this an availability risk against other users' mail and calendar | Owner predicate **is not sufficient**: flip the default to `False` **and** gate `deep=True` on admin | Assert a non-admin call performs **zero** refresh calls | RC-3 ruling | **YES** |
| S-10 | `POST /agent/message` *(found in planning)* | `main.py:1889` — `to_user = payload.get("to_user_id", "user_2")` | Live fail-open onto a **named account**, in shipped `main.py` | Make `to_user_id` required; 400 when absent | Extend the structural guard to `dict.get` defaults | — | **YES** |
| S-11 | Shared keyless network-reachable Qdrant | Holds another application's data | Cross-application exposure | Contain what this repo controls (one client accessor); **migrate nothing** | Client-accessor test | Consolidation §14 | Containment **YES**; migration **no** |
| S-12 | Six stale `main.py` copies | **Four are tracked in git** (C-6), plus 28 other tracked `.bak` files | Old auth logic resurfacing via copy-paste | `git mv` to a quarantine path; widen the AST guard | See the AST-guard warning below | P-0 | **YES** |

### Three corrections that make "safe" fixes actually safe

**(a) The `/dashboard/*` entitlement gate is false safety.** Gating `POST /dashboard/chat` and
`POST /dashboard/ask` does **not** close the CORE-SHARE exposure: `chat/unified.py:169,172`
imports `stream_dashboard` / `ask_stream` **directly** and never touches a `/dashboard` route
(verified). It is reached from the **shipped `/agent/chat`** surface on any message the regexes at
`unified.py:33-57` classify as `chart` or `data`. A route-level test would pass while the exposure
remained.

> **The chokepoint is not a route.** It is `backend/dashboard/coreshare_db.run_query` — the single
> function every analytics path funnels through, which already hosts `validate_select_only` and
> `validate_no_pii`. **No track owned it.** Add one blocking item: *every call into
> `run_query` carries a caller identity and an entitlement decision*, and make that the acceptance
> point for S-7. Test by monkeypatching `run_query` and asserting zero calls for an unentitled
> caller, driving **both** `/dashboard/chat` and `unified_stream`.

**Availability note:** "an allowlist that denies when unset", applied to a live customer
deployment, is an **outage of the shipped Dar Al Ber dashboard on merge day**. Ship it
deny-by-default in code with the initial allowlist populated **in the same change**, and make an
unset allowlist a loud boot-time warning, not a silent per-request 403.

**(b) The dashboard ownership migration must thread the WRITE first.** The enumerated read
functions omit `config_db.insert_chart` — **the only writer** — called at `dashboard/tools.py:274`
with `created_by="agent"` (verified). Combined with the rule *"NULL owner → admin-only"*, every
chart the agent creates in direct response to a user's own request becomes invisible to that user
**the instant it is saved**. The user asks for a chart, the agent reports success, the board
renders nothing.

> Correct order inside S-7: **(b0)** add the columns → **(b1)** thread the **write** → **(b2)**
> thread the reads → **(b3)** only then enforce NULL-is-admin-only. The identity needs no
> plumbing: `dashboard/stream.py:95` already puts `user_id` into the `ctx` dict handed to
> `_save_chart`. Test that A **can** see A's chart — the proposed tests only assert B **cannot**,
> which passes trivially when nobody can see anything.

**(c) Board ids are not secret, so S-3 → S-7 is one exposure chain.** `_board_id_for` returns
`uuid.UUID(session_id).hex`, docstring *"A session id IS the board id"*, with a hard-coded
namespace fallback. So: `GET /observability/sessions` (no identity) publishes every user's
`session_id` → `_board_id_for` converts it with no secret → `GET /dashboard/charts?board_id=…`
returns the victim's charts including raw SQL → `_render` **executes each one against the
customer's Azure database**. Add the edge **S-3 → S-7** explicitly, and drop the "board ids are the
access control" framing that made S-7 feel deferrable.

### Two warnings on the proposed fixes themselves

**The agent-inbox signature change breaks the shipped Telegram flow.** The caller inventory was
incomplete. Verified callers of `resolve_message` / `reject_message`:
`integrations/telegram_bot.py:959` (delegation **Accept** button) and `:1005`
(**Reject** button, calling with a **keyword** argument), plus `scripts/verify_task16.py:47,57,61,94`.
Appending a positional parameter is a **runtime `TypeError` on button press**, not an import error.

> Make `actor_agent` **keyword-only with no default**, so the keyword call site fails at
> signature inspection. And note the Telegram Accept path contains the **same IDOR** —
> `telegram_bot.py:936-940` runs `SELECT * FROM agent_messages WHERE id=?` with no `to_agent`
> predicate, then creates the task in the caller's own store. A store-level test **cannot see
> this**, because the handler reads the row itself.

**The widened AST guard is under-specified.** The existing test at
`tests/test_identity_normalization.py:151` is **not** a plain literal walk — it strips docstrings
first, precisely because the module's own prose describes the bug it fixed. A widened test written
without that stripping produces **14 hits, 11 of them prose** — i.e. the very comments documenting
the closed bug — and is unlandable. Done correctly (docstrings stripped, exact match), the true set
across `backend/**` is exactly **5**: `main.py:1889`, `observability_explorer.py:193`,
`observability.py:514`, `observability.py:846`, `evals/dataset.py:49`.

> Scoping the walk to `backend/routes/**` misses two of the five — **including `main.py:1889`,
> which is a live defect (D-2)**. Specify: reuse the docstring-stripping helper, walk
> `backend/**/*.py`, exact-match, land green at count=5 as the ratchet baseline, and extend the
> pattern to `dict.get` defaults.

---

## 6. TenantContext and tenant_id propagation

### The finding that reframes the work

Tenancy is not missing. **It is resolved once per request and thrown away.**

- `backend/auth/identity.py:80` already builds `{uid, org_id, supabase_uid, email}` from a
  cryptographically verified Supabase `sub`.
- `backend/auth/enforce.py:159` **discards the return value.**
- `backend/services/user_directory.py:59` independently carries `org_id` for every active user in
  a sync-readable snapshot.
- `backend/storage/indexing.py:94` already selects `org_id` per work item.

Three of the four tenant construction points a real design needs **already exist and are unused**.
The work is therefore *"stop discarding it, bind it to a request-scoped ContextVar, and turn on
filters surface by surface behind a three-state ratchet"* — not *"add tenancy"*.

### TenantContext v1 — frozen, seven fields, zero authorization semantics

Field names per the COLLIDE-1 glossary. Deliberately **not** a revival of
`backend/auth/context.py:19 UserContext`, whose `available_tools()` capability ladder and embedded
persona are exactly what makes a context object un-reusable — it is dead code and is **DEPRECATE,
not extend**.

**The canonical contract field is `tenant_id`; the SQL column stays `org_id` on ~25 tables.**
Renaming 25 columns buys nothing and is DELETE-LATER. One adapter at the repository boundary
reconciles the names and satisfies CI Gate 8.

### The 11 propagation surfaces

| # | Surface | Today | Phase 0 action |
|---|---|---|---|
| 1 | HTTP request | `enforce.py:159` discards org | Bind ident; inject `x-auth-org` + `x-auth-uid`; set ContextVar in `_forward` (COLLIDE-4) |
| 2 | PostgreSQL | `org_id` columns exist, **never read as a filter** | Three-state ratchet → NOT NULL backfill → filter **by table family** |
| 3 | Qdrant | `corporate_memory`, 993 points, org unset | **Order is load-bearing** — see the trap below |
| 4 | Neo4j | No tenant predicate | Write-side property + index + backfill **now**; read-side predicate **not** in Phase 0 |
| 5 | Object storage | One org-aware ACL | Make it fail **closed**; record the bucket decision |
| 6 | Background jobs / scheduler | No tenant | Two rules, one exception, and one job that can satisfy neither |
| 7 | Tool execution | Registry `ctx` dict | Extend the `ctx` dict; **do not** thread tenant through `tools.py` |
| 8 | Model gateway | No tenant | `ModelProfile.tenant_id` — same identifier, same type as the contract |
| 9 | Events / audit | Postgres branch writes org; **SQLite branch cannot** | The SQLite `events` schema **blocks** the EventEnvelope contract — fix the DDL early |
| 10 | Logs / metrics / traces | `x-trace-id` stamped at `main.py:906`, never joined | One logging filter reading the ContextVar; ships **with** surface 1 |
| 11 | Connectors | No tenant on audit or rate-limit key | Tenant on the audit record and the rate-limit key; per-tenant datasources are a **pack** concern |

### The two ordering traps that are genuinely blocking

1. **A Qdrant tenant filter turned on before the payload backfill silently empties
   `corporate_memory`.** All 993 points are org-unset. The filter returns zero results, and
   nothing errors.
2. **`_ingest_cycle` keeps manufacturing NULL-tenant points.** `main.py:700 → ingest.py:294
   ingest_file(client, path)` defaults to `owner=__org__, org_id=None`. It must be fixed
   **before** the filter, not after — otherwise the backfill is immediately re-polluted.

### Minimum migration sequence

```
T1 TenantContext type (pure, no imports)
     ↓
T2 enforce.py: stop discarding org, inject headers   ← behaviour-neutral: no handler reads them yet
     ↓
T3 Bind ContextVar + tenant_scope + 5 propagation paths
     ↓
T4 Fix the tenant BOUNDARY  ← MUST precede every backfill
     ↓
T5 require_caller chokepoint (= COLLIDE-2)
     ↓
T6 Postgres ratchet → backfill → filter by family
```

`T4` may run in parallel with `T3`. `T7` (close the live cross-tenant routes) needs only `T5` and
can be done by a separate pair. `T10` (Neo4j) write-side can run early and in parallel.

### Supabase RLS is emphatically not a backstop

It protects the **wrong database**, keys on `auth.uid()` with **zero org dimension**, and
`provider_connections.sql:36-42` grants `service_role` `using(true) with check(true)` — against the
`SUPABASE_SERVICE_KEY` client at `auth/supabase_client.py:37`. App-Postgres RLS is **not Phase 0**:
seven `org_id` columns are nullable and must be backfilled to NOT NULL first. Phase 0 ships the
**non-guarantee documentation** and an assertion test, nothing more.

### Blocking decision that was left as an open question

**`require_caller` must be user-REQUIRED, tenant-OPTIONAL.** The internal-token and dev-bypass
paths never call `resolve_or_provision`, so the org headers are injected **empty by design**. If
"absent" includes an absent tenant, **every internal self-call 401s** the moment the migration
reaches it — `action_parser.py:46,55,71,85` (`/tasks`, `/draft_email`, `/schedule_meeting`),
`tools.py:1139` (`/set_reminder`), `registry.py:210,217` (**the two outbound tools**),
`main.py:482`, and ten sites in `telegram_bot.py`.

> Merging "no identity" and "no tenant" into one 401 takes down task creation, email drafting,
> meeting scheduling, reminders and the whole Telegram surface at once — with an error that looks
> like an auth bug rather than a design decision. Raise 401 on a missing **subject**; return
> `tenant_id=None` on the internal path; any handler that needs a tenant asks explicitly and gets a
> **403 with a distinct reason**.

---

## 7. The 12 contracts

**Package:** `backend/contracts/`, importing **nothing** from `backend.*` or `config.settings`.
This is deliberate and load-bearing: conformance tests become the **first tests that can gate a PR**
on `pip install pytest pydantic` alone, while the main test job is still being repaired.

**Versioning (one policy, all twelve — COLLIDE-3):** two axes.
- `schema_version` — integer, **platform-owned**, governs load/refuse.
- `version` — semver, **manifest-author-owned**, describes the instance.
- Additive-only within a major; two majors may coexist; a registered dict→dict upgrader chain.

**The real Phase 0 value is not the types. It is the six conformance tests that turn today's
silent divergences into counted numbers.**

| # | Contract | Status | Backing implementation today | Callers to migrate | Depends on | Phase 0 deliverable |
|---|---|---|---|---|---|---|
| 10 | **TenantContext** | MISSING | `UserContext` (dead), user-scoped only | All 11 surfaces | — | **Type + glossary. FIRST, and alone** |
| 11 | **EntitlementLicense** | PARTIAL | `plan` string + feature list | `available_tools()` | 10 | Shape + **frozen dotted key namespace** + permissive resolver |
| 12 | **EventEnvelope** | MISSING | `events(id, ts, user_id, kind, …)` — a log table, not an envelope | ~20 `log_event` sites | 10 | All 9 fields; `tenant_id` **non-optional**, two reserved sentinels |
| 5 | **PluginManifest** | MISSING | Import-side-effect dict at `registry.py:48` | 4 `register_*_tools()` | — | **FIRST of the six manifests** — Agent + Pack both need it |
| 3 | **PromptTemplate** | PARTIAL | Module constants; **two live personas** (`main.py:2089`, `templates.py:13`) | 2 persona sites | — | `persona_ref` + a **count-of-2 baseline**. Parallel with #5 |
| 1 | **AgentManifest** | MISSING | **Two divergent `SPECIALISTS` dicts** — `agents.py:17` vs `templates.py:57`, disagreeing on whether `email_agent` may call outbound `send_email` | `delegate` tool, DB seed | 5, 3 | Type + the divergence count |
| 2 | **WorkflowManifest** | MISSING | Code-defined LangGraph + a regex lane router | `unified.py:53-57` | 1 | **v1 is DESCRIPTIVE** — the route table is the point |
| 4 | **PackManifest** | MISSING | None. Customer behaviour compiled into core | 4 dashboard modules | 1,2,3,5 | **LAST**; inventory compiled in parallel from day 1 |
| 6 | **ConfigSchema** | PARTIAL | `config/settings.py` — **71 `os.getenv` calls, zero validation**; 74 undeclared reads across 30 files | 43 importers | **none** | **PARALLEL from day 1.** Scope to the `tenant`-scoped key list (~15 keys), not all 70 |
| 7 | **ModelProviderInterface** | PARTIAL | `services/llm.py` **and** `orchestrator/router.py` | See §8 | 10 | Interface + `egress` field so Gate 3 has something to assert |
| 8 | **ConnectorInterface** | MISSING | Free-form modules on a shared HTTP client | 6 services | 10, 9 | Built on the **live encrypted token stack**, explicitly **not** the dead one |
| 9 | **PolicyInterface** | MISSING | `guardrails.py` **and** `Tool.is_outbound` + `agent_permissions` — unreconciled | All governed actions | 10, 12 | **Highest value.** Ships in **SHADOW MODE** |

### PolicyInterface — the design that makes it safe to ship in Phase 0

A three-way exhaustive `Effect` plus a composite engine taking the **most-restrictive** answer
resolves the `web_search` divergence (approval-gated at `skills.py:409`, auto-executed via
`guardrails.ACTION_CATEGORY`) **without editing either source**.

> It ships in **shadow mode**: it logs legacy-vs-composite divergence as a ratcheting baseline
> while **enforcing the legacy answer**. Nothing changes behaviour in Phase 0. The baseline JSON
> must be generated and committed **in the same change** that introduces the shadow call, or the
> first CI run has nothing to compare against.

### EventEnvelope — designed so the broker decision changes exactly one function

`event_type` (`<domain>.<entity>.<action>`, three lowercase tokens) is canonical and identical
across every transport. `subject_for(event_type, tenant_key, env)` is the **single** place any
transport address is composed:

- **NATS** — tenant is a subject token, so broker-level authz can enforce isolation
- **Redis** — tenant is deliberately **not** in the key (unbounded cardinality defeats consumer groups)
- **Postgres** — the existing indexed `events.kind` + `org_id` columns

`tenant_id` is **required and non-Optional**, with two reserved sentinels. An Optional tenant means
every consumer writes `if e.tenant_id` forever.

**Six of nine fields are new, but five are nullable metadata.** The two that matter — `tenant_id`
and `trace_id` — **both already exist elsewhere in the process** and merely need carrying:
`org_id` is already resolved and written by the Postgres branch, and `x-trace-id` is already
stamped by `main.py:906` and never joined.

### Scope demoted to NON-BLOCKING

Four items were BLOCKING without unblocking anything: PromptTemplate's `composes` graph / `safety`
sub-object / render-purity rule; PackManifest's `integrity`/`signature`/`sbom` (no signing
infrastructure exists); ConfigSchema's `test_defaults_match_current_values` across all ~70 keys
(the blocker is the ~15 tenant-scoped keys); and EventEnvelope's `partition_key` / `subject` /
64 KiB cap, all justified by broker properties the spike has not chosen.

---

## 8. Consolidations — summary

Full register with delete-preconditions in **`consolidation_decisions.md`**. The governing rule:

> **The survivor is the twin that already carries a governance primitive or a tenancy seam**,
> because those are the two things that cannot be retrofitted cheaply.

| Duplicate | Decision |
|---|---|
| Agent runtime | **KEEP** LangGraph / **DEPRECATE** NATIVE_TOOLS — but **do not cut `/api/chat` over in Phase 0** |
| Tool registries | **KEEP** typed `orchestrator/registry.py` / **DEPRECATE** `TOOL_SCHEMAS`; publish a name-by-name parity ledger |
| Model gateway | **KEEP** `services/llm.py` transport / **MIGRATE** `router.py`'s plan+fallback+cost onto it |
| Token stacks | **KEEP** Fernet `provider_tokens.py` / **DEPRECATE** the plaintext one |
| SQLite / Postgres | **KEEP** Postgres everywhere; the cutover is ~60% done |

**The hard call — the shipped React UI streams the ungated runtime — resolves to KEEP LangGraph,
but the traffic cutover is *not* Phase 0 work.** Two facts the audit did not connect:

1. The frozen GraphRAG pipeline's **only** production wiring is `build_ranked_context` at
   `main.py:2382`, which lives **exclusively on the losing runtime**.
2. **19 of the 24** tools that exist only on the losing side are the media/consumer surface
   **currently being written by another developer in the uncommitted tree**.

Phase 0's job is to **freeze the boundary and publish a parity ledger**, not to move traffic.

---

## 9. Model Gateway

**Verdict: `services/llm.py` is the correct survivor — but it is not the survivor the audit
describes.** It is missing a capability the **shipped** UI already needs.

The shipped React chat (`useStream.ts:42 → /api/chat → main.py:2266`) already uses `llm.py`, but
through **raw transport primitives** with a hand-rolled streaming tool-call SSE parser at
`main.py:1412-1496`. So `llm.py` has **no streaming-with-tools API**, and any "consolidate onto
llm.py" plan that ignores this will either fork `main.py` again or regress the shipped path.

| Direction | Content |
|---|---|
| **Port IN** | The `MODELS` registry (as data), `plan()` capability/tier/fallback resolution, `_normalise()`, and `_log()` token/latency/cost — **currently the only token/cost telemetry in the system** |
| **Port NOTHING out** | `llm.py`'s typed errors and `strip_think`/`ThinkFilter` are strictly better than router's `extra_body` flag, which `llm.py:16-18` explicitly documents cannot be trusted alone |
| **Do NOT port** | `dashboard_model()` and the `AsyncAzureOpenAI` branch — those are **deleted, not moved** |

**MG-0 first, and it is read-only.** A probe of the live gateway collapses the domain's worst P0:
`gpt-4.1` is **already registered in LiteLLM as `azure/gpt-4.1`** at the exact endpoint the
in-process bypass targets (C-8). The same probe confirms `qwen-vl`/`qwen-extract` exist, reveals
two aliases the repo does not know about (`qwen-test`, `qwen-coder`), and shows LiteLLM holds
**no** capability metadata for any local model — so `caps` must stay a **repo-owned declaration**
inside the model profile rather than be delegated to the gateway.

**And it overturns the embeddings P1 (C-9):** there is **no embedding model on the gateway at
all**. Embeddings cannot traverse it today and **must not be moved** — the frozen 174-case baseline
and the 993-point collection were produced by bge-small-en-v1.5 at 384 dims. Recorded as a
do-not-do in the same ADR.

---

## 10. Messaging

Full spike in **`messaging_decision_spike.md`**. Two separable answers, deliberately:

- **ARCHITECTURAL RECOMMENDATION: (D) PostgreSQL-backed queue/event architecture.**
- **IMPLEMENTATION RECOMMENDATION FOR PHASE 0: build no queue and no broker at all** — only the
  broker-agnostic contracts that make the transport a swappable detail.

The reason to separate them: **the transport is reversible; the contracts are not.** Swapping
`PostgresBus` for `NatsBus` later touches one adapter module. Retrofitting `tenant_id` into an
envelope after four tracks have written producers touches every producer, consumer, subject and
test.

**No broker is installed. No dependency is added. The project's own written target (Arq + Redis)
is explicitly superseded by an ADR rather than quietly ignored.**

---

## 11. GraphRAG — preservation, not re-tuning

**GraphRAG is CLOSED.** No re-tuning, no parameter changes, no dataset edits. The Phase 0 work is
entirely preservation and guarding — see §3 for the ordering constraint.

| Item | Action |
|---|---|
| G-1 | `.gitattributes` so dataset hashes are verifiable on any platform |
| G-2 | Replace the blanket `reports/` ignore — it covers **live imports** (`email_digest.py`, `pdf_generator.py`) |
| G-3 | `PROVENANCE.md` repairing the manifest↔commit chain **without editing frozen evidence** |
| G-4 | One frozen baseline of record; retire the two stale ones by **`git mv`, never delete** |
| G-5 | Make baseline comparison **refuse incommensurable runs** instead of reporting a fake regression |
| G-6 | `test_graphrag_frozen_config.py` — the six frozen parameters protected by test, not prose |
| G-7 | `test_golden_dataset_integrity.py` — hashes, counts, glob-drift lock |
| G-8 | Snapshot Neo4j/Qdrant — the half git cannot hold |
| G-9 | A do-not-touch register, **including a scorer defect that must NOT be fixed** |

**The stale baseline is a live wrong signal, not a documentation problem.**
`reports/evals/baseline.json` (0.6985) is read by `run_evals.py:37`, where 11.18pp against a 0.02
tolerance **guarantees `exit 1`**, and by the **mounted** `observability_explorer.py:772-791`,
which publishes fabricated per-family regressions today.

**The controlling rule for all later tenancy work:** `knowledge_graph/queries.py:193` MERGEs on
`(:Entity {id})` **alone**. Org scoping must be an **additive property plus a query-time filter**,
never a change to that identity key — changing entity ids invalidates every
`expected_graph_nodes` value in all 174 cases and reopens a closed workstream.

**One honest caveat:** "write-side-only is provably read-identical" is slightly stronger than the
evidence supports. Adding `org_id` to `SECONDARY_INDEXES` changes the planner's available indexes;
on a graph this size that will not change results, but it can change **ordering among
equal-scored candidates**, and fusion is order-sensitive at `graph_top_k 8`. The claim should be
**"result-set-identical, verified by re-running the golden set"** — which is exactly the test that
cannot run until P-0 lands.

---

## 12. Customer decoupling map

Customer coupling is **not one package**. It is five kinds of coupling across eleven directories,
and extracting `backend/dashboard/` de-customerizes nothing on its own.

**The central design decision: most coupled *files* split rather than move.**

| Item | CORE | PACK | PLUGIN | TENANT CONFIG | MODEL PROFILE |
|---|---|---|---|---|---|
| `coreshare_db.py` | SQL-safety + cache machinery | PII list | the engine | — | — |
| `metric_contract.py` | `no_frozen_numbers()` | the contract | — | — | — |
| `stream.py` / `ask.py` | agent runners, board protocol, **verification ledger** | the prompts | — | — | — |
| `metrics.py` | whitelist enforcement, dialect fragments | the catalog | — | — | — |
| `unified.py:33-82` | the intent router | `_DOMAIN` vocabulary | — | — | — |
| `templates.py` PRIMARY_PROMPT | tool-and-approval protocol | the persona name | — | — | — |
| `DEFAULT_ORG_SLUG` | — | — | — | **fail-closed provisioning** | — |
| `ALLOWED_EMAIL_DOMAINS` | deny-all default | — | — | **the domain list** | — |
| `DASHBOARD_LLM` / Azure host | — | — | — | ✔ | ✔ |
| React persona | `aria_` localStorage prefix (KEEP) | — | — | delivered at **runtime** | — |

> **Moving those files wholesale is the single most likely Phase 0 mistake**, because it drags core
> safety properties — the SELECT-only validator, the evidence-verification ledger, the
> outbound-approval sentence, the conservative route fallthrough — across the pack boundary, where
> a pack author can remove them.

**Three decisions that must be made in Phase 0 even though no code moves:**

1. **The connector result cache is keyed by SQL text alone.** Extraction without re-keying it by
   tenant creates a **live cross-tenant read**.
2. **`DEFAULT_ORG_SLUG` must fail closed.** An empty-slug fallback **merges tenants** — a worse
   outcome than the customer name it was flagged for.
3. **The 174-case golden dataset freezes WITH its customer nouns.** `discrimination.json:252`
   records that *"DB → Dar Al Ber Society at 0.8000"* is the deliberate counterweight justifying
   the frozen 0.82 resolver threshold. **Renaming those entities silently retunes a closed
   workstream.** Gate 1's scope must exclude the eval fixtures and the two deliberately
   customer-asserting unit tests **by construction, not by editing them.**

**The acceptance test must use two *invented* fixture packs**, asserting on the captured system
prompt and the built `frontend/dist` bundle. Anything that proves the point by renaming a real
customer proves only that a rename happened.

---

## 13. CI gates — order and ratchet

Gates run **without importing the application** (§4), so they can enforce on new code from week 1.

| Order | Gate | Lands | Enforcement |
|---|---|---|---|
| 1 | **G1** No customer names/logic in core | wk 1 | Report-only → ratchet wk 2 → full enforce only when the pack zone exists (Phase 6) — until then there is **nowhere for the names to legitimately move to** |
| 2 | **G3** No direct model-provider calls | wk 1 | Report-only 1 wk → ratchet. **Closed violating set**, so it can ratchet immediately |
| 3 | **G7** No hard-coded customer workflows | wk 2 | Second rule-set inside the G1 module |
| 4 | **G4a** Duplicate tool name across registries | wk 2 | The only part of Gate 4 that is real today |
| 5 | **G2** Cross-tenant isolation | wk 2–3 | 2a enforcing immediately (trivially green); 2d report-only; 2c after TenantContext |
| 6 | **G8** tenant_id propagation | wk of TenantContext | **Hard dependency on contract #10.** Inverted ratchet on coverage |
| 7 | **G4** full | Phase 6+ | Needs PluginManifest — **do not stub** |
| 8 | **G6** Pack/schema compatibility | split | Schema-diff half at Phase 0 exit; pack half at Phase 6 |
| 9 | **G5** Signed artifact | scaffold only | Blocked on infrastructure, not code |

**Gates 4, 5 and 6 cannot be written yet** (no PluginManifest, no PackManifest, no build, no
`VERSION`, no `Dockerfile`). **Writing vacuous versions is worse than their absence** — it
manufactures false assurance. Each gets a precursor rule that is real today instead.

### Ratchet design — two independent mechanisms

1. **Per-gate integer budget** that may only decrease → blocks net growth.
2. **Record-identity novelty** keyed on `(gate, path, symbol, rule, evidence)` — **not line
   numbers** → blocks like-for-like substitution and survives reformatting.

Waivers are CODEOWNERS-gated with a **mandatory 90-day expiry**, enforced by a scheduled job that
breaks `main`, not just the PR.

**ONE baseline file** with namespaced metric ids — not four across four tracks (COLLIDE-3), and
not under `docs/` until P-0 lands, or every ratchet is a test reading a file that does not exist in
CI.

---

## 14. Open questions requiring a decision

| # | Question | Blocks |
|---|---|---|
| **OQ-1** | Who owns `backend/contracts/`? | COLLIDE-3 — both contract tracks' step 0 |
| **OQ-2** | Confirm the identifier glossary; ratify banning bare `user_id` | COLLIDE-1 — every contract embedding a tenant |
| **OQ-3** | Confirm `require_caller` is user-required / tenant-optional | T5 and the entire internal self-call fleet |
| **OQ-4** | Does the deployed observability panel request `deep=true` by default? Flipping it is a **product** decision | S-9 |
| **OQ-5** | May `docs/` and the evaluation artifacts be committed? Any reason they are untracked? | **P-0, and therefore everything** |
| **OQ-6** | `DASHBOARD_LLM=azure` is live — is the Azure path a product requirement or a leftover? | S-8 removal (not the gate) |
| **OQ-7** | Initial `/dashboard/*` entitlement allowlist membership — required in the same change to avoid an outage | S-7 |
| **OQ-8** | Confirm the Phase 5–8 mapping in §0 | Only the closing sections of this document |

---
---

# PHASE 0 BLOCKERS

Work that **cannot start** until these are resolved, in this order.

**B-0 — Programme rulings (hours, not days; four decisions at the head of six critical paths)**
1. **COLLIDE-1** — one `TenantContext` owner, one module, one identifier glossary; bare `user_id`
   banned on the contract.
2. **COLLIDE-2** — one caller-identity chokepoint (`require_caller`), not three.
3. **COLLIDE-3** — one `backend/contracts/` skeleton: one `base.py`, one versioning policy, one
   type-system rule, **one** baseline file.
4. **COLLIDE-4** — `enforce.py` is a single change with one owner, in the five-step internal order,
   **removing** the third disjunct of the allowlist match.

**B-1 — P-0 evidence preservation** *(blocked on OQ-5)*
`.gitattributes` → path-scoped commit of `docs/`, `backend/evals/cases/`, the 22 untracked test
modules, `backend/tool_result.py` → replace the blanket `reports/` rule → `PROVENANCE.md` →
Neo4j/Qdrant snapshot. **Nothing else in Phase 0 may be claimed complete before this**: four ratchet
baselines, the Neo4j acceptance test, and CI Gate 2's PARTIAL rating all depend on it.

**B-2 — CI repair (CI-0.1 → CI-0.4)**
Break the torch import edge → pinned `requirements-test.txt` → three jobs with a dependency-free
`gates` job → preservation commit before baseline freeze. **Without this every test in this plan is
decoration.**

**B-3 — The RC-0 internal-token bypass**
Loopback restriction + resolve-and-verify `x-internal-user`. **Every owner predicate in §5 is
conditional on this**, and the internal path currently has no loopback restriction at all.

**B-4 — Answers to OQ-1 … OQ-3, OQ-5, OQ-7**
OQ-5 blocks B-1. OQ-7 blocks S-7 without an outage. OQ-1–3 block both contract tracks and T5.

---

# PHASE 0 SEQUENTIAL WORK

Strict order; each step's output is the next step's input.

**Chain A — Identity and tenancy**
```
B-0 rulings
  → TenantContext v1 type (pure, importless)
  → enforce.py single change  [strip → narrow → bind → inject → ContextVar]
  → require_caller chokepoint  (user-required, tenant-OPTIONAL)
  → tenant BOUNDARY fix        ← MUST precede every backfill
  → Postgres three-state ratchet → NOT NULL backfill → filter by table family
```

**Chain B — Security predicates** *(after RC-0; S-1/S-2 first — they need **no credential**)*
```
allowlist narrowing + loopback restriction (S-1, S-2)
  → RC-3 operator/user classification ruling
  → owner predicates (S-3, S-4, S-5, S-6, S-9, S-10)
  → S-3 → S-7 edge  (board ids are derivable from session ids)
  → S-7: columns → WRITE → reads → enforce   ← this order or the product breaks
  → coreshare_db.run_query chokepoint = the acceptance point for S-7
```

**Chain C — Contracts**
```
contracts skeleton (COLLIDE-3)
  → TenantContext (alone)
  → EventEnvelope + subject convention  ∥  EntitlementLicense key namespace
  → PolicyInterface → composite engine in SHADOW MODE + divergence baseline
  → PluginManifest → PromptTemplate → AgentManifest → WorkflowManifest → PackManifest
```

**Chain D — Model gateway**
```
MG-0 capture live LiteLLM model_list  (read-only)
  → survivor ADR + port table
  → ModelProviderInterface v1
  → ModelProfile  (kills DASHBOARD_LLM)
  → Azure bypass = alias swap
```

**Chain E — Evidence** — `P-0 → G1 .gitattributes → G2 ignore rewrite → G4 baseline of record → G5 refuse-incommensurable`

---

# PHASE 0 PARALLEL WORK

Independent tracks, safe to run concurrently by separate owners once B-0 lands.

| Track | Work | Depends only on |
|---|---|---|
| **P-a** | **ConfigSchema** — the only contract with **no** contract dependency. Its `scope="tenant"` key list is an **input** to the tenancy track, so finishing early unblocks someone else | B-0 |
| **P-b** | **Gates 1, 3, 7, 4a** — pure-stdlib, install nothing, sub-30s. Enforce on new code while the test job is still broken | B-2 |
| **P-c** | **MG-0 + MG-7 characterisation tests** — read-only probe; tests written **before** any port | — |
| **P-d** | **Messaging contracts** — EventEnvelope, subjects, idempotency, ordering, two Protocols, in-process adapters, ADR-0001. **Zero new dependencies** | TenantContext type |
| **P-e** | **Neo4j write-side** — property + index + backfill. Read-side predicate explicitly **not** Phase 0 | B-1 (for its acceptance test) |
| **P-f** | **Qdrant/embedder accessor consolidation** — 6 clients → 1, 3 embedders → 1. Cheapest item in the programme, and it hides a real bug: `main.py:1298` **hard-codes** the model and ignores `EMBED_MODEL_NAME` | — |
| **P-g** | **Decoupling taxonomy + split lines** — decided without moving a byte | B-0 |
| **P-h** | **`AGANETI_DATA_BACKEND` freeze** — declare it typed, add to `.env.example`, startup assertion. **Do not flip the default.** Precondition for the credibility of almost every test column in this plan | — |
| **P-i** | **Consolidation decision register** — a document, not code | B-0 |
| **P-j** | **Outbound-ness single source of truth** — collapse three disagreeing sources **before** PolicyInterface consumes the wrong one | — |

---

# PHASE 0 EXIT CRITERIA

Phase 0 is complete when **all** of the following are objectively true.

**Evidence & CI**
1. `git ls-files docs | wc -l` > 0; all 35 test modules and all three golden case files are tracked; `.gitattributes` governs them.
2. `PROVENANCE.md` reconciles the 13 manifests to a real commit; the golden set re-runs from a clean clone.
3. CI collects and runs the full suite; `requirements-test.txt` is pinned; the `gates` job installs nothing and completes in <60s.
4. **One** ratchet baseline file exists, is tracked, and every gate reads it; `test_baseline_file_parses` does **not** skip when the file is missing.

**Security**
5. All 12 items in §5 are closed **or** explicitly accepted in writing, each with a two-user test **and** an internal-token variant.
6. The internal-token path is loopback-restricted and resolves `x-internal-user` to an active user.
7. `/healthz-anything` and `/auth/providerXYZ` are proven **not** public.
8. No unentitled caller reaches `coreshare_db.run_query`, proven by driving **both** `/dashboard/chat` **and** `unified_stream`.
9. The corrected AST guard walks `backend/**`, strips docstrings, covers `dict.get` defaults, and is green at its baseline count.
10. The shipped Telegram Accept/Reject flow still works, proven by test.

**Contracts & tenancy**
11. All 12 contracts exist as types in a dependency-free `backend/contracts/`, with one versioning policy, and their conformance tests run on `pytest + pydantic` alone.
12. The identifier glossary is checked in and enforced by test; no contract field is named `user_id`.
13. `TenantContext` is constructed on every request; `enforce.py` no longer discards `org_id`.
14. Gate 8 exists and passes report-only across all 11 surfaces; Gate 2's tenant half is written.
15. PolicyInterface runs in shadow mode with a committed divergence baseline; **no behaviour has changed.**
16. `EventEnvelope` v1 is the shape written to `events`, with `tenant_id` non-optional. **No broker is installed.**

**Consolidation & decoupling**
17. Every duplicate in `consolidation_decisions.md` carries a KEEP/DEPRECATE/MIGRATE/DELETE-LATER verdict **and an explicit delete-precondition**. **Nothing has been deleted.**
18. The runtime parity ledger is published; `/api/chat` traffic has **not** moved.
19. Outbound-ness has exactly one source of truth.
20. Every tool registration carries a `pack` label; the seven-way decoupling taxonomy is ratified with per-item destinations.
21. `AGANETI_DATA_BACKEND` is a declared, validated setting; CI and production exercise the same system of record.

**GraphRAG**
22. The six frozen parameters are protected by test; dataset integrity is asserted by hash; baseline comparison refuses incommensurable runs; the substrate is snapshotted. **Nothing has been re-tuned.**

---

# PHASE 5 UNBLOCKED BY

*(Agent planning / runtime — assumption per §0)*

- **AgentManifest + WorkflowManifest + PromptTemplate** — a planner cannot plan over agents that
  exist only as two divergent hardcoded Python dicts that disagree on whether `email_agent` may
  send mail.
- **The runtime consolidation decision and its parity ledger** — a planner must target one
  executor. The decision (KEEP LangGraph) plus the one-endpoint parity gap is what makes Phase 5
  a traffic exercise instead of a rewrite (C-1).
- **One source of truth for outbound-ness** (D-5) — a planner reasoning about which steps need
  approval must not consume a persisted column seeded from the wrong source.
- **PolicyInterface in shadow mode with a divergence baseline** — the planner needs one place to
  ask "may this step run", and the shadow baseline is what proves the composite answer matches
  today's before it is enforced.
- **EventEnvelope with non-optional `tenant_id`** — plan/step/observation events are the planner's
  own audit trail; retrofitting a tenant into them later touches every producer.
- **The `MessageBus` / `WorkQueue` Protocols** — so multi-step plans can later be executed
  out-of-process without rewriting the planner.
- **The approval-gate governance hole closed as an invariant** — a delegated outbound action
  currently returns `awaiting_approval` as a **formatted string** to the parent and is never
  persisted, so it is neither executed nor approvable. Unreachable today only because no
  specialist roster grants an outbound tool. A planner that delegates makes it reachable.
- **P-0** — the golden set is the only regression evidence a planner change can be measured against.

# PHASE 6 UNBLOCKED BY

*(Pack extraction — the two-customer proof)*

- **PackManifest + PluginManifest** — the defining contracts. One artifact cannot serve two
  customers without them.
- **The seven-way decoupling taxonomy with per-item split lines** — and specifically the ruling
  that coupled files **split rather than move**, so the SELECT-only validator, the verification
  ledger and the outbound-approval sentence stay in core.
- **Gate 1 ratcheting from a reasoned baseline** — drift must stop growing on day one while the
  moves happen over months. Gate 1 cannot reach full enforcement **until** Phase 6 creates a
  `packs/` zone, because until then there is nowhere for the names to legitimately move to.
- **`pack` label on every tool registration** — core and customer tools are currently
  indistinguishable in one shared registry, and the count is **driver-dependent** (the dashboard
  router mounts inside a try/except).
- **The connector cache re-keyed by tenant** — extraction without it creates a live cross-tenant
  read on the customer's production database.
- **`DEFAULT_ORG_SLUG` failing closed** — an empty-slug fallback merges tenants.
- **`ALLOWED_EMAIL_DOMAINS` as tenant config with a deny-all core default** — its current shape
  cannot express two tenants at all.
- **The golden dataset frozen WITH its customer nouns**, and Gate 1's scope excluding it by
  construction — renaming those entities retunes a closed workstream.
- **TenantContext** — a pack boundary without a tenant boundary is cosmetic.

# PHASE 7 UNBLOCKED BY

*(Plugins & connectors)*

- **PluginManifest with a compatibility range** — nothing today has version metadata; the registry
  is a process-global dict populated by import side effects.
- **ConnectorInterface built on the live Fernet token stack** — and the explicit ruling that it is
  **not** built on `auth/providers/registry.py`, which reads and writes the **same Supabase table
  in plaintext**. That pair is one line of wiring from silently de-encrypting every user's OAuth
  tokens at rest.
- **PolicyInterface** — a connector without an authorization seam re-implements its own rules, which
  is how six connectors ended up with six behaviours.
- **EntitlementLicense's frozen dotted key namespace** — every plugin's `required_entitlement`
  references it, so it must be frozen before the first manifest is authored.
- **Gate 4a (duplicate tool names)** — the real precursor to plugin compatibility validation.
- **Tenant on the connector audit record and the rate-limit key.**
- **The unauthenticated provider routes closed (S-1, S-2)** — provider connectivity cannot be
  extended while its management surface is reachable with no credential.

# PHASE 8 UNBLOCKED BY

*(Control plane & packaging)*

- **ConfigSchema** — per-tenant configuration is impossible while 71 `os.getenv` calls are read at
  import time into process-global constants with zero validation.
- **`AGANETI_DATA_BACKEND` declared and validated** — *"the same signed artifact deploys to
  Customer #1 and #2"* is not currently true **even for one customer**, because the artifact's
  storage architecture is decided by an undocumented env var whose default disagrees with
  production.
- **ModelProfile as a tenant-configurable artifact** — and the versioned `model_list` in the repo,
  without which no deployment profile can be validated.
- **Contract versioning (two axes) with the upgrader chain** — the control plane's whole job is
  refusing incompatible artifacts.
- **TenantContext + Gate 8 across all 11 surfaces** — a control plane manages configuration *per
  tenant*.
- **Gate 5 scaffold + Gate 6 schema-diff half** — signing is blocked on infrastructure, not code,
  so Phase 0 ships only the scaffold and the half that is real.
- **The tenant-config store and a mounted endpoint decision** — the shipped React persona must be
  delivered at **runtime**; a build-time env var fails the very requirement it is meant to solve.
- **The two-invented-pack acceptance job** — designed in Phase 0 because it is what tells every
  other item when it is finished.

---

*End of plan. No source code, configuration, database, Docker or package change has been made.
Nothing has been staged or committed.*
