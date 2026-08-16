# Step 12 — End-to-End Graph/Qdrant Fusion Experiment

The first fusion experiment run through the **corrected provider path** (Step
11.6), so `graph_top_k` genuinely reaches the `GraphProvider` that builds the
prompt.

**Headline: GraphRAG does improve answers — and graph retrieval quality is
*anti-correlated* with answer quality.** Removing graph evidence costs 6.22pp
overall and 9.61pp groundedness, ~200× the measured noise floor. Meanwhile
`top_k = 4` has the *best* graph F1 (0.3130) and the *worst* answers, while
`top_k = 12` has the worst graph F1 and the best answers. Every retrieval-metric
conclusion from Steps 10–11 pointed the wrong way for end-to-end quality.

Production was **not changed**. Nothing staged, nothing committed. Dataset v3.1
hashes verified before and after.

| Artifact | Path |
|---|---|
| Results | `docs/evaluation/graphrag_fusion_experiment_v1.json` |
| Manifest | `docs/evaluation/graphrag_fusion_experiment_v1_manifest.json` |

**Method.** Eight full 174-case runs with answers enabled. Mentions pinned to the
Step 7 frozen set so graph seeds are identical and only the tested parameter
moves. Applying the Step 11.5 lesson, both experiment variables were **verified
to change the prompt hash before any long run**: `fusion_threshold` 0.5/0.6/0.7
produced three distinct hashes, and provider ablation produced distinct hashes.

**Comparability caveat.** Absolute scores here are not comparable with the
corrected baseline artifact (0.6175), because mentions are frozen rather than
live. All eight variants share the same frozen mentions and are comparable *with
each other*, which is what the experiment requires.

---

## 0. Nondeterminism floor — measured, not assumed

`prod` and `prod_repeat` are byte-identical configurations:

| | overall |
|---|---:|
| prod | 0.5822 |
| prod_repeat | 0.5794 |
| **|delta|** | **0.0028 (0.28pp)** |

All **174 prompts were byte-identical**, yet **62 answers differed (36%)**. At
temperature 0 the model still varies per case; the aggregate is stable to about
0.28pp because the variation averages out.

**Any effect below ~0.28pp overall is not distinguishable from noise**, and every
claim below is measured against that bar.

## 1. Provider ablation

| Variant | Graph | Qdrant | Graph F1 | Qdrant R@5 | Fusion P | Groundedness | Answer cov. | Citation | **Overall** |
|---|:-:|:-:|---:|---:|---:|---:|---:|---:|---:|
| Graph only | ✓ | | 0.1942 | 0.0000 | 0.9624 | 0.3992 | 0.2632 | 1.000 | **0.4809** |
| Qdrant only | | ✓ | 0.1942 | 0.2857 | 0.9624 | 0.5433 | 0.2632 | 1.000 | **0.5200** |
| **Graph + Qdrant** | ✓ | ✓ | 0.1942 | 0.2857 | 0.9605 | **0.6394** | **0.3947** | 1.000 | **0.5822** |

Combined beats **both** sources alone, decisively:

- vs Qdrant-only: **+6.22pp overall, +9.61pp groundedness, +13.15pp answer coverage**
- vs Graph-only: **+10.13pp overall, +24.02pp groundedness, +13.15pp answer coverage**

Both are an order of magnitude above the 0.28pp floor.

### Robustness — is the graph benefit broad, or a few cases?

Per-case groundedness delta (`prod` − `qdrant_only`):

| Population | n | mean delta | better | worse | same |
|---|---:|---:|---:|---:|---:|
| cases **with** graph evidence in the prompt | 106 | **+0.1434** | 52 | 34 | 20 |
| cases **without** graph evidence | 68 | +0.0224 | 11 | 1 | 56 |
| **noise control** (`prod` vs `prod_repeat`) | 106 | **+0.0007** | 21 | 18 | 67 |

The graph effect is **~200× the noise control** and spread across the cohort. It
is not uniformly positive — graph evidence makes 34 of 106 cases *worse* — but the
net is strongly positive and far outside noise.

## 2. Graph top-k on the real answer path

| top_k | Graph F1 | Graph noise | Context tokens | Groundedness | Answer cov. | **Overall** | p50 ms | p95 ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | **0.3130** | **0.7625** | 1187.3 | 0.5913 | 0.3684 | **0.5712** | 882.9 | 2785.7 |
| **8** *(prod)* | 0.1942 | 0.8750 | 1199.3 | **0.6394** | 0.3947 | 0.5822 | 680.4 | 3155.7 |
| 12 | 0.1622 | 0.9017 | 1212.9 | 0.6323 | **0.4474** | **0.5881** | 909.5 | 3224.5 |

