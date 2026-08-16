# GraphRAG v3 Baseline

**Step 6 of the GraphRAG Quality Gate.** The frozen, reproducible starting point
for the Steps 7–12 experiments.

Nothing was tuned. No production GraphRAG behaviour, configuration, schema,
collection or model was changed. Production data verified byte-identical before
and after the run. Nothing staged, nothing committed.

| Artifact | Path |
|---|---|
| Raw report (framework HTML) | `docs/evaluation/graphrag_baseline_v3.html` |
| Raw report (framework JSON) | `docs/evaluation/graphrag_baseline_v3.json` |
| Failure classification | `docs/evaluation/graphrag_baseline_v3_classification.json` |
| Reproducibility manifest | `docs/evaluation/graphrag_baseline_v3_manifest.json` |
| This report | `docs/evaluation/graphrag_baseline_v3.md` |

Produced with the existing CLI, not a parallel runner:

```
python run_evals.py --with-answers --report docs/evaluation/graphrag_baseline_v3.html
```

---

## A. Configuration

| | |
|---|---|
| git commit | `432fd5e` (`feat/enterprise-agentic-os`, working tree dirty — see §Git) |
| dataset | v3, 174 cases |
| dataset sha256[:16] | `core_retrieval` `cc9e9f2ddf118785` · `enterprise` `6bddf86935b8c31c` · `discrimination` `dad30e27249f643b` |
| GRAPH_RETRIEVAL_DEPTH | **1** |
| resolver fuzzy threshold | **0.82** |
| fusion threshold | **0.6** |
| graph_top_k / qdrant_top_k | **8** / **5** |
| ranking weights | confidence 1.0 · corroboration 1.2 · freshness 0.6 · importance 0.8 · frequency 0.7 · hop_decay 0.55 |
| LLM | `qwen-fast` via `http://localhost:4000/v1` (timeout 120s); KG extract `qwen-extract` |
| embedding | `BAAI/bge-small-en-v1.5`, dim 384 |
| Qdrant | `http://localhost:6333`, collection `corporate_memory` |
| Neo4j | `bolt://localhost:7687`, db `neo4j` |
| eval identity | `user_1` / session `eval` |
| runtime | 303s for 174 cases |

The runner's `graph_depth` default (1) was verified to equal production
`GRAPH_RETRIEVAL_DEPTH` (1), so the baseline reflects production rather than a
framework default that happens to differ.

## B. Dataset

v3 = 174 cases. **v2 confirmed frozen**: all Step-4 invariants re-verified
unchanged (116 cases, `dataset_version: 2`, calendar in 109 sources / **0**
orders, 14 `requires:depth-*`, 8 `requires:semantic-resolution`, 11
`requires:provider-*`, exactly 3 `forbidden_answer_keywords` cases).
`archive/v1/` still byte-identical to `git show HEAD:`. No case was added,
removed or edited during Step 6.

## C. Headline — and why it must not be read alone

```
overall  61.7%   over 174 cases
```

| Outcome | Cases |
|---|---:|
| pass | 65 |
| expected failures (known-limitation / experiment) | 40 |
| infrastructure-dependent | 8 |
| **unexpected failures** | **61** |

The 61 does **not** mean 61 production regressions. Decomposed:

| Sub-class | Cases | What it actually is |
|---|---:|---|
| Question never names the expected entity | 13 | latent inference capability, untagged in v2 |
| Blocked by the Qdrant document-id defect | 18 | **evaluation defect**, not retrieval |
| Only a stale `expected_provider_order` | 11 | v2 dataset staleness |
| **Genuine production failures** | **21** | the real baseline signal |

## D. Component metrics

**Entity resolution** — n=140 · precision 0.729 · recall 0.736 · F1 0.733 (median 1.000).
The median is a perfect score: resolution is either exactly right or absent.

**Graph** — n=100 · precision **0.125** · recall 0.583 · **F1 0.194** · noise ratio
**0.875** · nDCG 0.596 · hop_accuracy 1.000. Relationship precision 0.020, recall 0.289 (n=26).

**Retrieval (Qdrant)** — n=21 · P@5 0.143 · R@5 0.143 · MRR 0.143. **Not trustworthy** — see §E.

**Fusion** — n=173 · source precision **0.962** · recall 0.960 · corroboration accuracy
0.955 (n=11) · duplicate reduction 0.124. The strongest component by a wide margin, and
direct confirmation that the Step-4 calendar correction was right.

