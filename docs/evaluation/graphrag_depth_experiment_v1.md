# Step 10 — Graph Retrieval Depth Experiment

**Question:** does deeper traversal improve graph usefulness and end-to-end
quality enough to justify the noise, context and latency?

**Answer: no — and more sharply than expected. Depth changes nothing at all.**
Every graph metric is bit-identical at depth 1, 2 and 3; 172 of 174 cases return
the *same* node set; not one of the 18 depth-cohort cases passes at any depth.
The only thing depth buys is **3.1× graph latency**. The dominant problem is
`graph_top_k`/ranking, exactly as Step 6 suspected.

Production was **not changed** — `GRAPH_RETRIEVAL_DEPTH` is still 1. Nothing
staged, nothing committed. The dataset was not modified.

| Artifact | Path |
|---|---|
| Results | `docs/evaluation/graphrag_depth_experiment_v1.json` |
| Manifest | `docs/evaluation/graphrag_depth_experiment_v1_manifest.json` |

**Method.** The real pipeline was run three times through `EvaluationRunner` with
`RunnerConfig(graph_depth=1|2|3, with_answer=True)`. Only depth varies; threshold,
fusion, `graph_top_k=8`, `qdrant_top_k=5`, ranking weights and models are
production values, injected rather than written to `.env`. Because mention
extraction is an LLM call *inside* the pipeline, it was pinned in-process to the
Step 7 frozen mention set and restored afterwards — identical mentions give
identical graph seeds, so any difference observed is depth and not model
variability (§3, §5).

---

## A. Depth results (all 174 cases)

| Depth | Graph P | Graph R | Graph F1 | Noise | Ctx nodes | Tokens | graph ms | Answer kw | Groundedness |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **1** | 0.1250 | 0.5830 | 0.1942 | 0.8750 | 4.86 | 1281.3 | **45.1** | 0.3947 | 0.6432 |
| **2** | 0.1250 | 0.5830 | 0.1942 | 0.8750 | 4.87 | 1281.3 | **139.6** | 0.3947 | 0.6421 |
| **3** | 0.1250 | 0.5830 | 0.1942 | 0.8750 | 4.87 | 1281.3 | **150.1** | 0.3947 | 0.6429 |

Overall score: 0.6213 / 0.6211 / 0.6213 — a 0.02pp spread, far inside the 0.6%
answer-stage noise floor measured in the corrected baseline.

**Every graph metric is identical to four decimal places.** Precision, recall,
F1 and noise do not move at all. Context nodes move by 0.01. Token usage is
unchanged. That is not a small improvement — it is *no change*.

## B. Depth-sensitive cohort

Verified before running rather than assumed (§6): **21 cases tagged
`requires:depth-*`**, of which **3 produce no graph seed** (`hop3-full-chain`,
`long-full-arch`, `long-onboarding`) and are excluded per §3 — depth cannot fix a
case with no seed. **18 evaluable cases.** Every expected node was confirmed
present in the graph and reachable within depth 3 — 0 unreachable, 0 missing.

| Depth | Cases | Passed | Graph F1 | Noise | Ctx nodes |
|---:|---:|---:|---:|---:|---:|
| 1 | 18 | **0** | 0.1897 | 0.8681 | 8.00 |
| 2 | 18 | **0** | 0.1897 | 0.8681 | 8.00 |
| 3 | 18 | **0** | 0.1897 | 0.8681 | 8.00 |

**Zero cases become solvable at any depth.** Per-case attribution (§18) is
uniform: all 18 are `d1=fail, d2=fail, d3=fail`. None is a *true depth
limitation*; none is a *graph-data limitation* (paths exist); 3 are *seed
limitations* (excluded); the remaining 18 are **ranking limitations**.

A pre-run check already hinted at this: **6 of the 18** have all their expected
nodes reachable at depth ≤ 1, so they could never have been depth failures.

## C. Node origin analysis (§9)

Retrieved nodes by traversal distance, across the 18-case cohort:

| Depth | hop 0 | hop 1 | hop 2 | hop 3 | mean retrieved |
|---:|---:|---:|---:|---:|---:|
| 1 | 23 | 922 | 0 | 0 | 52.5 |
| 2 | 23 | 922 | **855** | 0 | 100.0 *(capped)* |
| 3 | 23 | 922 | **855** | **0** | 100.0 *(capped)* |

Nodes that actually survive `top_k=8` and reach the context:

| Depth | hop 0 | hop 1 | hop 2 | hop 3 |
|---:|---:|---:|---:|---:|
| 1 | 23 | 121 | — | — |
| 2 | 23 | 121 | **0** | — |
| 3 | 23 | 121 | **0** | **0** |

