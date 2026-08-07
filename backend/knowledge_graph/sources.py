"""
KnowledgeSource — what the pipeline consumes, whatever produced it.

Phase 2 took a bare string and assumed it came from a conversation. That coupled
the pipeline to chat: a document, an email or an OCR page had nowhere to put its
id, its author or its timestamp, so provenance was impossible.

A source is just (id, type, text, metadata). The builder never inspects `type` —
it only copies provenance through — so adding a new kind of input is a
registration, not a code change:

    register_source_type("slack_message", description="One Slack message")
    src = KnowledgeSource(id="msg-1", type="slack_message", text="...")
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class SourceType(str, Enum):
    """Input kinds the platform ships with. Not a closed set — see register_source_type."""

    CONVERSATION = "conversation"
    MESSAGE = "message"
    DOCUMENT = "document"
    EMAIL = "email"
    MEETING = "meeting"
    CALENDAR_EVENT = "calendar_event"
    OCR_TEXT = "ocr_text"
    TRANSCRIPT = "transcript"
    NOTE = "note"
    UNKNOWN = "unknown"


# Runtime registry so deployments can add source kinds without editing the enum.
_EXTRA_SOURCE_TYPES: dict[str, str] = {}


def register_source_type(name: str, description: str = "") -> str:
    """Make a new source type known. Idempotent; returns the normalised name."""
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    if not key:
        raise ValueError("source type name cannot be empty")
    _EXTRA_SOURCE_TYPES.setdefault(key, description or key)
    return key


def known_source_types() -> dict[str, str]:
    """Every source type currently accepted, built-in plus registered."""
    out = {t.value: t.name.title().replace("_", " ") for t in SourceType}
    out.update(_EXTRA_SOURCE_TYPES)
    return out


def is_known_source_type(name: str) -> bool:
    return str(name).lower() in known_source_types()


@dataclass(slots=True)
class KnowledgeSource:
    """One unit of text to turn into graph, plus where it came from.

    `metadata` carries the identifiers provenance needs — user_id,
    conversation_id, message_id, document_id — rather than a fixed field per
    kind, so an unforeseen source type has somewhere to put its own ids.
    """

    id: str
    type: str = SourceType.CONVERSATION.value
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.type = str(self.type).lower()
        if not self.id:
            # A stable content hash beats a random id: re-ingesting the same text
            # without an id then merges onto the same provenance entry instead of
            # accumulating a new one on every run.
            digest = hashlib.sha256((self.text or "").encode("utf-8")).hexdigest()[:16]
            self.id = f"{self.type}:{digest}"
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    # ── the identifiers provenance cares about ───────────────────────────────

    @property
    def user_id(self) -> str:
        return str(self.metadata.get("user_id") or "")

    @property
    def conversation_id(self) -> str:
        return str(self.metadata.get("conversation_id")
                   or (self.id if self.type == SourceType.CONVERSATION.value else ""))

    @property
    def message_id(self) -> str:
        return str(self.metadata.get("message_id")
                   or (self.id if self.type == SourceType.MESSAGE.value else ""))

    @property
    def document_id(self) -> str:
        doc_kinds = (SourceType.DOCUMENT.value, SourceType.OCR_TEXT.value)
        return str(self.metadata.get("document_id")
                   or (self.id if self.type in doc_kinds else ""))

    # ── constructors for the common kinds ────────────────────────────────────

    @classmethod
    def from_text(cls, text: str, source_id: str = "",
                  source_type: str = SourceType.CONVERSATION.value,
                  **metadata: Any) -> "KnowledgeSource":
        """Convenience for callers that only have a string (and the phase-2 API)."""
        return cls(id=source_id, type=source_type, text=text, metadata=dict(metadata))

    @classmethod
    def conversation(cls, conversation_id: str, text: str, **metadata: Any) -> "KnowledgeSource":
        return cls(id=conversation_id, type=SourceType.CONVERSATION.value,
                   text=text, metadata={"conversation_id": conversation_id, **metadata})

    @classmethod
    def document(cls, document_id: str, text: str, **metadata: Any) -> "KnowledgeSource":
        return cls(id=document_id, type=SourceType.DOCUMENT.value,
                   text=text, metadata={"document_id": document_id, **metadata})

    @classmethod
    def email(cls, message_id: str, text: str, **metadata: Any) -> "KnowledgeSource":
        return cls(id=message_id, type=SourceType.EMAIL.value,
                   text=text, metadata={"message_id": message_id, **metadata})

    @classmethod
    def meeting(cls, meeting_id: str, text: str, **metadata: Any) -> "KnowledgeSource":
        return cls(id=meeting_id, type=SourceType.MEETING.value,
                   text=text, metadata={"meeting_id": meeting_id, **metadata})

    @classmethod
    def transcript(cls, transcript_id: str, text: str, **metadata: Any) -> "KnowledgeSource":
        return cls(id=transcript_id, type=SourceType.TRANSCRIPT.value,
                   text=text, metadata={"transcript_id": transcript_id, **metadata})


def coerce_source(value: "str | KnowledgeSource", **defaults: Any) -> KnowledgeSource:
    """Accept either a bare string (phase-2 call style) or a KnowledgeSource.

    This is what keeps `pipeline.process("some text")` working unchanged."""
    if isinstance(value, KnowledgeSource):
        return value
    return KnowledgeSource.from_text(str(value or ""), **defaults)
