# GraphRAG Evaluation Dataset — v1 → v2 Changelog

**Step 4 of the GraphRAG Quality Gate.** Corrects stale and unenforceable
expectations found by the [v1 audit](graphrag_dataset_audit_v1.md).

Scope: 116 cases, 115 touched. No tuning, no threshold changes, no resolver
changes, no infrastructure. Nothing staged, nothing committed.

**Version boundary:** both case files now carry `"dataset_version": 2`. The
verbatim v1 files are preserved at `backend/evals/cases/archive/v1/` and were
verified byte-identical to `git show HEAD:` — any pre-v2 result remains
reproducible and comparable.

---

## The governing rule

Every correction here fixes a case where **the dataset recorded a broken
environment, or recorded an intention nothing could score**. No case was
adjusted to match what the system currently outputs. Two cohorts (semantic
resolution, depth) are *expected to keep failing* after v2 and were deliberately
left failing, with tags added so a baseline can report them as known limitations
rather than silently absorbing them.

---

## 1. Calendar expectations — 109 cases

`expected_context_sources` gained `calendar`.

| Old | New | Cases |
|---|---|---|
| `[graph, corporate, tasks]` | `+ calendar` | 70 |
| `[corporate, tasks]` | `+ calendar` | 38 |
| `[tasks]` | `+ calendar` | 1 |

**Why.** The dataset was authored while the evaluation identity's calendar token
was expired, so calendar contributed nothing and was written into the ground
truth as absent. After the token was fixed, calendar contributes to 100% of
cases. `expected_context_sources` is *precision-scored*
(`scoring.py:153`, `M.prf`), so a provider that contributes while unlisted
drives `source_precision` down on every case at once. That is the mechanism
behind the apparent broad regression — a stale expectation, not a retrieval
defect.

**What was deliberately NOT changed — `expected_provider_order`.** 104 cases
carry this field and none of them gained `calendar`. `rank_correlation`
(`metrics.py:108`) compares **only items present in both lists**, so calendar's
absence from the expected order penalises nothing today. Appending it would
assert "calendar always ranks last", a claim with no supporting measurement, and
would newly admit calendar into the correlation where it could *lower* scores.
The smallest correct change is to leave the order field alone.

**Caveat that must not be lost.** `CalendarProvider.collect()` returns `[]` when
there is no agenda text, so calendar is *conditionally* on, not unconditionally
on like `tasks`. These 109 expectations are valid **only while the evaluation
identity has calendar content**. If the eval account's calendar empties, these
cases will fail on `source_recall` — and that failure would be an environment
problem, not a retrieval regression. This is the same trap v1 fell into, one
direction reversed.

**6 cases deliberately left without calendar** — `graph-direct-usage`,
`graph-two-hop-chain`, `alias-ms-graph`, `alias-postgres`, `person-project`,
`mixed-graph-and-docs`. Each expects exactly `[graph]`: a deliberately narrow
set asserting that graph retrieval alone answers the question. Adding calendar
would destroy precisely what they discriminate.

---

## 2. False-premise cases — 3 cases, and the one scoring change

| Case | Question | `forbidden_answer_keywords` |
|---|---|---|
| `adv-leading-false` | "Why did we migrate from Neo4j to MongoDB?" | `migrated to mongodb`, `migration to mongodb`, `we migrated`, `replaced neo4j` |
| `adv-leading-person` | "When did Akshay leave the Agentic AI project?" | `left the project`, `departed`, `resigned`, `no longer works` |
| `adv-invented-rel` | "How does Qdrant route requests to vLLM?" | `qdrant routes`, `routes requests to vllm`, `qdrant sends` |

**The v1 defect.** These three carried their entire intent in prose `notes`
("Neo4j must resolve; MongoDB must NOT be invented"). Nothing machine-checkable
enforced it, so all three scored as ordinary retrieval cases and would pass
while confidently affirming a fabricated migration.

**Why `negative=True` is the wrong fix.** The existing `negative` flag asserts
that *nothing should resolve* (`scoring.py:198` — any resolved entity is a
leak). That is the opposite of what these cases need: Neo4j, Akshay, Qdrant and
vLLM are all real and **must** resolve. What must not happen is the *answer*
affirming a relationship no evidence supports. `negative=False` was already
correct; the gap was that no field expressed the answer-level contract.

*(The v1 audit report described these as "mis-flagged"; that was imprecise and
is corrected here — the flag was right, the enforcement was missing.)*

**The change** — two additions, both minimal:

- `GoldenCase.forbidden_answer_keywords: list[str]` (`dataset.py`)
- a scorer block (`scoring.py`) setting `CaseScore.negative_premise_ok`

```python
if case.forbidden_answer_keywords and trace.answer:
    low = trace.answer.lower()
    affirmed = [k for k in case.forbidden_answer_keywords if k.lower() in low]
    s.negative_premise_ok = not affirmed
    if affirmed:
        s.failures.append(f"answer affirmed an unsupported premise: {affirmed}")
```

Two guards keep this from contaminating anything else:

- `negative_premise_ok` defaults to **`None`**, not `False`. The 113 cases that
  declare no forbidden keywords never acquire a failing sub-score.
- The check runs only when `trace.answer` is non-empty, so retrieval-only runs
  are untouched rather than being scored as premise failures.

This is a substring check, not entailment. It catches the blunt failure mode
(the answer states the false premise as fact) and will not catch an elaborate
paraphrase. That is an accepted limit, recorded rather than hidden.

---

## 3. Depth-dependent cases — 14 cases

Tagged `requires:depth-N` (alongside the existing `depth:N`):

