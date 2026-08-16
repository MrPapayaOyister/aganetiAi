# Messaging Architecture — Decision Spike

Companion to `phase0_implementation_plan.md`.

**Repository** `/home/matrix/aganetiAi` · **HEAD** `432fd5e` · **Status:** decision spike, read-only.
**No broker was installed. No dependency was added. No configuration was changed.**

---

## 1. Two separate answers, deliberately

> ### ARCHITECTURAL RECOMMENDATION
> **(D) PostgreSQL-backed queue / event architecture.**
> Postgres becomes the single durable system of record for both background work and domain events:
> a `jobs` table claimed with `SELECT … FOR UPDATE SKIP LOCKED` and leased by heartbeat; the
> existing `events` table promoted to a gap-free append-only log with per-consumer-group cursors;
> `LISTEN/NOTIFY` used strictly as a **latency hint, never as delivery**, with the existing periodic
> sweep as the durability backstop.

> ### CURRENT IMPLEMENTATION RECOMMENDATION (Phase 0)
> **Build none of that. Build no queue and no broker at all.**
> Phase 0 ships only: `EventEnvelope` v1, a subject convention, an idempotency convention, an
> ordering decision, two Protocols (`MessageBus`, `WorkQueue`), in-process adapters that preserve
> today's behaviour byte-for-byte, and an ADR with **measurable trigger thresholds**.
> **Zero new dependencies. Zero new processes. Zero behaviour change.**

**Why they are separate:** the transport is reversible; the contracts are not. Swapping
`PostgresBus` for `NatsBus` later touches **one adapter module**. Retrofitting `tenant_id` into an
envelope after four tracks have written producers touches **every producer, every consumer, every
subject and every test** — the same "retrofitting tenancy" mistake the gap analysis names as the
single most expensive available.

**D is not recommended because it wins on every axis.** It is the weakest of the five on native
request/reply, native wildcard routing and raw throughput. None of those three is a binding
constraint at ~20 users on one host, and all three are exactly what the `MessageBus` seam exists to
let you buy later without a rewrite.

---

## 2. The facts this decision rests on

| Fact | Evidence | Bearing |
|---|---|---|
| One uvicorn process, **no `--workers`** | `deploy/aganeti-api.service:11` | In-process fan-out is a dict of asyncio queues. A bus buys nothing today |
| `asyncpg` 0.31.0 + SQLAlchemy 2.0.51 already installed | requirements | `SKIP LOCKED` and `LISTEN/NOTIFY` cost **zero** new dependencies |
| A correct durable Postgres state machine **already runs in production** | `backend/storage/indexing.py` — bounded retry, backoff, stale sweep, uuid5 idempotency | D is a **generalisation of working code**, not a new build |
| The host Redis is **disqualified** | `config/settings.py:268-272` — shared with another application's live Celery broker, **no `requirepass`**, `maxmemory-policy=allkeys-lru` under a 2 GB ceiling shared between two applications | See §4 |
| Scale is order **10¹ users** | 14 Qdrant `user_memory` collections, 68 `chat_*.json` | Three orders of magnitude below where a Postgres queue strains |
| The project's own written target is Arq + Redis | `project_overview/ENTERPRISE_AGENTIC_OS.md` | Must be **explicitly superseded by an ADR**, not quietly ignored |
| Nine of twelve envelope fields are absent; there is **no** subject convention | `backend/events.py:29-31` | This — not the transport — is what actually blocks Phases 5–8 |
| **No Prometheus, Grafana, OTel or Langfuse anywhere** | gap analysis §17 | A broker would be **unobservable on day one** |

---

## 3. The five options against sixteen criteria

Scale: ●●● strong · ●●○ adequate · ●○○ weak · ○○○ absent

