"""
Shapes the retrieval layer returns.

Retrieval hands back DATA, never text: no prompt is assembled and no answer is
generated here. Everything below is designed so that a later phase — or a human
in the Neo4j browser — can see exactly which sources support each fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class Evidence:
    """Why a fact is in the graph. Attached to every edge that is returned.

    An edge without evidence is an unsupported fact, and the retriever drops it
    rather than returning something it cannot justify."""

    source_ids: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    confidence: float = 0.0
    observations: int = 0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    models: list[str] = field(default_factory=list)

    @classmethod
    def from_properties(cls, props: dict[str, Any]) -> "Evidence":
        return cls(
            source_ids=list(props.get("source_ids") or []),
            source_types=list(props.get("source_types") or []),
            confidence=float(props.get("confidence") or 0.0),
            observations=int(props.get("observations") or 0),
            first_seen=props.get("first_seen"),
            last_seen=props.get("last_seen"),
            models=list(props.get("models") or []),
        )

    @property
    def is_supported(self) -> bool:
        """At least one source vouches for this."""
        return bool(self.source_ids) or self.observations > 0

    @property
    def is_corroborated(self) -> bool:
        """More than one distinct source — the strongest signal available here."""
        return len(set(self.source_ids)) > 1


@dataclass(slots=True)
class ResolvedEntity:
    """A mention from the question, bound to a real node (or not)."""

    mention: str                        # what the question actually said
    entity_id: str = ""                 # canonical node id, "" when unresolved
    canonical_name: str = ""
    labels: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    match_type: str = "none"            # exact | alias | registry | fuzzy | none
    score: float = 0.0                  # resolution confidence, not graph confidence
    evidence: Optional[Evidence] = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(self.entity_id)


@dataclass(slots=True)
class ScoredNode:
    """A node in the returned subgraph with its ranking breakdown.

    `signals` is kept alongside `score` deliberately: a single opaque float is
    impossible to tune or debug, and ranking quality is the thing most likely to
    need adjustment once real questions hit this."""

    entity_id: str
    canonical_name: str
    labels: list[str] = field(default_factory=list)
    hop: int = 0                        # 0 = a seed, 1 = one hop out, …
    score: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)
    evidence: Optional[Evidence] = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScoredEdge:
    """A relationship with its evidence and the score it inherited."""

    rel_type: str
    start_id: str
    end_id: str
    score: float = 0.0
    evidence: Evidence = field(default_factory=Evidence)
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Subgraph:
    """The retrieved neighbourhood: ranked nodes plus the edges among them."""

    nodes: list[ScoredNode] = field(default_factory=list)
    edges: list[ScoredEdge] = field(default_factory=list)
    seed_ids: list[str] = field(default_factory=list)
    truncated: bool = False             # a limit was hit; the graph has more
    dropped_unsupported: int = 0        # edges withheld for having no evidence

    @property
    def node_ids(self) -> list[str]:
        return [n.entity_id for n in self.nodes]

    def top(self, k: int) -> list[ScoredNode]:
        return self.nodes[:k]

    def as_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


@dataclass(slots=True)
class RetrievalStats:
    resolve_ms: float = 0.0
    extract_ms: float = 0.0
    expand_ms: float = 0.0
    rank_ms: float = 0.0
    total_ms: float = 0.0
    graph_queries: int = 0
    mentions_found: int = 0
    mentions_resolved: int = 0
    nodes_visited: int = 0
    edges_considered: int = 0
    edges_dropped_unsupported: int = 0


@dataclass(slots=True)
class GraphContext:
    """What the hybrid API returns: resolved entities, related entities, graph."""

    question: str = ""
    resolved: list[ResolvedEntity] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    related: list[ScoredNode] = field(default_factory=list)
    subgraph: Subgraph = field(default_factory=Subgraph)
    stats: RetrievalStats = field(default_factory=RetrievalStats)
    ok: bool = True
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)
