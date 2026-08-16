# Phase 0 Dependency Graph

Companion to `phase0_implementation_plan.md`. Every edge below is a **hard** dependency: the
predecessor's output is an input to the successor, or the successor is unsafe/untestable without it.

**Repository** `/home/matrix/aganetiAi` · **HEAD** `432fd5e` · **Branch** `feat/enterprise-agentic-os`

---

## 1. The critical path

The longest chain of hard dependencies. Everything else fits around it.

```
OQ-5 answered
   │
   ▼
B-1  P-0 evidence preservation ──────────────────────────────┐
   │  (.gitattributes → path-scoped commit → ignore rewrite) │
   │                                                          │
   ▼                                                          │
B-0  Programme rulings (COLLIDE-1..4)                         │
   │                                                          │
   ├─────────────► B-2  CI repair (CI-0.1 → 0.4) ◄────────────┘
   │                        │
   ▼                        ▼
TenantContext v1      gates job exists
   │                        │
   ▼                        │
enforce.py single change ◄──┤   [strip → narrow → bind → inject → ContextVar]
   │                        │
   ├──► B-3 RC-0 internal-token fix (same file, same edit)
   │            │
   │            ▼
   │       owner predicates S-3..S-6, S-9, S-10
   │            │
   │            ▼
   │       S-7 dashboard  [columns → WRITE → reads → enforce]
   │            │
   │            ▼
   │       coreshare_db.run_query chokepoint  ← the acceptance point
   ▼
require_caller chokepoint (COLLIDE-2)
   │
   ▼
tenant BOUNDARY fix (T4)  ◄── MUST precede every backfill
   │
   ▼
Postgres ratchet → NOT NULL backfill → filter by table family
   │
   ▼
Gate 8 (tenant_id propagation)  →  Gate 2c
   │
   ▼
PHASE 0 EXIT
```

**Critical-path length is set by two things that are not engineering work:** the answer to OQ-5,
and the four B-0 rulings. Both are decisions measured in hours that currently sit at the head of
six independent chains.

---

## 2. Edges that were missing from the design and had to be added

These are the edges the adversarial critique found. Each one is the difference between a fix that
works and a fix that only appears to.

| Edge | Why it exists |
|---|---|
| `S-3 → S-7` | Board ids are **derivable** from session ids (`_board_id_for`, docstring: *"A session id IS the board id"*). `/observability/sessions` publishes every session id. Without this edge, S-7 looks deferrable because "board ids are secret" — they are not |
| `RC-0 → S-3..S-7` | Every owner predicate filters on a value an internal-token holder chooses freely. The internal path has **no loopback restriction** |
| `B-1 → every ratchet` | All four baselines were specified under `docs/`, which has **never been tracked**. A ratchet reading a missing file is either a collection error or a permanently-green gate |
| `B-1 → Neo4j write-side` | Its **only** acceptance criterion is a golden-set re-run, and 58 of the 174 cases exist solely as an untracked file |
| `B-1 → Gate 2` | Gate 2 is rated PARTIAL on two tests that are **untracked**. In a fresh clone the gate is MISSING |
| `T4 → all backfills` | Backfilling with a wrong tenant boundary is worse than not backfilling |
| `_ingest_cycle fix → Qdrant filter` | The ingest cycle keeps manufacturing NULL-tenant points; filtering first silently empties a 993-point collection, and nothing errors |
| `outbound single-source → PolicyInterface` | Wiring a policy engine to `agent_permissions.is_outbound` before collapsing it **encodes the wrong answer** into the new abstraction |
| `MG-0 → S-8 removal` | `gpt-4.1` is already registered in LiteLLM as `azure/gpt-4.1`; the probe turns an integration into an alias swap |
| `AGANETI_DATA_BACKEND freeze → every migration test` | CI exercises the SQLite branch; production runs Postgres. A test proving "approvals persist" passes against a branch that does not ship |
| `tool-toggle migration → /api/chat cutover` | Cutting over first silently deletes a shipped user-visible feature and breaks 16 tests |
| `Tool `pack` label → Gate 1` | Gate 1 cannot be written against a registry where core and customer tools are indistinguishable |

---

## 3. Track-level graph

