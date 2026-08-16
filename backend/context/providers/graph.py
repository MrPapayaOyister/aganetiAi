"""Knowledge graph — wraps the phase-3 GraphRetrievalAPI, unchanged.

**ENABLED BY DEFAULT since phase 4** — this is the one deliberate behaviour
change of the hybrid engine: graph context now reaches the prompt.

It stays independently switchable, because a Neo4j outage must never be able to
degrade a chat turn beyond losing this one source:

    CONTEXT_GRAPH_ENABLED=false             # per deployment
    ContextBuilder(graph=False)             # per builder

`is_applicable` already returns False when Neo4j is unconfigured, so an
unavailable graph is recorded as `skipped`, not as an error.

It calls GraphRetrievalAPI exactly as phase 3 exposes it — no new Cypher, no
different traversal, no re-ranking.
"""
from __future__ import annotations

import asyncio
import os

from ..bundle import ContextItem
from .base import ContextProvider, ContextRequest


class GraphProvider(ContextProvider):
    name = "graph"
    # Retrieval costs an LLM mention-extraction call (~400ms) plus traversal
    # (~10ms). The ceiling is low on purpose: graph context is an enhancement,
    # and it must never be the reason a turn feels slow.
    timeout = 3.0

    def __init__(self, enabled: "bool | None" = None, depth: "int | None" = None,
                 top_k: int = 8, api=None) -> None:
        self.enabled = (os.getenv("CONTEXT_GRAPH_ENABLED", "true").lower() == "true"
                        if enabled is None else bool(enabled))
        # depth=None → GRAPH_RETRIEVAL_DEPTH (default 1, unchanged). Configuration
        # only: the traversal algorithm is untouched, this just chooses how many
        # times it runs. An explicit argument still wins, for tests and benchmarks.
        if depth is None:
            try:
                from config.settings import GRAPH_RETRIEVAL_DEPTH
                depth = GRAPH_RETRIEVAL_DEPTH
            except Exception:  # noqa: BLE001
                depth = 1
        self.depth = depth
        self.top_k = top_k
        self._api = api

    def _get_api(self):
        if self._api is None:
            from backend.knowledge_graph import GraphRetrievalAPI
            self._api = GraphRetrievalAPI(depth=self.depth)
        return self._api

    def is_applicable(self, request: ContextRequest) -> bool:
        if not (request.message or "").strip():
            return False
        try:
            from backend.knowledge_graph import is_enabled
            return is_enabled()
        except Exception:  # noqa: BLE001
            return False

    async def collect(self, request: ContextRequest) -> list[ContextItem]:
        api = self._get_api()
        # GraphRetrievalAPI is synchronous (the Neo4j driver's sync API is
        # thread-safe); keep it off the event loop exactly as every other
        # graph caller in the codebase does.
        #
        # tenant_id is passed as None when the request carries none, which applies
        # NO predicate — the eval harness and every other tenant-less caller keep
        # their exact current behaviour. depth/top_k are unchanged: the tenant
        # narrows WHICH nodes are eligible, never how far or how many we traverse.
        ctx = await asyncio.to_thread(api.retrieve, request.message,
                                      depth=self.depth, top_k=self.top_k,
                                      tenant_id=(request.tenant_id or None))
        out: list[ContextItem] = []

        # Edges carry the evidence, so they are the useful unit — a node alone
        # says "this exists", an edge says "this relates to that, per source X".
        for edge in ctx.subgraph.edges:
            ev = edge.evidence
            out.append(ContextItem(
                text=f"{edge.start_id} —{edge.rel_type}→ {edge.end_id}",
                provider=self.name,
                source=", ".join(ev.source_ids) or "graph",
                score=edge.score,
                timestamp=ev.last_seen,
                metadata={
                    "kind": "relationship", "rel_type": edge.rel_type,
                    "start_id": edge.start_id, "end_id": edge.end_id,
                    "source_ids": ev.source_ids, "source_types": ev.source_types,
                    "confidence": ev.confidence, "observations": ev.observations,
                    "corroborated": ev.is_corroborated,
                },
            ))
        for node in ctx.related:
            ev = node.evidence
            out.append(ContextItem(
                text=f"{node.canonical_name} ({'/'.join(node.labels) or 'Entity'})",
                provider=self.name,
                source=", ".join(ev.source_ids) if ev else "graph",
                score=node.score,
                timestamp=ev.last_seen if ev else None,
                metadata={"kind": "entity", "entity_id": node.entity_id,
                          "labels": node.labels, "hop": node.hop,
                          "signals": node.signals},
            ))
        # Already ranked by the phase-3 retriever; do not re-sort.
        return out[:self.top_k]
