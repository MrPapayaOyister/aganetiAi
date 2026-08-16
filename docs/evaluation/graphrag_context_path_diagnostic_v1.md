# Step 11.5 — Graph-to-Answer Context Trace (Diagnostic)

**Question:** does graph retrieval actually influence the final answer-generation
context?

**Answer: yes — but the knob Steps 10 and 11 turned does not.** The evaluation
harness performs **two independent graph retrievals**. The one configured by
`RunnerConfig.graph_depth` / `graph_top_k` feeds only the metrics; the prompt is
built from a *separate* `GraphProvider` that never sees those settings. That is a
harness plumbing defect, and it is the direct cause of the "answer metrics
identical to four decimals" result reported in Steps 10 and 11.

When the graph provider the prompt actually uses is varied, the prompt changes in
**7 of 8** cases and the answer changes in **5 of 8**.

Nothing was changed: production config, dataset and source are untouched. Nothing
staged, nothing committed.

| Artifact | Path |
|---|---|
| Results | `docs/evaluation/graphrag_context_path_diagnostic_v1.json` |
| Manifest | `docs/evaluation/graphrag_context_path_diagnostic_v1_manifest.json` |

---

## 1. The defect, in code

```
run_case()
├── _stage_graph(case, trace)
│      GraphRetrievalAPI(depth=config.graph_depth)
│      .retrieve(depth=config.graph_depth, top_k=config.graph_top_k)
│      └──> trace.graph_nodes / graph_hops / graph_relationships   ← METRICS ONLY
│
├── _stage_providers(case, trace)
│      ContextBuilder(providers=config.providers)      # config.providers is None
│      └── global registry → GraphProvider(top_k=8, depth=GRAPH_RETRIEVAL_DEPTH)
│          └──> ContextBundle                          ← a SECOND, independent retrieval
│
├── _stage_fuse_rank_compress_budget(...)   → trace.final_items
├── _stage_prompt(bundle, trace)            → trace.prompt_sections   ← FROM THE BUNDLE
└── _stage_answer(...)                      → trace.answer
```

`RunnerConfig.graph_top_k` and `graph_depth` reach only the first path.
`RunnerConfig.providers` defaults to `None`, so `ContextBuilder` builds its own
`GraphProvider` with `top_k = 8` hardcoded as the constructor default and depth
read from `config.settings.GRAPH_RETRIEVAL_DEPTH`.

Confirmed at runtime, not only by reading: the logs show
`kg resolver: 1 mention(s) → 1 resolved` **twice per case** — once per retrieval.

## 2. Variant A — exactly what Steps 10 and 11 varied

`RunnerConfig.graph_top_k` = 4 / 8 / 12, everything else fixed:

| Case | metric graph nodes (4/8/12) | prompt hash (4/8/12) | prompt changed |
|---|---|---|---|
| `d1-neo4j-hosted-on` | 4 / 8 / 12 | `f4a8e88714` ×3 | **no** |
| `d1-litellm-routes-to` | 4 / 8 / 12 | `d239e13b81` ×3 | **no** |
| `hop2-serving-stack` | 4 / 8 / 12 | `6f5b44828a` ×3 | **no** |
| `fus-qdrant-better-benchmark` | 4 / 8 / 12 | `b076c67b88` ×3 | **no** |
| `fus-corroborated-litellm-vllm` | 4 / 8 / 12 | `5fb7142289` ×3 | **no** |
| `conflict-graph-vs-rag` | 4 / 8 / 12 | `15595c5bbd` ×3 | **no** |
| `long-full-arch` | 0 / 0 / 0 | `ddcf58b749` ×3 | no *(no seed)* |
| `d2-agentic-ai-gateway-host` | 4 / 8 / 12 | `150ee568e0` ×3 | **no** |

The metric-stage node count tracks `top_k` perfectly (4/8/12) while the **prompt
hash is byte-identical in all eight cases**. Steps 10 and 11 were measuring a
retrieval that never reached the model.

## 3. Variant B — the same `top_k`, injected where the prompt actually reads it

The graph provider was swapped **while keeping every other provider identical**
(verified: provider sets match across all variants — an earlier draft of this test
replaced the whole provider list and had to be corrected, since that conflated
"graph changed" with "corporate/tasks/calendar removed").

