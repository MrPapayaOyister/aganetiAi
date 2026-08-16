# Step 7 — Resolver Threshold Experiment

**Question:** is `fuzzy_threshold=0.82` close to optimal, or is resolver quality
constrained by candidate quality, mention extraction and document pollution
rather than by the threshold itself?

**Answer: keep 0.82.** Threshold tuning is a low-leverage knob here. It can move
only 11% of the resolver cohort, the best achievable F1 gain is **+1.0pp**, and
every threshold that buys that gain simultaneously introduces a wrong resolution
— including a Document node resolving as an entity. The cases that matter most
are decided *before* the threshold is consulted.

Production configuration was **not changed**. `fuzzy_threshold` is still 0.82 in
source. Nothing staged, nothing committed.

| Artifact | Path |
|---|---|
| Results | `docs/evaluation/graphrag_resolver_threshold_experiment_v1.json` |
| Manifest | `docs/evaluation/graphrag_resolver_threshold_experiment_v1_manifest.json` |
| This report | `docs/evaluation/graphrag_resolver_threshold_experiment_v1.md` |
| Baseline (unmodified) | `docs/evaluation/graphrag_baseline_v3_corrected.*` |

---

## A. Experiment configuration

| | |
|---|---|
| baseline | corrected v3.1, 174 cases, sha `993520c44c8379a2` |
| dataset hashes | `cc9e9f2ddf118785` / `26435e37ec9f4d71` / `ce0cb2992228971f` — verified |
| production threshold | **0.82** (unchanged in source; injected per run only) |
| thresholds tested | 0.75, 0.78, 0.80, 0.82, 0.85, 0.88, 0.90 |
| held constant | depth 1 · fusion 0.6 · `graph_top_k` 8 · `qdrant_top_k` 5 · weights 1.0/1.2/0.6/0.8/0.7/0.55 · `qwen-fast` · `bge-small-en-v1.5` · `corporate_memory` |

### Mentions were frozen (spec §16)

Mention extraction is an LLM call. If it re-ran per threshold, the experiment's
independent variable would be the model's variability, not the threshold. So
mention extraction ran **once** across all 174 cases and the result was reused
verbatim for every threshold.

The resolution ladder then splits cleanly: rungs 1–4 (cache, normalizer, slug id,
exact name) are threshold-independent, and only rung 5 (fuzzy) consults
`fuzzy_threshold`. `fuzzy_candidates()` itself never reads the threshold — it
returns similarity-scored candidates and `resolve()` compares
`candidates[0].similarity` against it. Capturing `(deterministic_hit, candidates)`
once therefore lets every threshold be evaluated from byte-identical inputs.

**That equivalence was validated, not assumed:** the analytic model was checked
against real `CanonicalEntityRegistry` instances built at 0.75, 0.82 and 0.90
across all 99 unique mentions — **0 mismatches**.

The validation earned its place. Its first run found a genuine modelling error:
`resolve()` normalizes a mention *before* fuzzy matching
(`fuzzy_candidates(canonical or raw)`), so `M365` → `Microsoft 365` matches
Microsoft at 0.8182, while the raw string matches nothing. The capture was
mirroring `resolve()` incorrectly; had it not been caught, every number below
would have been subtly wrong.

## B. Threshold curve — primary resolver cohort (118 cases)

| Threshold | Precision | Recall | F1 | Correct | Wrong | Unresolved | No mention |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.75 | 0.9000 | **0.9070** | 0.9035 | 103 | 6 | 6 | 3 |
| **0.78** | 0.9134 | 0.8992 | **0.9063** | 103 | 5 | 7 | 3 |
| 0.80 | 0.9127 | 0.8915 | 0.9020 | 102 | 5 | 8 | 3 |
| **0.82** *(production)* | 0.9256 | 0.8682 | 0.8960 | 100 | **3** | 12 | 3 |
| 0.85 | 0.9322 | 0.8527 | 0.8907 | 98 | 3 | 14 | 3 |
| 0.88 | 0.9397 | 0.8450 | 0.8898 | 98 | **2** | 15 | 3 |
| 0.90 | **0.9391** | 0.8372 | 0.8852 | 97 | 2 | 16 | 3 |

