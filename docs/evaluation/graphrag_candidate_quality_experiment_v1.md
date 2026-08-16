# Step 8 — Candidate Quality and Document Pollution Experiment

**Question:** can candidate-quality improvements reduce wrong entity resolutions
and Document pollution without materially reducing legitimate entity recall?

**Answer, in one line:** at the production threshold of 0.82 a Document filter is
a **provable no-op** — zero pure Documents are selected — and the naive version of
it *loses 5 correct resolutions*. Filtering is nonetheless the better lever than
the threshold, because it is the thing that makes a lower threshold safe: it is
worth adopting only **together with** a threshold change, not on its own.

Production was **not changed**. `fuzzy_threshold` is still 0.82, the full-text
index is untouched, and no filtering was landed in the resolver. Nothing staged,
nothing committed.

| Artifact | Path |
|---|---|
| Results | `docs/evaluation/graphrag_candidate_quality_experiment_v1.json` |
| Manifest | `docs/evaluation/graphrag_candidate_quality_experiment_v1_manifest.json` |
| Tests | `tests/test_candidate_filtering.py` (9 passing) |
| Inputs (unmodified) | corrected baseline + Step 7 artifacts |

Filtering is applied **after** `fulltext_search()`, which already returns each
node's labels — so no index, schema or label change is required (spec §4).

---

## A. Baseline candidate behaviour

Across the 99 frozen Step 7 mentions:

| Measure | Value |
|---|---:|
| entities in graph | 522 |
| `:Document`-labelled | 101 (94 pure, **7 hybrid**) |
| mentions with ≥1 Document candidate | **43 / 99** |
| candidate slots occupied by Documents | **189 / 630 (30%)** |
| mentions whose rung-1–4 hit **is** a Document | **4** |
| **pure Documents actually selected at 0.82** | **0** |

Two structural facts drive every result below.

**1. Documents are reachable through more than the fuzzy rung.** `:Document`
nodes carry `:Entity`, so they appear in the registry cache and the exact-name
lookup too. Four mentions resolve to a Document at rungs 1–4 — `Ajman Police`,
`Context Engine`, `Evaluation Framework`, `knowledge graph`. A filter applied
only to fuzzy candidates would miss all four; variant B3 measures exactly that
and confirms it changes nothing on its own.

**2. All four of those are hybrid nodes, and 5 of the 6 Document selections at
baseline are correct.** `ajman-police` carries eight labels
(`User, Project, Document, Entity, Organization, Company, Location, Service`).
These are genuinely both a document and an organization.

Key candidate evidence:

| mention | top candidate | sim | kind | runner-up |
|---|---|---:|---|---|
| `K8` | Kubernetes | 0.8000 | entity ✅ | Knowledge Graph 0.5000 |
| `DB` | Dar Al Ber Society | 0.8000 | entity ❌ | Dubai Municipality 0.5000 |
| `VLM` | vLLM | 0.8571 | entity ❌ | Helm 0.5714 |
| `docker compose` | docker-compose.yml | 0.8125 | **pure Document** ❌ | Docker 0.6000 |
| `gateway` | Hermes Gateway | 0.6667 | entity | LiteLLM 0.6364 |

**Correction to a prior assumption:** the `gateway` → *"Hermes Gateway — Kickoff
Notes"* pollution described in the brief is **not reproduced**. The pure Document
candidates rank at 0.3889 / 0.3784, *below* the real Hermes Gateway entity at
0.6667. Nothing resolves at 0.82 because the top candidate is below threshold —
`gateway` is a weak-signal case, not a pollution case.

## B. Required experiment table (§15) — resolver cohort, 118 cases

| Variant | thr | Precision | Recall | F1 | Wrong | Doc FP | Contamination |
|---|---:|---:|---:|---:|---:|---:|---:|
| **A baseline** | 0.82 | 0.9256 | 0.8682 | 0.8960 | 3 | 1 | 104 |
| B1 exclude **all** Documents | 0.82 | 0.9304 | 0.8295 | 0.8770 | 3 | **0** | **35** |
| B2 exclude **pure** Documents | 0.82 | 0.9256 | 0.8682 | 0.8960 | 3 | 1 | 104 |
| B3 pure filter, fuzzy rung only | 0.82 | 0.9256 | 0.8682 | 0.8960 | 3 | 1 | 104 |
| D1 no filter | 0.80 | 0.9127 | 0.8915 | 0.9020 | 5 | 2 | 118 |
| **C1 pure filter** | 0.80 | 0.9200 | 0.8915 | 0.9055 | 4 | 1 | 116 |
| D2 no filter | 0.78 | 0.9134 | 0.8992 | 0.9063 | 5 | 2 | 118 |
| **C2 pure filter** | 0.78 | 0.9206 | 0.8992 | **0.9098** | 4 | 1 | 116 |
| D3 no filter | 0.75 | 0.9000 | 0.9070 | 0.9035 | 6 | 2 | 123 |
| C3 pure filter | 0.75 | 0.9070 | 0.9070 | 0.9070 | 5 | 1 | 121 |

