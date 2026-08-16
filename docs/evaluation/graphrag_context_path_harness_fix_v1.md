# Step 11.6 — Evaluation Harness Provider-Path Correction

**A harness correctness fix, not a tuning experiment.** No GraphRAG production
behaviour, configuration, dataset, Neo4j or Qdrant state was changed. Nothing
staged, nothing committed. Previous artifacts were left byte-identical.

One source file changed: `backend/evals/runner.py`. One test file added.

| Artifact | Path |
|---|---|
| Results | `docs/evaluation/graphrag_context_path_harness_fix_v1.json` |
| Manifest | `docs/evaluation/graphrag_context_path_harness_fix_v1_manifest.json` |
| Tests | `tests/test_eval_provider_path.py` (11 passing) |
| Diagnostic that found this | `docs/evaluation/graphrag_context_path_diagnostic_v1.md` |

---

## A. The defect

`EvaluationRunner` retrieved the graph **twice**, and `RunnerConfig` configured
only the copy that never reached the model:

```
_stage_graph      GraphRetrievalAPI(depth=config.graph_depth)
                  .retrieve(top_k=config.graph_top_k)
                  └──> trace.graph_*                      ← METRICS ONLY

_stage_providers  ContextBuilder(providers=config.providers)   # None by default
                  └── registry → GraphProvider(top_k=8,        # constructor default
                                               depth=settings.GRAPH_RETRIEVAL_DEPTH)
                      └──> ContextBundle → prompt → answer     ← THE ANSWER PATH
```

`RunnerConfig.providers` defaults to `None`, so `ContextBuilder` resolved the
global registry and the runner's `graph_top_k` / `graph_depth` never reached the
provider that builds the prompt.

**Consequence:** a `top_k` or `depth` sweep moved `trace.graph_*` exactly as
intended while the prompt stayed **byte-identical**, so every end-to-end answer
reading came back "no effect" for plumbing reasons.

## B. The fix

`EvaluationRunner._answer_path_providers()` — substitutes **only** the graph
provider into the registry's list:

```python
if self.config.providers is not None:
    return self.config.providers          # explicit override wins, untouched

provs = provider_registry.all_providers()
graph = next((p for p in provs if p.name == "graph"), None)
if graph is None:
    return None
if (graph.top_k == self.config.graph_top_k
        and graph.depth == self.config.graph_depth):
    return None                            # already identical — change nothing

replacement = GraphProvider(enabled=graph.enabled,
                            top_k=self.config.graph_top_k,
                            depth=self.config.graph_depth)
return [replacement if p.name == "graph" else p for p in provs]
```

`_stage_providers` now calls `ContextBuilder(providers=self._answer_path_providers())`.

Four properties make this safe:

1. **Default runs are unchanged by construction.** When the config already matches
   the registry — which it does at the production defaults `top_k=8, depth=1` —
   the method returns `None`, so the builder resolves the registry exactly as
   before, *by identity*. It is not "equivalent behaviour", it is the same objects.
2. **Only the graph provider is ever replaced.** Every other provider is passed
   through as the same object.
3. **The shared registry instance is never mutated.** A fresh `GraphProvider` is
   constructed, so an experiment cannot leak its settings into production code
   paths or into later tests in the same process.
4. **`enabled` is inherited** from the registry instance, so a deployment that
   disabled the graph via `CONTEXT_GRAPH_ENABLED=false` is not silently re-enabled.

Nothing else was touched: graph ranking, fusion, budget, resolver, mention
extraction, Neo4j, Qdrant, the dataset and all production settings are unchanged.

## C. Provider invariant

Registry set: `corporate, memory, graph, calendar, tasks, sql, history`.

With `RunnerConfig(graph_top_k=4, graph_depth=2)`:

| Check | Result |
|---|---|
| Provider names and order | identical to baseline ✅ |
| Non-graph providers | preserved **by object identity** (`old is new`) ✅ |
| `corporate` / `tasks` / `calendar` present | ✅ |
| Registry graph instance mutated | **no** — a new instance is built ✅ |
| Registry graph `top_k` / `depth` after the call | unchanged (8 / 1) ✅ |
| Live run: provider sets identical across all 5 variants | ✅ |