**Ranking** — provider-order correlation 0.391 (n=113) · ranking nDCG 0.451 (n=38).

**Answer** — keyword coverage 0.395 (n=38) · groundedness 0.652 · citation coverage **1.000**.

**Negative premise** — **7/7 refused correctly**, including all three v2 adversarials and
all four v3 additions. The Step-4 scoring mechanism works end-to-end and production
does not affirm false premises.

**Budget** — compression 0.088 · budget utilisation 0.288 · mean 1286 tokens ·
`context_nodes` mean 4.9 (max 8).

### Performance

| stage | mean ms |
|---|---:|
| entity resolution | 274.6 |
| graph | 283.6 |
| providers | 362.7 |
| fusion | 2.5 |
| ranking | 0.6 |
| compression | 1.5 |
| LLM answer | 815.3 |
| **total** | **1741.0** |

End-to-end p50 **1438.7ms** · p95 **3794.5ms** · max 8277.8ms.

## E. Failure classification

### Genuine production failures — 21

**15 of 21 are `graph nodes missing`.** This is the single dominant production
weakness and it is *not* a depth problem.

Evidence: `graph_top_k=8` and 83 of 100 graph cases returned exactly 8 nodes,
while the mean number of *expected* nodes is 2.1 and only **one** case expects
more than 8. So capacity is not the constraint — **the ranker is selecting the
wrong 8**. Concretely, `d1-neo4j-hosted-on` seeds on Neo4j (25 one-hop
neighbours, including `docker` and `db-primary-01`) and returns
`[Neo4j, Knowledge Graph, CCTV Analytics, Context Engine, Sneha Pillai,
Hermes API Server, Dar Al Ber Dashboard, Agentic AI]` — neither `HOSTED_ON`
neighbour survives ranking.

That is what `graph_node_precision = 0.125` / `graph_noise_ratio = 0.875` mean:
**at depth 1, roughly one in eight returned graph nodes is relevant.** Three of
the depth-1 *control* cases written in Step 5 to be trivially satisfiable
(`d1-litellm-routes-to`, `d1-neo4j-hosted-on`, `d1-joseph-works-on`) fail this
way, which is exactly the signal those controls exist to produce.

Remaining genuine failures: 3 resolver misses on questions that *do* name the
entity (`unicode-entity` "What is Café Neo4j?", `conflict-two-owners`,
`adv-leading-person` — all fail to resolve an entity the question states),
1 negative leak (`neg-google-founder` resolved `Google`), 1 answer-grounding
miss (`negp-wrong-person-role` did not surface `Ayesha Khan`), 1 stale order.

### Evaluation/scoring defects — 1 (affecting 18 cases)

**`runner.py:259` discards the document identifier the dataset uses.**

```python
trace.qdrant_documents = [
    (i.metadata.get("document_id") or i.source or "") for i in corporate]
```

`document_id` is a UUID; `i.source` is the filename the cases reference. Verified
directly: for the qwen-fast benchmark question the provider returns
`source='emd-0293-gpu_benchmark-…-int4-awq.md'` **and**
`document_id='347c5512-d227-5ab8-9eed-4a69310d5f8f'`, and the UUID wins.

**Retrieval is actually correct** — `emd-0293` is retrieved and ranked first — but
the metric scores 0. This is **pre-existing**, not introduced in Step 4/5: it
affects all 11 v2 `rag-*` cases equally. The entire Qdrant metric family
(P@5/R@5/MRR ≈ 0.143) is therefore measuring an identifier mismatch, not
retrieval quality. Legacy documents lacking a `document_id` fall through to
`source` and score normally, which is why the family is not exactly 0.

Per §7 this was **investigated and labelled, not fixed** — fixing it after seeing
failures would change what the baseline measures. It must be fixed before any
fusion or ranking experiment, since both depend on Qdrant metrics.

### Dataset defects found (recorded, not corrected — v3 is frozen)

1. **11 cases fail only on a stale `expected_provider_order`.** Step 4 verified
   that calendar's *absence* from that field was harmless (`rank_correlation`
   compares only shared items) but did not check the *relative* order of the
   items already there. Observed `[corporate, calendar, tasks]` vs expected
   `[tasks, corporate]` — `tasks` and `corporate` are genuinely swapped.
2. **13 v2 cases expect an entity the question never names** (`corrob-strongest`
   → LiteLLM, `ambig-model` → qwen-fast, `long-full-arch` → Agentic AI, …).
   These are structurally identical to the tagged mention-extraction cohort but
   carry no `requires:` tag, so they masquerade as production failures.