*Contamination = graph nodes injected by seeds in cases classified
WRONG_RESOLUTION. Reported this way deliberately: counting every unexpected
entity would include benign extras such as `Agentic AI` being legitimately
mentioned in a case that does not list it.*

Outcome counts:

| Variant | correct | wrong | unresolved | no mention |
|---|---:|---:|---:|---:|
| A baseline | 100 | 3 | 12 | 3 |
| B1 exclude all Documents | **95** | 3 | 17 | 3 |
| B2 exclude pure Documents | 100 | 3 | 12 | 3 |
| C2 pure filter @ 0.78 | **103** | 4 | 8 | 3 |
| D2 no filter @ 0.78 | 103 | 5 | 7 | 3 |

### The three results that matter

**B2 is bit-identical to baseline.** Precision, recall, F1, wrong resolutions,
Document false positives, contamination — every figure unchanged. Because **zero
pure Documents are selected at 0.82**: the only pure-Document candidate anywhere
near the line is `docker-compose.yml` at 0.8125, which the threshold already
rejects. *At current production settings there is nothing for a Document filter
to fix.*

**B1 is actively harmful.** Excluding every `:Document` node costs 5 correct
resolutions — `res-tp-0870-ajman-polis`, `poll-hybrid-ajman-police`,
`fus-qdrant-better-incident`, `rank-provenance-authoritative-doc`,
`prov-omit-unsupported-claim` — all hybrid nodes, all falling to
`NO_RESOLUTION`. F1 drops 0.8960 → 0.8770. This is precisely the regression
`poll-hybrid-ajman-police` was written in Step 5 to catch, and it caught it.

**Filtering's real value is conditional.** Comparing filtered against unfiltered
at the *same* threshold isolates it exactly:

| At threshold 0.78 | precision | wrong | Doc FP | F1 |
|---|---:|---:|---:|---:|
| no filter (D2) | 0.9134 | 5 | 2 | 0.9063 |
| **pure filter (C2)** | **0.9206** | **4** | **1** | **0.9098** |

The filter removes exactly one failure — `docker compose` → `docker-compose.yml`
— converting it from `WRONG_RESOLUTION` to `NO_RESOLUTION`. That is +0.72pp
precision at zero recall cost. **Step 7 rejected 0.78 because it reintroduced
Document pollution; pure-Document filtering removes that specific objection.**

## C. Entity-type / role filtering (§8) and candidate signals (§9)

Every fuzzy decision above 0.75 was examined against the metadata a runtime
filter could legitimately use — similarity, margin over the runner-up, node
degree, number of real labels, and exact canonical/alias match. **No signal
separates correct from wrong:**

| Signal | CORRECT range | WRONG range | Separable? |
|---|---|---|---|
| similarity | 0.7778 – 0.9655 | 0.7500 – 0.8571 | ❌ overlaps |
| margin over runner-up | 0.1231 – 0.4514 | 0.1500 – 0.3000 | ❌ overlaps |
| degree | 23 – 79 | 2 – 71 | ❌ overlaps |
| label count | 1 – 6 | 0 – 4 | ❌ overlaps |
| canonical/alias exact match | none of the 20 | none of the 20 | ❌ no signal |

The decisive pair:

| | mention | candidate | sim | margin | degree | labels |
|---|---|---|---:|---:|---:|---:|
| ✅ correct | `K8` | Kubernetes | 0.8000 | 0.3000 | 29 | 1 |
| ❌ wrong | `DB` | Dar Al Ber Society | 0.8000 | 0.3000 | 12 | 1 |

**Identical similarity, identical margin, identical label count.** Only degree
differs (29 vs 12), and separating two collisions on a degree cut derived from
two data points would be fitting to the answer, not using runtime information —
exactly what §8 forbids. Both are ordinary entities, so Document filtering cannot
help either.

