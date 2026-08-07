"""
Phase 3 retrieval tests.

  * pure   — ranking maths, Lucene escaping, similarity. No LLM, no database.
  * fake   — resolver mention extraction against a stubbed LLM. No network.
  * graph  — registry resolution, expansion, evidence, ranking order.
             Skipped automatically when Neo4j is unreachable.

Graph tests build a small fixture whose entity names are all prefixed "KGTest ",
so their ids land under `kgtest-` — an id space production never uses — and the
teardown deletes exactly that prefix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.knowledge_graph.normalizer import Normalizer
from backend.knowledge_graph import (
    BatchKnowledgeGraphBuilder,
    ExtractedEntity,
    ExtractedRelationship,
    Provenance,
    get_graph_service,
    is_enabled,
    verify_connectivity,
)
from backend.knowledge_graph.retrieval import (
    CanonicalEntityRegistry,
    EntityResolver,
    Evidence,
    GraphRetrievalAPI,
    GraphRetriever,
    RankingWeights,
    escape_lucene,
    score_edge,
    score_node,
    similarity,
)
from backend.knowledge_graph.retrieval import ranking

TEST_SOURCE = "pytest-retrieval"
# See tests/test_knowledge_graph.py: isolation comes from the NAME prefix, which
# keeps every fixture id under kgtest- and away from every production entity.
TEST_ID_PREFIX = "kgtest-"


def _graph_available() -> bool:
    try:
        return is_enabled() and verify_connectivity()
    except Exception:  # noqa: BLE001
        return False


needs_graph = pytest.mark.skipif(not _graph_available(), reason="Neo4j unreachable")


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def graph():
    """A small, fully-specified fixture graph.

        Akshay ─WORKS_ON→ Agentic AI ─USES→ LiteLLM ─ROUTES_TO→ qwen-fast
                                    └─USES→ Microsoft Graph

    LiteLLM is deliberately corroborated by two sources and Microsoft Graph by
    one, so ranking order is predictable rather than incidental.
    """
    svc = get_graph_service()

    def _clean() -> None:
        svc.run_query(
            "MATCH (n:Entity) WHERE n.id STARTS WITH $prefix DETACH DELETE n",
            {"prefix": TEST_ID_PREFIX}, op="pytest_clean")

    _clean()
    b = BatchKnowledgeGraphBuilder(service=svc, source=TEST_SOURCE)

    entities = [
        ExtractedEntity(name="KGTest Akshay", type="Person"),
        ExtractedEntity(name="KGTest AgenticAI", type="Project"),
        ExtractedEntity(name="KGTest LiteLLM", type="Technology"),
        ExtractedEntity(name="KGTest MicrosoftGraph", type="Technology",
                        secondary_labels=["API"], aliases=["KGTest MSGraph"]),
        ExtractedEntity(name="KGTest QwenFast", type="Model"),
    ]
    rels = [
        ExtractedRelationship(source="KGTest Akshay", type="WORKS_ON", target="KGTest AgenticAI"),
        ExtractedRelationship(source="KGTest AgenticAI", type="USES", target="KGTest LiteLLM"),
        ExtractedRelationship(source="KGTest AgenticAI", type="USES", target="KGTest MicrosoftGraph"),
        ExtractedRelationship(source="KGTest LiteLLM", type="ROUTES_TO", target="KGTest QwenFast"),
    ]
    b.build(entities, rels, provenance=Provenance(source_id="conv-a",
                                                  source_type="conversation",
                                                  confidence=0.9, model="KGTest QwenFast"))
    # Second source mentions only LiteLLM → it becomes the corroborated one.
    b.build([ExtractedEntity(name="KGTest LiteLLM", type="Technology")], [],
            provenance=Provenance(source_id="doc-b", source_type="document",
                                  confidence=0.8, model="gpt-4.1"))
    yield svc
    _clean()


@pytest.fixture()
def registry(graph):
    reg = CanonicalEntityRegistry(service=graph, alias_file=None)
    reg.warm(force=True)
    return reg


# ── 1. ranking (pure) ─────────────────────────────────────────────────────────

def test_signals_are_bounded_and_named():
    ev = Evidence(source_ids=["a", "b"], confidence=0.9, observations=3,
                  last_seen=datetime.now(timezone.utc).isoformat())
    signals = ranking.score_signals(ev, degree=10)
    assert set(signals) == {"confidence", "corroboration", "freshness",
                            "importance", "frequency"}
    assert all(0.0 <= v <= 1.0 for v in signals.values())


def test_corroboration_counts_distinct_sources_only():
    assert ranking.corroboration_score(Evidence(source_ids=["a", "a", "a"])) < \
           ranking.corroboration_score(Evidence(source_ids=["a", "b", "c"]))
    assert ranking.corroboration_score(Evidence(source_ids=[])) == 0.0


def test_freshness_decays_and_unknown_is_neutral():
    now = datetime.now(timezone.utc)
    fresh = ranking.freshness_score(now.isoformat(), now)
    old = ranking.freshness_score((now - timedelta(days=90)).isoformat(), now)
    assert fresh > old
    assert ranking.freshness_score(None) == 0.5, "missing timestamp is neutral, not stale"


def test_importance_is_log_scaled_so_hubs_do_not_dominate():
    small = ranking.importance_score(2) - ranking.importance_score(1)
    large = ranking.importance_score(51) - ranking.importance_score(50)
    assert small > large


def test_hop_decay_pushes_distant_nodes_down():
    ev = Evidence(source_ids=["a"], confidence=1.0, observations=1)
    near, _ = score_node(ev, degree=5, hop=0)
    far, _ = score_node(ev, degree=5, hop=2)
    assert near > far


def test_edge_score_excludes_importance():
    """Edges have no degree, so importance is omitted rather than scored zero —
    a zero would drag every edge down by the same amount and add only noise."""
    now = datetime.now(timezone.utc).isoformat()
    # Fully saturated on every signal an edge HAS (4 distinct sources, 8
    # observations, seen just now) must approach 1.0. It only can if the
    # missing importance term is excluded from the denominator.
    saturated = Evidence(source_ids=["a", "b", "c", "d"], confidence=1.0,
                         observations=8, last_seen=now)
    assert score_edge(saturated, hop=0) > 0.99

    # And the score must be insensitive to node degree, which edges lack.
    partial = Evidence(source_ids=["a", "b"], confidence=1.0, observations=4,
                       last_seen=now)
    node_score, _ = score_node(partial, degree=0, hop=0)
    assert score_edge(partial, hop=0) > node_score, \
        "excluding importance must not penalise the edge"


def test_weights_are_tunable():
    ev = Evidence(source_ids=["a", "b", "c", "d"], confidence=0.1, observations=1)
    corroboration_heavy = RankingWeights(confidence=0.1, corroboration=5.0,
                                         freshness=0.1, importance=0.1, frequency=0.1)
    default_score, _ = score_node(ev, degree=1)
    tuned_score, _ = score_node(ev, degree=1, weights=corroboration_heavy)
    assert tuned_score > default_score


# ── 2. registry helpers (pure) ───────────────────────────────────────────────

def test_lucene_special_characters_are_escaped():
    """Unescaped Lucene syntax either errors or silently changes the query."""
    for ch in "+-!(){}[]^\"~*?:\\/":
        assert "\\" + ch in escape_lucene(f"a{ch}b")


def test_similarity_is_bounded():
    assert similarity("KGTest MicrosoftGraph", "KGTest MicrosoftGraph") == 1.0
    assert 0.0 <= similarity("abc", "xyz") <= 1.0


# ── 3. resolver mention extraction (fake LLM) ────────────────────────────────

class _FakeLLM:
    def __init__(self, payload): self.payload, self.calls = payload, 0

    def complete(self, *a, **kw):
        self.calls += 1
        return self.payload


def test_mention_extraction_is_one_call_and_deduplicates(monkeypatch):
    fake = _FakeLLM('{"mentions": ["KGTest AgenticAI", "KGTest LiteLLM", "kgtest agenticai"]}')
    monkeypatch.setattr("backend.knowledge_graph.retrieval.resolver._llm.complete",
                        fake.complete)
    resolver = EntityResolver(registry=CanonicalEntityRegistry(alias_file=None))
    mentions, _ = resolver.extract_mentions("How does Agentic AI use LiteLLM?")
    assert fake.calls == 1
    assert mentions == ["KGTest AgenticAI", "KGTest LiteLLM"], "case-duplicate must be dropped"


def test_mention_extraction_survives_bad_json(monkeypatch):
    monkeypatch.setattr("backend.knowledge_graph.retrieval.resolver._llm.complete",
                        _FakeLLM("not json").complete)
    resolver = EntityResolver(registry=CanonicalEntityRegistry(alias_file=None))
    mentions, took = resolver.extract_mentions("anything")
    assert mentions == [] and took >= 0


# ── 4. registry resolution (needs Neo4j) ─────────────────────────────────────

@needs_graph
def test_exact_and_alias_resolution(registry):
    assert registry.resolve("KGTest AgenticAI").entity_id == "kgtest-agenticai"
    assert registry.resolve("kgtest agenticai").entity_id == "kgtest-agenticai"
    ms = registry.resolve("KGTest MSGraph")
    assert ms.entity_id == "kgtest-microsoftgraph", "stored alias must resolve"
    assert ms.resolved and ms.match_type in ("registry", "alias", "exact")


@needs_graph
def test_static_alias_map_feeds_resolution(graph):
    """The normalizer's alias map applies BEFORE any database lookup.

    Production ships "MS Graph" → "Microsoft Graph" statically. Asserting on that
    pair would resolve to the real `microsoft-graph` node, so the same behaviour
    is exercised with a registered namespaced alias instead: the variant is not a
    stored alias on the node and does not slugify to its id, so resolution can
    only succeed if the normalizer rewrote the name first."""
    n = Normalizer()
    n.register_alias("KGTest MSGraphVariant", "KGTest MicrosoftGraph")
    reg = CanonicalEntityRegistry(service=graph, normalizer=n, alias_file=None)
    reg.warm(force=True)
    assert reg.resolve("KGTest MSGraphVariant").entity_id == "kgtest-microsoftgraph"


@needs_graph
def test_fuzzy_resolves_typos_but_not_nonsense(registry):
    typo = registry.resolve("KGTest MicrosftGraph")
    assert typo.entity_id == "kgtest-microsoftgraph"
    assert typo.match_type == "fuzzy"
    assert typo.score >= registry.fuzzy_threshold

    junk = registry.resolve("Zzzzz Quuux Nonexistent")
    assert not junk.resolved, "a bad guess is worse than admitting no match"
    assert junk.match_type == "none"


@needs_graph
def test_unresolved_still_reports_candidates(registry):
    """A near-miss must be inspectable, not silently swallowed."""
    result = registry.resolve("Micro", allow_fuzzy=True)
    if not result.resolved:
        assert isinstance(result.candidates, list)


@needs_graph
def test_register_alias_persists_to_the_node(registry, graph):
    assert registry.register_alias("The KGTest GraphAPI", "kgtest-microsoftgraph")
    node = graph.find_entity("kgtest-microsoftgraph")
    assert "The KGTest GraphAPI" in node[0].properties["aliases"]
    registry.invalidate()
    registry.warm(force=True)
    assert registry.resolve("The KGTest GraphAPI").entity_id == "kgtest-microsoftgraph"


@needs_graph
def test_warm_caches_aliases(registry):
    count = registry.warm(force=True)
    assert count > 0


@needs_graph
def test_historical_alias_merge_is_dry_run_by_default(registry):
    report = registry.merge_historical_aliases()
    assert report["applied"] is False, "must not rewrite data unless asked"
    assert "planned" in report and "legacy_found" in report


# ── 5. retrieval (needs Neo4j) ───────────────────────────────────────────────

@needs_graph
def test_depth_controls_expansion(graph):
    r = GraphRetriever(service=graph)
    assert len(r.expand(["kgtest-agenticai"], depth=0).nodes) == 1
    one = r.expand(["kgtest-agenticai"], depth=1)
    two = r.expand(["kgtest-agenticai"], depth=2)
    assert len(one.nodes) == 4, "Akshay, LiteLLM, Microsoft Graph + the seed"
    assert len(two.nodes) > len(one.nodes), "qwen-fast is two hops out"


@needs_graph
def test_expansion_cost_is_one_query_per_hop(graph):
    r = GraphRetriever(service=graph)
    r.expand(["kgtest-agenticai"], depth=1)
    one_hop = r.queries
    r.expand(["kgtest-agenticai"], depth=2)
    assert r.queries == one_hop + 1, "each extra hop costs exactly one more query"


@needs_graph
def test_every_returned_edge_carries_evidence(graph):
    sg = GraphRetriever(service=graph).expand(["kgtest-agenticai"], depth=2)
    assert sg.edges
    for edge in sg.edges:
        assert edge.evidence.is_supported, "unsupported facts must never be returned"
        assert edge.evidence.source_ids
        assert edge.evidence.observations >= 1
        assert edge.evidence.last_seen


@needs_graph
def test_unsupported_edges_are_dropped_and_counted(graph):
    """An edge written without provenance must not surface."""
    graph.run_query(
        "MATCH (a:Entity {id:'kgtest-agenticai'}), (b:Entity {id:'kgtest-qwenfast'}) "
        "MERGE (a)-[r:RELATED_TO]->(b) SET r.note = 'no provenance'", op="pytest")
    sg = GraphRetriever(service=graph).expand(["kgtest-agenticai"], depth=1)
    assert sg.dropped_unsupported >= 1
    assert not any(e.rel_type == "RELATED_TO" and e.end_id == "KGTest QwenFast"
                   for e in sg.edges)


@needs_graph
def test_require_evidence_can_be_disabled_for_inspection(graph):
    graph.run_query(
        "MATCH (a:Entity {id:'kgtest-agenticai'}), (b:Entity {id:'kgtest-qwenfast'}) "
        "MERGE (a)-[r:RELATED_TO]->(b) SET r.note='x'", op="pytest")
    sg = GraphRetriever(service=graph, require_evidence=False).expand(["kgtest-agenticai"], depth=1)
    assert any(e.rel_type == "RELATED_TO" for e in sg.edges)


@needs_graph
def test_seeds_outrank_their_neighbours(graph):
    sg = GraphRetriever(service=graph).expand(["kgtest-agenticai"], depth=2)
    assert sg.nodes[0].entity_id == "kgtest-agenticai", "the seed must rank first"
    assert sg.nodes[0].hop == 0
    hops = [n.hop for n in sg.nodes]
    assert hops == sorted(hops) or sg.nodes[-1].hop >= sg.nodes[0].hop


@needs_graph
def test_corroborated_node_outranks_an_equal_uncorroborated_one(graph):
    """LiteLLM has two sources; Microsoft Graph has one, at the same hop."""
    sg = GraphRetriever(service=graph).expand(["kgtest-agenticai"], depth=1)
    by_id = {n.entity_id: n for n in sg.nodes}
    lite, msg = by_id["kgtest-litellm"], by_id["kgtest-microsoftgraph"]
    assert lite.hop == msg.hop == 1
    assert lite.signals["corroboration"] > msg.signals["corroboration"]


@needs_graph
def test_max_nodes_truncates_and_reports_it(graph):
    sg = GraphRetriever(service=graph, max_nodes=2).expand(["kgtest-agenticai"], depth=2)
    assert len(sg.nodes) <= 2
    assert sg.truncated


@needs_graph
def test_rel_type_filter(graph):
    sg = GraphRetriever(service=graph).expand(["kgtest-agenticai"], depth=1,
                                              rel_types={"USES"})
    assert {e.rel_type for e in sg.edges} == {"USES"}


# ── 6. hybrid API (needs Neo4j) ──────────────────────────────────────────────

@needs_graph
def test_api_neighbourhood_needs_no_llm(graph):
    api = GraphRetrievalAPI(service=graph)
    sg = api.neighbourhood("kgtest-agenticai", depth=1)
    assert len(sg.nodes) > 1


@needs_graph
def test_api_lookup_needs_no_llm(graph):
    api = GraphRetrievalAPI(service=graph)
    assert api.lookup("KGTest MSGraph").entity_id == "kgtest-microsoftgraph"


@needs_graph
def test_api_splits_resolved_from_related(graph, monkeypatch):
    monkeypatch.setattr("backend.knowledge_graph.retrieval.resolver._llm.complete",
                        _FakeLLM('{"mentions": ["KGTest AgenticAI"]}').complete)
    api = GraphRetrievalAPI(service=graph)
    ctx = api.retrieve("What does Agentic AI use?", depth=1)
    assert [e.entity_id for e in ctx.resolved] == ["kgtest-agenticai"]
    assert "kgtest-agenticai" not in [n.entity_id for n in ctx.related], \
        "a seed is never also 'related'"
    assert ctx.related, "one hop from the seed must find something"
    assert ctx.stats.total_ms > 0


@needs_graph
def test_api_reports_unresolved_without_failing(graph, monkeypatch):
    monkeypatch.setattr("backend.knowledge_graph.retrieval.resolver._llm.complete",
                        _FakeLLM('{"mentions": ["Zzzz Nonexistent Thing"]}').complete)
    api = GraphRetrievalAPI(service=graph)
    ctx = api.retrieve("What about Zzzz Nonexistent Thing?")
    assert ctx.ok, "an unknown entity is a valid outcome, not an error"
    assert ctx.resolved == []
    assert ctx.unresolved == ["Zzzz Nonexistent Thing"]
    assert ctx.subgraph.nodes == []


@needs_graph
def test_api_top_k_prunes_both_nodes_and_edges(graph, monkeypatch):
    monkeypatch.setattr("backend.knowledge_graph.retrieval.resolver._llm.complete",
                        _FakeLLM('{"mentions": ["KGTest AgenticAI"]}').complete)
    ctx = GraphRetrievalAPI(service=graph).retrieve("x", depth=2, top_k=2)
    keep = {n.entity_id for n in ctx.subgraph.nodes}
    assert len(keep) <= 2
    for edge in ctx.subgraph.edges:
        assert edge.start_id in keep and edge.end_id in keep, \
            "an edge must never dangle outside the pruned node set"
