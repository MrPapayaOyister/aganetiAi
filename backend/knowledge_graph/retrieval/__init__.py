"""
Graph retrieval (phase 3) — entity resolution and evidence-carrying subgraph reads.

    registry.py    CanonicalEntityRegistry — alias → canonical id, cached, persisted
    resolver.py    EntityResolver — question → canonical entities (LLM finds mentions only)
    ranking.py     five signals → one score, with the breakdown kept
    retriever.py   GraphRetriever — BFS expansion, evidence required
    api.py         GraphRetrievalAPI — the composed entry point
    types.py       Evidence, ResolvedEntity, ScoredNode, ScoredEdge, Subgraph, GraphContext

This layer is READ-ONLY and generation-free: it queries Neo4j, ranks what it
finds, and returns data. It builds no prompts, produces no answers, and does not
touch Qdrant, memory, the orchestrator or the chat pipeline.
"""
from .api import GraphRetrievalAPI
from .ranking import RankingWeights, score_edge, score_node
from .registry import CanonicalEntityRegistry, escape_lucene, similarity
from .resolver import EntityResolver
from .retriever import GraphRetriever
from .types import (
    Evidence,
    GraphContext,
    ResolvedEntity,
    RetrievalStats,
    ScoredEdge,
    ScoredNode,
    Subgraph,
)

__all__ = [
    "GraphRetrievalAPI",
    "CanonicalEntityRegistry", "EntityResolver", "GraphRetriever",
    "RankingWeights", "score_node", "score_edge", "similarity", "escape_lucene",
    "Evidence", "ResolvedEntity", "ScoredNode", "ScoredEdge",
    "Subgraph", "GraphContext", "RetrievalStats",
]
