"""
Deterministic eval fixture.

Golden expectations only mean something against known data, so the framework
ships the graph its cases assert. Everything is written under
`source="eval-fixture"` and removed by the same tag, so seeding is safe against
a populated database and never touches real content.

This is the ONLY module in the framework that writes anything, and it writes
only when explicitly asked (`run_evals.py --seed`).
"""
from __future__ import annotations

import logging

log = logging.getLogger("aganeti.evals.fixture")

SOURCE = "eval-fixture"


def _entities():
    from backend.knowledge_graph import ExtractedEntity
    return [
        ExtractedEntity(name="Akshay", type="Person"),
        ExtractedEntity(name="Agentic AI", type="Project"),
        ExtractedEntity(name="LiteLLM", type="Technology"),
        ExtractedEntity(name="Microsoft Graph", type="Technology",
                        secondary_labels=["API"], aliases=["MS Graph"]),
        ExtractedEntity(name="qwen-fast", type="Model"),
        ExtractedEntity(name="vLLM", type="Technology"),
        ExtractedEntity(name="PostgreSQL", type="Technology"),
        ExtractedEntity(name="Qdrant", type="Technology"),
        ExtractedEntity(name="Neo4j", type="Technology"),
    ]


def _relationships():
    from backend.knowledge_graph import ExtractedRelationship as R
    return [
        R(source="Akshay", type="WORKS_ON", target="Agentic AI"),
        R(source="Agentic AI", type="USES", target="LiteLLM"),
        R(source="Agentic AI", type="USES", target="Microsoft Graph"),
        R(source="Agentic AI", type="STORES", target="PostgreSQL"),
        R(source="Agentic AI", type="USES", target="Qdrant"),
        R(source="Agentic AI", type="USES", target="Neo4j"),
        R(source="LiteLLM", type="ROUTES_TO", target="qwen-fast"),
        R(source="qwen-fast", type="HOSTED_ON", target="vLLM"),
    ]


def seed() -> dict:
    """Create the fixture graph. Idempotent — re-seeding merges, never duplicates."""
    from backend.knowledge_graph import (
        BatchKnowledgeGraphBuilder, ExtractedEntity, Provenance, get_graph_service)
    svc = get_graph_service()
    teardown()
    builder = BatchKnowledgeGraphBuilder(service=svc, source=SOURCE)

    first = builder.build(_entities(), _relationships(), provenance=Provenance(
        source_id="eval-conv-1", source_type="conversation",
        conversation_id="eval-conv-1", confidence=0.9, model="qwen-fast"))
    # A SECOND source corroborates part of the graph, so corroboration
    # expectations in the golden cases have something real to measure.
    second = builder.build(
        [ExtractedEntity(name="LiteLLM", type="Technology"),
         ExtractedEntity(name="Agentic AI", type="Project"),
         ExtractedEntity(name="Microsoft Graph", type="Technology")],
        [__import__("backend.knowledge_graph", fromlist=["x"]).ExtractedRelationship(
            source="Agentic AI", type="USES", target="LiteLLM")],
        provenance=Provenance(source_id="eval-doc-1", source_type="document",
                              document_id="eval-doc-1", confidence=0.85, model="gpt-4.1"))

    try:
        from backend.knowledge_graph import GraphRetrievalAPI
        GraphRetrievalAPI().registry.warm(force=True)
    except Exception:  # noqa: BLE001
        pass

    summary = {"nodes_created": first.nodes_created + second.nodes_created,
               "edges_created": first.relationships_created + second.relationships_created,
               "source": SOURCE}
    log.info("eval fixture seeded: %s", summary)
    return summary


def teardown() -> int:
    """Remove every fixture node. Returns how many were deleted."""
    from backend.knowledge_graph import get_graph_service
    try:
        svc = get_graph_service()
        # Count BEFORE deleting: aggregating in the same statement as DETACH
        # DELETE returns whatever survived the delete, not what was removed.
        rows = svc.run_query("MATCH (n) WHERE n.source = $s RETURN count(n) AS c",
                             {"s": SOURCE}, op="eval_fixture_count")
        n = int(rows[0]["c"]) if rows else 0
        svc.run_query("MATCH (n) WHERE n.source = $s DETACH DELETE n",
                      {"s": SOURCE}, op="eval_fixture_teardown")
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("eval fixture teardown failed: %s", e)
        return 0
