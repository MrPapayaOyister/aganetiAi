"""
Phase 4 — Hybrid Context Fusion tests.

All pure: providers are stubbed, so no Qdrant, Neo4j or LLM. The behaviours that
matter are (a) cross-provider agreement is detected and counted, (b) nothing is
silently lost, and (c) every stage fails soft.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.context.budget import BudgetPolicy, ContextBudget, estimate_tokens
from backend.context.bundle import ContextBundle, ContextItem, RankedContextBundle
from backend.context.compression import ContextCompressor
from backend.context.evidence import Evidence, corroboration_of, merge_evidence
from backend.context.fusion import HybridContextFusion, containment, jaccard
from backend.context.ranking import (
    ProviderReliability, RankingWeights, WeightedRanker, query_relevance,
)


def item(text, provider, score=None, **meta):
    return ContextItem(text=text, provider=provider, score=score, metadata=meta)


def edge(start, rel, end, score=0.5, sources=("conv-1",), types=("conversation",)):
    return ContextItem(
        text=f"{start} —{rel}→ {end}", provider="graph", score=score,
        metadata={"kind": "relationship", "start_id": start, "rel_type": rel,
                  "end_id": end, "source_ids": list(sources),
                  "source_types": list(types), "confidence": 0.9})


# ── Evidence ─────────────────────────────────────────────────────────────────

def test_graph_metadata_explodes_into_one_evidence_per_source():
    ev = Evidence.from_graph_metadata({
        "kind": "relationship", "start_id": "a", "rel_type": "USES", "end_id": "b",
        "source_ids": ["conv-1", "doc-2"], "source_types": ["conversation", "document"],
        "confidence": 0.9})
    assert len(ev) == 2
    assert ev[0].conversation_id == "conv-1" and ev[1].document_id == "doc-2"
    assert all(e.graph_edge == "a -USES-> b" for e in ev)


def test_corroboration_counts_distinct_sources_not_list_length():
    dup = [Evidence(provider="graph", source_id="conv-1"),
           Evidence(provider="graph", source_id="conv-1")]
    assert corroboration_of(dup) == 1
    distinct = [Evidence(provider="graph", source_id="conv-1"),
                Evidence(provider="corporate", source_id="doc-2")]
    assert corroboration_of(distinct) == 2


def test_merge_evidence_is_order_stable_and_deduped():
    a = [Evidence(provider="graph", source_id="s1")]
    b = [Evidence(provider="graph", source_id="s1"), Evidence(provider="memory", source_id="s2")]
    merged = merge_evidence([a, b])
    assert len(merged) == 2 and merged[0].provider == "graph"


# ── normalization ────────────────────────────────────────────────────────────

def test_scores_normalize_to_0_1_without_losing_raw():
    b = ContextBundle()
    b.put("corporate", [item("doc", "corporate", 0.83)])
    b.put("calendar", [item("10:00 standup", "calendar")])
    items, _ = HybridContextFusion().fuse(b)
    by = {i.provider: i for i in items}
    assert by["corporate"].raw_score == 0.83
    assert 0.0 <= by["corporate"].normalized_score <= 1.0
    # A source with no score must not normalise to zero — that would rank the
    # user's own calendar below every fuzzy vector hit.
    assert by["calendar"].raw_score is None
    assert by["calendar"].normalized_score > 0.5


# ── fusion ───────────────────────────────────────────────────────────────────

def test_same_fact_from_qdrant_and_neo4j_becomes_one_corroborated_item():
    """The core claim of the phase."""
    b = ContextBundle()
    b.put("corporate", [item("Agentic AI uses LiteLLM for inference.", "corporate", 0.82)])
    b.put("graph", [edge("agentic-ai", "USES", "litellm",
                         sources=("conv-1", "doc-2"),
                         types=("conversation", "document"))])
    items, stats = HybridContextFusion().fuse(b)

    assert len(items) == 1, "the same fact from two stores must fuse"
    fused = items[0]
    assert set(fused.providers) == {"corporate", "graph"}
    assert fused.corroboration >= 2
    assert stats.cross_provider_merges == 1
    assert stats.corroborated_items == 1


def test_fusion_keeps_the_longer_more_complete_text():
    b = ContextBundle()
    b.put("corporate", [item("Agentic AI uses LiteLLM for all model inference.", "corporate", 0.6)])
    b.put("graph", [edge("agentic-ai", "USES", "litellm", score=0.99)])
    items, _ = HybridContextFusion().fuse(b)
    assert "all model inference" in items[0].text


def test_fusion_records_what_it_merged():
    b = ContextBundle()
    b.put("corporate", [item("Agentic AI uses LiteLLM", "corporate", 0.8)])
    b.put("memory", [item("Agentic AI uses LiteLLM", "memory")])
    items, _ = HybridContextFusion().fuse(b)
    assert items[0].metadata.get("merged_from"), "a merge must be traceable"


def test_unrelated_items_are_never_merged():
    b = ContextBundle()
    b.put("corporate", [item("Expense policy requires AED", "corporate", 0.8)])
    b.put("graph", [edge("agentic-ai", "USES", "litellm")])
    items, stats = HybridContextFusion().fuse(b)
    assert len(items) == 2 and stats.duplicates_merged == 0


def test_similarity_bridges_prose_and_triple_forms():
    assert jaccard("Agentic AI uses LiteLLM", "agentic-ai —USES→ litellm") >= 0.6
    assert containment("Agentic AI uses LiteLLM for inference", "agentic-ai —USES→ litellm") >= 0.85


# ── ranking ──────────────────────────────────────────────────────────────────

def test_query_relevance_beats_a_higher_raw_score():
    """The phase-3 limitation this fixes: structural score alone is not relevance."""
    relevant = item("Agentic AI uses LiteLLM", "graph")
    relevant.normalized_score, relevant.corroboration = 0.6, 3
    irrelevant = item("Office parking policy", "corporate")
    irrelevant.normalized_score = 0.95
    out = WeightedRanker().rank([irrelevant, relevant], "How does Agentic AI use LiteLLM?")
    assert out[0] is relevant


def test_corroborated_item_outranks_an_equal_uncorroborated_one():
    a = item("Agentic AI uses LiteLLM", "graph"); a.normalized_score = 0.7; a.corroboration = 4
    b = item("Agentic AI uses LiteLLM too", "graph"); b.normalized_score = 0.7; b.corroboration = 1
    out = WeightedRanker().rank([b, a], "Agentic AI LiteLLM")
    assert out[0] is a


def test_absent_signals_are_excluded_not_scored_zero():
    """A calendar entry has no graph centrality; it must not be penalised for it."""
    cal = item("10:00 standup with Akshay", "calendar"); cal.normalized_score = 0.85
    signals = WeightedRanker().signals_for(cal, "standup")
    assert "graph_centrality" not in signals and "relationship_depth" not in signals
    assert signals["provider_reliability"] == 1.0


def test_weights_are_configurable_not_hardcoded():
    it = item("x y z", "graph"); it.normalized_score = 0.1; it.corroboration = 4
    base = WeightedRanker().score(it, "x y z")
    tuned = WeightedRanker(weights=RankingWeights(
        query_relevance=0.1, provider_confidence=0.1, corroboration=9.0,
        freshness=0.1, importance=0.1, provider_reliability=0.1,
        graph_centrality=0.1, relationship_depth=0.1)).score(it, "x y z")
    assert tuned > base


def test_provider_reliability_is_configurable():
    r = ProviderReliability(scores={"graph": 0.1}, default=0.5)
    assert r.of("graph") == 0.1 and r.of("unknown") == 0.5


def test_query_relevance_ignores_stopwords():
    assert query_relevance("what is the LiteLLM gateway", "LiteLLM gateway") > 0.9


# ── compression ──────────────────────────────────────────────────────────────

def test_chains_collapse_into_one_path():
    items = [edge("a", "USES", "b"), edge("b", "ROUTES_TO", "c"), edge("c", "HOSTED_ON", "d")]
    for i in items:
        i.normalized_score = 0.5
    out = ContextCompressor().compress(items)
    assert len(out.items) == 1
    assert out.items[0].text == "a —USES→ b —ROUTES_TO→ c —HOSTED_ON→ d"
    assert out.collapsed_chains == 2


def test_forked_paths_are_not_collapsed_into_a_false_chain():
    """b→c and b→d must not become one path — that asserts a route that isn't there."""
    items = [edge("a", "USES", "b"), edge("b", "USES", "c"), edge("b", "USES", "d")]
    for i in items:
        i.normalized_score = 0.5
    out = ContextCompressor().compress(items)
    assert all("—USES→ c —USES→ d" not in i.text for i in out.items)


