# Step 9 — Mention Extraction and Semantic/Contextual Entity Resolution

**Question:** is the remaining resolver weakness caused by mention extraction,
lexical candidate generation, or candidate disambiguation?

**Answer: by mention extraction — but most of that "failure" is the extractor
being correct.** 62% of the no-mention population is *deliberate, appropriate*
suppression. Of the genuine misses, none is a missed name, alias or acronym;
every one is a conceptual reference. And semantic embeddings — the obvious fix —
**do not help**: as a fallback they gain 8 cases and break 7, doubling wrong
resolutions and increasing contamination by 71%.

Production was **not changed**. Threshold still 0.82, extractor untouched, no
Qdrant writes, no new model or infrastructure. Nothing staged, nothing committed.

| Artifact | Path |
|---|---|
| Results | `docs/evaluation/graphrag_mention_semantic_experiment_v1.json` |
| Manifest | `docs/evaluation/graphrag_mention_semantic_experiment_v1_manifest.json` |

**Method.** The Step 7 frozen mention set (174 cases, 99 unique mentions) was
reused verbatim, so extraction variability cannot contaminate the semantic
measurements (spec §3). Semantic evidence uses the platform's existing embedder
(`BAAI/bge-small-en-v1.5`, 384-dim) via `backend.ingest.get_embedder()`, with 687
entity surface forms embedded into an **in-memory numpy array** — no second model,
no new collection, nothing written to `corporate_memory` (§9). Lexical
exact/alias/normalization/fuzzy remain authoritative in every variant; semantic is
only ever consulted after lexical declines (§8).

---

## A. Layer A — mention extraction

**Population A (no mention extracted): 40 cases. Population B: 134.**

| Category | Cases | expects an entity | correctly silent |
|---|---:|---:|---:|
| A — exact name not extracted | **0** | 0 | — |
| B — alias not extracted | **0** | 0 | — |
| C — abbreviation not extracted | **0** | 0 | — |
| D — semantic paraphrase not extracted | 7 | 7 | 0 |
| E/F — generic noun suppressed / ambiguous | *(folded into H)* | — | — |
| G — latent inference | 5 | 5 | 0 |
| H — other | 28 | 3 | **25** |

Two findings decide this layer.

**1. Categories A, B and C are empty.** The extractor never misses a literal
entity name, a stored alias, or an acronym that is present in the question. Every
miss is a *conceptual* reference. So "make the extractor more aggressive about
names" is not an available improvement — there is nothing there to win.

**2. 25 of 40 (62%) are correct suppression.** These are the task, temporal,
memory, and RAG cases that legitimately name no entity (`tasks-overdue`,
`temporal-today`, `memory-team-pref`, `rag-handbook`, `ambig-platform`, …). The
extractor returning nothing is the right answer.

**Genuine misses: 15 of 174 cases (8.6%)** — 7 semantic paraphrase, 5 latent
inference, 3 other (`sem-speech-to-text`, `sem-object-storage`, `long-full-arch`).

## B. Generic-noun safety (§6)

For each phrase: is there an unambiguous entity behind it? "Gap" is the semantic
margin between the top candidate and the runner-up — a near-tie means the phrase
has several equally plausible referents and must **not** become a mention.

| Phrase | Unambiguous? | Semantic top | Gap | Safe to extract? |
|---|---|---|---:|---|
| `database` | no — near-tie | Azure SQL (0.7785) | 0.0042 | ❌ |
| `API` | yes | **API** (1.0000) | 0.1747 | ❌ attractor |
| `model` | yes | **Model** (1.0000) | 0.1984 | ❌ attractor |
| `graph database` | no — near-tie | Neo4j (0.8945) | 0.0339 | ❌ |
| `vector database` | yes | **Qdrant** (0.8703) | 0.1334 | ✅ |
| `mail API` | no — near-tie | API (0.8203) — **wrong** | 0.0827 | ❌ |
| `LLM` | no — near-tie | **Dubai Municipality** (0.8284) | 0.0744 | ❌ |
| `gateway` | no — near-tie | Gateway Abstraction (0.8575) | 0.0286 | ❌ |

**Only 1 of 8 generic phrases (`vector database`) has an unambiguous referent.**

Three results deserve emphasis:

- **`LLM` → Dubai Municipality at 0.8284.** LiteLLM is fourth at 0.7174. This is
  precisely the "generic noun → random entity" failure §15 warns about, and it
  would be introduced by any naive un-suppression.
- **The graph contains entities literally named `API` and `Model`**, both matching
  at similarity 1.0000. They are perfect generic-noun attractors: any question
  containing "model" or "API" would bind to them.
- **`mail API` retrieves the wrong entity.** Microsoft Graph is not even in the
  top 5 — despite carrying the aliases *"Graph API"* and *"The Graph API"*.

