# Step 11 — Graph Ranking, Top-K and Hop Decay Experiment

**Question:** can graph candidate ranking and top-k selection recover the
already-retrieved relevant nodes without turning the context into noise?

**Answer: no — raising top-k makes things worse, and `hop_decay` is inert.** The
real finding is diagnostic: **graph node ranking inside a neighbourhood is driven
almost entirely by `freshness`**, because three of the five signals are
effectively constant there. Expected nodes lose on document recency, not on
relevance. `top_k` and `hop_decay` are both downstream of that, which is why
neither can fix it.

Production was **not changed**: `graph_top_k = 8`, `hop_decay = 0.55`,
`depth = 1`, `max_nodes = 100`. Nothing staged, nothing committed. The dataset
was not modified.

| Artifact | Path |
|---|---|
| Results | `docs/evaluation/graphrag_graph_ranking_experiment_v1.json` |
| Manifest | `docs/evaluation/graphrag_graph_ranking_experiment_v1_manifest.json` |

**Method.** `api.retrieve(top_k=N)` truncates as `subgraph.nodes[:N]`, so the full
ranked list was captured once per `hop_decay` value and every `top_k` evaluated
exactly in memory. `hop_decay` changes ordering, so it was captured as five real
retrievals. Mentions were pinned to the Step 7 frozen set (restored afterwards),
so graph seeds are identical across every configuration; depth stayed 1 and the
other five ranking weights stayed at production values. End-to-end runs were then
performed at `top_k` = 4, 8, 12 with answers enabled.

---

## A. Top-k curve (depth 1, `hop_decay` 0.55, 100 cases with `expected_graph_nodes`)

| graph_top_k | Precision | Recall | F1 | Noise | Context nodes |
|---:|---:|---:|---:|---:|---:|
| **4** | **0.2375** | 0.5696 | **0.3130** | **0.7625** | 3.36 |
| 6 | 0.1617 | 0.5720 | 0.2363 | 0.8383 | 5.04 |
| **8** *(production)* | 0.1263 | 0.5845 | 0.1955 | 0.8738 | 6.70 |
| 10 | 0.1070 | 0.6030 | 0.1719 | 0.8930 | 8.34 |
| 12 | 0.0992 | 0.6429 | 0.1632 | 0.9008 | 9.98 |
| 16 | 0.0825 | 0.6857 | 0.1409 | 0.9175 | 13.26 |
| 20 | 0.0675 | 0.6957 | 0.1186 | 0.9325 | 16.54 |

**F1 falls monotonically as k rises.** Going 8 → 20 buys +11pp recall and costs
−59pp precision. The Step 10 observation that expected nodes sit at ranks 9–10
is real, but exposing them is not worth what comes with them.

**Marginal value of each additional slot** (§13) — what the extra capacity
actually contains:

| Slots | +expected nodes | +other nodes | Marginal precision |
|---|---:|---:|---:|
| 1–4 | **95** | 241 | **0.2827** |
| 5–6 | 2 | 166 | 0.0119 |
| 7–8 | 4 | 162 | 0.0241 |
| 9–10 | 6 | 158 | 0.0366 |
| 11–12 | 12 | 152 | 0.0732 |
| 13–16 | 13 | 315 | 0.0396 |
| 17–20 | 3 | 325 | 0.0091 |

Slots 1–4 carry 95 of the 135 expected nodes found anywhere in the top 20. The
production window's own slots 5–8 contribute **6 expected nodes against 328
irrelevant ones**. There is a small bump at 11–12 (12 expected), which is the
rank-9–10 population Step 10 identified — but it arrives with 152 noise nodes.

## B. Hop-decay curve (top_k = 8, depth 1)

| hop_decay | Precision | Recall | F1 | Noise | top-8 hit | median expected rank |
|---:|---:|---:|---:|---:|---:|---:|
| 0.35 | 0.1263 | 0.5845 | 0.1955 | 0.8738 | 78/100 | 1.0 |
| 0.45 | 0.1263 | 0.5845 | 0.1955 | 0.8738 | 78/100 | 1.0 |
| **0.55** | 0.1263 | 0.5845 | 0.1955 | 0.8738 | 78/100 | 1.0 |
| 0.65 | 0.1263 | 0.5845 | 0.1955 | 0.8738 | 78/100 | 1.0 |
| 0.75 | 0.1263 | 0.5845 | 0.1955 | 0.8738 | 78/100 | 1.0 |