| Case | graph items (8/4/12/d2) | prompt hash (8/4/12/d2) | prompt changed | answer changed |
|---|---|---|---|---|
| `d1-neo4j-hosted-on` | 5/3/8/5 | `f4a8e88`/`36ad1df`/`59628ba`/`f4a8e88` | **YES** | **YES** |
| `d1-litellm-routes-to` | 6/3/8/6 | `d239e13`/`21493cf`/`d3bd1c4`/`d239e13` | **YES** | **YES** |
| `hop2-serving-stack` | 6/3/8/6 | `6f5b448`/`d576d73`/`ece4d62`/`6f5b448` | **YES** | **YES** |
| `fus-qdrant-better-benchmark` | 4/3/7/4 | `b076c67`/`7397c1d`/`68ecb1a`/`b076c67` | **YES** | no |
| `fus-corroborated-litellm-vllm` | 6/3/8/6 | `5fb7142`/`efdb17f`/`05b6b7b`/`5fb7142` | **YES** | **YES** |
| `conflict-graph-vs-rag` | 4/1/6/4 | `15595c5`/`2dccc10`/`8526863`/`15595c5` | **YES** | **YES** |
| `long-full-arch` | 0/0/0/0 | `ddcf58b` ×4 | no *(no seed)* | no |
| `d2-agentic-ai-gateway-host` | 5/1/6/5 | `150ee56`/`60509e9`/`12b0a0a`/`150ee56` | **YES** | no |

Context size moves with it — e.g. `d1-neo4j-hosted-on`: 2073 chars / 1281 tokens
at k=4, 2138 / 1295 at k=8, 2240 / 1317 at k=12.

**Provider depth = 2 produces a byte-identical prompt to production in every
case** (`f4a8e88` = `f4a8e88`). This is important: it means **Step 10's depth
conclusion is genuine, not an artifact.** Even through the correct path, depth 2
changes nothing — consistent with Step 11's finding that no hop-2 node survives
`top_k = 8`.

## 4. Variant C — graph evidence removed entirely

| Case | graph items | prompt changed | answer changed |
|---|---|---|---|
| `d1-neo4j-hosted-on` | 5 → 0 | YES | no |
| `d1-litellm-routes-to` | 6 → 0 | YES | **YES** |
| `hop2-serving-stack` | 6 → 0 | YES | **YES** |
| `fus-qdrant-better-benchmark` | 4 → 0 | YES | no |
| `fus-corroborated-litellm-vllm` | 6 → 0 | YES | **YES** |
| `conflict-graph-vs-rag` | 4 → 0 | YES | **YES** |
| `long-full-arch` | 0 → 0 | no | no |
| `d2-agentic-ai-gateway-host` | 5 → 0 | YES | **YES** |

The prompt carries an explicit `[KNOWLEDGE GRAPH]` section that disappears when
the provider is disabled:

```
with graph : ['[COMPANY KNOWLEDGE]', '[KNOWLEDGE GRAPH]']
no graph   : ['[COMPANY KNOWLEDGE]']
```

Graph evidence demonstrably reaches the model and demonstrably changes answers.

## 5. Required table

| Case | Graph retrieved | Graph in fusion | Graph in prompt | Qdrant in prompt | Prompt changed¹ | Answer changed¹ | Classification |
|---|---|---|---|---|---|---|---|
| `d1-neo4j-hosted-on` | yes | 5 items | yes | yes | YES | YES | **A** |
| `d1-litellm-routes-to` | yes | 6 items | yes | yes | YES | YES | **A** |
| `hop2-serving-stack` | yes | 6 items | yes | yes | YES | YES | **A** |
| `fus-corroborated-litellm-vllm` | yes | 6 items | yes | yes | YES | YES | **A** |
| `conflict-graph-vs-rag` | yes | 4 items | yes | yes | YES | YES | **A** |
| `fus-qdrant-better-benchmark` | yes | 4 items | yes | yes | YES | no | **B** |
| `d2-agentic-ai-gateway-host` | yes | 5 items | yes | yes | YES | no | **B** |
| `long-full-arch` | no (no seed) | 0 | no | yes | no | no | no evidence |
| **all 8, via `RunnerConfig.graph_top_k`** | yes | — | — | — | **NO** | **NO** | **C** |

¹ when the *provider* is varied. The final row is what Steps 10/11 actually did.

Classification key: **A** graph affects prompt and answer · **B** affects prompt
but not the answer · **C** graph retrieval changes but the prompt does not.

## 6. Fusion and budget inspection

| | |
|---|---|
| window | 8192 |
| reserves | system 1200 + history 1500 + response 1024 |
| **available** | **4468** |
| per-provider shares | corporate 0.35, **graph 0.25**, memory 0.20, calendar 0.15, tasks 0.15, history 0.10 |
| **graph cap** | **≈1117 tokens** |
| **observed graph allocation** | **34 / 46 / 46 tokens** |
| observed `tasks` allocation | **717 tokens** |
| `dropped_for_budget` | **0** |

**There is a per-provider cap, but it is not binding.** Graph uses **3–4% of its
1117-token allowance**, and nothing is dropped for budget. Fusion does not discard
graph evidence — it merges duplicates and "never drops information".

The striking number is the comparison: `tasks` consumes **717 tokens (>50% of the
context)** while graph gets 34–46. Graph evidence reaches the prompt, but as a
very small share of it. That, rather than any cap, is why some prompt changes do
not move the answer.