**Graph F1 runs exactly opposite to answer quality.** `top_k = 4` — the
configuration Step 11 identified as the graph-metric optimum, with 61% better F1
and the lowest noise — produces the **worst** end-to-end result: −1.10pp overall
and −2.63pp answer coverage versus production.

This settles the conclusion withdrawn in Step 11.6. It is no longer "untested":
**`top_k = 4` measurably hurts answers.** The mechanism is visible in the table —
tighter selection raises precision on the expected-node set while removing context
the model was using.

`top_k = 12` is the best end-to-end variant (+0.59pp overall, +5.27pp answer
coverage) despite the worst retrieval metrics. +0.59pp is only ~2× the noise
floor, so it is suggestive rather than proven; the +5.27pp answer-coverage gain is
the more convincing signal, since keyword coverage is a deterministic check on the
answer text.

## 3. Fusion threshold

| fusion | Source P | Source R | Corroboration | Dedup | Groundedness | Answer cov. | Overall |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.5 | 0.9605 | 0.6951 | 0.9545 | 0.1569 | 0.6352 | 0.3947 | 0.5852 |
| **0.6** *(prod)* | 0.9605 | 0.6951 | 0.9545 | 0.1379 | 0.6394 | 0.3947 | 0.5822 |
| 0.7 | 0.9605 | 0.6951 | 0.9545 | 0.0098 | 0.6328 | 0.3947 | 0.5792 |

Source precision, source recall, corroboration accuracy and answer coverage are
**identical** across the range. Only deduplication moves meaningfully (0.1569 →
0.0098 — at 0.7 almost nothing is merged). The overall spread is 0.60pp, roughly
2× noise, with no consistent direction.

**The fusion threshold is not a meaningful lever at this operating point**, and
0.6 sits in the middle of a flat region.

## 4. Corroboration cohort (13 cases)

| Variant | Groundedness | Answer cov. | Corroboration acc. | Overall (cohort) |
|---|---:|---:|---:|---:|
| Graph only | 0.5394 | 0.5000 | 0.9545 | 0.5627 |
| Qdrant only | 0.5744 | 0.5000 | **0.5000** | 0.4876 |
| **Combined** | **0.8067** | 0.5000 | **0.9545** | **0.6448** |

Fusion handles corroboration **correctly and with a large benefit**: groundedness
0.8067 combined versus 0.5394 / 0.5744 alone — +23pp over the better single
source. Corroboration accuracy collapses to 0.5000 without graph, confirming that
the corroboration signal is largely graph-supplied.

## 5. Conflict cohort (4 cases)

| Case | Graph ev. | Qdrant ev. | Final provider order | Answer follows | Correct? |
|---|---:|---:|---|---|---|
| `cross-stack-doc` | 5 | 1 | graph, corporate, tasks | **neither alone** | reasonable — surfaces both |
| `conflict-graph-vs-rag` | 4 | 1 | graph, tasks, corporate | **neither alone** | reasonable — states what documents do/don't say |
| `conflict-doc-vs-memory` | 5 | 1 | corporate, tasks, graph | **neither alone** | reasonable — recommends the recent decision |
| `conflict-two-owners` | **0** | 1 | tasks, corporate | qdrant-only | n/a — no graph evidence |

In the three cases that genuinely carry both sources, the fused answer matches
**neither** single-source answer — the model is synthesising rather than following
one provider. `conflict-graph-vs-rag` explicitly reports what the documents do and
do not contain, which is the desired behaviour.

**Caveat:** only 2–3 of the 4 conflict cases actually present conflicting evidence
from both providers (`conflict-two-owners` and `conflict-stale-fact` resolve no
graph entity). Conflict handling is therefore *suggestively* correct but thinly
evidenced. This is a dataset coverage gap, recorded, not corrected.

## 6. Context accounting (production)

| Provider | Mean tokens | Share of context | Cases present |
|---|---:|---:|---:|
| **tasks** | **717.0** | **59.0%** | 174 / 174 |
| corporate (Qdrant) | 456.0 | 37.5% | 174 / 174 |
| **graph** | **43.1** | **3.5%** | **106 / 174** |

`dropped_for_budget = 0` across all 174 cases — the budget never binds, and the
graph's ~1117-token share cap is never approached.

**Graph receives 3.5% of the context yet removing it costs 6.22pp overall and
9.61pp groundedness.** By contrast `tasks` consumes 59% of every prompt in every
case, including the 106 where graph evidence is present and relevant. Graph
evidence has far more value per token than its allocation reflects — the single
clearest structural finding of this step, and consistent with `top_k = 12`
(more graph tokens) scoring best end-to-end.

