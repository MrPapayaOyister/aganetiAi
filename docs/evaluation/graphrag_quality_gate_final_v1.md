# GraphRAG Quality Gate — Final Validation and Freeze

> **STATUS: CLOSED.** The GraphRAG evaluation workstream is complete.
> **Production configuration is unchanged and recommended to stay unchanged.**

**Decision: keep `graph_top_k = 8`.** `top_k = 12` wins on the headline number
(+0.49pp overall) and every k=12 observation beats every k=8 observation — but it
fails two of the seven decision criteria: the answer-coverage gain comes from
**3 of 38 cases**, and groundedness regresses **−1.28pp** with 26 cases made
worse. Per §9, that means keep 8.

Nothing was staged or committed. Dataset v3.1 hashes verified before and after.

| Artifact | Path |
|---|---|
| Results | `docs/evaluation/graphrag_quality_gate_final_v1.json` |
| Manifest | `docs/evaluation/graphrag_quality_gate_final_v1_manifest.json` |

---

## 1. Method

Six full 174-case runs with answers, **interleaved A,B,A,B,A,B** so environmental
drift affects both candidates equally rather than confounding one. Mentions pinned
to the Step 7 frozen set; `graph_top_k` was the only variable.

Wiring verified first, per §4:

```
top_k=8  → registry default (no substitution needed) → provider top_k = 8
top_k=12 → provider top_k = 12, depth 1
           others preserved: corporate, memory, calendar, tasks, sql, history
```

## 2. Repeated-run comparison

| Metric | A1 | A2 | A3 | **k=8 mean** | B1 | B2 | B3 | **k=12 mean** | delta |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall | 0.5867 | 0.5867 | 0.5867 | **0.5867** | 0.5916 | 0.5916 | 0.5916 | **0.5916** | **+0.0049** |
| Groundedness | 0.6267 | 0.6267 | 0.6267 | **0.6267** | 0.6139 | 0.6139 | 0.6139 | **0.6139** | **−0.0128** |
| Answer coverage | 0.3947 | 0.3947 | 0.3947 | **0.3947** | 0.4474 | 0.4474 | 0.4474 | **0.4474** | **+0.0526** |
| Citation | 1.0000 | 1.0000 | 1.0000 | **1.0000** | 1.0000 | 1.0000 | 1.0000 | **1.0000** | 0.0000 |
| Negative premise | 1.0000 | 1.0000 | 1.0000 | **1.0000** | 0.8571 | 0.8571 | 0.8571 | **0.8571** | **−0.1429**\* |
| Graph F1 *(diag)* | 0.1942 | 0.1942 | 0.1942 | 0.1942 | 0.1622 | 0.1622 | 0.1622 | 0.1622 | −0.0320 |
| Graph noise *(diag)* | 0.8750 | — | — | 0.8750 | 0.9017 | — | — | 0.9017 | +0.0267 |
| Context tokens | 824.3 | 824.3 | 824.3 | **824.3** | 837.9 | 837.9 | 837.9 | **837.9** | +13.5 |
| p50 latency ms | 566.9 | 562.3 | 569.4 | **566.2** | 571.0 | 584.1 | 604.2 | **586.4** | +20.2 |
| p95 latency ms | 2780.7 | 2701.9 | 2755.1 | **2745.9** | 2766.1 | 2746.2 | 3028.2 | **2846.8** | +100.9 |

\* a scorer artifact — see §4.

### Determinism, and an honest correction to Step 12

**Within this session the runs were deterministic**: k=8 produced *zero* differing
answers across three repeats; k=12 differed on 2 of 174 in one repeat without
moving the aggregate.

That contradicts Step 12, which measured a 0.28pp floor with 62/174 answers
differing between two identical runs. Comparing across sessions explains it:
Step-13-A1 and Step-12-prod have **identical prompts in 174/174 cases but
identical answers in only 112/174**. So the nondeterminism is real but
**session-state dependent** (consistent with an LLM-server cache warming), not
per-run random.

The practical consequence is important:

| Config | observations across both sessions | range |
|---|---|---:|
| k=8 | 0.5794, 0.5822, 0.5867, 0.5867, 0.5867 | **0.0073** |
| k=12 | 0.5881, 0.5916, 0.5916, 0.5916 | 0.0035 |

**Cross-session spread for an identical config (0.73pp) is larger than the k=12
advantage (0.49pp)** — yet the two sets are cleanly **separated**: every k=12
observation exceeds every k=8 observation. So the effect is real in direction, but
its magnitude is not resolvable above session drift.

## 3. Per-case analysis

Comparing k=8 to k=12: **prompts differ in 106/174, answers in 68/174, and zero
answers differ without a prompt change** — clean causality, no noise contamination.