`hop2-model-behind`, `hop2-serving-stack`, `hop2-akshay-stack`,
`hop2-from-postgres`, `hop3-shared-dependency`, `hop3-blast-radius`,
`long-full-arch`, `long-onboarding`, `multi-entity-3` (depth-2);
`hop2-inference-chain`, `hop3-reverse`, `hop3-impact`, `adv-very-long-question`
(depth-3); `hop3-full-chain` (depth-4).

Production runs `GRAPH_RETRIEVAL_DEPTH=1`. These 14 cases **cannot** pass and
were never able to. Expectations are unchanged — they describe correct desired
behaviour. The tag exists so a baseline segments them out explicitly instead of
counting a configuration ceiling as a retrieval regression. A test asserts every
tagged case genuinely requires more depth than production provides, so the tag
cannot rot into a blanket excuse.

---

## 4. Semantic-resolution cases — 8 cases

Tagged `requires:semantic-resolution`: `syn-vector-db`, `syn-graph-db`,
`syn-gateway`, `syn-relational`, `syn-mail-api`, `abbrev-llm`, `abbrev-db`,
`abbrev-api`.

**Expectations deliberately unchanged.** These ask the resolver to map
"vector database" → Qdrant, "mail API" → Microsoft Graph, and so on. The current
lexical resolver cannot: measured, "vector database" → Qdrant scores 0.811
against a 0.82 threshold, and "mail API" produces no match at all. Lowering the
expectation to whatever the lexical resolver returns would delete the only
evidence that semantic resolution is needed. **These 8 stay failing on
purpose.** A test asserts they still name their original target entities, so a
future change cannot quietly weaken them into passing.

---

## 5. Infrastructure-dependent cases — 11 cases

Tagged `requires:provider-*` from their existing `needs:*` tags:

- `requires:provider-calendar` (4) — `temporal-tomorrow`,
  `temporal-after-tomorrow`, `temporal-this-week`, `temporal-today`
- `requires:provider-memory` (5) — `memory-neo4j-decision`,
  `memory-preference-currency`, `memory-reporting-style`, `memory-past-project`,
  `memory-team-pref`
- `requires:provider-email` (2) — `email-recent`, `email-from-person`

These depend on live external state. When one fails, the report must say
*infrastructure*, never *retrieval regression* — the confusion that produced the
calendar problem in §1 in the first place.

---

## Verification

**Dataset**
- 116 cases load; ids unique
- no expectation names an unknown provider
- calendar in `expected_context_sources`: 109 · in `expected_provider_order`: **0** (intentional)
- `requires:depth-*` 14 · `requires:semantic-resolution` 8 · `requires:provider-*` 11
- `forbidden_answer_keywords` on exactly the 3 adversarial cases
- no adversarial case carries `negative=True`
- schema shape unchanged except `forbidden_answer_keywords` in `enterprise.json`

**Tests** — `tests/test_eval_scoring.py`, 13 new tests, all passing. Covers
premise affirmed → fail, premise refused → pass, case-insensitivity,
`None` for unrelated cases, `None` for retrieval-only runs, and that a
false-premise case still requires its entities to resolve.

Full suite: **249 passed, 4 failed**. The 4 failures are pre-existing in
`tests/test_analytics.py` (`backend/analytics.py` has no `DB_PATH`); neither
file is touched by this work. Separately, `tests/test_insight_evidence.py` and
`tests/test_routing.py` are scripts with module-level `sys.exit()` and crash
pytest collection — also pre-existing, also untouched.

**Production integrity** (Step 4 performed zero graph, vector or storage writes)

| Check | Result |
|---|---|
| Neo4j `:Entity` nodes | 522 ✅ |
| Neo4j relationships | 3414 ✅ |
| `kgtest-*` leakage | 0 ✅ |
| Canary entities | 6/6 present ✅ |
| Qdrant `corporate_memory` | 993 points ✅ |
| Enterprise corpus | 310 files ✅ |

Two figures worth stating precisely rather than glossing:

- **Total Neo4j nodes is 523, not 522.** The extra node is a single non-`:Entity`
  node, `(:User {name: "Akshay"})`. The 522 baseline counts `:Entity` nodes; both
  numbers are correct and consistent.
- **Neo4j `:Document` nodes number 101, not 310.** 310 is the *corpus file* count
  (verified intact on disk); it was never the Neo4j Document-node count, and no
  earlier report recorded 101 as a baseline. All 101 also carry `:Entity`, which
  is the Document-pollution mechanism the v1 audit identified — the full-text
  index covers all `:Entity`.

Also confirmed: the Postgres `documents` table holds 0 rows. The B3/B4 test
documents were hard-deleted by their own cleanup; the 310-document corpus
predates that table and never lived in it.

---

## Files changed

| File | Change |
|---|---|
| `backend/evals/cases/enterprise.json` | 108 cases; v2 boundary |
| `backend/evals/cases/core_retrieval.json` | 8 cases; v2 boundary |
| `backend/evals/dataset.py` | `+ forbidden_answer_keywords` |
| `backend/evals/scoring.py` | `+ negative_premise_ok`, false-premise check |
| `backend/evals/cases/archive/v1/` | **new** — verbatim v1 snapshot |
| `tests/test_eval_scoring.py` | **new** — 13 tests |
| `docs/evaluation/graphrag_dataset_v2_changes.md` | **new** — this file |

Nothing staged. Nothing committed. No other developer's files touched.

---

## What v2 does not fix

- **Document pollution** has one proven detector (`syn-gateway`). One case is not
  a regression family; building that out is future work.
- **Substring premise matching** will not catch a sophisticated paraphrase.
- **Calendar expectations are environment-coupled** — see the caveat in §1.
- **22 cases still have no `expected_context_sources`** and are therefore unscored
  on fusion.