3. **`res-tp-0824-graph-database` is mis-classified in v3.** Step 5 measured
   `graph database` → Neo4j at 0.8235 by probing the registry *directly*. The
   end-to-end pipeline never gets there: mention extraction suppresses the
   generic noun, so nothing resolves. It is a mention-extraction case, not a
   threshold case. My Step 5 note that it "passes today" was measured at the
   wrong layer.

### Infrastructure-dependent — 8

All 8 are `requires:provider-memory` (5) and `requires:provider-email` (2) plus
1 calendar case. Reported separately; not counted as production failures.

## F. Resolver boundary (Step 7/8 starting point)

| Case | Step 5 measured | Observed now | Verdict |
|---|---|---|---|
| `res-tp-0800-k8` (K8 → Kubernetes) | 0.8000 | resolved `[]` | expected-fail ✓ |
| `res-tn-0800-db-collision` (DB → Dar Al Ber) | 0.8000 | resolved `[]` | **pass** ✓ |
| `res-tn-0857-vlm` (VLM → vLLM) | 0.8571 | resolved **`['vLLM']`** | expected-fail ✓ — false positive reproduced |
| `poll-docker-compose` | 0.8125 (Document) | resolved `[]` | expected-fail ✓ |
| `res-tp-0818-tailscale-vpn` | 0.8182 | resolved `['Tailscale']` | **pass** |
| `res-tp-0833-qdrent` | 0.8333 | resolved `['Qdrant']` | pass |
| `res-tp-0870-ajman-polis` | 0.8696 | resolved `['Ajman Police']` | pass |
| `res-tp-0824-graph-database` | 0.8235 | resolved `[]` | **unexpected** — mis-classified, see §E |

The 0.8000 collision pair behaves exactly as designed: `K8` does not resolve and
`DB` does not resolve to *Dar Al Ber Society*. The `VLM` → `vLLM` false positive
above threshold is reproduced end-to-end. **The boundary is real and both costs
are now measured.**

`res-tp-0818-tailscale-vpn` passing is worth noting: Step 5 measured 0.8182 —
below threshold — yet it resolves end-to-end, because mention extraction emits
`Tailscale` rather than `Tailscale VPN`, which then hits an exact rung. Another
instance of registry-layer measurements not predicting pipeline behaviour.

## G. Semantic stage breakdown

Reported by **stage**, not lumped as "semantic resolver failures":

| Stage | Cases |
|---|---|
| **mention extraction** (6) | `syn-vector-db`, `syn-graph-db`, `syn-gateway`, `syn-relational`, `syn-mail-api`, `mext-generic-noun-suppressed` — all resolved `[]` |
| **entity resolution — no lexical path** (2) | `sem-embedding-model` → resolved **`['Model']`**, `sem-speech-to-text` → `[]` |
| **entity resolution — empty candidates** (1) | `sem-object-storage` → `[]` |
| **entity resolution — threshold** (1) | `abbrev-api` → resolved **`['API']`** |
| **entity resolution — wrong entity** (1) | `abbrev-db` → resolved `['Knowledge Graph']` |
| **already passing** (1) | `abbrev-llm` → resolved `['LiteLLM']` |

Two new observations from the end-to-end run: `sem-embedding-model` resolves to a
graph entity literally named **`Model`**, and `abbrev-api` to one named **`API`**.
Generic-noun entities in the graph act as attractors — a distinct failure mode
from "no candidate found", and relevant to both Step 8 (pollution) and Step 9.

**Consequence for Step 9 (unchanged from Step 5, now confirmed end-to-end):** a
semantic resolver placed in the registry cannot move the 6 mention-extraction
cases. Add the 13 untagged v2 inference cases from §E and the extraction stage is
the larger population.

## H. Graph-depth cohort at depth=1

| | |
|---|---:|
| depth-1 satisfiable cases | 67 (30 pass, 29 unexpected failures) |
| latent depth-2 cases | 14 — **0** incidentally passing |
| latent depth-3 cases | 6 — **0** incidentally passing |
| latent depth-4 cases | 1 — **0** incidentally passing |

The 21 latent cases are reported as **capability cases not expected to pass at
depth=1**, not as regressions. That none passes incidentally is a good property:
the cohort is clean, so any movement in Steps 10–11 is attributable to the depth
change rather than to noise.

Depth 2 and 3 were **not** run, per §8.