This is precisely the mistake Step 11.5 made in its first draft — replacing the
whole provider list, which silently dropped corporate/tasks/calendar and
conflated "graph changed" with "other evidence removed". The invariant is now
pinned by tests rather than by care.

## D. Tests

`tests/test_eval_provider_path.py` — **11 passing**:

| Test | Asserts |
|---|---|
| `graph_top_k_reaches_the_provider…` | `RunnerConfig(graph_top_k=4)` → provider `top_k == 4` |
| `graph_depth_reaches_the_provider…` | `RunnerConfig(graph_depth=2)` → provider `depth == 2` |
| `both_graph_parameters_are_applied_together` | both applied at once |
| `every_other_provider_is_preserved_by_identity` | `old is new` for all non-graph providers |
| `named_providers_survive_the_substitution` | corporate/tasks/calendar/graph present |
| `the_shared_registry_instance_is_never_mutated` | registry object and its values unchanged |
| `default_config_leaves_the_registry_untouched` | default → returns `None` |
| `config_matching_the_registry_is_a_no_op` | matching values → returns `None` |
| `explicit_providers_override_is_passed_through_untouched` | escape hatch still works |
| `production_defaults_are_unchanged` | `(1, 8, 0.6, 5)` and provider default `top_k=8` |
| `graph_enabled_flag_is_carried_over` | `CONTEXT_GRAPH_ENABLED` state inherited |

The first test is the regression guard: if the substitution is reverted,
`_stage_graph` still honours `graph_top_k` and the metrics still look correct —
only this test fails.

Full backend suite: **367 passed, 4 failed** — the 4 are the known pre-existing
`tests/test_analytics.py` failures (`backend/analytics.py` has no `DB_PATH`), not
touched and not fixed.

## E. Live validation

Eight representative cases through the **real provider path**, mentions pinned to
the Step 7 frozen set so only the tested parameter moves.

### Top-k

| Case | Kind | Graph items 4/8/12 | Context changed | Prompt changed | Answer changed |
|---|---|---|---|---|---|
| `d1-neo4j-hosted-on` | graph-heavy | 3 / 5 / 8 | **YES** | **YES** | **YES** |
| `d1-litellm-routes-to` | graph ranking failure | 3 / 6 / 8 | **YES** | **YES** | **YES** |
| `fus-qdrant-better-benchmark` | graph + Qdrant | 3 / 4 / 7 | **YES** | **YES** | no |
| `conflict-graph-vs-rag` | graph/Qdrant conflict | 1 / 4 / 6 | **YES** | **YES** | **YES** |
| `kb-uses-litellm` | normal enterprise | 4 / 3 / 5 | **YES** | **YES** | no |
| `tasks-urgent` | graph + tasks | 0 / 0 / 0 | no | no | no |
| `temporal-today` | graph + calendar | 0 / 0 / 0 | no | no | *(see below)* |
| `long-full-arch` | no graph seed | 0 / 0 / 0 | no | no | no |

Context size tracks it, e.g. `d1-neo4j-hosted-on`: 2073c/1281t → 2138c/1295t →
2240c/1317t.

**The prompt now changes in every case that has graph evidence (5/5).** The three
that do not change resolve no entity, so they contribute 0 graph items at any
`top_k` — correct behaviour, not a wiring failure.

Before the fix, the same sweep produced byte-identical prompts in **all eight**
cases.

### Depth

| Case | Graph items 1/2 | Context changed | Prompt changed |
|---|---|---|---|
| all 8 cases | identical | **no** | **no** |

Depth still changes nothing — now demonstrated through the corrected path. This
**confirms Step 10's conclusion independently**: it was a genuine property, not a
wiring artifact. It is also exactly what Step 11 predicts, since no hop-2 node
survives `top_k = 8`.

### Two observations worth recording

**LLM nondeterminism at temperature 0.** `temporal-today` produced an identical
prompt hash (`270ad39b`) across all three variants but two different answers —
*"I do not know what is happening today."* vs *"I do not have information about
what is happening today…"*. Both are refusals, but the hashes differ. Answer-level
metrics therefore retain a small noise floor even with identical input, consistent
with the 0.6% measured in the corrected baseline. Any future answer-level
comparison must treat single-case answer changes as weak evidence.