Entity-level counts:

| Threshold | TP | FP | FN |
|---:|---:|---:|---:|
| 0.75 | 117 | 13 | 12 |
| 0.78 | 116 | 11 | 13 |
| 0.80 | 115 | 11 | 14 |
| 0.82 | 112 | 9 | 17 |
| 0.85 | 110 | 8 | 19 |
| 0.88 | 109 | 7 | 20 |
| 0.90 | 108 | 7 | 21 |

The curve is flat and monotone: precision rises and recall falls smoothly, F1
peaks at 0.78 at **0.9063 vs 0.8960** at production — a gain of **+1.03pp**.

**Wrong-resolution rate, reported separately (spec §9):**

| Threshold | 0.75 | 0.78 | 0.80 | **0.82** | 0.85 | 0.88 | 0.90 |
|---|---:|---:|---:|---:|---:|---:|---:|
| wrong resolutions | 6 | 5 | 5 | **3** | 3 | 2 | 2 |
| wrong rate | 5.1% | 4.2% | 4.2% | **2.5%** | 2.5% | 1.7% | 1.7% |

Production sits at the knee: below it wrong resolutions rise 67% (3 → 5), above
it F1 falls without ever eliminating them.

## C. Family breakdown — correct resolutions

| family | n | 0.75 | 0.78 | 0.80 | **0.82** | 0.85 | 0.88 | 0.90 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| exact | 81 | 73 | 73 | 73 | **73** | 73 | 73 | 73 |
| alias | 11 | 11 | 11 | 11 | **11** | 11 | 11 | 11 |
| typo | 9 | 9 | 8 | 8 | **7** | 6 | 5 | 5 |
| fuzzy-band | 14 | 10 | 10 | 9 | **8** | 6 | 6 | 5 |
| document-pollution | 7 | 3 | 3 | 3 | **3** | 3 | 3 | 3 |
| abbreviation | 1 | 1 | 1 | 1 | **0** | 0 | 0 | 0 |

**`exact` and `alias` are completely flat.** 92 of 118 cohort cases — 78% — are
decided at rungs 1–4 and are untouchable by any threshold. All movement is
confined to `typo` (9) and `fuzzy-band` (14), and `document-pollution` never
improves at any threshold.

## D. Boundary cases

### The 0.8000 collision — proven impossible (spec §11)

| Threshold | `K8` → Kubernetes (want: resolve) | `DB` → Dar Al Ber (want: decline) | both satisfied |
|---:|---|---|---|
| 0.75 | ✅ resolves | ❌ resolves *Dar Al Ber Society* | **no** |
| 0.78 | ✅ resolves | ❌ resolves | **no** |
| 0.80 | ✅ resolves | ❌ resolves | **no** |
| 0.82 | ❌ declines | ✅ declines | **no** |
| 0.85–0.90 | ❌ declines | ✅ declines | **no** |

**No threshold in the tested range satisfies both**, exactly as the Step 5
measurement predicted. Both sit at similarity 0.8000, so a scalar cutoff cannot
separate them — this is a property of the candidate scores, not of the cutoff.
Any fix must change candidate generation or scoring, not the threshold.

### VLM → vLLM (0.8571, want: decline)

Wrong at 0.75 → 0.85; correct only at **0.88 and above**. But 0.88 costs
`res-tp-0870-ajman-polis` (0.8696, a legitimate client-name transliteration) and
0.90 additionally costs `res-tp-0889-litellm-gate` (0.8889). Eliminating this one
false positive by threshold means discarding real matches above it.

### docker compose → docker-compose.yml (0.8125, want: not-a-Document)

Resolves the **pure Document node** at 0.75/0.78/0.80; correctly declines at
0.82+. The Step 5 prediction is confirmed exactly: *lowering the threshold to win
`K8` starts resolving documents as entities.* Note that even at 0.82 the case is
`NO_RESOLUTION`, not correct — the real `Docker` entity is never reached, which is
a candidate-ranking failure the threshold cannot address.

### `res-tp-0824-graph-database` — a mention-extraction case, not a threshold case