The extractor's suppression of generic nouns is therefore **well-founded**, and
removing it would be actively unsafe.

## C. Layer B — semantic resolution

Lexical stays authoritative; semantic is a fallback only.

| Variant | Correct | Wrong | Unresolved | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| **Lexical baseline** | 101 | **7** | 14 | **0.8968** | 0.7533 | **0.8188** |
| Semantic fallback (≥0.80) | **106** | **14** | 2 | 0.8067 | **0.8067** | 0.8067 |
| Semantic fallback + margin ≥0.05 | 101 | 9 | 12 | 0.8712 | 0.7667 | 0.8156 |

| Variant | Document FP | hybrid FP | contamination nodes |
|---|---:|---:|---:|
| Lexical baseline | 2 | 2 | **407** |
| Semantic fallback | 3 | 2 | **695** (+71%) |
| Semantic + margin | 2 | 2 | 493 (+21%) |

**Neither semantic variant improves F1.** The permissive one doubles wrong
resolutions (7 → 14) and raises contamination by 288 nodes. The strict one gains
no correct resolutions at all while still adding 2 wrong ones.

What the permissive variant gains and breaks:

| Gained (8) | Broken (7) |
|---|---|
| `Qdrant Databse` → Qdrant | `DB` → **db-primary-01** (a Server) |
| `LiteLLM Proxy Server` → LiteLLM | `Graph Microsoft` → Microsoft Graph *(was correctly declined)* |
| `K8` → Kubernetes | `vLLM Engine` → vLLM *(was correctly declined)* |
| `CCTV Analysis` → CCTV Analytics | `docker compose` → **docker-compose.yml** |
| `Neo4G` → Neo4j | `gateway` → Gateway Abstraction |
| `Café Neo4j` → Neo4j | `inference model` → Inference Layer |
| `Agentic AI project` → Agentic AI (×2) | `GPU server` → GPU |

The gains are all **typo/variant** cases — the same population a lower threshold
already reaches (Step 7). The breaks are new, and three of them were cases the
lexical resolver correctly *declined*.

**Semantic does not reduce Document pollution.** It selects
`docker-compose.yml` itself — the same pure Document node that lexical matching
picks below 0.82. Document FP goes **up** (2 → 3), not down. This directly
answers §21.6: no.

## D. Acronym agreement (§12)

**The evidence is not present in the graph.** Only **4 of 522 entities (0.8%)**
store their own acronym as an alias (`Knowledge Graph`→KG, `Dubai
Municipality`→DM, `Ministry Of Economy & Tourism`→MoET, `Sharjah Airport
Authority`→SAA). None of them is a collision case.

Two algorithmic substitutes were tested rather than assumed:

| Rule | `DB`→Dar Al Ber | `VLM`→vLLM | `K8`→Kubernetes | `M365`→Microsoft | Verdict |
|---|---|---|---|---|---|
| initials match | reject ✅ | reject ✅ | **reject ❌** | — | breaks a true positive |
| subsequence of a stored form | **accept ❌** | **accept ❌** | accept ✅ | **reject ❌** | 1 of 4 correct |

The initials rule works on the two false positives but also rejects
`K8`→Kubernetes, because *K8s* is a numeronym, not an acronym (initials of
"Kubernetes" = "K"). The subsequence rule fails outright: `DB` *is* a subsequence
of "Dar Al Ber" and `VLM` *is* a subsequence of "vLLM".

> **Acronym agreement requires a future resolver capability.** It is not available
> from existing data, and neither obvious substitute generalises. Per §12 it was
> **not implemented**.

## E. Latency

| Stage | Cost |
|---|---:|
| corpus embedding (687 surface forms, once) | 1247 ms |
| per-mention embedding | **7.19 ms** |
| per-mention cosine search (in-memory, 687×384) | **5.92 ms** |
| added per mention | **≈13 ms** |
| lexical resolve (Step 6 measured) | ≈5–25 ms |

Semantic roughly doubles resolver latency per mention and needs a 1.2 s warm-up
or a persisted index. Modest in absolute terms — but it buys nothing measurable
here, so the cost has nothing to offset it.

## F. Known difficult cases

| Case | Lexical (production) | Semantic | Verdict |
|---|---|---|---|
| `graph database` | not extracted | Neo4j 0.8945, gap 0.0339 | semantic *could* resolve, but the gap is a near-tie with Microsoft Graph |
| `mail API` | not extracted | **API** (wrong); Microsoft Graph not in top 5 | semantic fails |
| `vector database` | not extracted | **Qdrant** 0.8703, gap 0.1334 | the one clean semantic win |
| `M365` | Microsoft @ 0.8182 — declines | — | threshold miss, unchanged |
| `DB` | correctly declines | **db-primary-01** — wrong | semantic makes it worse |
| `VLM` | vLLM — wrong | vLLM — wrong | unchanged |
| `model` | not extracted | **Model** @ 1.0000 | attractor — must stay suppressed |
| `API` | not extracted | **API** @ 1.0000 | attractor — must stay suppressed |
| `database` | not extracted | Azure SQL, gap 0.0042 | hopelessly ambiguous |