## 7. Decision

**1. Does GraphRAG improve end-to-end answer quality?** **Yes.** Removing it costs
6.22pp overall, 9.61pp groundedness, 13.15pp answer coverage — ~200× the noise
control, broad across 106 cases.

**2. Does Qdrant improve end-to-end answer quality?** **Yes, more so.** Removing
it costs 10.13pp overall and 24.02pp groundedness.

**3. Does graph + Qdrant outperform either alone?** **Yes, clearly** — on overall,
groundedness and answer coverage simultaneously.

**4. Does `graph_top_k = 4` improve answers on the real path?** **No — it makes
them worse** (−1.10pp overall, −2.63pp coverage) despite the best graph F1. The
Step 11 conclusion withdrawn in 11.6 is now replaced by a measured one.

**5. Is `graph_top_k = 8` still the safest production value?** **Yes, for now.**
`top_k = 12` is better end-to-end but by +0.59pp overall — about 2× the noise
floor. It is a **candidate for a dedicated repeated-run validation**, not a
change to make on one run.

**6. Does the current fusion threshold remain defensible?** **Yes.** 0.5/0.6/0.7
are indistinguishable on every metric except dedup; 0.6 sits mid-range. Keep it.

**7. Does fusion handle corroboration correctly?** **Yes** — +23pp groundedness
over the better single source, and corroboration accuracy depends on graph.

**8. Does fusion handle conflicts correctly?** **Probably, on thin evidence.** In
all three genuinely two-sided cases the answer synthesises rather than following
one provider. Only 2–3 usable conflict cases exist.

**9. Is graph evidence under-allocated?** **Yes, markedly.** 3.5% of context
tokens for a component whose removal costs 6.22pp, while `tasks` takes 59% in
every case. This is the highest-leverage structural finding.

**10. Is any production change justified?** **No — not on this evidence.** Nothing
clears the §16 bar of a clear, stable win beyond the noise floor. Two candidates
are worth dedicated follow-up: `graph_top_k = 12`, and rebalancing the budget
shares away from `tasks` toward `graph`.

**11. What remains the final GraphRAG weakness?** Two things, in order:
- **Graph token allocation** — 3.5% of context for demonstrable value, while an
  always-on provider consumes 59%. Cheap to test, likely the biggest available win.
- **Graph node ranking** — still freshness-dominated (Step 11). Note this now cuts
  differently: since answer quality *rises* with looser selection, better ranking
  matters less than more graph context. Fixing ranking and raising `top_k` should
  be evaluated together.

**12. Ready for the final quality-gate step?** **Yes.** The harness is causally
truthful, the noise floor is measured, every component has been ablated
end-to-end, and the remaining weaknesses are characterised with specific
follow-ups.

## 8. Methodological note for whoever runs the final step

The most transferable result here is not a number, it is the pattern: **three
consecutive steps optimised graph retrieval metrics, and the end-to-end result
moved in the opposite direction.** `top_k = 4` maximises graph F1 and minimises
answer quality. Any future GraphRAG tuning should be judged on end-to-end
measurements through the corrected provider path, with a repeated-run control, and
should treat retrieval metrics as diagnostics rather than objectives.

## 9. Production integrity

| Check | Value |
|---|---|
| Neo4j total / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| `kgtest-*` residue / canaries | 0 / 6-of-6 ✅ |
| **Qdrant `corporate_memory`** | **993 points — unchanged ✅** |
| Qdrant collections | 20 → **21** ⚠️ see below |
| Corpus / Postgres `documents` | 310 files / 0 rows ✅ |
| `graph_top_k` / `fusion_threshold` / depth in source | 8 / 0.6 / 1 — unchanged ✅ |
| resolver threshold / `hop_decay` / `max_nodes` | 0.82 / 0.55 / 100 — unchanged ✅ |
| Dataset v3.1 hashes | re-verified unchanged ✅ |

**The collection count moved from 20 to 21 during this step, and it was not this
experiment.** The new collection is `user_memory_<uuid>` with 6 points — the
per-user memory pattern the running application creates during normal use (these
went 12 → 14 over the session while a separate service was live). No experiment
harness in Steps 7–12 contains a Qdrant write, create or delete call, verified by
inspection. `corporate_memory` — the corpus collection the invariant exists to
protect — is unchanged at 993 points.

The fixed "20 collections" invariant inherited from Step 6 is therefore **stale by
design**: it counts a number the live application legitimately changes. The
meaningful invariants are `corporate_memory = 993` and "no collection created by
the experiment", both of which hold.