| # | Criterion | (A) NATS JetStream | (B) Redis + Arq | (C) Redis Streams | **(D) Postgres** | (E) Hybrid |
|---|---|---|---|---|---|---|
| 1 | Agent-to-agent | ●●● subjects fix the address split by construction | ●○○ a job queue, not a bus | ●●○ stream per agent | ●●○ `agent_messages` **already is this**, minus a tenant column | ●●● |
| 2 | Request / reply | ●●● native inbox subjects | ●●○ result-key polling | ●●○ hand-rolled reply stream | ●○○ reply row + NOTIFY (~40 lines) — **D's weakest axis** | ●●● |
| 3 | Pub/sub | ●●● native wildcards | ○○○ Arq has none | ●●● consumer groups | ●●○ log + `consumer_offsets` | ●●● |
| 4 | Fan-out | ●●● 6 WS channels map 1:1 with no code | ○○○ only via non-durable pub/sub | ●●● independent group cursors | ●●○ N groups, independent cursors | ●●● |
| 5 | Durable events | ●●● — but in **JetStream's own file store**, a *second* thing to back up | ○○○ jobs deleted after completion | ●●○ memory-resident | ●●● the `events` table already exists, already org-scoped, already indexed | ●●● |
| 6 | Replay | ●●● from sequence | ○○○ none | ●○○ **bounded by RAM** — replay depth is a memory budget, not a retention policy | ●●● `WHERE stream_seq > X`, bounded only by disk, **inside the same `pg_dump` as the golden datasets** | ●●● |
| 7 | Retries / backoff / DLQ | ●●● native `max_deliver` + `AckWait` | ●●● `max_tries`, `defer_by` | ●○○ hand-write attempt counting against a *less* durable store | ●●● copy `indexing.py:51-57` verbatim — proven code | ●●● |
| 8 | Worker groups | ●●● queue groups | ●●● **its real value** — a genuine cross-process worker tier | ●●● native | ●●○ `SKIP LOCKED` + lease + heartbeat | ●●● |
| 9 | Idempotency | ●●● `Nats-Msg-Id` | ●●○ `_job_id` enqueue dedup | ●○○ hand-rolled `SET NX` | ●●● uuid5 + UNIQUE — **already proven** at `ingest.py:257` | ●●● |
| 10 | Ordering | ●●● per-subject | ●○○ FIFO-ish | ●●● per-stream | ●●○ requires an explicit decision — see §6 | ●●● |
| 11 | **Tenant isolation** | ●●● accounts — but a **4th** isolation model | ●○○ key prefix | ●○○ key naming | ●●● **THE decisive advantage** — a column filtered by the same repository predicate and testable by the same Gates 2 and 8 | ●●○ |
| 12 | Observability | ●●○ good tooling, **nothing scrapes it** | ●○○ needs a new exporter | ●●○ `XINFO`/`XLEN` | ●●● **SQL.** Queue depth and per-tenant backlog become real numbers on the existing 24 observability endpoints | ●●○ |
| 13 | On-prem fit | ●●● single binary | ●●○ | ●●○ | ●●● **zero** new components, ports, daemons, credentials or backups | ●○○ |
| 14 | Operational complexity | ●○○ +daemon +port +file store +accounts +CLI | ●○○ +daemon +worker tier +AOF +auth | ●○○ same, plus a RAM ceiling that now matters | ●●● **lowest by a wide margin** | ○○○ **highest of the five** |
| 15 | Migration cost | ○○○ highest — new client, server, backup story, **and you still need the whole contract set** | ●○○ D's contract cost **plus** a broker | ●○○ disproportionately code you own | ●●● shape already exists in three places | ●○○ |
| 16 | Multi-user scale headroom | ●●● massive overshoot | ●●● | ●●● | ●●● `SKIP LOCKED` sustains thousands of claims/sec vs ~10¹ users | ●●● |

### Per-option verdicts

**(A) NATS JetStream — technically the best transport, at a scale this system is nowhere near.**
Subjects would fix the `agent_messages` address split by construction, and it is the only option
where the six documented WS channels map 1:1 onto subjects with no code. But it introduces a
**fourth isolation model** into a platform whose #1 structural problem is that it already has
divergent ones (Supabase user-scoped RLS bypassed by `service_role`, alembic `org_id` never read,
Qdrant `user_id` filter, Neo4j none); it stores durable state in a second place that must be backed
up alongside `pg_dump`, directly threatening the evidence-reproducibility requirement; and
`nats-py` is absent from `requirements.txt` and from all 581 venv packages.
**Reject for now. Named successor if T1+T4 fire together.**

**(B) Redis + Arq — the project's own written target; it does not survive contact with the audit.**
Arq answers **none** of the envelope / subject / replay / event-log requirements, so its true cost
is *D's contract cost **plus** a broker*. It solves the worker-tier problem and only that problem.
**Reject for Phase 0 — but if T1 fires alone, B is the correct minimal step**: smallest possible
move, matches the written spec, and Arq's retry/defer semantics map cleanly onto the `WorkQueue`
Protocol. Record that branch now so it is not re-litigated later.