### §8 graph-metric verification (depth=1, graph-expectation cases only, n=100)

| metric | mean |
|---|---:|
| graph_node_precision | 0.1250 |
| graph_node_recall | 0.5830 |
| **graph_node_f1** | **0.1942** |
| **graph_noise_ratio** | **0.8750** |
| context_nodes | 6.7 (expected relevant: 2.1) |

All three Step-5 metrics are computed and populated on real runs, and
`graph_node_f1` is the value entering `overall`. Had `overall` still averaged
bare recall, the graph family would read 0.583 instead of 0.194 — a 3× flattering
number that would have hidden the dominant production weakness. The Step-5
scoring change is doing exactly what it was added for.

## I. Production integrity

Verified before **and** after the run; the two snapshots are byte-identical.

| Check | Pre | Post |
|---|---|---|
| Neo4j total nodes | 523 | 523 ✅ |
| Neo4j `:Entity` | 522 | 522 ✅ |
| Neo4j relationships | 3414 | 3414 ✅ |
| Neo4j `:Document` | 101 | 101 ✅ |
| `kgtest-*` residue | 0 | 0 ✅ |
| Canary entities | 6/6 | 6/6 ✅ |
| Qdrant `corporate_memory` | 993 | 993 ✅ |
| Qdrant collections | 20 | 20 ✅ |
| Corpus files | 310 | 310 ✅ |
| Postgres `documents` | 0 rows | 0 rows ✅ |

The harness was audited for writes before running: no `MERGE`/`CREATE`/`DELETE`/
`upsert`/`register_alias`/`save_alias_table` anywhere in the runner, scoring or
provider path. `--seed` and `--teardown` (which *do* write) were not used.

## J. Decision

**1. Is v3 a trustworthy baseline?** Yes for entity resolution, graph, fusion,
ranking, answer and negative-premise. **No for the Qdrant family**, which is
measuring an identifier mismatch rather than retrieval. Trustworthy *as recorded*,
because the defect is identified, quantified and bounded to 18 cases.

**2. Largest current GraphRAG weaknesses**, in order:
- **Graph node ranking at depth 1** — precision 0.125, noise 0.875, 15 of 21
  genuine failures. The ranker returns 8 nodes and the directly-relevant one-hop
  neighbours are frequently not among them. Capacity is not the constraint.
- **Mention extraction** — 6 tagged cases plus 13 untagged v2 cases die before the
  resolver is called. This is a larger population than resolver-threshold misses.
- **Relationship retrieval** — precision 0.020, recall 0.289 (n=26); weaker than
  node retrieval and largely unexamined.
- **Answer keyword coverage 0.395** with groundedness 0.652 — retrieved context
  is not reliably reaching the answer.

**3. Unexpected production failures remaining: 21** (of 61 raw unexpected; the
other 40 are 18 evaluation-defect, 13 latent-inference, 11 stale-order — with
overlap resolved in favour of the more specific class).

**4. Ready for Step 7?** Yes, with one precondition: **fix `runner.py:259`
first** and re-run, or the fusion and ranking experiments will optimise against a
metric that cannot move. The resolver-threshold experiment (Step 7) itself is
unblocked — its boundary cases all behave as designed.

**5. What must NOT change before Step 7:**
- v1, v2, v3 case files, and the three dataset hashes above
- resolver threshold 0.82, graph depth 1, fusion threshold 0.6, graph_top_k 8,
  qdrant_top_k 5, and the six ranking weights
- `graph_node_f1` in `overall`, and `graph_noise_ratio` / `context_nodes`
- the production Neo4j, Qdrant and corpus state recorded in §I
- this baseline JSON/HTML — Step 7 must compare against it, not regenerate it

Two changes are recommended *before* Step 7 rather than during it, each recorded
here and deliberately not applied: the `runner.py:259` identifier fix, and
tagging the 13 untagged inference cases so they stop reading as production
failures.

---

### Git

Working tree is dirty by design: Steps 4–6 are uncommitted, alongside ~27 files
belonging to another developer (weather/tool-prefs/YouTube/websearch/embeds),
which were not touched. Files intentionally produced by Step 6:

```
docs/evaluation/graphrag_baseline_v3.html
docs/evaluation/graphrag_baseline_v3.json
docs/evaluation/graphrag_baseline_v3.md
docs/evaluation/graphrag_baseline_v3_classification.json
docs/evaluation/graphrag_baseline_v3_manifest.json
```

No source file was modified in Step 6. Nothing staged, nothing committed.
