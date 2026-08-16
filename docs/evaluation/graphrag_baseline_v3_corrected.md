# GraphRAG v3 Baseline — Corrected

> **This is the corrected baseline after fixing evaluation defects. The original
> Step 6 baseline remains preserved for auditability** at
> `docs/evaluation/graphrag_baseline_v3.{md,json,html}` and is unmodified.

Two evaluation-layer defects found during Step 6 are fixed here. **No GraphRAG
behaviour was tuned or changed**: resolver threshold, graph depth, fusion
threshold, ranking weights, `graph_top_k`, `qdrant_top_k`, models and collections
are byte-identical to Step 6, verified programmatically. Production data verified
unchanged before and after. Nothing staged, nothing committed.

| Artifact | Path |
|---|---|
| Corrected report (HTML) | `docs/evaluation/graphrag_baseline_v3_corrected.html` |
| Corrected report (JSON) | `docs/evaluation/graphrag_baseline_v3_corrected.json` |
| Corrected classification | `docs/evaluation/graphrag_baseline_v3_corrected_classification.json` |
| Corrected manifest | `docs/evaluation/graphrag_baseline_v3_corrected_manifest.json` |
| Pre-correction dataset snapshot | `backend/evals/cases/archive/v3.0/` |
| **Superseded (preserved)** | `docs/evaluation/graphrag_baseline_v3.*` |

Produced with the same command and configuration as Step 6:

```
python run_evals.py --with-answers --report docs/evaluation/graphrag_baseline_v3_corrected.html
```