## G. Decision

**1. How many current failures are mention-extraction failures?** **15 of 174
cases (8.6%)** are genuine misses. Population A is 40, but 25 of those are correct
suppression.

**2. Can mention extraction be improved safely?** **Only very narrowly.** There is
nothing to gain on names, aliases or acronyms — those are already at 100%. The
only gap is conceptual reference, and 7 of the 8 tested generic phrases have no
unambiguous referent. Relaxing suppression would introduce `LLM` → *Dubai
Municipality* and bind every "model"/"API" question to the attractor entities.

**3. Does semantic candidate generation improve candidate recall?** Recall yes
(0.7533 → 0.8067), **F1 no** (0.8188 → 0.8067). The recall is bought with
precision that costs more than it returns.

**4. Does it increase wrong resolutions?** **Yes — it doubles them** (7 → 14), and
contamination rises 71% (407 → 695 nodes).

**5. Does it create generic-noun attractors?** **Yes.** `API` and `Model` are real
entities matching their generic phrase at similarity 1.0000, and `LLM` maps to
*Dubai Municipality* at 0.8284.

**6. Does it reduce Document/hybrid pollution?** **No — it increases it.** Semantic
picks `docker-compose.yml` itself; Document FP goes 2 → 3.

**7. Is acronym agreement already available?** **No.** 0.8% coverage, and both
substitute rules fail on the real cases.

**8. Is it worth implementing?** Not on this evidence. It would fix 2 known false
positives while breaking at least one true positive, and there is no way to
validate it beyond 3–4 data points in the current dataset.

**9. Is semantic resolution worth implementing now?** **No.** It does not improve
F1 in any configuration tested, it doubles wrong resolutions in the permissive
form, it adds nothing in the strict form, and it worsens both Document pollution
and contamination — the two metrics Steps 7–8 established as the safety
constraints.

**10. Smallest safe production change?** **None is recommended from Step 9.** The
only defensible change still on the table is the Step 8 candidate
(`pure-Document filter + threshold 0.78`), which is unaffected by these results
and still requires downstream validation. Step 9 adds no new production candidate.

**11. What should remain unchanged?** Threshold 0.82; the mention-extractor prompt
and its generic-noun suppression (now positively justified rather than merely
inherited); the full-text index; `corporate_memory`; the alias table; graph depth,
fusion and ranking; and dataset v3.1.

**12. Is Step 10 graph-depth experimentation now safe to begin?** **Yes.** The
resolver stage is characterised end to end and closed for now: threshold (Step 7),
candidate quality (Step 8), extraction and semantics (Step 9) have each been
measured and each returned "do not change production". Nothing in the resolver
will shift underneath a depth experiment. One caveat to carry forward: 15 cases
never produce a mention, so their graph seeds are empty regardless of depth —
Step 10 should exclude them from depth cohorts or they will look like depth
failures.

## H. Why the negative results are the useful ones

Three plausible fixes were tested and all three failed, each for a measured
reason rather than an assumed one:

- *"Extract generic nouns too"* → 7 of 8 phrases are ambiguous; `LLM` binds to
  Dubai Municipality.
- *"Use embeddings for resolution"* → doubles wrong resolutions, worsens
  contamination, still picks the Document.
- *"Use acronym agreement"* → the data has 0.8% coverage, and both substitute
  rules break real cases.

The resolver's remaining errors are **not** caused by a missing signal that is
cheaply available. `DB`/`Dar Al Ber Society` and `VLM`/`vLLM` are genuinely
under-determined by every signal present in the system — lexical similarity,
margin, degree, labels, embeddings and acronym evidence all fail on them. That is
a bounded, well-characterised limitation, and the honest conclusion is to stop
optimising the resolver and move to the graph stage.

## I. Production integrity

Verified before and after; identical to the Step 7 pre-snapshot.

| Check | Value |
|---|---|
| Neo4j total / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| `kgtest-*` residue | 0 ✅ |
| Canaries | 6/6 ✅ |
| Qdrant `corporate_memory` / collections | 993 / 20, none created ✅ |
| Corpus | 310 files ✅ |
| Postgres `documents` | 0 rows ✅ |
| Resolver threshold in source | 0.82 ✅ |
| Alias table | untouched ✅ |
| Dataset v3.1 hashes | re-verified unchanged ✅ |

Entity embeddings existed only as an in-process numpy array and were never
persisted. Read-only throughout: `resolve(allow_fuzzy=False)`,
`fuzzy_candidates()`, `warm()`, read-only Cypher, and the existing embedder.