```
                          ┌─────────────────────────┐
                          │  B-0  Programme rulings │
                          │  COLLIDE-1 .. COLLIDE-4 │
                          └────────────┬────────────┘
                                       │
        ┌──────────────┬───────────────┼───────────────┬──────────────┐
        ▼              ▼               ▼               ▼              ▼
   ┌─────────┐   ┌──────────┐   ┌────────────┐   ┌──────────┐   ┌──────────┐
   │ TENANCY │   │ SECURITY │   │ CONTRACTS  │   │ GATEWAY  │   │ DECOUPLE │
   └────┬────┘   └────┬─────┘   └─────┬──────┘   └────┬─────┘   └────┬─────┘
        │             │               │               │              │
        │  ┌──────────┘               │               │              │
        │  │  (RC-0 is a TENANCY      │               │              │
        │  │   finding shipped by     │               │              │
        │  │   SECURITY — one owner)  │               │              │
        ▼  ▼                          ▼               ▼              ▼
   ┌──────────────┐          ┌─────────────┐   ┌───────────┐   ┌───────────┐
   │ enforce.py   │          │ contracts   │   │  MG-0     │   │ taxonomy  │
   │ ONE change   │          │ skeleton    │   │ (probe)   │   │ + splits  │
   └──────┬───────┘          └──────┬──────┘   └─────┬─────┘   └─────┬─────┘
          │                         │                │               │
          ▼                         ▼                ▼               ▼
   require_caller           TenantContext      ModelProvider     pack labels
          │                    (alone)          Interface             │
          ▼                         │                │               │
   tenant boundary                  ▼                ▼               ▼
          │              EventEnvelope ∥ Entitlement  ModelProfile  Gate 1
          ▼                         │                                (report-only)
   Postgres ratchet                 ▼
          │              PolicyInterface (SHADOW)
          ▼                         │
       Gate 8 ◄─────────────────────┘
          │
          ▼
    PHASE 0 EXIT
```

**MESSAGING** hangs off `TenantContext` only (its envelope needs a non-optional `tenant_id`) and is
otherwise fully parallel. **GRAPHRAG** hangs off `B-1` only. **CI-GATES** hangs off `B-2` only.

---

## 4. Fan-out from single decisions

Ranked by how much they unblock — this is where decision latency costs the most.

| Decision | Unblocks | Cost to decide |
|---|---|---|
| **OQ-5** (may `docs/` be committed?) | **All four ratchets, the Neo4j acceptance test, Gate 2's real status, the entire GraphRAG preservation plan** | One answer |
| **COLLIDE-1** (TenantContext owner + glossary) | Both contract tracks, all 11 propagation surfaces, 5 of the 6 remaining contracts (each embeds a tenant field) | ~30 minutes |
| **COLLIDE-3** (contracts package owner) | Both contract tracks' step 0 | One line |
| **COLLIDE-2** (one caller chokepoint) | Security items S-3…S-7 **and** T5 — currently each route is edited twice, the second time right after it was security-fixed | One item merge |
| **OQ-3** (tenant-optional?) | The entire internal self-call fleet: `action_parser`, `tools.py:1139`, both outbound tools at `registry.py:210,217`, `main.py:482`, 10 Telegram sites | One ruling |
| **MG-0** (read-only probe) | S-8 removal, ModelProfile, Gate 3's rule 3b, and it **overturns** the embeddings P1 | Already done — read-only |
| **`AGANETI_DATA_BACKEND` freeze** | The credibility of the test column of almost every other item | Small, no behaviour change |

---

## 5. What is genuinely parallel

Ten tracks that share no files and no decisions once B-0 lands. Assign to separate owners.

| Track | Files touched | Shares nothing with |
|---|---|---|
| ConfigSchema | `config/settings.py`, new contract module | every other contract (**zero** contract dependencies) |
| Gates 1/3/7/4a | `ci/gates/**` only | the application entirely — installs nothing, imports nothing |
| MG-0 + MG-7 | read-only probe + new tests | all of tenancy and security |
| Messaging contracts | new `backend/contracts/` modules | any transport, any dependency |
| Neo4j write-side | `bootstrap.py`, `queries.py` | all read paths |
| Qdrant/embedder accessors | new `services/vectorstore.py` | everything — pure indirection |
| Decoupling taxonomy | documentation only | all code |
| `AGANETI_DATA_BACKEND` | `config/settings.py`, `.env.example` | behaviour (declaration only) |
| Consolidation register | documentation only | all code |
| Outbound single-source | `templates.py` (derived read) | the executor's behaviour (registry already wins) |

**Contention warning — `backend/auth/enforce.py`.** Four BLOCKING items across two tracks modify
the same 40 lines. It is the repository's most security-sensitive file and the only one with a
dedicated structural test suite. **One owner, one change, one review** (COLLIDE-4). Splitting it
means three merges into the auth middleware in one phase, where a mis-sequenced edit is a silent
authentication bypass.

