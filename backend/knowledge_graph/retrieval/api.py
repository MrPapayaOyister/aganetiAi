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
import os
import time
from typing import Any, Optional

from ..service import GraphService
from .ranking import RankingWeights
from .registry import CanonicalEntityRegistry
from .resolver import EntityResolver
from .retriever import GraphRetriever
from .types import GraphContext, RetrievalStats, Subgraph

log = logging.getLogger("aganeti.kg.api")

# ── tenancy ──────────────────────────────────────────────────────────────────
# The SAME two knobs backend/orchestrator/graph_tools.py reads, deliberately by
# env rather than by import: knowledge_graph is the lower layer and must not
# depend on orchestrator. One vocabulary, one default, no import cycle.
#
# STATE OF THE GRAPH, HONESTLY: no node carries an organisational property today
# (522 of 522 entities have none), so in lenient mode this filter is a verified
# no-op and GraphRAG behaviour is unchanged — which is the only reason it is safe
# to add mid-flight to a frozen pipeline. It begins enforcing the moment nodes are
# stamped. Strict mode on today's graph returns nothing, which is why it is off.
TENANT_PROPERTY = os.getenv("GRAPH_TENANT_PROPERTY", "org_id")
TENANT_STRICT = os.getenv("GRAPH_TENANT_STRICT", "false").strip().lower() in ("1", "true", "yes")


def _tenant_visible(properties: "dict[str, Any]", tenant_id: str, *, strict: bool) -> bool:
    """Is this node visible to `tenant_id`?

    Lenient (default): an UNSTAMPED node is shared, so it is visible. Strict: an
    unstamped node belongs to nobody and is hidden. A node stamped with ANOTHER
    tenant is hidden under both — that is the part that is not configurable.
    """
    value = (properties or {}).get(TENANT_PROPERTY)
    if value in (None, ""):
        return not strict
    return str(value) == str(tenant_id)


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
                 allow_fuzzy: bool = True,
                 tenant_id: Optional[str] = None,
                 tenant_strict: Optional[bool] = None) -> GraphContext:
        """Resolve a question to entities and return their ranked neighbourhood.

        `tenant_id` is OPTIONAL and defaults to None, which applies NO tenant
        predicate at all. Every existing caller — the evaluation harness, the
        observability probes, the CLI — therefore behaves exactly as before. A
        tenant is filtered on only when one is supplied; it is never inferred, and
        an empty string is treated as absent rather than as a tenant that matches
        nothing.
        """
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

        # ── tenant predicate ────────────────────────────────────────────────
        # BEFORE top_k, deliberately. Filtering afterwards would take the global
        # top 8 and then remove some, silently narrowing the neighbourhood to
        # fewer than graph_top_k; filtering first keeps "the top 8 nodes THIS
        # tenant can see", which is what preserves the frozen graph_top_k
        # semantics rather than merely the frozen number.
        #
        # An edge survives only if BOTH endpoints do: a relationship is evidence
        # about two entities, so a half-visible edge would leak the existence of
        # the hidden one through its own text.
        if tenant_id:
            strict = TENANT_STRICT if tenant_strict is None else tenant_strict
            before = len(subgraph.nodes)
            visible = {n.entity_id for n in subgraph.nodes
                       if _tenant_visible(n.properties, tenant_id, strict=strict)}
            subgraph.nodes = [n for n in subgraph.nodes if n.entity_id in visible]
            subgraph.edges = [e for e in subgraph.edges
                              if e.start_id in visible and e.end_id in visible]
            # A seed the tenant cannot see must not survive in `resolved` either:
            # that list is rendered to the model, so leaving it would disclose the
            # entity's NAME across the boundary even with its edges removed.
            context.resolved = [e for e in context.resolved if e.entity_id in visible]
            context.unresolved = list(context.unresolved)
            stats.nodes_visited = len(subgraph.nodes)
            stats.edges_considered = len(subgraph.edges)
            if before != len(subgraph.nodes):
                log.info("kg api: tenant filter kept %d/%d node(s) for tenant=%s (strict=%s)",
                         len(subgraph.nodes), before, tenant_id, strict)

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