| | |
|---|---|
| Groundedness on differing cases | 34 better, **26 worse**, 8 same; mean **−0.0233** |
| Answer coverage | 13 cases affected, **3 improved, 0 degraded** |
| The 3 improvements | `kb-routes-to` +1.000, `graph-two-hop-chain` +0.500, `hop2-model-behind` +0.500 |

The entire +5.26pp answer-coverage gain comes from **3 of 38 scorable cases**, two
of which are multi-hop graph cases where extra graph context genuinely helps. That
is a real but narrow effect — and it is exactly what criterion 3 asks about.

## 4. The negative-premise result is a scorer artifact, not a regression

The 1.0000 → 0.8571 drop is a single case, `negp-unsupported-migration`:

| | Answer | Scored |
|---|---|---|
| k=8 | *"I do not know."* | ✅ pass |
| k=12 | *"I do not know why Neo4j was moved off Docker and onto Kubernetes. The provided context does not contain information about such a move."* | ❌ fail |

**Both are correct refusals.** The k=12 answer is the *more informative* one — it
names what it cannot support. It is flagged only because it restates the premise
while denying it, so the forbidden keywords `off docker` and `onto kubernetes`
appear in the text.

This is the substring limitation recorded in the Step 4 change log
(*"This is a substring check, not entailment"*) firing in the opposite direction:
it was expected to miss sophisticated affirmations, and here it instead punishes a
correct denial.

**Consequence:** criterion 5 is *unmeasurable* for this comparison, not failed. It
is neither evidence for nor against k=12. If the negative-premise metric is ever
used to decide a production change, the check needs negation awareness first.

That finding does not change the decision, which rests on criteria 3 and 4.

## 5. Decision against the §9 criteria

| # | Criterion | Verdict |
|---|---|---|
| 1 | Consistently improves end-to-end metrics | **PASS** — +0.49pp, every k=12 obs beats every k=8 obs |
| 2 | Improvement exceeds measured noise credibly | **PARTIAL** — zero within-session variance, but cross-session spread (0.73pp) exceeds the effect (0.49pp) |
| 3 | Not caused by a tiny number of cases | **FAIL** — 3 of 38 coverage cases produce the entire gain |
| 4 | Groundedness does not regress materially | **FAIL** — −1.28pp; 26 of 68 differing cases worse |
| 5 | Negative-premise behaviour stable | **UNMEASURABLE** — scorer artifact (§4) |
| 6 | Latency/context cost acceptable | **PASS** — p50 +20ms, p95 +101ms, +13.5 tokens |
| 7 | Reproducible | **PASS** within session; separated across sessions |

**Two clear failures → `graph_top_k` stays at 8.**

The honest summary: k=12 trades broad groundedness for narrow coverage. It helps 3
multi-hop cases a lot and makes 26 cases slightly less grounded. At a production
cutoff, the broad measure wins.

## 6. Final quality-gate scorecard

| Area | Result | Production decision | Status |
|---|---|---|---|
| Dataset | v3.1 frozen, hashes verified | no change | **PASS** |
| Evaluation harness | corrected (Step 11.6), causally truthful | use corrected runner | **PASS** |
| Resolver threshold | 0.82; sweep showed no better point | keep 0.82 | **PASS** |
| Mention extraction | bounded — 15 cases never reach the resolver | no change | **PASS** |
| Semantic resolver | no F1 improvement; doubles wrong resolutions | do not implement | **PASS** |
| Document filtering | no-op at 0.82; naive form loses 5 cases | no change | **PASS** |
| Graph depth | depth 1 sufficient; depth 3 blocked by `max_nodes` | keep 1 | **PASS** |
| **Graph top-k** | **8 vs 12 repeated validation — 12 fails criteria 3 & 4** | **keep 8** | **PASS (decided)** |
| Hop decay | provably inert at depth 1 | keep 0.55 | **PASS** |
| Graph ranking | freshness-dominated; 3 of 5 signals near-constant | known limitation | **ACCEPTED** |
| Qdrant | useful; R@5 0.2857, near-duplicate corpus | keep | **PASS** |
| Fusion | graph + Qdrant beats either alone; 0.5/0.6/0.7 indistinguishable | keep 0.60 | **PASS** |
| Negative premise | measured; scorer has a substring false-positive mode | preserve; fix check before reusing | **PASS (noted)** |
| Graph → LLM | verified reaching the prompt and changing answers | keep | **PASS** |
| Production integrity | zero evaluation writes across all 13 steps | unchanged | **PASS** |

## 7. Final production configuration — unchanged

```
resolver threshold : 0.82
graph depth        : 1
graph_top_k        : 8
hop_decay          : 0.55
fusion threshold   : 0.60
max_nodes          : 100
```

**No production file was edited.** This is a recommendation, and the
recommendation is to change nothing.

## 8. Known limitations (closed — not a new experiment loop)