## 7. LLM determinism

Answers are generated at `temperature = 0.0`, `max_tokens = 300`, model
`qwen-fast`. Throughout this diagnostic, **identical prompt hashes always produced
identical answer hashes**, and every answer difference was accompanied by a prompt
difference. So the answer-change column reflects genuine context sensitivity, not
sampling noise — and where the prompt changed but the answer did not (Class B),
the correct reading is *"graph evidence reached the model and the model's answer
was insensitive to it"*, not *"graph evidence never arrived"*.

## 8. Decision

**1. Does graph retrieval reach the final LLM context?** **Yes.** There is a
dedicated `[KNOWLEDGE GRAPH]` prompt section carrying 4–6 items, ~34–46 tokens.

**2. Does changing `graph_top_k` change the final context?** **Through
`RunnerConfig`: no** — prompt hashes identical at 4/8/12. **Through the provider:
yes** — prompt changes in 7/8 cases and answers in 5/8.

**3. Does changing depth change the final context?** **No**, even through the
correct path: provider depth 2 yields byte-identical prompts. Step 10's conclusion
stands on its own merits.

**4. Can fusion discard graph evidence?** No. Fusion merges duplicates and does
not drop information; `dropped_for_budget = 0` in every case measured.

**5. Is there a hidden context budget?** Yes — an 8192 window with 3724 reserved
and a 25% graph share (~1117 tokens) — but it is **not binding**: graph uses 3–4%
of it.

**6. Does graph evidence materially change prompts?** Yes: 7/8 cases, with
measurable context-size deltas.

**7. Does graph evidence materially change answers?** Yes: 5/8 when varying graph
`top_k`, and 5/8 when removing graph entirely.

**8. Why were the Step 10/11 answer metrics insensitive?** **Because the
experiments varied a parameter that only feeds the metrics stage.** The prompt was
byte-identical across every configuration, so the answers could not differ. This
is a harness defect, not a property of GraphRAG.

**9. Is Graph/Qdrant fusion safe to experiment on next?** **Not yet — fix the
plumbing first.** A fusion experiment that varies graph inputs through
`RunnerConfig` would reproduce exactly the same false negative. Fusion weights
live in `ContextBuilder` and *are* on the answer path, so a fusion-only experiment
would be valid; but any experiment touching graph retrieval parameters must go
through the provider.

**10. Is any plumbing defect blocking Step 12?** **Yes, one**, stated precisely:

> `EvaluationRunner` does not pass `config.graph_depth` / `config.graph_top_k` to
> the `GraphProvider` used by `ContextBuilder`. The smallest correct fix is for
> `_stage_providers` to construct the builder with a `GraphProvider(top_k=
> config.graph_top_k, depth=config.graph_depth)` substituted into the registry's
> provider list, leaving all other providers untouched — the same substitution
> this diagnostic performed in-process.

Not implemented here: Step 11.5 is diagnostic only, and this changes what every
prior baseline measured.

## 9. What this means for Steps 10 and 11

| Conclusion | Status |
|---|---|
| Depth 2/3 does not improve graph metrics | **Stands** — re-confirmed via the provider path |
| `max_nodes = 100` blocks depth 3 | **Stands** — measured directly on the retriever |
| `top_k > 8` lowers graph F1 | **Stands** — a property of retrieval, measured correctly |
| `hop_decay` is inert at depth 1 | **Stands** — structural, proven from the formula |
| Ranking is freshness-dominated | **Stands** — measured on real candidate signals |
| *"Changing graph context produces zero answer change"* | **WITHDRAWN** — an artifact of this defect |
| *"top_k = 4 delivers no answer benefit"* | **WITHDRAWN** — untested on the real answer path |

The graph-metric findings of Steps 10 and 11 are unaffected, because they were
computed from `trace.graph_*`, which the varied parameter genuinely controls. Only
the *end-to-end* claims are invalid, and they are withdrawn above rather than
quietly left standing.

## 10. Production integrity

Verified before and after; snapshots identical.

| Check | Value |
|---|---|
| Neo4j total / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| `kgtest-*` residue | 0 ✅ |
| Canaries | 6/6 ✅ |
| Qdrant `corporate_memory` / collections | 993 / 20 ✅ |
| Corpus | 310 files ✅ |
| Postgres `documents` | 0 rows ✅ |
| `graph_top_k` / depth / fusion / `qdrant_top_k` | 8 / 1 / 0.6 / 5 — unchanged ✅ |
| Dataset v3.1 hashes | re-verified unchanged ✅ |

All Neo4j and Qdrant access was read-only. Provider substitution and the frozen
mention extractor were in-process monkeypatches, restored on exit; no production
file was modified. Prompts were hashed rather than stored, with only short
redacted excerpts retained.