**`hop_decay` is a complete no-op at depth 1** — bit-identical across the whole
range. This is not a measurement artefact; it is structural.
`ranking.combine()` computes `base * (hop_decay ** hop)`. At depth 1 every
candidate is hop 0 or hop 1, so every hop-1 node receives the *same* multiplier
and the ordering within the hop-1 set cannot change. `hop_decay` can only matter
at depth ≥ 2 — which Step 10 showed contributes nothing while `max_nodes = 100`
holds.

## C. Joint top-k × hop-decay (§11)

**Not run, by design.** A joint grid is degenerate here: since `hop_decay` has
provably zero effect at depth 1, every `top_k × hop_decay` combination collapses
onto the top-k curve in §A. Running the grid would have produced 15–35
configurations that are identical in sets of five. §11 asks for combinations
"justified by observed results"; the observed result is that no hop-decay value
is distinguishable, so none is justified.

## D. Depth-sensitive cohort (18 cases)

| graph_top_k | Precision | Recall | F1 | Noise |
|---:|---:|---:|---:|---:|
| 4 | 0.2639 | 0.3472 | **0.2970** | 0.7361 |
| 6 | 0.1759 | 0.3472 | 0.2315 | 0.8241 |
| **8** | 0.1319 | 0.3472 | 0.1897 | 0.8681 |
| 10 | 0.1333 | 0.4444 | 0.2031 | 0.8667 |
| 12 | 0.1389 | **0.5602** | 0.2207 | 0.8611 |
| 16 | 0.1181 | 0.6343 | 0.1976 | 0.8819 |
| 20 | 0.1000 | 0.6713 | 0.1730 | 0.9000 |

Here raising k *does* help recall — 0.3472 → 0.5602 at k = 12 — and F1 improves
over production (0.1897 → 0.2207). But k = 4 still scores highest.

**Per-case rank of every expected node** (this corrects a Step 10 framing):

| Case | Ranks of expected nodes | Found | k for full recall |
|---|---|---|---|
| `multi-entity-3` | 1, 2, 3 | 3/3 | **3** |
| `hop2-from-postgres` | 1, 9 | 2/2 | **9** |
| `hop3-blast-radius` | 1, 9 | 2/2 | **9** |
| `hop3-shared-dependency` | 1, 2, 12 | 3/3 | **12** |
| `hop2-model-behind` | 1, 11, 14 | 3/3 | 14 |
| `hop2-serving-stack` | 11, 14 | 2/2 | 14 |
| `d2-joseph-graph-db-runtime` | 1, 2, 20 | 3/3 | 20 |
| `d2-agentic-ai-gateway-host` | 1, 42 | 2/3 | never |
| `d2-nazo-upstream-gpu` | 1, 29 | 2/3 | never |
| `d3-yusuf-mentioned-model` | 1, 9 | 2/4 | never |
| `adv-very-long-question` | 38, 39, 40 | 3/5 | never |
| *(7 more)* | — | partial | never |

Step 10 reported a "median expected rank of 9–10". More precisely: the **first**
expected node is usually rank 1 (it is the seed itself); the *second and third*
expected nodes sit at ranks 9–42. Only **4 of 18 cases** reach full recall at
k ≤ 12, and **10 of 18 never reach it at any k**, because some expected nodes are
not retrieved at all.

## E. Ranking diagnostics (§14) — the actual root cause

Signal variance **within a single case's top-12 hop-1 candidates** — the only
place ranking discriminates:

| Signal | Mean within-case stdev | Discriminating? |
|---|---:|---|
| confidence | 0.0047 | **no — constant** |
| corroboration | 0.0134 | **no — constant** |
| frequency | 0.0039 | **no — constant** |
| importance | 0.0475 | weak |
| **freshness** | **0.1809** | **yes — dominant** |

Mean score spread across a case's top 12: **0.0535**; median 0.0496. Ranks are
near-ties decided in the third decimal place.

A representative loss — `hop2-serving-stack`, expected node `qwen-fast` at rank 11:

| Rank | Node | score | confidence | corroboration | freshness | importance | frequency |
|---:|---|---:|---:|---:|---:|---:|---:|
| 6 | Hermes API Server | 0.4780 | 1.00 | 1.00 | **0.06** | 1.00 | 1.00 |
| 7 | Qwen3-VL | 0.4776 | 1.00 | 1.00 | **0.06** | 1.00 | 1.00 |
| 8 | Dar Al Ber Dashboard | 0.4774 | 1.00 | 1.00 | **0.05** | 1.00 | 1.00 |
| **11** | **qwen-fast** *(expected)* | 0.4761 | 1.00 | 1.00 | **0.037** | 1.00 | 1.00 |

Four of five signals are identical to two decimals. `qwen-fast` loses by 0.0019
— **entirely on freshness**. Across all cases: irrelevant nodes inside the top 8
have mean freshness 0.2639; expected nodes pushed outside have 0.0168.

**Graph node ranking is, in practice, a recency sort over near-tied candidates.**
Freshness derives from `last_seen`, i.e. which document was ingested most
recently — information essentially uncorrelated with whether a node answers the
question. That is why precision is 0.125, and it is why neither `top_k` nor
`hop_decay` can repair it: both operate on an ordering that carries almost no
relevance signal.

## F. End-to-end (§16) — only `graph_top_k` varies

| top_k | overall | Graph F1 | Graph P | Graph R | Noise | Ctx nodes | Tokens | Answer kw | Groundedness | Citation |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **4** | **0.6333** | **0.3130** | 0.2375 | 0.5696 | 0.7625 | 2.44 | 1281.3 | 0.3947 | 0.6430 | 1.000 |
| **8** | 0.6213 | 0.1942 | 0.1250 | 0.5830 | 0.8750 | 4.86 | 1281.3 | 0.3947 | 0.6430 | 1.000 |
| 12 | 0.6211 | 0.1622 | 0.0983 | 0.6415 | 0.9017 | 7.21 | 1281.3 | 0.3947 | 0.6430 | 1.000 |

Graph latency 46.2 / 44.0 / 42.1 ms; e2e p95 2755 / 2767 / 2657 ms — flat.
Cases with no failures: 65 / 66 / **68**.

**Answer keyword coverage, groundedness and citation coverage are identical to
four decimals at every k**, and token usage does not move. Tripling the graph
context (2.44 → 7.21 nodes) produces **no measurable downstream change
whatsoever**. That is the same conclusion Step 10 reached about depth, now
reproduced for top-k: the graph context is not currently influencing the answer.

Note the tension in the "no failures" column: k = 12 passes the most cases (68)
while scoring the worst graph F1, because the `graph nodes missing` failure is
recall-based while F1 penalises the noise that comes with it. Both numbers are
reported rather than the flattering one.

## G. `max_nodes` diagnostic (§17) — isolated, not mixed with the above

Depth 3, 8 sample cases, `max_nodes` 100 vs 400:

| max_nodes | mean nodes | hop 0 | hop 1 | hop 2 | **hop 3** |
|---:|---:|---:|---:|---:|---:|
| 100 | 100.0 | 9 | 407 | 384 | **0** |
| 400 | 400.0 | 9 | 407 | 1776 | **1008** |

**`max_nodes = 100` is a hidden architectural ceiling, confirmed.** Depth 3
retrieves zero hop-3 nodes at 100 and 1008 at 400. Step 10's conclusion that
"depth 3 is structurally identical to depth 2" is now established by controlled
comparison rather than inference. Any future depth work must raise this first, or
the depth setting is meaningless.

## H. Production recommendation

**1. Should `graph_top_k` remain 8?** **Yes — keep 8 for now.** Do not raise it:
every value above 8 lowers F1 and raises noise. Lowering it to 4 is the only
change with a positive measured effect, but see (2).

**2. If not, what value?** `top_k = 4` is the sole evidence-backed candidate:
graph F1 0.1942 → **0.3130 (+61% relative)**, precision 0.125 → 0.2375, noise
0.875 → 0.7625, context halved, recall −1.3pp, latency unchanged. It is
**recommended for controlled validation only, not for immediate adoption** —
because the answer-level benefit is exactly zero (§F) and because of the caveat
in (11).

**3. Should `hop_decay` remain 0.55?** **Yes.** It is provably inert at depth 1;
changing it would be a no-op dressed up as a tuning decision.

