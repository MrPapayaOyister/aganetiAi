"""
GraphRetrievalAPI — the one entry point the rest of the platform will call.

    question ──► EntityResolver ──► GraphRetriever ──► GraphContext
                 (LLM: mentions)    (Neo4j: expand)     (data, not text)

"Hybrid" here names the SHAPE, not a second store: the response is split into
resolved entities (what the question is about), related entities (what the graph
connects them to) and the supporting subgraph (the edges, with evidence). When
vector retrieval is added in a later phase it slots in beside `related` without
changing this contract.

Explicitly out of scope, per the phase brief: no Qdrant query, no prompt
assembly, no answer generation. This returns structured data and stops.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..service import GraphService
from .ranking import RankingWeights
from .registry import CanonicalEntityRegistry
from .resolver import EntityResolver
from .retriever import GraphRetriever
from .types import GraphContext, RetrievalStats, Subgraph

log = logging.getLogger("aganeti.kg.api")


class GraphRetrievalAPI:
    """Question → graph context. Composes resolver + retriever."""

    def __init__(self, service: Optional[GraphService] = None,
                 registry: Optional[CanonicalEntityRegistry] = None,
                 resolver: Optional[EntityResolver] = None,
                 retriever: Optional[GraphRetriever] = None,
                 weights: Optional[RankingWeights] = None,
                 depth: int = 1, max_nodes: int = 100) -> None:
        self.registry = registry or CanonicalEntityRegistry(service=service)
        self.resolver = resolver or EntityResolver(registry=self.registry, service=service)
        self.retriever = retriever or GraphRetriever(service=service, weights=weights,
                                                     max_nodes=max_nodes)
        self.default_depth = depth

    # ── the main call ────────────────────────────────────────────────────────

    def retrieve(self, question: str, *, depth: Optional[int] = None,
                 rel_types: Optional[set[str]] = None,
                 top_k: Optional[int] = None,
                 allow_fuzzy: bool = True) -> GraphContext:
        """Resolve a question to entities and return their ranked neighbourhood."""
        started = time.perf_counter()
        stats = RetrievalStats()
        context = GraphContext(question=question or "")

        entities, timings = self.resolver.resolve(question, allow_fuzzy=allow_fuzzy)
        stats.extract_ms = timings["extract_ms"]
        stats.resolve_ms = timings["resolve_ms"]
        stats.mentions_found = timings["mentions_found"]
        stats.mentions_resolved = timings["mentions_resolved"]

        context.resolved = [e for e in entities if e.resolved]
        context.unresolved = [e.mention for e in entities if not e.resolved]

        if not context.resolved:
            stats.total_ms = (time.perf_counter() - started) * 1000
            context.stats = stats
            # Not an error: a question about something the graph has never seen
            # is a legitimate, informative outcome.
            log.info("kg api: no entity resolved from %r", (question or "")[:80])
            return context

        expand_started = time.perf_counter()
        subgraph = self.retriever.expand([e.entity_id for e in context.resolved],
                                         depth=self.default_depth if depth is None else depth,
                                         rel_types=rel_types)
        stats.expand_ms = (time.perf_counter() - expand_started) * 1000
        stats.graph_queries = self.retriever.queries
        stats.nodes_visited = len(subgraph.nodes)
        stats.edges_considered = len(subgraph.edges)
        stats.edges_dropped_unsupported = subgraph.dropped_unsupported

        if top_k:
            keep = {n.entity_id for n in subgraph.nodes[:top_k]}
            subgraph.nodes = [n for n in subgraph.nodes if n.entity_id in keep]
            subgraph.edges = [e for e in subgraph.edges
                              if e.start_id in keep and e.end_id in keep]

        seed_ids = {e.entity_id for e in context.resolved}
        context.related = [n for n in subgraph.nodes if n.entity_id not in seed_ids]
        context.subgraph = subgraph

        stats.total_ms = (time.perf_counter() - started) * 1000
        context.stats = stats
        log.info("kg api: %r → %d resolved, %d related, %d edge(s), %.0fms",
                 (question or "")[:60], len(context.resolved), len(context.related),
                 len(subgraph.edges), stats.total_ms)
        return context

    # ── narrower entry points ────────────────────────────────────────────────

    def resolve_only(self, question: str, *, allow_fuzzy: bool = True):
        """Entity resolution without traversal — for measuring resolution accuracy."""
        entities, timings = self.resolver.resolve(question, allow_fuzzy=allow_fuzzy)
        return entities, timings

    def neighbourhood(self, entity_id: str, *, depth: int = 1,
                      rel_types: Optional[set[str]] = None) -> Subgraph:
        """Expand a KNOWN entity id — no LLM call at all.

        The cheap path when the caller already has an id (a click-through, a
        follow-up on a previously resolved entity)."""
        return self.retriever.expand([entity_id], depth=depth, rel_types=rel_types)

    def lookup(self, name: str, *, allow_fuzzy: bool = True):
        """Resolve a single name. No LLM — straight to the registry."""
        self.registry.warm()
        return self.registry.resolve(name, allow_fuzzy=allow_fuzzy)