**Two structural findings explain the entire result.**

**1. No hop-2 node ever survives `top_k=8`.** Depth 2 retrieves 855 additional
hop-2 nodes and every single one ranks below position 8. `hop_decay = 0.55`
penalises each extra hop, so hop-2 nodes are systematically ranked beneath the
hop-0/hop-1 nodes that depth 1 already had. The extra traversal is performed,
paid for, and then discarded.

**2. Depth 3 is structurally identical to depth 2.** `retriever.py:79` breaks
expansion when `len(nodes) >= max_nodes` (100). Depth 2 already exhausts that cap
for every cohort case — mean retrieved is exactly 100.0 — so **hop 3 is never
attempted**. Zero hop-3 nodes were retrieved at depth 3. "Depth 3" is not a
configuration this system can currently express.

Consistent with both: **172 of 174 cases return an identical node set at all
three depths.** Only `neg-google-founder` and `conflict-doc-vs-memory` differ, and
only by filling spare top-8 slots (7→8 nodes) with hop-1 material.

## D. Marginal value (§10)

| Transition | +retrieved nodes | +relevant kept | +context nodes | +graph latency |
|---|---:|---:|---:|---:|
| depth 1 → 2 | **+855 (+90%)** | **0** | +0.01 | **+94.5 ms (+210%)** |
| depth 2 → 3 | 0 | **0** | 0.00 | +10.5 ms (+7.5%) |

Depth 2 nearly doubles the nodes traversed and returns **nothing** to the
context. Depth 3 does not even traverse further; its extra 10.5 ms is the cost of
attempting a third hop that immediately hits the node cap.

## E. Context explosion (§11)

| Depth | mean | median | p95 | max | mean tokens | p95 tokens |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4.86 | 8.0 | 8 | 8 | 1281.3 | 1369 |
| 2 | 4.87 | 8.0 | 8 | 8 | 1281.3 | 1369 |
| 3 | 4.87 | 8.0 | 8 | 8 | 1281.3 | 1369 |

**Context does not explode — because `top_k=8` prevents it.** The Step 5
prediction that depth 2 reaches 65–86% of the graph and depth 3 ~94% is confirmed
at the *traversal* layer (855 extra hop-2 nodes) but never materialises in the
context, since the cap discards all of it. The explosion is real and paid for in
latency; it is simply thrown away before it can do damage — or good.

This is worth stating precisely: the system is protected from context flooding by
`top_k`, not by depth being safe.

## F. Latency (§14)

| Depth | graph ms | ranking ms | total ms | e2e p50 | e2e p95 |
|---:|---:|---:|---:|---:|---:|
| 1 | **45.1** | 0.52 | 940.8 | 591.7 | 2726.5 |
| 2 | **139.6** | 0.59 | 1048.8 | 695.0 | 3208.2 |
| 3 | **150.1** | 0.57 | 1040.4 | 705.1 | 2757.4 |

Graph retrieval costs **3.1× at depth 2** and **3.3× at depth 3**. End-to-end p50
rises ~18%. The whole of that cost buys zero measurable benefit.

## G. Ranking interaction (§15) — the actual finding

| Depth | expected node retrieved | ≥1 expected node in top-8 | median expected-node rank |
|---:|---:|---:|---:|
| 1 | 18/18 | 15/18 | **9** |
| 2 | 18/18 | 15/18 | **10** |
| 3 | 18/18 | 15/18 | **10** |

**The expected nodes are retrieved at every depth — they simply rank just below
the cut.** Median rank is 9–10 against `graph_top_k = 8`. Depth does not change
which nodes are found; it changes only how many irrelevant ones are found
alongside them, and deeper hops push the median *down* slightly (9 → 10) because
extra candidates compete for the same eight slots.

This confirms the Step 6 hypothesis exactly: `graph_node_precision = 0.125` and
`graph_noise_ratio = 0.875` are a **ranking and top-k problem, not a reach
problem**. The graph already contains and retrieves the right answers.

## H. End-to-end effect (§13)

| Depth | overall | answer keyword | groundedness | citation coverage |
|---:|---:|---:|---:|---:|
| 1 | 0.6213 | 0.3947 | 0.6432 | 1.000 |
| 2 | 0.6211 | 0.3947 | 0.6421 | 1.000 |
| 3 | 0.6213 | 0.3947 | 0.6429 | 1.000 |

Answer keyword coverage is **identical**; groundedness varies by 0.11pp, well
inside the 0.6% answer-stage noise floor established in the corrected baseline.
Since the graph context is the same, this is the expected result — and it is
reported as *no signal*, not as a tie-break in favour of any depth.

## I. Flooding safety (§12)

