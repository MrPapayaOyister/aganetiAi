"""
Knowledge graph — Neo4j infrastructure (phase 1) plus the builder that populates
it from natural language (phase 2).

Two layers, deliberately separable:

    ── infrastructure ──────────────────────────────────────────────
    client.py     driver singleton, connectivity probe, shutdown
    models.py     labels, relationship types, shapes, identifier validation
    queries.py    parameterized Cypher, one function per statement
    service.py    GraphService — THE ONLY executor of Cypher
    bootstrap.py  IF NOT EXISTS constraints + indexes

    ── knowledge-graph builder ─────────────────────────────────────
    types.py      extraction / build / pipeline dataclasses
    schemas.py    JSON contract + prompts, generated from the label enums
    extractor.py  EntityExtractor, RelationshipExtractor (LiteLLM json_object)
    normalizer.py alias mapping, casing, deduplication
    builder.py    KnowledgeGraphBuilder — calls GraphService, never Cypher
    pipeline.py   KnowledgeGraphPipeline — text in, statistics out

This is NOT GraphRAG and NOT retrieval: nothing here is queried to answer a user.
The pipeline is standalone — memory, chat, routing, orchestration, retrieval and
the provider integrations neither import it nor are affected by it.

Importing this package never opens a connection; the driver is built on first
query, so an unreachable Neo4j cannot affect application startup.
"""
from .client import GraphUnavailable, close_driver, get_driver, is_enabled, verify_connectivity
from .bootstrap import bootstrap_schema, describe_schema
from .models import (
    GraphNode,
    GraphRelationship,
    InvalidLabel,
    Neighbor,
    NodeLabel,
    QueryStats,
    RelType,
)
from .service import GraphService, get_graph_service

# ── knowledge-graph builder ──────────────────────────────────────────────────
from .builder import (
    BatchKnowledgeGraphBuilder,
    KnowledgeGraphBuilder,
    entity_id_for,
    node_id_for,
    slugify,
)
from .extractor import EntityExtractor, KnowledgeExtractor, RelationshipExtractor
from .normalizer import CANONICAL_ALIASES, Normalizer
from .pipeline import KnowledgeGraphPipeline
from .provenance import Provenance, ProvenanceSummary
from .sources import (
    KnowledgeSource,
    SourceType,
    coerce_source,
    known_source_types,
    register_source_type,
)
from .retrieval import (
    CanonicalEntityRegistry,
    EntityResolver,
    Evidence,
    GraphContext,
    GraphRetrievalAPI,
    GraphRetriever,
    RankingWeights,
    ResolvedEntity,
    ScoredEdge,
    ScoredNode,
    Subgraph,
)
from .types import (
    BuildResult,
    ExtractedEntity,
    ExtractedRelationship,
    ExtractionResult,
    PipelineResult,
    PipelineStats,
)

__all__ = [
    # ── infrastructure ──
    "get_driver", "verify_connectivity", "close_driver", "is_enabled", "GraphUnavailable",
    "GraphService", "get_graph_service",
    "bootstrap_schema", "describe_schema",
    "NodeLabel", "RelType", "GraphNode", "GraphRelationship", "Neighbor",
    "QueryStats", "InvalidLabel",
    # ── builder ──
    "KnowledgeGraphPipeline",
    "KnowledgeExtractor", "BatchKnowledgeGraphBuilder",
    "KnowledgeGraphBuilder", "EntityExtractor", "RelationshipExtractor", "Normalizer",
    "CANONICAL_ALIASES", "slugify", "node_id_for", "entity_id_for",
    "ExtractedEntity", "ExtractedRelationship", "ExtractionResult",
    "BuildResult", "PipelineResult", "PipelineStats",
    # ── phase 2.5 ──
    "KnowledgeSource", "SourceType", "register_source_type", "known_source_types",
    "coerce_source", "Provenance", "ProvenanceSummary",
    # ── phase 3: retrieval ──
    "GraphRetrievalAPI", "CanonicalEntityRegistry", "EntityResolver", "GraphRetriever",
    "RankingWeights", "Evidence", "ResolvedEntity", "ScoredNode", "ScoredEdge",
    "Subgraph", "GraphContext",
]
