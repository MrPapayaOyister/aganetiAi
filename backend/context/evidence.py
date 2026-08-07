"""
Evidence — why an item is in the bundle, and who vouches for it.

Distinct from `backend.knowledge_graph.retrieval.types.Evidence`, which describes
provenance of ONE graph edge. This is the cross-provider version: a single fact
can be attested by a Qdrant chunk, a graph edge and a conversation at once, and
that agreement is the most valuable signal the hybrid engine has. Graph Evidence
converts into this via `from_graph_metadata`.

The point of keeping a list rather than a merged blob: corroboration is
*counting distinct sources*, and you cannot count what you have already flattened.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class Evidence:
    """One provider's attestation of one piece of information."""

    provider: str                              # "corporate" | "graph" | "memory" | …
    source_id: str = ""                        # the provider's own identifier
    document_id: Optional[str] = None
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    graph_node: Optional[str] = None
    graph_edge: Optional[str] = None           # "start -REL-> end"
    confidence: float = 0.0                    # the provider's own certainty, 0–1
    corroboration: int = 1                     # distinct sources behind THIS attestation
    timestamp: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Identity for dedup: two attestations are the same when the provider AND
        the underlying source match. Without the provider, a graph edge and a
        document chunk sharing an id would collapse into one."""
        return f"{self.provider}:{self.source_id or self.graph_edge or self.graph_node or ''}"

    @classmethod
    def from_graph_metadata(cls, meta: dict[str, Any], score: float = 0.0) -> "list[Evidence]":
        """Explode a GraphProvider item into one Evidence per originating source.

        A graph edge already records WHICH conversations and documents asserted
        it, so one graph item legitimately produces several attestations — that
        is precisely the corroboration the knowledge graph was built to capture.
        """
        source_ids = list(meta.get("source_ids") or [])
        source_types = list(meta.get("source_types") or [])
        edge = None
        if meta.get("kind") == "relationship":
            edge = f"{meta.get('start_id')} -{meta.get('rel_type')}-> {meta.get('end_id')}"
        node = meta.get("entity_id") or meta.get("start_id")

        if not source_ids:
            return [cls(provider="graph", graph_node=node, graph_edge=edge,
                        confidence=float(meta.get("confidence") or score or 0.0),
                        corroboration=1, timestamp=meta.get("last_seen"))]

        out = []
        for i, sid in enumerate(source_ids):
            stype = source_types[i] if i < len(source_types) else ""
            out.append(cls(
                provider="graph", source_id=sid,
                document_id=sid if stype == "document" else None,
                conversation_id=sid if stype == "conversation" else None,
                message_id=sid if stype in ("message", "email") else None,
                graph_node=node, graph_edge=edge,
                confidence=float(meta.get("confidence") or 0.0),
                corroboration=len(source_ids),
                timestamp=meta.get("last_seen"),
                metadata={"source_type": stype},
            ))
        return out

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def merge_evidence(groups: "list[list[Evidence]]") -> "list[Evidence]":
    """Flatten several evidence lists, dropping exact duplicates by `key`.

    Order is preserved so the first (highest-ranked) attestation stays first —
    which is what makes "keep the strongest provenance" work downstream.
    """
    seen: set[str] = set()
    out: list[Evidence] = []
    for group in groups:
        for ev in group:
            if ev.key in seen:
                continue
            seen.add(ev.key)
            out.append(ev)
    return out


def corroboration_of(evidence: "list[Evidence]") -> int:
    """How many DISTINCT sources back this item.

    Counts unique (provider, source_id) pairs rather than list length: one graph
    edge citing the same document twice is one source, not two.
    """
    distinct = {
        (e.provider, e.source_id or e.document_id or e.conversation_id
         or e.graph_edge or e.graph_node or "")
        for e in evidence
    }
    return len(distinct) or (1 if evidence else 0)


def providers_of(evidence: "list[Evidence]") -> "list[str]":
    """Distinct provider names, order-stable — the 'agreed across sources' signal."""
    seen, out = set(), []
    for e in evidence:
        if e.provider not in seen:
            seen.add(e.provider)
            out.append(e.provider)
    return out