The Step 5 guard was re-run explicitly: `test_retrieving_everything_does_not_raise_the_headline_score`
asserts that two runs with identical perfect recall — one returning 1 relevant
node, one returning that node plus 99 irrelevant — must score the flooding run
**strictly lower** overall. Together with `graph_noise_ratio` and `context_nodes`,
3 tests pass. `overall` uses `graph_node_f1`, not bare recall, so a deeper
configuration cannot be rewarded for volume.

In this experiment the guard was never exercised in anger, because `top_k=8`
prevented flooding from reaching the scorer at all — but it remains the reason a
future `top_k` experiment can be run safely.

## J. Decision

**1. Is depth 1 sufficient?** **Yes.** It retrieves exactly the same context as
depth 2 and 3 at one third of the graph latency.

**2. Does depth 2 materially improve Graph F1?** **No — 0.1942 at every depth,
identical to four decimals.**

**3. Does depth 3 provide useful additional information?** **No, and it cannot.**
`max_nodes=100` is exhausted at depth 2, so no hop-3 node is ever retrieved.
Depth 3 is structurally identical to depth 2 in this system.

**4. Noise cost?** Unchanged in the context (0.8750 at all depths) — but 855
irrelevant hop-2 nodes are traversed and discarded per cohort run. The noise is
paid for in latency rather than in context.

**5. Latency cost?** Graph retrieval **+210% at depth 2**, +233% at depth 3;
end-to-end p50 +18%.

**6. Does context explode?** Not in the delivered context — `top_k=8` caps it at
8 nodes and ~1281 tokens at every depth. The explosion happens during traversal
and is discarded.

**7. How many depth-sensitive cases become solvable?** **Zero, of 18.**

**8. Are remaining failures actually ranking failures?** **Yes.** All 18 expected
nodes are retrieved at every depth; the median expected-node rank is 9–10 against
a cut-off of 8, and no hop-2 node ever enters the top 8. Three further cases are
seed failures, correctly excluded.

**9. Should production depth change?** **No. Keep `GRAPH_RETRIEVAL_DEPTH = 1`.**
It fails criteria 1, 2, 3 and 7 of §23 outright: no F1 improvement, no recall
improvement, no case becomes solvable, and there is a 3× latency cost.

**10. Should Step 11 focus on graph ranking/fusion?** **Yes — that is now the
clearly indicated next lever**, and §23 says so explicitly: *"If depth improves
retrieval but correct nodes remain poorly ranked, recommend the ranking
experiment instead of changing depth."* That is precisely what was measured.

### What Step 11 should investigate, in priority order

- **`graph_top_k`** — the median expected node ranks 9–10 against a cut of 8.
  This is the single highest-leverage untested parameter, and Step 10 deliberately
  did not touch it (§16). A `top_k` sweep may recover most of the missing recall
  with no traversal change at all.
- **`hop_decay = 0.55`** — it is why no hop-2 node ever reaches the top 8. If
  deeper nodes are ever to be usable, this weight and `top_k` must be tested
  *together*; testing depth without them, as Step 10 did, is provably a no-op.
- **`max_nodes = 100`** — an undocumented ceiling that silently makes depth 3
  unreachable. Any future depth work must raise it first or the setting is
  meaningless.
- The other five ranking weights (confidence, corroboration, freshness,
  importance, frequency), untouched here per §17.

## K. Dataset note (not corrected — v3.1 is frozen, §19)

The 18-case depth cohort is measuring something other than what its tags claim.
Six cases (`hop2-from-postgres`, `hop2-model-behind`, `hop2-serving-stack`,
`hop3-blast-radius`, `hop3-shared-dependency`, `multi-entity-3`) have all expected
nodes reachable at depth ≤ 1, so `requires:depth-2` overstates their requirement.
They are genuine failures — but of ranking, not depth. Recorded here as a
recommendation only; no expectation was altered.

## L. Production integrity

Verified before and after; snapshots identical.

| Check | Value |
|---|---|
| Neo4j total / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| `kgtest-*` residue | 0 ✅ |
| Canaries | 6/6 ✅ |
| Qdrant `corporate_memory` / collections | 993 / 20, none created ✅ |
| Corpus | 310 files ✅ |
| Postgres `documents` | 0 rows ✅ |
| `GRAPH_RETRIEVAL_DEPTH` in settings | **1 — unchanged** ✅ |
| Resolver threshold | 0.82 — unchanged ✅ |
| Dataset v3.1 hashes | re-verified unchanged ✅ |

All Cypher was read-only traversal. Depth was injected through `RunnerConfig`;
`.env` and `config/settings.py` were never touched, and the mention-extractor
patch was in-process only and restored on exit.
