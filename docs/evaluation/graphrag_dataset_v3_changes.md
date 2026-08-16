# GraphRAG Evaluation Dataset — v2 → v3 (Discriminating Benchmark)

**Step 5 of the GraphRAG Quality Gate.** Builds a benchmark able to support the
controlled experiments in Steps 7–12.

Not a tuning phase. No production GraphRAG behaviour changed: resolver threshold,
graph depth, fusion threshold, ranking weights, Neo4j schema, Qdrant collections
and LiteLLM config are all untouched. Nothing staged, nothing committed.

---

## Version layout

`load_dataset()` globs `cases/*.json` **non-recursively** and unions the results.
That is the repository's existing convention, so v3 follows it rather than
inventing a parallel structure:

| Version | Location | Status |
|---|---|---|
| v1 | `backend/evals/cases/archive/v1/` | preserved; verified byte-identical to `git show HEAD:` |
| v1-audit | `docs/evaluation/graphrag_dataset_audit_v1.md` | audit only |
| v2 | `cases/core_retrieval.json` + `cases/enterprise.json` | **FROZEN** — 116 cases, `dataset_version: 2` |
| v3 | the above **+** `cases/discrimination.json` | 174 cases; the new file carries `dataset_version: 3` |

`archive/v1/` is invisible to the loader because the glob is not recursive, so
v1 stays reproducible without polluting a run. Every new case is tagged `v3`,
so `--tags v3` selects only the additions and the v2 subset stays addressable.

**v2 freeze verified** by re-measuring the invariants recorded at the end of
Step 4: 116 cases, `dataset_version: 2`, calendar in 109 `expected_context_sources`
and **0** `expected_provider_order`, 14 `requires:depth-*`, 8
`requires:semantic-resolution`, 11 `requires:provider-*`, and exactly the three
`forbidden_answer_keywords` cases. All still hold.

---

## Design matrix (produced before any case was written)

| Experiment | Existing coverage | Missing discrimination | New cases |
|---|---|---|---|
| **Resolver threshold** | 26 cases tagged typo/alias/casing/abbreviation/synonym | None record a *measured* similarity, and most resolve at ladder rungs 1–4 (fixed scores 1.0/0.98/0.95) without ever reaching fuzzy matching. The 0.78–0.90 fuzzy band — the only region a threshold change can move — was unconstrained. | 15 |
| **Document pollution** | 1 (`syn-gateway`) | One case is not a regression family, and it covers only one of the **two** mechanisms found. | 7 |
| **Semantic resolution** | 8 tagged | Adequate in count, but the cohort is confounded (see below). Missing: conceptual paraphrases with *no* lexical path. | 3 (+1 extraction) |
| **Graph depth** | 14 depth-dependent | No depth-1 **controls**, so a depth change had no case that must stay passing. Depth-2 claims were not verified to lack a 1-hop shortcut. | 10 |
| **Context explosion** | 5 (budget/compression) | Nothing measured graph *precision* under depth, and `overall` could not fall when retrieval flooded. | 6 |
| **Fusion** | 10 (corroboration/conflict/cross-source) | Nearly all are meta-questions ("which facts are confirmed by more than one source?") declaring `sources=[graph,corporate,tasks,calendar]` — i.e. *everything*. **No case asserted which provider should win.** | 8 |
| **Ranking** | 3 (freshness 1, provenance 2) | No case isolates a single ranking signal (centrality, hop distance, corroboration, provenance, freshness). | 6 |
| **Negative premise** | 9 (3 premise + 6 negative) | Missing: grounded *correction* (the corpus holds the right answer) and premises the model's world knowledge actively pushes toward. | 4 |
| **Provenance** | 2 | Nothing tests per-claim attribution or the omission of an unsupported claim. | 8 |

Two families were examined and judged **already sufficient**, so nothing was
added: general alias/casing handling (26 v2 cases) and prompt-injection /
degenerate-input adversarials (10 v2 cases).

---

## Measurements that ground the cases