def test_repeated_entity_collapses_into_one_line():
    items = [edge("proj", "USES", "a"), edge("proj", "USES", "b"), edge("proj", "STORES", "c")]
    for i in items:
        i.normalized_score = 0.5
    out = ContextCompressor().compress(items)
    assert out.collapsed_entities == 2
    text = out.items[0].text
    assert "proj" in text and "a" in text and "b" in text and "c" in text


def test_compression_preserves_evidence():
    a, b = edge("p", "USES", "x", sources=("s1",)), edge("p", "USES", "y", sources=("s2",))
    c = edge("p", "USES", "z", sources=("s3",))
    items, _ = HybridContextFusion().fuse(_bundle_of(graph=[a, b, c]))
    out = ContextCompressor().compress(items)
    assert corroboration_of(out.items[0].evidence) >= 3, "collapsing must not lose sources"


def test_exact_duplicates_are_removed():
    a, b = item("identical text", "corporate", 0.5), item("identical  TEXT ", "memory")
    out = ContextCompressor().compress([a, b])
    assert len(out.items) == 1 and out.removed_exact == 1


def _bundle_of(**groups):
    b = ContextBundle()
    for name, items in groups.items():
        b.put(name, items)
    return b


# ── budget ───────────────────────────────────────────────────────────────────