| | |
|---|---|
| dataset | v3, **correction v3.1**, 174 cases |
| dataset sha256[:16] | `core_retrieval` `cc9e9f2ddf118785` (unchanged) · `enterprise` `26435e37ec9f4d71` · `discrimination` `ce0cb2992228971f` |
| pre-correction sha | `cc9e9f2ddf118785` / `6bddf86935b8c31c` / `dad30e27249f643b` — archived, matches the Step 6 manifest |
| git commit | `432fd5e` (`feat/enterprise-agentic-os`) |
| config | depth **1** · threshold **0.82** · fusion **0.6** · top_k **8**/**5** · weights 1.0/1.2/0.6/0.8/0.7/0.55 |
| models | `qwen-fast` @ `:4000` · `qwen-extract` · `bge-small-en-v1.5` (384) · `corporate_memory` |
| runtime | 294s |

---

## A. Evaluation defect: document identity

**Root cause.** `runner.py` collapsed a retrieved chunk's two identities into one
and kept the wrong one:

```python
trace.qdrant_documents = [
    (i.metadata.get("document_id") or i.source or "") for i in corporate]
```

`document_id` is a storage UUID (`347c5512-…`); `i.source` is the filename the
golden cases name (`emd-0293-…-int4-awq.md`). Because `document_id` won, a case
naming the filename could never match — even when that exact document was
retrieved and ranked first. Verified directly against the provider: for the
qwen-fast benchmark question it returns both, and the UUID was emitted.

The defect is **pre-existing**, not introduced in Steps 4–6: it affects the 11
v2 `rag-*` cases identically. Documents predating the corpus carry no
`document_id`, fall through to `source`, and scored normally — which is why the
family read 0.143 rather than 0.000, and why the bug was easy to miss.

**Fix** (evaluation layer only; retrieval untouched):

1. `runner.py` — keep both identities: `qdrant_documents` now holds the source
   filename (the identifier the dataset uses) and a parallel
   `qdrant_document_ids` holds the UUIDs, exposed together as
   `qdrant_identities`.
2. `metrics.resolve_document_identity()` — per retrieved document, if **either**
   identity is an exact normalised match for something the case expects, emit
   that expected string; otherwise emit the document's own identity. Downstream
   `precision_at_k` / `recall_at_k` / `mrr` then compare like with like.
3. `scoring.py` — resolve identity before measuring.

Matching stays **exact**: a filename is never compared against a UUID, and no
substring or fuzzy rule is applied. The fix can only recognise a document the
retriever genuinely returned; it cannot invent a hit.

**Affected cases: 21** declare `expected_qdrant_documents` (11 v2 `rag-*`, 10
v3). Of those, **3 were false negatives now correctly scored**
(`fus-qdrant-better-benchmark`, `fus-qdrant-better-incident`,
`fus-qdrant-semantically-near-miss`), 3 already scored correctly via the legacy
fallback, and **15 are genuine retrieval misses** — see §C.

**Regression tests** (`tests/test_eval_scoring.py`, 8 new):

| Test | Asserts |
|---|---|
| A — filename ↔ retrieved `source` | recall 1.0, MRR 1.0 |
| B — UUID ↔ retrieved `document_id` | recall 1.0, MRR 1.0 |
| C — filename vs a **UUID-only** hit | recall **0.0** — must not pass |
| D — wrong filename and wrong UUID | recall 0.0, precision 0.0 |
| precision not inflated | 1 correct of 3 retrieved → P@5 < 1.0, MRR 0.5 |
| rank order preserved | first → MRR 1.0; third → MRR ⅓ |
| exactness | `emd-0293-gpu_benchmark.md` must **not** match the full filename |
| legacy docs | no `document_id` → still scores |

## B. Latent inference

Step 6 reported "13 untagged latent-inference cases". **Individual verification
per §6 shows the correct number is 9.** The 13 came from a first-word substring
heuristic in my Step 6 analysis, which misfired; the criterion is *the question
does not name the expected entity*, not *the case failed*.

Re-tested against real graph aliases and near-miss surface forms:

| Dropped from the 13 | Question | Why it is NOT latent inference |
|---|---|---|
| `alias-postgres`, `alias-postgres-platform` | "Does the platform use **Postgres**?" | names a real stored alias (0.889 to canonical) |
| `typo-litellm` | "What is **Litelm** used for?" | deliberate typo of the name (0.923) |
| `typo-neo4j` | "What is **Neo4G** used for?" | deliberate typo of the name (0.800) |

| Added | Question | Why it IS latent inference |
|---|---|---|
| `res-tp-0824-graph-database` | "Which **graph database** do we operate?" | a category, not a name (0.105 to "Neo4j"); the alias match was on the generic portion of the longer alias "Neo4j Graph Database" |

**The 9 cases**, all tagged `requires:latent-inference`:

| case_id | question | expects | why latent | result | expected to fail at current config |
|---|---|---|---|---|---|
| `ambig-model` | "What model do we use?" | qwen-fast | no entity named | resolved `[]` | yes |
| `conflict-stale-fact` | "Is our stated inference model still current?" | qwen-fast | descriptive only | resolved `[]` | yes |
| `corrob-strongest` | "What is the best-supported fact about our inference stack?" | LiteLLM | "inference stack" is a category | resolved `[]` | yes |
| `corrob-single-source` | "What do we know that only one source claims?" | Agentic AI | no entity named | resolved `[]` | yes |
| `conflict-doc-vs-memory` | "…docs say we use one model but a recent decision changed it…" | LiteLLM | no entity named | resolved `['Model']` | yes |
| `cross-stack-doc` | "Is the technology stack in the graph consistent with our documents?" | Agentic AI | no entity named | resolved `['Microsoft Graph']` | yes |
| `fanout-datastores` | "What data stores does the platform use?" | Agentic AI | "the platform" is indirect | resolved `[]` | yes |
| `fus-qdrant-semantically-near-miss` | "Who was the incident commander for INC-1259?" | Context Engine | requires linking INC-1259 → its project | resolved `[]` | yes |
| `res-tp-0824-graph-database` | "Which graph database do we operate in production?" | Neo4j | category, not name | resolved `[]` | yes |

**Expectations were not weakened.** A programmatic diff of all 174 cases against
the pre-correction snapshot shows **0 non-tag field changes** — only 9 tag
additions. A test asserts each tagged case still carries its original
`expected_entities` *and* that its question genuinely does not name them, so the
tag cannot later be used to excuse a case that should pass.

One prose `notes` field was corrected: `res-tp-0824-graph-database` previously
said "Passes today", which Step 6 disproved. No expectation changed.

## C. Corrected baseline

```
overall  61.8%   over 174 cases
```

| Outcome | Cases |
|---|---:|
| pass | 66 |
| expected failures (known-limitation / experiment) | 40 |
| **latent inference** (newly isolated) | **9** |
| infrastructure-dependent | 8 |
| unexpected failures | 51 |

**Components** — entity P 0.732 / R 0.736 / **F1 0.736** · graph P 0.125 / R 0.583 /
**F1 0.194** / **noise 0.875** / nDCG 0.596 · **Qdrant P@5 = R@5 = MRR = 0.286** ·
fusion source P **0.962** / R 0.960 / corroboration 0.955 · ranking order-corr
0.391 / nDCG 0.451 · groundedness 0.644 / **citation 1.000** / keyword 0.395 ·
negative premise **6/7**.

**Latency** — mean 1688ms (resolution 260 · graph 303 · providers 370 · fusion 2.7 ·
ranking 0.7 · LLM 750); p50 **1438ms**, p95 **3465ms**, max 8051ms.

### Qdrant verification (§12) — per-case retrieved identity, not just scores

**Now correctly recognised as retrieved (6):** `fus-qdrant-better-benchmark`
(`emd-0293`, 0.0 → 1.0), `fus-qdrant-better-incident` (`emd-0107`, 0.0 → 1.0),
`fus-qdrant-semantically-near-miss` (0.0 → 1.0), plus `rag-kpi`, `rag-audio`,
`rag-budget` which already scored via the legacy fallback.

**Genuinely not retrieved (15)** — inspected individually; these are real
retrieval behaviour, not identity artifacts:

| case | expected | actually retrieved |
|---|---|---|
| `rank-freshness-recent-benchmark` | `emd-0293` | `emd-0309` — a *different* GPU benchmark |
| `prov-document-only-claim` | `emd-0293` | `emd-0300` — a *different* GPU benchmark |
| `fus-graph-plausible-but-irrelevant` | `emd-0293` | `emd-0294` — YOLOv8 benchmark |
| `rank-provenance-authoritative-doc` | `emd-0045` (architecture) | `emd-0139` (project doc) |
| `prov-omit-unsupported-claim` | `emd-0045` | `emd-0150` (project doc) |
| `fus-cross-source-complementary` | `emd-0211` | `emd-0096` (email) |
| `prov-two-source-claim` | `emd-0211` | `emd-0144` (project doc) |
| 8 legacy `rag-*` | e.g. `company_handbook.txt` | e.g. `邀请单位公司营业执照.pdf` |

This is exactly the discrimination those v3 cases were built for: in a dense
corpus (18 `gpu_benchmark`, 24 `incident_report` documents sharing structure and
vocabulary) vector search returns a topically-similar but **wrong** document.
That weakness was invisible while every case scored 0.

## D. Before vs after

| Metric | Step 6 Original | Corrected | Delta |
|---|---:|---:|---:|
| Overall | 0.6169 | 0.6175 | **+0.0006** |
| Entity F1 | 0.7333 | 0.7357 | +0.0024 |
| Graph F1 | 0.1942 | 0.1942 | **±0.0000** |
| Graph precision | 0.1250 | 0.1250 | **±0.0000** |
| Graph noise | 0.8750 | 0.8750 | **±0.0000** |
| **Qdrant recall@5** | 0.1429 | **0.2857** | **+0.1429** |
| **Qdrant MRR** | 0.1429 | **0.2857** | **+0.1429** |
| Fusion source precision | 0.9619 | 0.9619 | **±0.0000** |
| Ranking order corr | 0.3905 | 0.3905 | **±0.0000** |
| Ranking nDCG | 0.4509 | 0.4509 | **±0.0000** |
| Groundedness | 0.6521 | 0.6436 | −0.0085 |
| Citation coverage | 1.0000 | 1.0000 | ±0.0000 |
| Answer keyword | 0.3947 | 0.3947 | ±0.0000 |
| Negative premise | 7/7 | 6/7 | −1 |

| Classification | Step 6 | Corrected |
|---|---:|---:|
| pass | 65 | 66 |
| expected failures | 40 | 40 |
| infrastructure | 8 | 8 |
| latent inference | — (0, unclassified) | **9** |
| unexpected | 61 | **51** |
| genuine production failures | 21 (reported) | **40** (recounted, §E) |
| Qdrant evaluation-defect cases | 18 | **0** |

**Every graph, fusion and ranking metric is bit-identical.** That is the
strongest available evidence that the correction changed measurement only, and
that no retrieval behaviour moved.

### Run-to-run noise floor

Of 174 cases, **17 changed result**: 16 are explained by the identity fix, and
exactly **1** (`negp-unsupported-auth-relation`) differs from LLM
non-determinism — it affirmed the false premise in this run but not in Step 6,
with identical retrieval both times.

**Noise floor: 1/174 = 0.6%, confined to the answer stage.** Retrieval metrics
were fully deterministic across two independent runs. Overall moved 0.06pp
against the framework's 2.00pp `REGRESSION_TOLERANCE`. Step 7 can treat
retrieval movement as signal, but must not read a single answer-stage case as
one.

## E. Genuine failure recount (§13)

The Step 6 figure of 21 was computed **before** the identity fix, when 18 cases
were suppressed as evaluation artifacts. Recounted honestly, it is **higher**,
not lower:

| Class | Cases |
|---|---:|
| unexpected failures | 51 |
| — only a stale `expected_provider_order` | 11 |
| **genuine production failures** | **40** |
| — document-retrieval misses (now real and measurable) | 15 |
| — graph / entity / answer | 25 |

Dominant reasons across the genuine 40: **21× graph nodes missing**, 15× no
expected document retrieved, 11× relationships missing, 4× entities not resolved,
1× negative leak (`neg-google-founder` resolved `Google`), 1× premise affirmed.

The graph finding from Step 6 stands unchanged and remains the largest weakness:
`graph_top_k=8`, 83 of 100 graph cases return exactly 8 nodes, the mean number of
*expected* nodes is 2.1, and only one case wants more than 8 — so capacity is not
the constraint and **the ranker is selecting the wrong 8**. `graph_node_precision
= 0.125` and `graph_noise_ratio = 0.875` at depth 1.

The 11 stale-provider-order cases are a v2 dataset issue Step 4 missed: it
verified that calendar's *absence* from `expected_provider_order` was harmless,
but not the *relative* order of the items already there (`[corporate, calendar,
tasks]` observed vs `[tasks, corporate]` expected). Recorded, not corrected —
correcting expectations after seeing failures is out of scope here.

## F. Production integrity

Verified before and after; snapshots identical.

| Check | Value |
|---|---|
| Neo4j total nodes / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| Neo4j `:Document` | 101 ✅ |
| `kgtest-*` residue | 0 ✅ |
| Canary entities | 6/6 ✅ |
| Qdrant `corporate_memory` | 993 points, 20 collections, none new ✅ |
| Corpus files | 310 ✅ |
| Postgres `documents` | 0 rows ✅ |

No Neo4j writes/deletes, no Qdrant upserts/deletes, no corpus change, no alias
registration or alias-table write, no Redis.

## G. Tests

`tests/test_eval_scoring.py`: **33 passing** (24 prior + 8 document-identity +
1 latent-inference).

Full backend suite: **279 passed, 4 failed**. The 4 are the known pre-existing
`tests/test_analytics.py` failures (`backend/analytics.py` has no `DB_PATH`) —
not touched by this task and not fixed, per §15. `test_insight_evidence.py` and
`test_routing.py` remain module-level `sys.exit()` scripts excluded from
collection; also pre-existing. The suite grew from 264 to 279: +9 from this task,
the remainder from the other developer's untracked test files (including a new
`test_chat_context.py`), which were not modified.

## H. Decision

**1. Is the corrected baseline trustworthy?** Yes. Every metric family now
measures what it claims. The one known measurement defect is fixed, verified by
per-case identity inspection rather than by scores rising, and guarded by four
regression tests including an explicit must-not-pass case.

**2. Is Qdrant evaluation valid enough for fusion/ranking experiments?** Yes.
It now distinguishes "retrieved correctly" from "not retrieved" — demonstrated
in both directions on the same run (3 cases moved 0.0 → 1.0; 15 stayed at 0.0
for verified reasons). The family score of 0.286 is a real, low retrieval-precision
number rather than an artifact, and it is now a legitimate optimisation target.

**3. Are the 9 latent-inference cases correctly isolated?** Yes, and the count is
9 rather than the 13 Step 6 reported — corrected by individual verification. They
are classified, not excused: expectations unchanged, still failing, and a test
enforces both.

**4. Ready for Step 7?** Yes. The Step 6 precondition is discharged. Retrieval is
deterministic across runs (0.6% noise, answer-stage only), the resolver boundary
cases behave exactly as designed, and the latent-inference cohort is separated
from resolver-threshold cases — which matters because 1 of the 15
resolver-family cases (`res-tp-0824-graph-database`) was previously
miscategorised as a threshold case and would have polluted the Step 7 result.

**5. Which artifact should Step 7 use?**

```
docs/evaluation/graphrag_baseline_v3_corrected.json      <- compare against this
docs/evaluation/graphrag_baseline_v3_corrected_manifest.json
```

Dataset must be v3.1 with hashes `cc9e9f2ddf118785` / `26435e37ec9f4d71` /
`ce0cb2992228971f`. The Step 6 artifacts are historical and must not be used for
comparison, nor overwritten.

**Must not change before Step 7:** the three dataset hashes above; resolver
threshold 0.82, depth 1, fusion 0.6, `graph_top_k` 8, `qdrant_top_k` 5 and the six
ranking weights; `graph_node_f1` in `overall` plus `graph_noise_ratio` and
`context_nodes`; the document-identity resolution; and the production state in §F.