Variants C (entity-type filter) and D (candidate-score signal) as originally
scoped are therefore reported **NOT RUN as production candidates**: the graph's
existing labels carry no information that distinguishes these cases at runtime.
Building one would require a new signal — acronym-expansion agreement, or
mention/entity type compatibility — which §9 explicitly defers.

## D. Known cases

| Case | Baseline 0.82 | B1 all-Docs | B2 pure-Docs | C1 filter @ 0.80 | C2 filter @ 0.78 |
|---|---|---|---|---|---|
| `K8` → Kubernetes *(want resolve)* | ❌ nothing | ❌ nothing | ❌ nothing | ✅ **Kubernetes** | ✅ **Kubernetes** |
| `DB` → Dar Al Ber *(want decline)* | ✅ declines | ✅ declines | ✅ declines | ❌ resolves | ❌ resolves |
| `VLM` → vLLM *(want decline)* | ❌ resolves | ❌ resolves | ❌ resolves | ❌ resolves | ❌ resolves |
| `docker compose` *(want no Document)* | ✅ nothing | ✅ nothing | ✅ nothing | ✅ **nothing** | ✅ **nothing** (D2 without filter: ❌ resolves the Document) |
| `gateway` *(want no Document)* | ✅ nothing | ✅ nothing | ✅ nothing | ✅ nothing | ✅ nothing |
| `Ajman Police` hybrid *(want resolve)* | ✅ resolves | ❌ **lost** | ✅ resolves | ✅ resolves | ✅ resolves |

`VLM` → vLLM is wrong in **every** variant: it is an entity-vs-entity collision
above threshold that neither filtering nor (per Step 7) any threshold below 0.88
can address. The K8/DB collision remains unresolvable in both directions — the
filter does not change that, it only stops the *Document* from being a third
casualty.

## E. Contamination (§11)

Restricted to cases classified `WRONG_RESOLUTION`:

| Variant | wrong | nodes injected | Δ vs baseline | sources |
|---|---:|---:|---:|---|
| A baseline | 3 | 104 | — | **Knowledge Graph (69)**, vLLM (24), Google (6), Model (5) |
| B1 exclude all Documents | 3 | **35** | **−69** | vLLM (24), Google (6), Model (5) |
| B2 exclude pure Documents | 3 | 104 | 0 | unchanged |
| C1 filter @ 0.80 | 4 | 116 | +12 | + Dar Al Ber Society (12) |
| C2 filter @ 0.78 | 4 | 116 | +12 | + Dar Al Ber Society (12) |
| D2 no filter @ 0.78 | 5 | 118 | +14 | + Dar Al Ber (12) + docker-compose.yml (2) |

**The Step 7 69-node figure is confirmed and its source corrected.** *Knowledge
Graph* has degree 69 and is **66% of all baseline contamination** — but it is a
**hybrid** Document node reached at **rung 1** (registry cache) from the mention
`knowledge graph`, not a fuzzy candidate. Two consequences:

- pure-Document filtering cannot touch it (B2: Δ0);
- only the naive B1 removes it — at the cost of 5 correct resolutions.

The hybrid nodes are simultaneously **the largest single contamination source and
the source of 5 correct resolutions.** They cannot be filtered wholesale, which is
the sharpest finding of this experiment.

*(Step 7 attributed the 69 nodes to `abbrev-db`. Both are true and describe
different cohorts: `abbrev-db` sits in the excluded semantic cohort, while inside
the resolver cohort the same entity is injected by `adv-very-long-question`.)*

## F. Family breakdown (§16) — correct / wrong / unresolved

| family | n | A baseline | B1 all-Docs | B2 pure | C2 filter @ 0.78 | D2 no filter @ 0.78 |
|---|---:|---|---|---|---|---|
| exact | 81 | 73/2/5 | **70**/2/8 | 73/2/5 | 73/2/5 | 73/2/5 |
| alias | 11 | 11/0/0 | 11/0/0 | 11/0/0 | 11/0/0 | 11/0/0 |
| typo | 9 | 7/0/2 | **6**/0/3 | 7/0/2 | **8**/0/1 | 8/0/1 |
| fuzzy | 14 | 8/1/5 | 7/1/6 | 8/1/5 | **10**/2/2 | 10/**3**/1 |
| document-pollution | 7 | 3/0/2 | **2**/0/3 | 3/0/2 | 3/**0**/2 | 3/**1**/1 |
| abbreviation | 1 | 0/0/1 | 0/0/1 | 0/0/1 | **1**/0/0 | 1/0/0 |