1. Generic-noun attractors — the graph contains entities literally named `API` and `Model`, matching those words at similarity 1.0.
2. `DB` → *Dar Al Ber Society* at 0.8000 — identical similarity to the correct `K8` → Kubernetes, so no cutoff separates them.
3. `VLM` → *vLLM* at 0.8571, a false positive above threshold.
4. Conceptual references are suppressed at mention extraction; 15 cases never reach the resolver at all.
5. Graph ranking is freshness-dominated — 3 of 5 signals are near-constant within a neighbourhood.
6. Graph candidates are frequently near-tied (mean top-12 score spread 0.0535).
7. Qdrant has near-duplicate documents and imperfect recall (R@5 0.2857).
8. The conflict cohort is small — only 2–3 cases carry genuine two-sided evidence.
9. LLM answer nondeterminism exists at temperature 0 and is **session-state dependent**.
10. Graph and Qdrant compete with other providers for context allocation.
11. `tasks` consumes ~59% of context tokens in every case; graph gets ~3.5%.
12. Graph retrieval is weak by standalone F1 (0.194) yet demonstrably improves answers.
13. **Graph F1 is anti-correlated with end-to-end answer quality.**
14. Forbidden-keyword scoring is substring-based and can false-positive on refusals that restate the premise.

Items 11 and 13 are the most actionable if GraphRAG work ever resumes: graph is
under-allocated relative to its demonstrated value, and retrieval metrics should
never again be used as the optimisation target.

## 9. Production integrity

| Check | Value |
|---|---|
| Neo4j total / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| `kgtest-*` residue / canaries | 0 / 6-of-6 ✅ |
| **Qdrant `corporate_memory`** | **993 points — unchanged ✅** |
| Collections created by the evaluation | **none** ✅ |
| Evaluation Qdrant writes | **none** ✅ |
| Corpus / Postgres `documents` | 310 files / 0 rows ✅ |
| Production config | unchanged (§7) ✅ |
| Dataset v3.1 hashes | `cc9e9f2ddf118785` / `26435e37ec9f4d71` / `ce0cb2992228971f` ✅ |

Per §16, total collection count is deliberately **not** used as an invariant — the
running application creates `user_memory_<uuid>` collections during normal use
(12 → 14 over this workstream). The targeted invariants above all hold.

Tests: **400 passed, 4 failed** — the 4 are the long-standing
`tests/test_analytics.py` failures (`backend/analytics.py` has no `DB_PATH`),
untouched and not fixed throughout.

## 10. Answers to the final questions

1. **Is the harness trustworthy?** Yes — after Step 11.6. Before it, `RunnerConfig` controlled only the metrics path; that is fixed, tested (11 tests) and verified live.
2. **Is the dataset frozen and reproducible?** Yes — v3.1, hashes verified at every step, v1/v2/v3.0 snapshots archived.
3. **Is resolver threshold 0.82 defensible?** Yes — a 7-point sweep found no better operating point, and the `K8`/`DB` collision is provably unsolvable by any cutoff.
4. **Is depth 1 sufficient?** Yes — depths 2 and 3 produce byte-identical prompts, and depth 3 is unreachable under `max_nodes = 100`.
5. **Is `hop_decay` 0.55 acceptable?** Yes — provably inert at depth 1.
6. **Should `graph_top_k` remain 8 or move to 12?** **Remain 8.** k=12 fails criteria 3 and 4.
7. **Is fusion threshold 0.60 defensible?** Yes — 0.5/0.6/0.7 are indistinguishable except in dedup behaviour.
8. **Does Graph + Qdrant outperform either alone?** Yes, decisively: +6.22pp over Qdrant-only, +10.13pp over graph-only.
9. **Does graph evidence materially improve final answers?** Yes — +0.1434 groundedness where graph evidence is present, against a +0.0007 noise control (~200×).
10. **Remaining limitations?** §8 — 14 items, all characterised and bounded.
11. **Is any production change justified?** **No.** Nothing clears the decision bar.
12. **Is the quality gate complete?** **Yes.**
13. **Can GraphRAG tuning stop?** **Yes.**

---

## GraphRAG evaluation is CLOSED

Thirteen steps produced **zero production changes** — and that is the result, not a
failure to find one. Every proposed improvement was tested and rejected on
evidence: a lower resolver threshold, Document filtering, semantic resolution,
acronym agreement, deeper traversal, wider and narrower top-k, and three fusion
thresholds.

What the workstream did produce: a trustworthy 174-case benchmark, three corrected
evaluation defects (calendar expectations, Qdrant document identity, the
provider-path wiring), a measured noise floor, and the finding that **graph
retrieval metrics move opposite to answer quality** — which invalidates the
optimisation target three earlier steps were using.

Production configuration is frozen as in §7. No further GraphRAG experimentation
is recommended.