All read-only. Resolver numbers come from real `fuzzy_candidates()` probes
against production Neo4j; graph paths were verified to have no shorter shortcut;
cited documents were confirmed present in Qdrant.

### The resolver decision boundary is a genuine trade-off

`fuzzy_threshold = 0.82` (`registry.py:62`). Measured, with real entities:

| Probe | → candidate | similarity | today |
|---|---|---:|---|
| `Qdrant Databse` | Qdrant | 0.7778 | ✗ misses |
| `LiteLLM Proxy Server` | LiteLLM | 0.7879 | ✗ misses |
| **`K8`** | **Kubernetes** | **0.8000** | ✗ misses |
| **`DB`** | **Dar Al Ber Society** | **0.8000** | ✓ correctly rejected |
| `docker compose` | `docker-compose.yml` *(a Document)* | 0.8125 | ✓ correctly rejected |
| `CCTV Analysis` | CCTV Analytics | 0.8148 | ✗ misses |
| `Tailscale VPN` | Tailscale | 0.8182 | ✗ misses by 0.0018 |
| `graph database` | Neo4j | 0.8235 | ✓ resolves |
| `Qdrent` | Qdrant | 0.8333 | ✓ resolves |
| **`VLM`** | **vLLM** | **0.8571** | ✓ resolves — **and is wrong** |
| `Ajman Polis` | Ajman Police | 0.8696 | ✓ resolves |

Three findings make this band discriminating rather than decorative:

1. **`K8` → Kubernetes and `DB` → Dar Al Ber Society both sit at exactly 0.8000**,
   one must resolve and one must not. No threshold of 0.80 can satisfy both.
   The boundary has a provable cost in each direction.
2. **`VLM` → vLLM at 0.8571 is a false positive above the current threshold.**
   VLM means Vision-Language Model; vLLM is a serving engine. It caps how low
   the threshold can go and is the only measured wrong-resolution above 0.82.
3. **`docker compose` → `docker-compose.yml` at 0.8125** couples two experiments:
   lowering the threshold to 0.80 to win `K8` would simultaneously start
   resolving a *document* as an entity.

### Document pollution has two mechanisms, not one

Of 101 `:Document` nodes, **94 are pure documents** (only `:Entity` + `:Document`)
and **7 are hybrids** where a real entity also acquired `:Document` —
`ajman-police` carries eight labels (`User, Project, Document, Entity,
Organization, Company, Location, Service`).

Measured candidate crowding: `compliance matrix` and `environment matrix` each
return **5 of 5 Document candidates**; `architecture overview` returns 4 of 5
with a Document top-ranked at 0.7636. Even a clean probe like `Agentic AI`
spends 4 of 5 candidate slots on Documents.

This is why `poll-hybrid-ajman-police` exists: the obvious fix — excluding
`:Document` from candidate generation — would break the 7 hybrid nodes. The
case makes that failure visible instead of silent.

### The "semantic resolution" cohort is confounded

The v2 cohort was assumed to need semantic resolution. Measured, it fails at
**three different stages**:

| Case | Mentions extracted | Actual failure |
|---|---|---|
| `syn-vector-db`, `syn-graph-db`, `syn-gateway`, `syn-relational`, `syn-mail-api` | `[]` | **mention extraction** — the resolver prompt explicitly excludes generic nouns ("our database"), so the phrase never reaches the registry |
| `abbrev-llm` | `['LLM gateway']` → `litellm` @ 0.8462 | **already passes** — mis-tagged in v2 |
| `abbrev-db` | `['DB','knowledge graph']` → `knowledge-graph` | resolves to the **wrong** entity |
| `abbrev-api` | `['M365']` → Microsoft @ 0.8182 | genuine **threshold** miss |

Additionally, `graph database` → Neo4j succeeds at 0.8235 only because Neo4j
carries the stored alias *"Neo4j Graph Database"*; `vector database` → Qdrant
fails at 0.811 against the alias *"Qdrant Vector Database"*. Whether one of
these "semantic" cases passes is an accident of alias string length.

