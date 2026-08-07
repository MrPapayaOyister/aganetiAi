"""
Graph vocabulary — the labels, relationship types and shapes GraphService accepts.

Labels and relationship types CANNOT be Cypher parameters (they are part of the
query plan, not data), so they are the one thing that must be interpolated into a
query string. That makes them the only injection surface in this package, which
is why they live here as closed enums and every call site is validated by
`validate_label` / `validate_rel_type` before any string reaches Cypher.

Property values are always passed as real parameters — see queries.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class NodeLabel(str, Enum):
    """Every node label the platform knows about — the single source of truth.

    Bootstrap creates an `id` uniqueness constraint for each one, the extraction
    prompt is generated from this list, and GraphService rejects anything not in
    it. Adding a label here is the ONLY step needed to support it end to end.
    """

    # ── platform / identity ──
    USER = "User"
    ORGANIZATION = "Organization"
    PERSON = "Person"
    COMPANY = "Company"
    LOCATION = "Location"

    # ── content ──
    CONVERSATION = "Conversation"
    MESSAGE = "Message"
    DOCUMENT = "Document"
    EMAIL = "Email"
    MEETING = "Meeting"
    MEMORY = "Memory"

    # ── work ──
    TASK = "Task"
    PROJECT = "Project"
    REPOSITORY = "Repository"
    ISSUE = "Issue"
    PR = "PR"
    CALENDAR = "Calendar"

    # ── technical ──
    TECHNOLOGY = "Technology"
    PROVIDER = "Provider"
    MODEL = "Model"
    PLUGIN = "Plugin"
    API = "API"
    SERVICE = "Service"
    # Physical or virtual compute. Distinct from Technology on purpose: "what a
    # thing runs ON" is the question DEPLOYED_ON/HOSTED_ON answer, and collapsing
    # a DGX box into Technology makes that traversal meaningless.
    SERVER = "Server"

    # ── catch-all: anything the extractor could not classify ──
    ENTITY = "Entity"


class RelType(str, Enum):
    """Every relationship type. Direction is expressed at call time, not here.

    Same contract as NodeLabel: the extraction prompt is generated from this
    list and unknown types are dropped rather than written.
    """

    # ── usage / dependency ──
    USES = "USES"
    DEPENDS_ON = "DEPENDS_ON"
    CONFIGURED = "CONFIGURED"
    DEPLOYED_ON = "DEPLOYED_ON"
    HOSTED_ON = "HOSTED_ON"
    CONNECTED_TO = "CONNECTED_TO"
    ROUTES_TO = "ROUTES_TO"
    AUTHENTICATES_WITH = "AUTHENTICATES_WITH"

    # ── ownership / membership ──
    OWNS = "OWNS"
    WORKS_ON = "WORKS_ON"
    MEMBER_OF = "MEMBER_OF"
    PART_OF = "PART_OF"
    BELONGS_TO = "BELONGS_TO"
    # Org chart. MEMBER_OF says "is in this team"; REPORTS_TO says who to, and
    # only the latter can answer "who is above X" without a second lookup.
    REPORTS_TO = "REPORTS_TO"
    ASSIGNED_TO = "ASSIGNED_TO"

    # ── reference ──
    MENTIONS = "MENTIONS"
    REFERENCES = "REFERENCES"
    RELATED_TO = "RELATED_TO"
    LINKED_TO = "LINKED_TO"
    DERIVED_FROM = "DERIVED_FROM"

    # ── activity ──
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    GENERATED = "GENERATED"
    AUTHORED = "AUTHORED"
    ATTENDS = "ATTENDS"
    PARTICIPATED_IN = "PARTICIPATED_IN"
    STORES = "STORES"
    READS = "READS"
    WRITES = "WRITES"
    PROCESSES = "PROCESSES"
    # A component realises a capability (YOLOv8 IMPLEMENTS object detection).
    # USES would invert the meaning — the model is not a consumer here.
    IMPLEMENTS = "IMPLEMENTS"
    # Content-about-a-thing. DOCUMENTS is asserted (a doc's subject); MENTIONS is
    # incidental. DISCUSSED_IN points the other way: thing → meeting/email.
    DOCUMENTS = "DOCUMENTS"
    DISCUSSED_IN = "DISCUSSED_IN"


# Defensive shape check for identifiers that must be interpolated. Enum
# membership is the primary gate; this is the backstop for callers passing a raw
# string for a label the platform has not enumerated yet.
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class InvalidLabel(ValueError):
    """A label or relationship type that is not safe to interpolate into Cypher."""


def validate_label(label: "str | NodeLabel") -> str:
    """Return a label string safe to embed in a query, or raise."""
    value = label.value if isinstance(label, NodeLabel) else str(label)
    if not _IDENT.match(value):
        raise InvalidLabel(
            f"invalid node label {value!r}: must match {_IDENT.pattern} "
            "(labels cannot be parameterized, so they are strictly validated)")
    return value


def validate_rel_type(rel_type: "str | RelType") -> str:
    """Return a relationship type safe to embed in a query, or raise."""
    value = rel_type.value if isinstance(rel_type, RelType) else str(rel_type)
    if not _IDENT.match(value):
        raise InvalidLabel(
            f"invalid relationship type {value!r}: must match {_IDENT.pattern}")
    return value


@dataclass(slots=True)
class GraphNode:
    """A node as the service returns it.

    `id` is the platform's business key (the property carrying the uniqueness
    constraint), never Neo4j's internal element id — that is not stable across
    restarts and must not be persisted anywhere.
    """

    id: str
    label: str
    properties: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, node: Any) -> "GraphNode":
        props = dict(node)
        labels = list(getattr(node, "labels", []) or [])
        return cls(id=props.get("id", ""), label=labels[0] if labels else "", properties=props)


@dataclass(slots=True)
class GraphRelationship:
    """A relationship plus the two node ids it connects."""

    type: str
    start_id: str
    end_id: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Neighbor:
    """A node reached from another, with the relationship that got us there."""

    node: GraphNode
    rel_type: str
    direction: str                      # "out" | "in"
    rel_properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QueryStats:
    """What a write actually did — surfaced so callers can assert on effects."""

    nodes_created: int = 0
    nodes_deleted: int = 0
    relationships_created: int = 0
    relationships_deleted: int = 0
    properties_set: int = 0
    took_ms: Optional[float] = None