**4. If not, what value?** Not applicable — no value is distinguishable.

**5. Graph F1 improvement?** +0.1188 absolute (0.1942 → 0.3130) at k = 4;
+0.0310 on the depth cohort at k = 12. Every k > 8 is worse.

**6. Noise?** 0.8750 → **0.7625** at k = 4; rises to 0.9325 at k = 20.

**7. Previously missed expected nodes recovered?** At k = 12, 12 additional
expected nodes across 100 cases, alongside 152 irrelevant ones. In the depth
cohort, full recall goes from 1 case (k=4) to 4 cases (k=12) of 18; 10 of 18 can
never reach it because some expected nodes are never retrieved.

**8. Context size?** 4.86 → **2.44** nodes at k=4; → 7.21 at k=12. Token usage
unchanged at 1281.3 in all cases — the graph is not the binding constraint on
context.

**9. Latency?** Flat: 46.2 / 44.0 / 42.1 ms graph, e2e p95 within 4%.

**10. Does answer quality improve?** **No.** Keyword coverage, groundedness and
citations are identical at every k. `overall` moves +1.20pp at k = 4 purely
because `graph_node_f1` feeds it — a metric improvement, not an answer
improvement.

**11. Is graph ranking now sufficiently characterised?** **Yes, and the answer is
uncomfortable.** The bottleneck is neither depth (Step 10) nor top-k nor
hop-decay (Step 11) — it is that **the ranking signals carry almost no
discriminating information inside a neighbourhood**, leaving freshness to decide.
One caveat stated plainly: mean `expected_graph_nodes` per case is ~2.1, so F1
structurally favours small k, and part of k = 4's advantage is that arithmetic.
The findings that do **not** depend on it are that raising k always hurts, that
hop-decay is inert, and that freshness dominates ranking.

**12. Is Step 12 ready for Graph/Qdrant fusion?** **Yes, with one caveat worth
carrying.** Graph retrieval is now fully characterised across depth, top-k and
hop-decay. But three consecutive experiments have shown that changing the graph
context — deeper, wider, narrower — produces **zero** change in answer keyword
coverage, groundedness or citations. Before or alongside fusion work, it is worth
establishing whether graph context reaches the answer at all; if it does not,
fusion weights between graph and Qdrant will be equally inert.

### What a later ranking-weight step should investigate

Not attempted here (§17 of the brief forbids it), but the diagnostic points
squarely at:

- **`freshness = 0.6`** — the only signal with real within-neighbourhood variance,
  and it is ranking by ingestion recency rather than relevance. Lowering it, or
  making it conditional on the question being time-sensitive, is the single
  highest-value candidate.
- **`confidence`, `corroboration`, `frequency`** — near-constant within a
  neighbourhood (stdev < 0.014). They contribute weight to the mean but no
  ordering information; their weights are effectively unused.
- **A relevance signal that does not exist yet** — nothing in the current five
  measures *"is this node about the question?"*. That, not the weights, is the
  gap.

## I. Dataset note (recorded only — v3.1 frozen, §20)

Ten of the 18 depth-cohort cases can never reach full recall at any k because
some expected nodes are not retrieved at depth 1 at all (e.g.
`d2-agentic-ai-gateway-host` finds 2 of 3; `adv-very-long-question` 3 of 5).
Those expectations describe nodes that are reachable in the graph but not
retrieved under production settings. No expectation was altered.

## J. Production integrity

Verified before and after; snapshots identical.

| Check | Value |
|---|---|
| Neo4j total / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| `kgtest-*` residue | 0 ✅ |
| Canaries | 6/6 ✅ |
| Qdrant `corporate_memory` / collections | 993 / 20, none created ✅ |
| Corpus | 310 files ✅ |
| Postgres `documents` | 0 rows ✅ |
| `graph_top_k` / `hop_decay` / depth / `max_nodes` in source | 8 / 0.55 / 1 / 100 — **all unchanged** ✅ |
| Dataset v3.1 hashes | re-verified unchanged ✅ |

All Cypher was read-only traversal. `top_k` and `hop_decay` were injected through
`RunnerConfig` and `RankingWeights`; `.env` and `config/settings.py` were never
touched, and the mention-extractor patch was in-process only and restored on exit.