**Mentions extracted: `[]`.** The resolver is never invoked, so its outcome is
identical at all seven thresholds. Step 5 measured 0.8235 at the *registry* layer
by probing it directly; end-to-end, the extractor suppresses the generic noun and
the phrase never reaches the registry. It is correctly excluded from the primary
cohort (tagged `requires:latent-inference` in v3.1) and **must not be counted as
a threshold miss**.

## E. Stage analysis (spec §6)

Of **99 unique mentions** extracted across the dataset:

| Stage | Mentions | Share |
|---|---:|---:|
| resolve at rungs 1–4 — **threshold-immune** | 42 | 42% |
| reach the fuzzy rung — **threshold-active** | 49 | 49% |
| produce **no candidate at all** | 8 | 8% |

Three resolver-cohort cases extract no mention whatsoever
(`poll-compliance-matrix`, `poll-environment-matrix`, `long-full-arch`); the
resolver was invoked for 115 of 118.

**Only 13 of 118 cohort cases (11%) change outcome anywhere in 0.75–0.90.** The
remaining 105 are fixed regardless of the threshold. That single number is the
core finding: the threshold is a small lever attached to a small part of the
system.

## F. Candidate pollution (spec §12)

| Threshold | FP entities | Document FPs | pure-Document FPs |
|---:|---:|---:|---:|
| 0.75 | 13 | 1 | 1 |
| 0.78 | 11 | 1 | 1 |
| 0.80 | 11 | 1 | 1 |
| **0.82** | **9** | **0** | **0** |
| 0.85 | 8 | 0 | 0 |
| 0.88 | 7 | 0 | 0 |
| 0.90 | 7 | 0 | 0 |

Threshold and pollution are **not independent**: every threshold below 0.82
admits a pure Document node as an entity. Production is the highest-recall
setting that keeps Document pollution at zero.

### Threshold-invariant wrong resolutions

Measuring the graph neighbourhood each wrong seed injects:

| Wrongly resolved entity | 1-hop nodes injected | Present at |
|---|---:|---|
| **Knowledge Graph** (`abbrev-db`) | **69** | all thresholds |
| vLLM (`res-tn-0857-vlm`) | 24 | 0.75 – 0.85 |
| Dar Al Ber Society (`res-tn-0800-db-collision`) | 12 | 0.75 – 0.80 |
| Google (`neg-google-founder`) | 6 | all thresholds |
| Model (`conflict-doc-vs-memory`) | 5 | all thresholds |
| Microsoft (`res-tn-0750-graph-microsoft`) | 5 | 0.75 only |
| docker-compose.yml (`poll-docker-compose`) | 2 | 0.75 – 0.80 |

**Three of the seven distinct wrong resolutions occur at every threshold**, and
the single worst contaminator — `DB`/"knowledge graph" resolving to *Knowledge
Graph*, injecting a 69-node neighbourhood — is completely threshold-invariant. A
wrong resolution is more damaging than an unresolved one precisely because of
this: it seeds a whole irrelevant subgraph into the context.

## G. What each threshold actually buys, vs 0.82

| Threshold | net | gained | lost | new wrong |
|---:|---:|---|---|---|
| 0.75 | **+3** | qdrant-typo, litellm-proxy-server, K8, cctv-analysis, typo-neo4j | DB-collision, graph-microsoft | Dar Al Ber, Microsoft, docker-compose.yml |
| 0.78 | **+3** | litellm-proxy-server, K8, cctv-analysis, typo-neo4j | DB-collision | Dar Al Ber, docker-compose.yml |
| 0.80 | **+2** | K8, cctv-analysis, typo-neo4j | DB-collision | Dar Al Ber, docker-compose.yml |
| 0.85 | **−2** | — | qdrent, whisper-large-v3 | — |
| 0.88 | **−2** | VLM | qdrent, whisper-large-v3, ajman-polis | — |
| 0.90 | **−3** | VLM | qdrent, whisper-large-v3, ajman-polis, litellm-gate | — |

## H. Recommendation

**1. Is 0.82 optimal?** Not by F1 alone — 0.78 scores +1.03pp higher. But 0.82 is
the best *defensible* operating point: it is the lowest threshold at which
Document pollution is zero and the wrong-resolution rate is at its knee (2.5%).