**(C) Redis Streams — strictly the "build a broker out of primitives" option.**
Better bus semantics than Arq (real consumer groups), but replay is **bounded by RAM**, retries are
hand-written, and you would re-implement `indexing.py`'s claim/lease/retry/backoff/DLQ logic against
a *less* durable store. **Reject.** If you are going to hand-write the state machine anyway, write
it against the store that already holds `tenant_id`, is already transactionally consistent with the
business writes, already gets backed up, and where the reference implementation already runs in
production.

**(D) PostgreSQL — recommended.** See §5 for its honest weaknesses.

**(E) Hybrid — rejected as a Phase 0 choice; adopted as the pre-authorised evolution path.**
The coherent hybrid is: Postgres stays the durable system of record and transactional outbox; a
broker carries only **ephemeral, non-authoritative** traffic — cross-process WS/SSE fan-out,
presence, cache invalidation — where losing a message costs a UI refresh. That is exactly what the
project spec already asserts for approvals (*"socket miss → row is source of truth"*). It is the
right **target**, but it buys nothing until there is a second process to fan out to, and today there
is one process and one WebSocket route.

> **Record the rule now:** *a hybrid is only legal if the durable side remains authoritative. Any
> design where a message exists on the broker and **not** in Postgres is out of contract.* That one
> sentence prevents the hybrid decaying into two sources of truth.

---

## 4. "Redis is already on this host" is false, and the ADR must kill it explicitly

In the codebase's own words (`config/settings.py:268-272`), the host Redis is:

- **shared with another application's live Celery broker** (Video Indexer, `_kombu.binding.*` keys on db0);
- running **without `requirepass`**;
- running `maxmemory-policy=allkeys-lru` under a 2 GB ceiling **shared between the two applications** —
  *"a separate DB index isolates the KEYSPACE but NOT eviction, so either side can evict the other."*

For a cache, eviction is harmless — and that is all it was scoped for (it is disabled at
`settings.py:273`). For a queue or a stream it is **silent, unrecoverable job loss with no error
and no dead-letter.** No configuration of Arq or Redis Streams survives `allkeys-lru` eviction of
its own keys.

**Consequence: options B and C do not get to count Redis as free.** Both require provisioning a
**new dedicated instance** with `maxmemory-policy noeviction`, `appendonly yes` and `requirepass`/ACLs
— a genuinely new operational component on a single-host deployment that has none.

This is the same class of hazard as the shared, keyless, network-reachable Qdrant that also holds
another application's data, and deserves the same suspicion. *"Redis is already running, let's just
use it"* is the single most likely way this decision gets reversed by accident, so it gets its own
paragraph in the ADR.

---

## 5. Honest weaknesses of the recommendation

Recorded in ADR-0001 so they are chosen, not discovered:

1. **It is a build, not an install** — roughly 400–600 lines of infrastructure owned forever,
   including the BIGSERIAL visibility trap.
2. **Postgres becomes a single point of failure for messaging as well as state** — though it
   already is for state.
3. **`LISTEN/NOTIFY` is not durable** and does not survive a dropped connection. The periodic sweep
   is **mandatory, not optional** — a design property, not an oversight.
4. **Each `LISTEN` connection is a held connection** and must live **outside** the pool configured at
   `backend/db/base.py:31` (`pool_size=10, max_overflow=20`), or it starves request handling.
5. **It contradicts the project's own written spec** and must supersede it explicitly.

---

## 6. Decisions Phase 0 must make even though no transport is built

