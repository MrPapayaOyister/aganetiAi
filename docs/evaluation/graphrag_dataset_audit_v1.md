# GraphRAG Dataset Audit v1

Step 3 of the GraphRAG Quality Gate. **Audit only — no expectation was changed.**

## A. Dataset summary

- Total cases: **116** (core_retrieval.json + enterprise.json)
- Difficulty: easy 32 / medium 44 / difficult 32
- Cases asserting graph nodes or relationships: **76**
- Cases asserting graph must NOT contribute: **40**
- Multi-hop (depth>1): **14** (depth2=9, depth3=4, depth4=1); tagged `multi-hop`: 11
- Negative-premise (negative=True): **6**; adversarial false-premise not flagged: **3**
- Semantic-paraphrase cases: **8**
- Cases with expected_relationships: 15 · expected_qdrant_documents: 11 · corroboration: 7

## Classification totals

- **STALE**: 79
- **NEEDS_REVIEW**: 25
- **INFRASTRUCTURE_DEPENDENT**: 11
- **TRUSTED**: 1

## B. Case-by-case audit

| Case | Family | Status | Reason |
|---|---|---|---|
| `email-from-person` | email,needs:email | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `email-recent` | email,needs:email | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `memory-neo4j-decision` | memory,needs:memory | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `memory-past-project` | memory,needs:memory | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `memory-preference-currency` | memory,needs:memory | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `memory-reporting-style` | memory,needs:memory | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `memory-team-pref` | memory,needs:memory | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `temporal-after-tomorrow` | temporal,calendar | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `temporal-this-week` | temporal,calendar | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `temporal-today` | temporal,calendar | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `temporal-tomorrow` | temporal,calendar | INFRASTRUCTURE_DEPENDENT | provider expectation omits calendar (now contributes to 100% of cases); depends on a live provider (needs:* tag) |
| `abbrev-api` | graph,abbreviation | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); requires SEMANTIC resolution; lexical resolver cannot satisfy it |
| `abbrev-db` | graph,abbreviation | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); requires SEMANTIC resolution; lexical resolver cannot satisfy it |
| `abbrev-llm` | graph,abbreviation | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); requires SEMANTIC resolution; lexical resolver cannot satisfy it |
| `adv-invented-rel` | adversarial,negative-premise | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); false-premise question but negative=False, so refusal is not scored |
| `adv-leading-false` | adversarial,negative-premise | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); false-premise question but negative=False, so refusal is not scored |
| `adv-leading-person` | adversarial,negative-premise | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); false-premise question but negative=False, so refusal is not scored |
| `adv-very-long-question` | long-context,multi-entity | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop2-akshay-stack` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop2-from-postgres` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop2-inference-chain` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop2-model-behind` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop2-serving-stack` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop3-blast-radius` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop3-full-chain` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop3-impact` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop3-reverse` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `hop3-shared-dependency` | graph,multi-hop | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `long-full-arch` | long-context,fan-out | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `long-onboarding` | long-context,budget | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `multi-entity-3` | multi-entity,graph | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); multi-hop beyond shipped GRAPH_RETRIEVAL_DEPTH=1 |
| `syn-gateway` | graph,synonym | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); requires SEMANTIC resolution; lexical resolver cannot satisfy it |
| `syn-graph-db` | graph,synonym | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); requires SEMANTIC resolution; lexical resolver cannot satisfy it |
| `syn-mail-api` | graph,synonym | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); requires SEMANTIC resolution; lexical resolver cannot satisfy it |
| `syn-relational` | graph,synonym | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); requires SEMANTIC resolution; lexical resolver cannot satisfy it |
| `syn-vector-db` | graph,synonym | NEEDS_REVIEW | provider expectation omits calendar (now contributes to 100% of cases); requires SEMANTIC resolution; lexical resolver cannot satisfy it |
| `adv-injection` | adversarial,injection | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `adv-mixed-known-unknown` | adversarial,partial-negative | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `adv-repeated-entity` | edge-case,adversarial | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `alias-litellm-proxy` | graph,alias | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `alias-ms-graph` | graph,alias | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `alias-ms-graph-usage` | graph,alias | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `alias-msgraph-nospace` | graph,alias | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `alias-postgres` | graph,alias | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `alias-postgres-platform` | graph,alias | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `alias-qwen-fast-spaced` | graph,alias | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `ambig-graph-word` | ambiguity,alias | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `ambig-it` | ambiguity,pronoun | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `ambig-model` | ambiguity | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `ambig-platform` | ambiguity,long-context | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `case-lower` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `case-mixed` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `case-upper` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `conflict-doc-vs-memory` | conflict,cross-source | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `conflict-graph-vs-rag` | conflict,cross-source | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `conflict-stale-fact` | conflict,freshness | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `conflict-two-owners` | conflict,people | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `corrob-single-source` | corroboration,provenance | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `corrob-strongest` | corroboration,ranking | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `cross-akshay-doc` | cross-source,graph | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `cross-corroborated` | cross-source,corroboration | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `cross-stack-doc` | cross-source,conflict | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `empty-ish` | edge-case,degenerate | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `fanout-datastores` | graph,fan-out | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `fanout-everything-agentic` | graph,fan-out | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `fanout-technologies` | graph,fan-out | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `graph-direct-usage` | graph,core | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `graph-two-hop-chain` | graph,multi-hop | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `kb-hosted-on` | graph,relationship-reasoning | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `kb-routes-to` | graph,relationship-reasoning | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `kb-stores-postgres` | graph | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `kb-uses-litellm` | graph,knowledge-lookup | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `kb-uses-msgraph` | graph | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `kb-uses-neo4j` | graph | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `kb-uses-qdrant` | graph | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `kb-who-works-on` | graph,people | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `long-compare-stores` | long-context,multi-entity | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `long-everything` | long-context,budget | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `mixed-graph-and-docs` | graph,fusion | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `multi-entity-alias-mix` | multi-entity,alias | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `neg-competitor` | negative | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `neg-google-founder` | negative | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `neg-mars` | negative | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `neg-unknown-person` | negative,people | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `neg-zorblatt` | negative | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `person-project` | graph,core | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `provenance-cite` | provenance,citation | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `punct-heavy` | edge-case,punctuation | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-audio` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-budget` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-findings` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-handbook` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-kpi` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-ministry` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-multi-ocr` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-ocr` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-phase2` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-portfolio` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `rag-testing` | rag,qdrant | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `short-query-litellm` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `short-query-neo4j` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `tasks-count` | tasks,structured | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `tasks-only` | providers,structured | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `tasks-overdue` | tasks,structured | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `tasks-pending` | tasks,structured | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `tasks-urgent` | tasks,structured | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `temporal-next-due` | temporal,tasks | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `temporal-overdue-week` | temporal,tasks | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `typo-agentic` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `typo-litellm` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `typo-msgraph` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `typo-neo4j` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `typo-postgre` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `typo-qdrant` | graph,edge-case | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `unicode-entity` | edge-case,unicode | STALE | provider expectation omits calendar (now contributes to 100% of cases) |
| `negative-unknown-entity` | negative,graph | TRUSTED | expectations match documented behaviour |

## C. Stale expectation analysis — Calendar / Microsoft Graph

**Every provider expectation in the dataset is stale.** Measured, not inferred:

- 104 cases carry `expected_provider_order`; **0** include `calendar`
- 115 cases carry `expected_context_sources`; **0** include `calendar`
- A prior phase measured the calendar provider contributing to **100%** of cases
  after the Microsoft token-refresh fix

Only four distinct expected orders exist across 104 cases:

| Expected order | Cases |
|---|---|
| `[graph, corporate, tasks]` | 70 |
| `[tasks, corporate]` | 32 |
| `[graph]` | 1 |
| `[tasks]` | 1 |

The dataset was authored while the calendar token was expired, so calendar was
invisible and never written into any expectation. It is now healthy. Any
provider-order or context-source assertion is therefore measuring the *old
broken deployment*, not intended behaviour.

This is the mechanism behind the apparent 69.8% regression: all 40 provider-order
failures in that run contained `calendar` in the ACTUAL order. The system got
better; the dataset did not follow.

**Recommended action (Step 4):** add `calendar` to the expected sets wherever
calendar genuinely should contribute, rather than deleting the assertion. If
calendar should NOT contribute for a given question, that is a product decision
about provider gating and must be recorded as such — not papered over.

## D. GraphRAG coverage

| Dimension | Cases | Assessment |
|---|---|---|
| Single-hop | 56 | adequate |
| Multi-hop (depth>1) | 14 | present but **all fail by design at depth=1** |
| Negative premise | 6 flagged + 3 unflagged | flagging is inconsistent |
| Provenance | 2 | thin |
| Corroboration | 7 | thin |
| Conflict | 5 | thin |
| Ambiguity | 4 | thin |
| Semantic resolution | 8 | present, none satisfiable lexically |
| Qdrant document assertions | 11 | thin |
| Relationship assertions | 15 | thin |

## E. Experiment readiness

| Experiment | Dataset ready? | Missing coverage |
|---|---|---|
| Resolver threshold | **Partially** | Has true positives at 0.80–0.82 (`syn-vector-db` measured 0.811, `Neo4G` 0.80) but **no measured true negatives near threshold**; worst negative sits at 0.60, so the 0.82–0.90 range is untested from below |
| Document pollution | **Yes, barely** | `syn-gateway` is the one proven detector ("gateway" → *Hermes Gateway — Kickoff Notes*). One case is not a regression suite |
| Semantic resolver | **Yes** | 8 genuine paraphrase cases already exist and currently fail; they are the correct target |
| Graph depth | **No** | 14 multi-hop cases exist but they cannot distinguish depth 2 from 3 — no case requires exactly 3 hops and no case would *regress* if depth grew (no context-explosion detector) |
| Fusion | **No** | 7 corroboration + 5 conflict cases is too few to separate graph-useful from graph-irrelevant; no case asserts graph should be *outranked* |
| Ranking | **No** | This is why the earlier +0.005 weight search was unreliable. No freshness variation, no per-provider confidence variation, no centrality signal |

## F. Recommended new cases for Step 5

1. **Threshold true-negatives (0.75–0.85 band)** — near-miss mentions that must
   NOT resolve. Currently the highest negative scores 0.60, so nothing constrains
   raising the threshold.
2. **Document-pollution regression family** — several generic mentions
   (`notes`, `report`, `review`, `minutes`) that must resolve to a concept or to
   nothing, never to a Document title.
3. **Depth-2-sufficient vs depth-3-required pairs** — otherwise the depth
   experiment cannot justify 3 over 2.
4. **Context-explosion guards** — cases asserting a maximum retrieved-node count,
   so a depth increase that floods context is a FAILURE, not a silent win.
5. **Graph-should-lose cases** — questions where Qdrant verbatim text is the
   correct source and graph must be outranked. None exist today.
6. **Freshness/conflict pairs** for the ranking experiment.
7. **Re-flag the 3 adversarial false-premise cases** (`adv-leading-false`,
   `adv-leading-person`, `adv-invented-rel`) so refusal is actually scored.