**Consequence for Step 9:** a semantic resolver placed in the *registry* cannot
fix 5 of the 8, because they die before the registry is called. Without
`mext-generic-noun-suppressed` to isolate that stage, the semantic-resolution
experiment would be measured against cases it structurally cannot move, and
would be judged a failure for the wrong reason.

v2 is frozen, so `abbrev-llm`'s incorrect tag is **recorded here, not corrected**.
It should be retagged in a future version.

### Depth 3 is not a retrieval setting

Measured neighbourhood growth from real anchors (522 entities total):

| Anchor | depth 1 | depth 2 | depth 3 | d2/d1 | Documents @ d2 |
|---|---:|---:|---:|---:|---:|
| vLLM | 24 | 340 | 490 | 14.2× | 20% |
| Neo4j | 25 | 343 | 490 | 13.7× | 19% |
| Microsoft Graph | 27 | 324 | 488 | 12.0× | 19% |
| LiteLLM | 42 | 404 | 491 | 9.6× | 22% |
| CCTV Analytics | 79 | 448 | 497 | 5.7× | 22% |

Depth 2 reaches **65–86% of the graph** from a single seed; depth 3 reaches
**~94%** regardless of the question. Roughly a fifth of every depth-2
neighbourhood is `:Document` nodes, linking the explosion and pollution
experiments quantitatively.

---

## Scoring change (§9 — mandatory, and the reason it was mandatory)

`CaseScore.overall` averaged `graph_node_recall`, `qdrant_recall_at_k` and
`source_recall` — **recall without precision**. Combined with the measurement
above, a depth-3 configuration returning 94% of the graph would have scored
near-perfect recall on every graph case and *raised* the headline number while
answers got worse. The benchmark would have recommended the worst setting.

Three additions to `backend/evals/scoring.py` (evaluation-side only; no
production retrieval code touched):

- **`graph_node_f1`** — and `overall` now averages it instead of
  `graph_node_recall`. Entity scoring already used F1; graph now matches.
- **`graph_noise_ratio`** (`1 − precision`) — rises with depth even when recall
  is flat, which is exactly the trade a depth experiment must see.
- **`context_nodes`** — recorded on every case, including those stating no graph
  expectation, so the depth comparison has a size axis alongside the existing
  `tokens_used`, `compression_ratio` and `latency_ms`.

Together these give §9's required comparison across depth 1/2/3 on recall,
precision, groundedness, context size and latency. A test asserts the property
directly: two runs with identical perfect recall, one flooding, and the flooding
run must score strictly lower.

---

## New cases

58 cases in `cases/discrimination.json`. **19 are tagged `expected-fail`** — they
are known to fail today and exist to measure an experiment, not to be made green.

Every case records `case_id`, experiment family (`exp:*` tag), why the existing
dataset was insufficient, question, expected behaviour, required depth where
applicable, expected entities, expected source contribution, and what makes it
discriminating — in the `notes` field, enforced by a test.

### Resolver threshold (15)
`res-tp-0778-qdrant-typo`, `res-tp-0788-litellm-proxy-server`, `res-tp-0800-k8`,
`res-tp-0815-cctv-analysis`, `res-tp-0818-tailscale-vpn`, `res-tp-0824-graph-database`,
`res-tp-0833-qdrent`, `res-tp-0842-whisper-large-v3`, `res-tp-0870-ajman-polis`,
`res-tp-0889-litellm-gate`, `res-tn-0857-vlm`, `res-tn-0800-db-collision`,
`res-tn-0750-graph-microsoft`, `res-tn-0696-vllm-engine`, plus `poll-docker-compose`
(dual-family). Every id encodes its measured similarity; every case carries a
`band:` tag and a `MEASURED:` note.

### Document pollution (7)
`poll-docker-compose` (threshold interaction), `poll-compliance-matrix` and
`poll-environment-matrix` (total crowding, at two different similarities),
`poll-architecture-overview` (closest a document comes to threshold),
`poll-hybrid-ajman-police` (guards the naive fix), `poll-gateway-terse`
(generalises the single v2 detector), `expl-doc-share-at-depth`.

