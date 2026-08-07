"""
Data shapes that flow through the knowledge-graph pipeline.

    raw text ─► ExtractedEntity[] ─► (normalize) ─► ExtractedEntity[] ─┐
                                                                       ├─► BuildResult
             ─► ExtractedRelationship[] ─► (resolve) ────────────────┘

These are deliberately plain dataclasses, not the Neo4j-facing shapes in
models.py: extraction output is *untrusted* (an LLM produced it) and only becomes
a GraphNode once the builder has validated its label and minted a stable id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class ExtractedEntity:
    """One entity as the extractor found it, before and after normalization.

    `name` is the current best form; `raw_name` keeps what the model actually
    emitted so a bad alias mapping can be traced back to its source.

    Phase 2.5 added the multi-label fields. A thing is one node with several
    labels — Microsoft Graph is a Technology *and* an API *and* a Service — so
    `type`/`primary_label` names the most specific one and `secondary_labels`
    carries the rest. `type` is retained as an alias of `primary_label` purely so
    phase-2 code that reads `.type` keeps working.
    """

    name: str
    type: str                                   # a NodeLabel value once validated
    raw_name: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    # Filled by the builder — the deterministic id the node is merged on.
    node_id: str = ""
    # ── phase 2.5 ──
    canonical_name: str = ""
    aliases: list[str] = field(default_factory=list)
    secondary_labels: list[str] = field(default_factory=list)
    # None means "the extractor did not state one" — distinct from a stated 1.0.
    # Without that distinction the default would silently override the
    # source-level confidence when provenance is written.
    confidence: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.raw_name:
            self.raw_name = self.name
        if not self.canonical_name:
            self.canonical_name = self.name

    @property
    def primary_label(self) -> str:
        return self.type

    @primary_label.setter
    def primary_label(self, value: str) -> None:
        self.type = value

    @property
    def all_labels(self) -> list[str]:
        """Primary first, then secondaries, de-duplicated and order-stable."""
        out = [self.type] + [l for l in self.secondary_labels if l and l != self.type]
        seen, ordered = set(), []
        for label in out:
            if label not in seen:
                seen.add(label)
                ordered.append(label)
        return ordered


@dataclass(slots=True)
class ExtractedRelationship:
    """One relationship, expressed between entity NAMES rather than ids.

    The extractor works in names because that is what the text contains; the
    builder resolves them to nodes and drops any edge whose endpoints did not
    survive extraction/normalization.
    """

    source: str
    type: str                                   # a RelType value once validated
    target: str
    raw_source: str = ""
    raw_target: str = ""
    raw_type: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None          # phase 2.5; None = unstated

    def __post_init__(self) -> None:
        if not self.raw_source:
            self.raw_source = self.source
        if not self.raw_target:
            self.raw_target = self.target
        if not self.raw_type:
            self.raw_type = self.type


@dataclass(slots=True)
class ExtractionResult:
    """Output of one extractor call, with what it cost.

    Phase 2.5 folds entities AND relationships into a single response, so this
    now also carries the document-level fields that come with them."""

    entities: list[ExtractedEntity] = field(default_factory=list)
    relationships: list[ExtractedRelationship] = field(default_factory=list)
    took_ms: float = 0.0
    # Raw text the model returned when parsing failed — kept for debugging, not logged.
    error: Optional[str] = None
    # ── phase 2.5: document-level output of the single call ──
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    confidence: float = 1.0
    llm_calls: int = 1


@dataclass(slots=True)
class BuildResult:
    """What the builder actually wrote."""

    nodes_created: int = 0
    nodes_merged: int = 0                       # matched an existing node
    relationships_created: int = 0
    relationships_merged: int = 0
    skipped_entities: list[str] = field(default_factory=list)
    skipped_relationships: list[str] = field(default_factory=list)
    took_ms: float = 0.0
    # ── phase 2.5 ──
    graph_queries: int = 0                      # Neo4j round trips actually issued
    labels_merged: int = 0                      # existing nodes that gained a label


@dataclass(slots=True)
class PipelineStats:
    """End-to-end statistics for one `pipeline.process()` call.

    Stage timings are separated because the two LLM calls dominate: knowing the
    split is what tells you whether to batch, cache, or shrink the prompt.
    """

    entities_extracted: int = 0
    entities_after_normalization: int = 0
    duplicates_removed: int = 0
    relationships_extracted: int = 0
    relationships_resolved: int = 0

    nodes_created: int = 0
    nodes_merged: int = 0
    relationships_created: int = 0
    relationships_merged: int = 0

    skipped_entities: list[str] = field(default_factory=list)
    skipped_relationships: list[str] = field(default_factory=list)

    extract_entities_ms: float = 0.0
    extract_relationships_ms: float = 0.0
    normalize_ms: float = 0.0
    build_ms: float = 0.0
    total_ms: float = 0.0

    # ── phase 2.5 ──
    llm_calls: int = 0                 # 1 for the unified extractor, 2 for legacy
    extract_ms: float = 0.0            # single-call extraction time
    graph_queries: int = 0             # actual Neo4j round trips the build used
    labels_merged: int = 0             # nodes that gained a label instead of forking
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    extraction_confidence: float = 0.0
    source_id: str = ""
    source_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


@dataclass(slots=True)
class PipelineResult:
    """Everything one pipeline run produced — stats plus the final graph payload."""

    entities: list[ExtractedEntity] = field(default_factory=list)
    relationships: list[ExtractedRelationship] = field(default_factory=list)
    stats: PipelineStats = field(default_factory=PipelineStats)
    ok: bool = True
    error: Optional[str] = None