| # | Decision | Ruling |
|---|---|---|
| **M2** | `EventEnvelope` v1 | Nine fields, `tenant_id` **required and non-Optional** with two reserved sentinels. An Optional tenant means every consumer writes `if e.tenant_id` forever |
| **M2b** | **Ordering** | A **gap-free `stream_seq` from a single-writer outbox drain**, *not* raw `BIGSERIAL`. Sequences allocate before commit, so a cursor consumer reading `WHERE seq > X` can skip a row that commits later. This is the trap that makes naive Postgres logs lose events |
| **M2c** | **Idempotency** | Reuse the uuid5 formula already proven at `ingest.py:257` and in initiatives' `dedup_key` |
| **M2d** | The SQLite `events` backend | **DEPRECATE for envelope-carrying events** — its seven-column table cannot carry a tenant at all, so it is a tenancy hole for envelopes |
| **M3** | **Two Protocols, not one** | `MessageBus` (pub/sub, fan-out, at-least-once) and `WorkQueue` (claim, lease, retry, DLQ) are different contracts with different guarantees. Collapsing them is what forces a job queue to pretend to be a bus |
| **M4** | In-process adapters | Preserve today's behaviour **exactly**. The "nothing breaks" rule |
| **M5** | **Subject convention** | Logical subjects are transport-agnostic; **tenancy is added by the transport, not by the producer**. One function, `subject_for(event_type, tenant_key, env)`, is the only place an address is composed |
| **M6** | `agent_messages` address space | **KEEP** the table, **MIGRATE** the address space, **DEPRECATE** literal names. Must complete before the delegation consolidation begins |
| **M7** | ADR-0001 + CI gate M | The ADR supersedes the written Arq+Redis target. Gate M enforces that no module outside the adapter package imports a transport client |

---

## 7. Trigger thresholds — when to revisit, and what to adopt

The existing trigger at `indexing.py:25-29` is correct but qualitative. Phase 0 makes it
measurable and adds the branch rule.

| Trigger | Threshold |
|---|---|
| **T1 — DEPLOYMENT** *(a design trigger, not a load trigger)* | `--workers > 1`, a second replica, or a second host. **Fires the moment it is proposed, not when it is measured** |
| **T2 — BACKLOG** | `stored + processing > 30` (3× `SWEEP_BATCH`) sustained 30 min; or >1 document/day exceeding `STALE_PROCESSING_AFTER` |
| **T3 — LATENCY** | p95 enqueue→start > 60 s; or agent-to-agent delivery p95 > 30 s becoming user-visible |
| **T4 — FAN-OUT** | The first consumer of an event that does not live in the API process; or a second independent consumer group over the same stream |
| **T5 — VOLUME** | `events` insert rate > 50/s sustained; or any `/analytics/*` aggregation > 1 s p95 |
| **T6 — TENANT FAIRNESS** | One tenant's burst measurably starves another |

### Branch rule

| Trigger | Adopt |
|---|---|
| **T2, T3 or T5 alone** | **No broker.** Scale D: raise `SWEEP_BATCH`, add NOTIFY-driven wakeup, partition `events`. This is tuning, not architecture |
| **T1 alone** (worker tier, no fan-out) | **(B) Redis + Arq for jobs only**, on a new dedicated hardened instance. Smallest step; matches the written spec; maps onto `WorkQueue` |
| **T1 + T4 together** | **(E) hybrid with (A) NATS JetStream** as the accelerator. Postgres stays authoritative |
| **T6** | Per-tenant fairness is a **scheduling** concern — priority/quota columns in D, before a broker |

**What T1 breaks the moment it fires**, named so the cost is not rediscovered: `_active`
(`indexing.py:63`), `_briefed_events` (`main.py:322`), `_buckets` (`ratelimit.py:30`), `_last_run`
(`activity.py:33`), the APScheduler `MemoryJobStore` (**all 18 jobs double-fire, with no leader
election**), and the email `triaged_ids.json` dedup file.

**Most likely trigger, named honestly: T1, fired for reasons unrelated to messaging.**
`indexing.py:17-18` measures embedding at ~60 s for a 2 MB document, and `store_document` performs
blocking SeaweedFS I/O directly on the event loop; GPU contention between fastembed, vLLM and
whisper could force a worker split on CPU grounds alone. **Messaging should be designed assuming T1
fires first.**

---

## 8. What Phase 0 must NOT do

- Install a broker, a queue library, or any new dependency.
- Create a `jobs` table or an outbox drain.
- Convert `backend/events.py` into a bus. It stays the **audit sink**; the envelope is the shape it
  writes.
- Move `agent_messages` off its current transport before the address space is migrated.
- Write any producer against an envelope that does not yet have `tenant_id`.
- Flip `AGANETI_DATA_BACKEND`, or let any messaging test run against the SQLite branch — the
  SQLite `events` table has no tenant column, so such a test proves nothing.

---

*Decision spike only. No broker installed, no dependency added, no configuration changed. Nothing
staged or committed.*