**2. Best alternative candidate?** 0.78, if recall were the only concern. It is
**not recommended**.

**3. Recall gained at 0.78:** +3.10pp (0.8682 → 0.8992); 4 cases gained, 1 lost,
net +3 of 118.

**4. Precision lost at 0.78:** −1.22pp (0.9256 → 0.9134).

**5. Additional wrong resolutions at 0.78:** +2 (3 → 5, a 67% increase in the
wrong-resolution rate) — `DB` → *Dar Al Ber Society* and `docker compose` →
*docker-compose.yml*.

**6. Document pollution at 0.78:** 1 pure-Document false positive, up from **0**.
Production is currently the boundary at which this is exactly zero.

**7. Does threshold tuning solve the resolver problem?** **No.**
- 78% of the cohort resolves at rungs 1–4 and cannot be affected at all.
- Only 11% of cases change outcome anywhere in 0.75–0.90.
- The 0.8000 collision is **provably** unsolvable by any cutoff.
- The worst contaminator (69 injected nodes) is threshold-invariant.
- 9 of the 12 semantic cases and 6 of the 9 latent-inference cases never reach
  the resolver at all — they die at mention extraction.

**8. Should production remain at 0.82?** **Yes.** Do not change it. The available
F1 gain is +1.0pp and is paid for with a 67% increase in wrong resolutions plus
the reintroduction of Document pollution — a bad trade when a wrong seed injects
an entire irrelevant neighbourhood into the context.

**9. What should Step 8 investigate?** Candidate quality, which is where the
evidence points:
- **Exclude or down-weight `:Document` nodes in candidate generation** — but note
  the 7 hybrid nodes (e.g. `ajman-police`, 8 labels) that would break under a
  naive exclusion; `poll-hybrid-ajman-police` guards exactly this.
- **Separate the 0.8000 collision by a signal other than string similarity** —
  label, degree, or type compatibility with the mention. No cutoff can do it.
- **Investigate `abbrev-db`'s 69-node contamination** — the largest single
  measured pollution source, and threshold-invariant.
- **Mention extraction** is arguably higher-leverage still: it silently discards
  the input for 15 cases across the semantic and latent-inference cohorts, which
  no candidate or threshold change can recover.

## I. Excluded cohorts — reported by stage, not as threshold failures (§13)

**Semantic (12):** 9 are `MENTION_NOT_EXTRACTED`; `abbrev-api` and `abbrev-db`
are wrong-entity resolutions (`M365`→*Microsoft*/`API`; `DB`→*Knowledge Graph*);
`abbrev-llm` already passes and is the only threshold-sensitive member.

**Latent inference (9):** 6 `MENTION_NOT_EXTRACTED`, `conflict-doc-vs-memory`
resolves to *Model*, `conflict-stale-fact` `NO_RESOLUTION`,
`fus-qdrant-semantically-near-miss` extracts `INC-1259` but has **no lexical
candidate**. Only `cross-stack-doc` is threshold-sensitive.

**Infrastructure (11):** 7 `MENTION_NOT_EXTRACTED`, 2 `NOT_APPLICABLE`, 1 correct.
None is threshold-sensitive.

Excluding these is what keeps Step 7 from being contaminated by Step 9: had the
12 semantic cases been scored in the primary cohort, they would have depressed
every threshold equally and made the curve look worse without changing its shape.

## J. Production integrity

Verified before and after; snapshots identical.

| Check | Value |
|---|---|
| Neo4j total / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| `kgtest-*` residue | 0 ✅ |
| Canaries | 6/6 ✅ |
| Qdrant `corporate_memory` / collections | 993 / 20, none new ✅ |
| Corpus | 310 files ✅ |
| Postgres `documents` | 0 rows ✅ |
| `data_vault/kg_aliases.json` | unmodified ✅ |

The experiment used only `resolve(allow_fuzzy=False)`, `fuzzy_candidates()`,
`warm()` and read-only Cypher. `register_alias()` and `save_alias_table()` were
never called, so the persisted alias table is untouched. No production file,
`.env` value or service configuration was modified; the threshold was injected
per run into throwaway registry instances.
