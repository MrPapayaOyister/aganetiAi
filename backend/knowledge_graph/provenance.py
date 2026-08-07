"""
Provenance — where a node or edge came from, accumulated rather than overwritten.

The constraint that shapes this file: **Neo4j properties are scalars or arrays of
scalars.** You cannot store a list of provenance maps on a node. So one logical
"list of observations" is stored as parallel arrays plus a few roll-up scalars:

    source_ids:   ["conv-1", "doc-7"]      append-only, deduplicated
    source_types: ["conversation", "document"]
    user_ids / conversation_ids / message_ids / document_ids : same treatment
    first_seen:   set once, ON CREATE, never touched again
    last_seen:    overwritten every observation
    observations: how many times this fact has been seen
    confidence:   the MAXIMUM seen, not the latest
    models:       every model that has asserted this

Why max confidence rather than latest: a later low-confidence mention should not
erase the fact that a previous pass was certain. Observation count carries the
"seen repeatedly" signal separately, so nothing is lost.

Everything here is pure data — no Cypher, no driver. The builder turns these
dicts into query parameters; the merge semantics live in queries.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# Fields that accumulate as deduplicated arrays on the node.
LIST_FIELDS: tuple[str, ...] = (
    "source_ids", "source_types", "user_ids",
    "conversation_ids", "message_ids", "document_ids", "models",
)


@dataclass(slots=True)
class Provenance:
    """One observation of a fact, from one source, by one model."""

    source_type: str = ""
    source_id: str = ""
    message_id: str = ""
    conversation_id: str = ""
    document_id: str = ""
    user_id: str = ""
    model: str = ""
    confidence: float = 1.0
    created_at: str = ""
    last_seen: str = ""

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.last_seen:
            self.last_seen = self.created_at
        # Clamp rather than reject: a model returning 1.5 or -0.2 is a prompt
        # problem, not a reason to lose the extraction.
        try:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 1.0

    @classmethod
    def from_source(cls, source: Any, model: str = "", confidence: float = 1.0) -> "Provenance":
        """Build provenance from a KnowledgeSource (duck-typed to avoid a cycle)."""
        return cls(
            source_type=getattr(source, "type", "") or "",
            source_id=getattr(source, "id", "") or "",
            message_id=getattr(source, "message_id", "") or "",
            conversation_id=getattr(source, "conversation_id", "") or "",
            document_id=getattr(source, "document_id", "") or "",
            user_id=getattr(source, "user_id", "") or "",
            model=model,
            confidence=confidence,
            created_at=getattr(source, "created_at", "") or "",
        )

    def as_params(self) -> dict[str, Any]:
        """Flatten into the parameter shape the batch queries expect.

        Empty strings are filtered OUT of the list fields — an empty
        `document_id` on a conversation must not become a "" entry in the array.
        """
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "document_id": self.document_id,
            "model": self.model,
            "confidence": self.confidence,
            "first_seen": self.created_at,
            "last_seen": self.last_seen or self.created_at,
            # Parallel single-element lists; Cypher appends them if new.
            "add_source_ids": [v for v in (self.source_id,) if v],
            "add_source_types": [v for v in (self.source_type,) if v],
            "add_user_ids": [v for v in (self.user_id,) if v],
            "add_conversation_ids": [v for v in (self.conversation_id,) if v],
            "add_message_ids": [v for v in (self.message_id,) if v],
            "add_document_ids": [v for v in (self.document_id,) if v],
            "add_models": [v for v in (self.model,) if v],
        }


@dataclass(slots=True)
class ProvenanceSummary:
    """Read-back view of the accumulated provenance on an existing node."""

    source_ids: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    observations: int = 0
    confidence: float = 0.0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    models: list[str] = field(default_factory=list)

    @classmethod
    def from_properties(cls, props: dict[str, Any]) -> "ProvenanceSummary":
        return cls(
            source_ids=list(props.get("source_ids") or []),
            source_types=list(props.get("source_types") or []),
            observations=int(props.get("observations") or 0),
            confidence=float(props.get("confidence") or 0.0),
            first_seen=props.get("first_seen"),
            last_seen=props.get("last_seen"),
            models=list(props.get("models") or []),
        )

    @property
    def is_corroborated(self) -> bool:
        """Seen in more than one distinct source — the cheapest trust signal
        the graph can offer without a scoring model."""
        return len(self.source_ids) > 1
