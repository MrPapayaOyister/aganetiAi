"""
The pipeline — text in, graph updates and statistics out.

    text
      │
      ├─► EntityExtractor        (LiteLLM, json_object)
      │
      ├─► Normalizer             (clean → alias → case → dedupe)
      │
      ├─► RelationshipExtractor  (LiteLLM, grounded in the normalized entities)
      │
      ├─► Normalizer             (repoint endpoints, drop duplicate edges)
      │
      └─► KnowledgeGraphBuilder ─► GraphService ─► Neo4j

Standalone by design: nothing here is imported by memory, chat, routing,
orchestration, retrieval or the provider integrations. Call it by hand:

    from backend.knowledge_graph import KnowledgeGraphPipeline
    result = KnowledgeGraphPipeline().process("Akshay configured Microsoft Graph…")

Relationship extraction is grounded in the NORMALIZED entity list rather than the
raw one. Prompting with "Microsoft Graph" after normalizing "MS Graph" means the
model returns endpoints that already match the nodes, so far fewer edges are
dropped as unresolvable.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .builder import BatchKnowledgeGraphBuilder, KnowledgeGraphBuilder
from .extractor import EntityExtractor, KnowledgeExtractor, RelationshipExtractor
from .normalizer import Normalizer
from .provenance import Provenance
from .service import GraphService
from .sources import KnowledgeSource, coerce_source
from .types import PipelineResult, PipelineStats

log = logging.getLogger("aganeti.kg.pipeline")


class KnowledgeGraphPipeline:
    """Wires the stages together. Every stage is injectable for testing.

    Phase 2.5 changed two things and kept the old behaviour reachable:
      * `extractor` is now the single-call KnowledgeExtractor. Pass
        `legacy_two_call=True` to use the phase-2 EntityExtractor →
        RelationshipExtractor pair (the benchmark does exactly that).
      * `process()` accepts a KnowledgeSource OR a bare string. A string is
        wrapped as a conversation, so `process("some text")` is unchanged.
    """

    def __init__(self,
                 extractor: Optional[KnowledgeExtractor] = None,
                 normalizer: Optional[Normalizer] = None,
                 builder: Optional[BatchKnowledgeGraphBuilder] = None,
                 service: Optional[GraphService] = None,
                 source: str = "conversation",
                 legacy_two_call: bool = False,
                 legacy_builder: bool = False,
                 entity_extractor: Optional[EntityExtractor] = None,
                 relationship_extractor: Optional[RelationshipExtractor] = None) -> None:
        self.extractor = extractor or KnowledgeExtractor()
        self.normalizer = normalizer or Normalizer()
        self.legacy_two_call = legacy_two_call
        # Retained so phase-2 constructor kwargs still work.
        self.entity_extractor = entity_extractor or EntityExtractor()
        self.relationship_extractor = relationship_extractor or RelationshipExtractor()
        if builder is not None:
            self.builder = builder
        elif legacy_builder:
            self.builder = KnowledgeGraphBuilder(service=service, source=source)
        else:
            self.builder = BatchKnowledgeGraphBuilder(service=service, source=source)

    # ── extraction strategies ────────────────────────────────────────────────

    def _extract(self, source: KnowledgeSource, stats: PipelineStats):
        """One call by default; two when legacy_two_call is set."""
        if not self.legacy_two_call:
            result = self.extractor.extract(source.text, source.type)
            stats.llm_calls = result.llm_calls
            stats.extract_ms = result.took_ms
            stats.extract_entities_ms = result.took_ms
            return result

        first = self.entity_extractor.extract(source.text)
        stats.extract_entities_ms = first.took_ms
        if first.error or not first.entities:
            first.llm_calls = 1
            stats.llm_calls = 1
            stats.extract_ms = first.took_ms
            return first
        second = self.relationship_extractor.extract(source.text, first.entities)
        first.relationships = second.relationships
        first.took_ms += second.took_ms
        first.llm_calls = 2
        stats.extract_relationships_ms = second.took_ms
        stats.llm_calls = 2
        stats.extract_ms = first.took_ms
        return first

    def process(self, source: "str | KnowledgeSource", *,
                dry_run: bool = False) -> PipelineResult:
        """Run the full pipeline over one knowledge source.

        `source` may be a KnowledgeSource or a bare string (wrapped as a
        conversation). `dry_run=True` extracts and normalizes but writes
        nothing — the cheap way to inspect what WOULD be written.
        """
        started = time.perf_counter()
        stats = PipelineStats()
        src = coerce_source(source)
        stats.source_id, stats.source_type = src.id, src.type
        if not (src.text or "").strip():
            stats.total_ms = 0.0
            return PipelineResult(stats=stats, ok=True)

        # ── 1. extract (entities + relationships + summary + keywords) ───────
        extraction = self._extract(src, stats)
        stats.entities_extracted = len(extraction.entities)
        stats.relationships_extracted = len(extraction.relationships)
        stats.summary = extraction.summary
        stats.keywords = list(extraction.keywords)
        stats.extraction_confidence = extraction.confidence
        if extraction.error:
            stats.total_ms = (time.perf_counter() - started) * 1000
            return PipelineResult(stats=stats, ok=False,
                                  error=f"extraction failed: {extraction.error}")
        if not extraction.entities:
            stats.total_ms = (time.perf_counter() - started) * 1000
            log.info("kg pipeline: no entities found in %d chars of %s",
                     len(src.text), src.type)
            return PipelineResult(stats=stats, ok=True)

        # ── 2. normalize: canonicalise, merge labels, resolve aliases ────────
        norm_started = time.perf_counter()
        entities, merged, rename = self.normalizer.resolve(extraction.entities)
        stats.duplicates_removed = merged
        stats.labels_merged = merged
        stats.entities_after_normalization = len(entities)

        # ── 3. repoint relationships at canonical names ──────────────────────
        relationships = self.normalizer.normalize_relationships(
            extraction.relationships, rename)
        stats.relationships_resolved = len(relationships)
        stats.normalize_ms = (time.perf_counter() - norm_started) * 1000

        if dry_run:
            stats.total_ms = (time.perf_counter() - started) * 1000
            return PipelineResult(entities=entities, relationships=relationships,
                                  stats=stats, ok=True)

        # ── 4. build, carrying provenance from the source ────────────────────
        model = getattr(self.extractor, "model_name", "")
        prov = Provenance.from_source(src, model=model, confidence=extraction.confidence)
        try:
            build = self.builder.build(entities, relationships, provenance=prov)
        except TypeError:
            # A phase-2 builder injected by an older caller has no provenance arg.
            build = self.builder.build(entities, relationships)
        stats.build_ms = build.took_ms
        stats.graph_queries = getattr(build, "graph_queries", 0)
        stats.nodes_created = build.nodes_created
        stats.nodes_merged = build.nodes_merged
        stats.relationships_created = build.relationships_created
        stats.relationships_merged = build.relationships_merged
        stats.skipped_entities = build.skipped_entities
        stats.skipped_relationships = build.skipped_relationships

        stats.total_ms = (time.perf_counter() - started) * 1000
        log.info("kg pipeline [%s %s]: %d entities → %d nodes (+%d new), "
                 "%d relationships → %d edges (+%d new), %d LLM call(s), "
                 "%d graph quer(y/ies), %.0fms total (extract %.0fms, build %.0fms)",
                 stats.source_type, stats.source_id,
                 stats.entities_extracted, stats.nodes_created + stats.nodes_merged,
                 stats.nodes_created, stats.relationships_extracted,
                 stats.relationships_created + stats.relationships_merged,
                 stats.relationships_created, stats.llm_calls, stats.graph_queries,
                 stats.total_ms, stats.extract_ms, stats.build_ms)
        return PipelineResult(entities=entities, relationships=relationships,
                              stats=stats, ok=True)

    def process_many(self, sources: "list[str | KnowledgeSource]") -> list[PipelineResult]:
        """Sequential convenience wrapper.

        Sequential on purpose: each text costs two LLM calls, and firing them
        concurrently at a single-GPU vLLM backend queues them anyway while making
        failures harder to attribute. Batch concurrency belongs at the gateway.
        """
        return [self.process(s) for s in sources]