**Graph item counts are not monotonic in `top_k`.** `kb-uses-litellm` yields
4 / 3 / 5 items at k = 4 / 8 / 12. `top_k` truncates the subgraph *before* fusion,
so a different node set changes what fusion merges and how the budget allocates.
Not a defect — but it means "more `top_k`" does not mean "more graph items in the
prompt", and a future experiment should not assume it does.

## F. Historical correction

**No previous artifact was overwritten or edited.** Steps 6–11.5 files are
byte-identical; this correction is additive.

**Still valid** — these came from `trace.graph_*`, which the experiments genuinely
controlled:

- depth 2/3 does not improve graph retrieval metrics *(re-confirmed here through the corrected path)*
- depth 3 is blocked by `max_nodes = 100`
- `graph_top_k > 8` worsens graph F1
- `hop_decay` is inert at depth 1
- graph ranking is freshness-dominated
- expected graph candidates often rank poorly; graph noise is high

**Withdrawn** — these varied `_stage_graph` while `_stage_providers` stayed fixed:

- *"changing graph context produces zero answer change"* (Steps 10 and 11)
- *"`top_k = 4` delivers no answer benefit"* (Step 11)
- any other end-to-end answer conclusion resting on that sweep

The live validation above already contradicts the first: with the fix, graph
changes alter answers in 3 of 5 graph-bearing cases.

**Not re-established here.** Step 11.6 validated wiring on 8 cases; it did not
re-run the benchmark. The end-to-end effect of `top_k = 4` across all 174 cases is
now *unmeasured* rather than *measured as zero*. Whoever wants that number must
re-run — and should note that the corrected harness makes every previous
end-to-end graph comparison non-reproducible by construction.

## G. Production state

| Check | Value |
|---|---|
| `GRAPH_RETRIEVAL_DEPTH` | 1 — unchanged ✅ |
| `RunnerConfig.graph_top_k` default | 8 — unchanged ✅ |
| `GraphProvider.top_k` constructor default | 8 — unchanged ✅ |
| fusion threshold / `qdrant_top_k` | 0.6 / 5 — unchanged ✅ |
| resolver threshold / `hop_decay` / `max_nodes` | 0.82 / 0.55 / 100 — unchanged ✅ |
| Default `RunnerConfig` → provider substitution | **no-op (returns `None`)** ✅ |
| Neo4j total / `:Entity` / relationships | 523 / 522 / 3414 ✅ |
| `kgtest-*` residue / canaries | 0 / 6-of-6 ✅ |
| Qdrant `corporate_memory` / collections | 993 / 20, none created ✅ |
| Corpus / Postgres `documents` | 310 files / 0 rows ✅ |
| Dataset v3.1 hashes | re-verified unchanged ✅ |
| Unrelated developer files | untouched ✅ |

All Neo4j and Qdrant access was read-only.

## H. Decision

**1. Is the harness now correctly controlling the real GraphProvider?** **Yes** —
proven twice: by unit test (`RunnerConfig(graph_top_k=4)` → provider `top_k == 4`)
and by live run (prompt hashes now differ in every graph-bearing case, where
before they were identical in all eight).

**2. Are all other providers preserved?** **Yes** — by object identity, with order
and membership unchanged, verified in both the unit tests and the live run.

**3. Can Step 12 fusion experiments now safely vary graph inputs through
`RunnerConfig`?** **Yes.** `graph_top_k` and `graph_depth` now reach the answer
path, and fusion parameters (`fusion_threshold`, `weights`, `budget_policy`) were
always on it via `ContextBuilder`.

**4. Does any remaining wiring defect block Step 12?** **No blocker.** Three
things should be carried forward as known properties rather than surprises:

- The graph is still retrieved **twice** per case — once for metrics, once for the
  answer. They are now configured consistently, but it is duplicated work
  (~40–190 ms per case) and a latent source of divergence if one path is changed
  without the other. Consolidating them is a reasonable future cleanup; it was not
  attempted here because it would change what the metrics measure.
- Answer metrics carry a small nondeterminism floor even at temperature 0.
- Graph item count is not monotonic in `top_k`.