**Contention warning — the uncommitted working tree.** 82 files are dirty, including another
developer's in-flight media/consumer tool surface (19 of the 24 tools that exist only on the
losing runtime) and a staged deletion of `config/users.py`. Every commit in Phase 0 must be
**path-scoped**. No `git add -A`, no reset, no clean, no stash.

---

## 6. Ordering traps — where a correct-looking sequence produces a wrong result

| Trap | Wrong order | Consequence |
|---|---|---|
| **Dashboard ownership** | reads before write | Every chart the agent creates for a user becomes invisible to that user **the instant it is saved**. The user asks for a chart, the agent reports success, the board renders nothing |
| **Qdrant tenancy** | filter before backfill | Silently empties a 993-point collection. Nothing errors |
| **Qdrant tenancy** | backfill before fixing `_ingest_cycle` | Immediately re-polluted with NULL-tenant points |
| **Agent-inbox signature** | signature before caller inventory | Runtime `TypeError` in the shipped Telegram Accept/Reject buttons — `:1005` calls with a **keyword** argument, so it fails on button press, not at import |
| **`/api/chat` cutover** | cutover before tool-toggle migration | Silently deletes a shipped feature; breaks 16 tests |
| **PolicyInterface** | policy before outbound single-source | Encodes the wrong answer into the new abstraction |
| **Baseline freeze** | freeze before preservation commit | Baseline describes an untracked tree |
| **Allowlist fix** | adding `path == p or startswith(p + "/")` | Already present. The defect is the **third disjunct**, which must be **removed** — implemented as described, the hole survives its own fix |
| **Neo4j org scoping** | changing the MERGE identity key | Invalidates every `expected_graph_nodes` value in all 174 cases; reopens a closed workstream |
| **Entitlement allowlist** | deny-by-default with an empty list | Outage of the shipped customer dashboard on merge day |

---

## 7. Dependency edges into later phases

Only edges that Phase 0 **creates**. Each is a reason the corresponding Phase-0 item exists.

```
TenantContext ──────────► Phase 5 (plan events), 6 (pack≠tenant), 7 (connector audit), 8 (per-tenant config)
EventEnvelope ──────────► Phase 5 (planner audit trail), 8 (control-plane events)
PolicyInterface ────────► Phase 5 (may this step run), 7 (connector authorization)
PluginManifest ─────────► Phase 6 (pack.provides.plugins), 7 (compatibility validation)
PackManifest ───────────► Phase 6 (the two-customer proof), 8 (artifact composition)
ConfigSchema ───────────► Phase 8 (per-tenant configuration)
ModelProfile ───────────► Phase 8 (deployment profiles)
AgentManifest ──────────► Phase 5 (a planner needs agents as data)
WorkflowManifest ───────► Phase 5 (multi-step plans), 6 (pack workflows)
Runtime parity ledger ──► Phase 5 (one executor to target)
Gate 1 baseline ────────► Phase 6 (drift stops growing while moves happen over months)
Gate 8 ─────────────────► Phase 6, 7, 8 (isolation provable, not asserted)
P-0 preservation ───────► Phase 5 (the only regression evidence any change can be measured against)
```

---

## 8. Suggested week ordering

Indicative, not a commitment. Assumes OQ-5 is answered in week 1.

| Week | Sequential (one owner each) | Parallel |
|---|---|---|
| **1** | B-0 rulings → B-1 preservation → CI-0.1/0.2 | MG-0 probe, ConfigSchema, `AGANETI_DATA_BACKEND`, decoupling taxonomy, Qdrant/embedder accessors |
| **2** | `enforce.py` single change (incl. RC-0) → S-1/S-2 | Gates 1 + 3 report-only, contracts skeleton, MG-7 characterisation tests, outbound single-source |
| **3** | `require_caller` → owner predicates S-3…S-6, S-9, S-10 | TenantContext type, Gate 7 + 4a, EventEnvelope + subjects, Neo4j write-side |
| **4** | tenant boundary (T4) → S-7 (columns→write→reads→enforce) | PolicyInterface shadow mode, EntitlementLicense namespace, Plugin→Prompt manifests, Gate 2a/2d |
| **5** | Postgres ratchet → backfill → filter by family | Agent→Workflow→Pack manifests, ModelProfile, messaging adapters + ADR-0001 |
| **6** | Gate 8 + Gate 2c → exit-criteria sweep | GraphRAG guards G-5…G-9, consolidation register, parity ledger |

---

*Companion document. Nothing here has been implemented, staged or committed.*