### Semantic resolution (3) + mention extraction (1)
`sem-embedding-model`, `sem-speech-to-text` (measured `speech to text` → *Tom
Baker* @ 0.3478 — the lexical path is not weak but nonsensical),
`sem-object-storage` (empty candidate set, a distinct failure mode), and
`mext-generic-noun-suppressed`, which isolates the extraction stage the v2 cohort
conflates. Expected entities are unchanged and all four stay failing on purpose.

### Graph depth (10)
Three **depth-1 controls** (`d1-litellm-routes-to`, `d1-neo4j-hosted-on`,
`d1-joseph-works-on`) that must stay passing — v2 had none, so a depth change had
nothing to regress against. Five verified **true 2-hop** chains, each confirmed
to have no 1-hop shortcut. Two verified **3-hop** chains through the
`ravi-shankar` hub, paired to distinguish "depth-3 works" from "this one seed
works".

### Context explosion (6)
`expl-vllm-narrow`, `expl-msgraph-narrow`, `expl-neo4j-narrow`,
`expl-doc-share-at-depth`, `expl-budget-under-depth`, `rank-hop-distance-penalty`.
Narrow questions on anchors with measured 12–14× depth-2 growth: the expected
answer is one node, so precision and noise must degrade visibly as depth rises.

### Fusion (8)
Graph-authoritative (`fus-graph-better-deployment`, `d1-litellm-routes-to`),
Qdrant-authoritative (`fus-qdrant-better-benchmark` — 492 tokens/s, a number the
graph cannot hold; `fus-qdrant-better-incident` — INC-1259 lasted 288 minutes),
corroboration (`fus-corroborated-litellm-vllm`), graph-irrelevant
(`fus-graph-plausible-but-irrelevant`), Qdrant-irrelevant
(`fus-qdrant-semantically-near-miss`, against 24 near-identical incident reports),
and complementary cross-source (`fus-cross-source-complementary`).

Provider preference is asserted with **`expected_provider_order`**, not by
narrowing `expected_context_sources`. `rank_correlation` compares only items
present in both lists, so an order claim discriminates without punishing the
always-on providers — whereas a narrow source list would permanently depress
`source_precision`, which is the v2 defect Step 4 documented.

### Ranking (6)
One case per signal: centrality (`rank-low-centrality-relevant` — the answer is a
low-degree leaf beside a 42-node hub), hop distance (`rank-hop-distance-penalty` —
"directly" makes 1-hop right and 2-hop wrong), corroboration, provenance by real
`doc_type` metadata, freshness across 18 real dated benchmark documents.
No synthetic confidence numbers were invented.

### Negative premise (4)
`negp-unsupported-migration` (every component true, only the transition invented),
`negp-wrong-person-role` (the corpus holds the *correct* commander, so grounded
**correction** is required, not just refusal), `negp-unsupported-auth-relation`
(Tailscale genuinely supports Entra SSO in the real world, so the model's prior
pushes toward fabrication), `prov-omit-unsupported-claim`.

### Provenance (8)
Graph-only, document-only, two-source per-claim attribution, and
`prov-omit-unsupported-claim` — a three-part question where exactly one part is
unsupported and must be omitted. That last is the only case scoring partial-answer
discipline, which §13 requires and no v2 case provides.

---

## Coverage report

| Experiment | Existing | New | Total | Ready? |
|---|---:|---:|---:|---|
| Resolver threshold | 26 | 15 | 41 | **Yes** |
| Document pollution | 1 | 7 | 8 | **Yes** |
| Semantic resolution | 8 | 3 | 11 | **Partially** |
| Graph depth | 14 | 10 | 24 | **Yes** |
| Context explosion | 5 | 6 | 11 | **Yes** |
| Fusion | 10 | 8 | 18 | **Yes** |
| Ranking | 3 | 6 | 9 | **Partially** |
| Negative premise | 9 | 4 | 13 | **Yes** |
| Provenance | 2 | 8 | 10 | **Yes** |
| Mention extraction *(new family)* | 0 | 1 | 1 | **Partially** |