No family is silently traded away. B1's damage is visible in `exact` (73→70) and
`document-pollution` (3→2) — it degrades the very family it was meant to protect,
because the hybrids live there. C2's gains are in `fuzzy` (8→10), `typo` (7→8)
and `abbreviation` (0→1), and it is the only variant that keeps
`document-pollution` at 0 wrong while raising recall.

## G. Decision

**1. Does excluding Document candidates improve quality?** **Not at 0.82** — it is
measurably a no-op, because no pure Document is selected there. It improves
quality *only in combination with a lower threshold*, where it removes one wrong
resolution and one Document false positive for +0.72pp precision at no recall
cost.

**2. Does it preserve legitimate entity recall?** The **pure**-Document filter
does: recall identical at 0.82, and at 0.78 it costs nothing relative to the
unfiltered variant. The **naive** filter does not: −3.87pp recall and 5 correct
resolutions lost.

**3. Can existing entity/type information solve the remaining collisions?**
**No.** All four available runtime signals overlap between correct and wrong
resolutions, and the `K8`/`DB` pair is identical on similarity, margin and label
count. Nothing currently in the graph distinguishes them.

**4. Is candidate filtering a better lever than threshold tuning?** **Yes, but
only as an enabler.** Alone it changes nothing (B2 ≡ A). Its value is that it
removes the specific objection that made Step 7 reject a lower threshold. The two
levers are complements, not alternatives — which is the substantive correction to
the Step 7 conclusion.

**5. Should production resolver code be changed?** **Not yet, and not on its
own.** A filter alone buys nothing measurable. It should be considered only as
part of a controlled `filter + threshold 0.78` change, and that decision needs
the downstream evidence Step 8 did not gather (this experiment is resolver-level
and deterministic by design).

**6. If yes, what is the smallest safe change?** Should that combined change be
approved later:

> In `CanonicalEntityRegistry`, reject a candidate when it carries `:Document`
> **and no other real label**, applied to **both** the fuzzy rung and the
> rungs-1–4 paths — with the threshold moved to 0.78 in the same change.

It must be a *pure*-Document test (never `"Document" in labels`), must apply to
both paths, and must not touch the full-text index. `tests/test_candidate_filtering.py`
pins all three properties, and one test fails loudly if filtering appears in
production without a controlled rollout.

**7. If no, why not now?** Because the honest measurement is that the filter
alone yields zero improvement, and the combined change trades 3 wrong
resolutions for 4 (gaining `K8`, `CCTV Analysis`, `typo-neo4j`,
`LiteLLM Proxy Server` but admitting `DB` → *Dar Al Ber Society*). That is a
defensible trade but not an obvious one, and it changes production behaviour on
the strength of resolver-level evidence only.

**8. What should Step 9 investigate?** The evidence points away from the resolver
entirely:

- **Mention extraction** is the largest untouched lever. 15 cases across the
  semantic and latent-inference cohorts never reach the resolver at all, and 3
  resolver-cohort cases extract no mention. No candidate or threshold work can
  recover them.
- **A disambiguation signal that does not exist yet** — acronym-expansion
  agreement would separate `DB`/`Dar Al Ber` (initials DAB ≠ DB) and `VLM`/`vLLM`
  where similarity, margin, degree and labels all fail.
- **Hybrid-node handling**: 7 nodes are both document and entity, and they are
  both the top contamination source (69 of 104 nodes) and the source of 5 correct
  resolutions. Splitting document-ness from entity-ness in the model — rather
  than filtering it — is the durable fix.
- **A downstream run of the combined `filter + 0.78` variant**, before any
  production change, to confirm the resolver-level gain survives end-to-end.

## H. Production integrity

Verified before and after; identical to the Step 7 pre-snapshot.

| Check | Value |
|---|---|
| Neo4j total / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| `kgtest-*` residue | 0 ✅ |
| Canaries | 6/6 ✅ |
| Qdrant `corporate_memory` / collections | 993 / 20, none new ✅ |
| Corpus | 310 files ✅ |
| Postgres `documents` | 0 rows ✅ |
| Full-text index | `FOR (n:Entity) ON EACH [canonical_name, aliases]` — unchanged ✅ |
| Resolver threshold in source | 0.82 — unchanged ✅ |

Read-only throughout: `resolve(allow_fuzzy=False)`, `fuzzy_candidates()`, `warm()`
and read-only Cypher. `register_alias()` / `save_alias_table()` never called; the
persisted alias table is untouched. The dataset was not modified — v3.1 hashes
re-verified before and after.