def test_budget_reserves_before_allocating():
    p = BudgetPolicy(window=8192, reserve_system=1200, reserve_history=1500, reserve_response=1024)
    assert p.available == 8192 - 1200 - 1500 - 1024


def test_budget_stops_when_exhausted_and_keeps_the_best():
    p = BudgetPolicy(window=1000, reserve_system=100, reserve_history=100, reserve_response=100)
    items = []
    for n in range(10):
        it = item("x" * 400, "corporate")
        it.final_score = 1.0 - n * 0.1
        items.append(it)
    r = ContextBudget(p).apply(items)
    assert r.dropped > 0
    assert r.tokens_used <= r.tokens_available
    assert r.items[0].final_score == pytest.approx(1.0), "highest-ranked must survive"


def test_budget_reports_allocation_and_contribution():
    p = BudgetPolicy(window=4000, reserve_system=200, reserve_history=200, reserve_response=200)
    items = [item("a" * 200, "corporate"), item("b" * 200, "graph"), item("c" * 100, "tasks")]
    for n, i in enumerate(items):
        i.final_score = 1.0 - n * 0.1
    r = ContextBudget(p).apply(items)
    assert set(r.allocation) == {"corporate", "graph", "tasks"}
    assert sum(r.contribution.values()) == len(r.items)
    assert 0.0 <= r.utilization <= 1.0


def test_no_partial_items():
    """Half a sentence is worse than no sentence."""
    p = BudgetPolicy(window=500, reserve_system=100, reserve_history=100, reserve_response=100)
    big = item("x" * 4000, "corporate"); big.final_score = 1.0
    r = ContextBudget(p).apply([big])
    assert r.items == [] and r.dropped == 1


def test_estimate_tokens_is_never_zero_for_real_text():
    assert estimate_tokens("hi") >= 1 and estimate_tokens("") >= 1