**Semantic resolution — partially ready.** The cases are sound and the confound
is now documented, but 5 of the 8 v2 cases fail at mention extraction, and v2 is
frozen so they cannot be retagged. Step 9 must segment by stage or the experiment
will be judged against cases it cannot move. `abbrev-llm` is mis-tagged
`requires:semantic-resolution` and already passes.

**Ranking — partially ready.** Each of the five signals now has an isolating
case, but the scorer has no per-signal attribution: `ranking_ndcg` and
`provider_order_correlation` report the *outcome*, not which signal produced it.
Attributing a weight change to a specific signal will need either per-signal
score capture in the trace or an ablation harness. That is a Step 11 decision,
not a dataset gap.

**Mention extraction — partially ready.** One case establishes the family and
proves the stage exists. If Step 9 pursues extraction changes, this needs
expanding to 4–6 cases across phrasings; one case cannot carry an experiment.

---

## Validation

- 174 cases load; all ids unique
- v2 frozen: all Step-4 invariants re-verified unchanged
- `archive/v1/` still byte-identical to `git show HEAD:`
- no expectation names an unknown provider
- every v3 case maps to a known `exp:` family and justifies itself (enforced)
- every `expected_qdrant_documents` entry verified to exist in the real corpus
- no v3 case combines `negative=True` with expectations or forbidden keywords
- every resolver case declares a `band:` tag and a `MEASURED:` note
- depth cases: `requires:depth-N` only where N exceeds production depth; depth-1
  controls asserted not to be `expected-fail`

`tests/test_eval_scoring.py`: **24 tests, all passing** (13 from v2, 11 new).
Full suite: **264 passed, 4 failed** — the 4 are pre-existing in
`tests/test_analytics.py` (`backend/analytics.py` has no `DB_PATH`), untouched by
this work. `tests/test_insight_evidence.py` and `tests/test_routing.py` remain
scripts with module-level `sys.exit()` that crash pytest collection; also
pre-existing.

## Production integrity

| Check | Result |
|---|---|
| Neo4j `:Entity` | 522 ✅ |
| Neo4j total nodes | 523 ✅ (522 + `(:User {name:"Akshay"})`) |
| Neo4j relationships | 3414 ✅ |
| `kgtest-*` residue | 0 ✅ |
| Canary entities | 6/6 ✅ |
| `:Document` nodes | 101 ✅ (unchanged; 310 is the corpus **file** count) |
| Qdrant `corporate_memory` | 993 points ✅ |
| New Qdrant collections | none ✅ |
| Corpus files | 310 ✅ |
| Postgres `documents` | 0 rows, unchanged ✅ |

No graph fixtures were required, so no `kgtest-*` nodes were created. All
inspection used read-only Cypher and read-only registry calls
(`resolve`, `fuzzy_candidates`, `warm`) — never `register_alias` or
`save_alias_table`. No Qdrant writes; no throwaway collection was needed.

## Files changed by this task

| File | Change |
|---|---|
| `backend/evals/cases/discrimination.json` | **new** — 58 v3 cases |
| `backend/evals/scoring.py` | `+ graph_node_f1`, `+ graph_noise_ratio`, `+ context_nodes`; `overall` uses F1 |
| `tests/test_eval_scoring.py` | +11 tests (24 total) |
| `docs/evaluation/graphrag_dataset_v3_changes.md` | **new** — this file |

`core_retrieval.json`, `enterprise.json` and `dataset.py` are **unchanged by
Step 5** (they carry Step 4's edits). The other developer's ~27 files are
untouched. Nothing staged, nothing committed.

---

## Next step (Step 6 — do not run the experiments)

Run the corrected baseline against v3, establish the benchmark baseline, verify
regression/expectation behaviour, and save the baseline JSON/HTML before any
tuning. Expect roughly 19 v3 cases plus the v2 known-limitation cohorts to fail;
that is the designed outcome and the baseline must record it rather than treat it
as a defect.
