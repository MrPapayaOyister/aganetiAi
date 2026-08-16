"""graph_search — direct, tenant-scoped access to the Neo4j knowledge graph.

Runtime B already has `knowledge_search` (orchestrator/knowledge.py), which
federates the Qdrant corpus and the graph through the frozen GraphRAG pipeline and
returns fused, ranked, cited prose. That is the right tool for "what does the
company know about X".

This is a different question: "what is X connected to, and how". It goes straight
to `GraphService` and returns entities and the relationships between them, which
the fused pipeline flattens into text and reorders by relevance. The two are
complementary and both are registered.

THREE INVARIANTS, each of which has a test.

1. **The model never supplies Cypher.** The tool's input schema is a natural-language
   `query`, an integer `limit` and a boolean `expand`. There is no parameter through
   which a statement, a fragment, a label or a property name can be passed. An LLM
   with a `run_query` tool is an LLM with arbitrary read access to the graph, and no
   amount of prompt instruction is a substitute for not offering it.

2. **GraphService is the only Cypher executor.** This module authors no Cypher. It
   calls typed methods (`tenant_search_entities`, `tenant_find_entities`,
   `tenant_expand_one_hop`) whose statements live in `knowledge_graph/queries.py`
   with every value parameterised. `GraphService.run_query` — the escape hatch — is
   deliberately not imported here.

3. **The tenant reaches the query, not just the log line.** Every call passes
   `tenant_id` into a real Cypher predicate. See the caveat below, stated plainly
   because it is the kind of thing that quietly becomes untrue.

TENANCY, HONESTLY. The graph carries no organisational property today: a live
inspection of every distinct node key found `user_ids` and `confidentiality` and
nothing else relevant. So the predicate is on a CONFIGURABLE property
(`GRAPH_TENANT_PROPERTY`, default `org_id`) in one of two modes:

  * lenient (default) — an unstamped node is treated as shared. The filter is
    therefore a no-op on today's data and starts biting the moment ingestion
    stamps the property. This is the only mode that both works now and is correct
    later.
  * strict (`GRAPH_TENANT_STRICT=true`) — an unstamped node belongs to nobody.
    Correct for a stamped graph; returns nothing on this one.

A caller with NO tenant never reaches either mode: the authorization boundary
denies the tool call before the handler runs (`authz` rule `no_tenant`). The
handler re-checks anyway, because a defence that exists in one place only is one
refactor from existing nowhere.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .registry import Tool, register

log = logging.getLogger("aganeti.orchestrator.graph_tools")

#: Node property carrying the tenant. Configuration, never request data — it is
#: interpolated into Cypher (a property key cannot be parameterised) and is
#: validated by queries.validate_property_name before it gets there.
TENANT_PROPERTY = os.getenv("GRAPH_TENANT_PROPERTY", "org_id")

#: When true, a node with no tenant property is EXCLUDED. See the module docstring.
TENANT_STRICT = os.getenv("GRAPH_TENANT_STRICT", "false").strip().lower() in ("1", "true", "yes")

MAX_LIMIT = 25
MAX_RELATIONSHIPS = 40
MAX_NAME_CHARS = 120


@dataclass(slots=True)
class GraphEntity:
    entity_id: str
    name: str
    labels: list[str] = field(default_factory=list)
    degree: int = 0

    def as_dict(self) -> dict:
        return {"entity_id": self.entity_id, "name": self.name,
                "labels": list(self.labels), "degree": self.degree}


@dataclass(slots=True)
class GraphRelationship:
    from_name: str
    rel_type: str
    to_name: str
    from_id: str = ""
    to_id: str = ""

    def as_dict(self) -> dict:
        return {"from_name": self.from_name, "rel_type": self.rel_type,
                "to_name": self.to_name, "from_id": self.from_id, "to_id": self.to_id}


@dataclass(slots=True)
class GraphSearchResult:
    """What was asked, what came back, and under which tenant scope."""

    query: str
    tenant_id: str
    entities: list[GraphEntity] = field(default_factory=list)
    relationships: list[GraphRelationship] = field(default_factory=list)
    tenant_property: str = TENANT_PROPERTY
    tenant_strict: bool = TENANT_STRICT
    available: bool = True

    @property
    def is_empty(self) -> bool:
        return not self.entities and not self.relationships

    def as_dict(self) -> dict:
        return {"query": self.query, "tenant_id": self.tenant_id,
                "entities": [e.as_dict() for e in self.entities],
                "relationships": [r.as_dict() for r in self.relationships],
                "tenant_property": self.tenant_property,
                "tenant_strict": self.tenant_strict,
                "available": self.available,
                "counts": {"entities": len(self.entities),
                           "relationships": len(self.relationships)}}


#: Words carrying no entity signal. Dropped before the token pass so "the Agentic
#: AI project" does not match every node whose name contains "project".
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "about",
    "what", "who", "which", "how", "is", "are", "was", "were", "does", "do",
    "project", "projects", "team", "teams", "entity", "entities", "thing", "things",
    "connected", "connection", "connections", "related", "relationship", "relationships",
    "find", "search", "show", "tell", "me", "my", "our", "please",
}


def _terms(query: str) -> list[str]:
    """Significant tokens for the loose resolution pass."""
    words = [w.strip(".,;:!?'\"()[]").lower() for w in (query or "").split()]
    return [w for w in words if len(w) >= 3 and w not in _STOPWORDS]


def _name_of(node: Any) -> str:
    props = getattr(node, "properties", None) or {}
    return str(props.get("canonical_name") or props.get("name") or
               getattr(node, "id", "") or "?")[:MAX_NAME_CHARS]


def _id_of(node: Any) -> str:
    props = getattr(node, "properties", None) or {}
    return str(props.get("id") or getattr(node, "id", "") or "")


def _search_sync(query: str, tenant_id: str, limit: int, expand: bool) -> GraphSearchResult:
    """The blocking half. The neo4j driver here is synchronous, so the async handler
    hands this to a thread rather than stalling the executor's event loop."""
    from backend.knowledge_graph.client import GraphUnavailable, is_enabled
    from backend.knowledge_graph.service import get_graph_service

    result = GraphSearchResult(query=query, tenant_id=tenant_id,
                               tenant_property=TENANT_PROPERTY, tenant_strict=TENANT_STRICT)
    if not is_enabled():
        result.available = False
        return result

    svc = get_graph_service()
    kw = {"tenant_id": tenant_id, "prop": TENANT_PROPERTY, "strict": TENANT_STRICT}

    # Three passes, tightest first. The loosening matters: a model asks for "the
    # Agentic AI project" when the node is called "Agentic AI", and a tool that
    # answers "nothing is recorded" to that reads as an authoritative absence —
    # which is a worse failure than a slightly loose match.
    try:
        rows = svc.tenant_find_entities(query, limit=limit, **kw)            # exact
        if not rows:
            rows = svc.tenant_search_entities(query, limit=limit, **kw)      # substring
        if not rows:
            rows = svc.tenant_search_entities_any_token(                     # any token
                _terms(query), limit=limit, **kw)
    except GraphUnavailable as e:
        log.warning("graph_search: graph unavailable: %s", e)
        result.available = False
        return result

    seen: set[str] = set()
    for row in rows:
        node = row.get("node")
        eid = _id_of(node)
        if not eid or eid in seen:
            continue
        seen.add(eid)
        result.entities.append(GraphEntity(
            entity_id=eid, name=_name_of(node),
            labels=[l for l in (row.get("labels") or []) if l != "Entity"],
            degree=int(row.get("degree") or 0)))

    if not expand or not result.entities:
        return result

    seed_ids = [e.entity_id for e in result.entities[:5]]
    try:
        hops = svc.tenant_expand_one_hop(seed_ids, visited=[], limit=MAX_RELATIONSHIPS, **kw)
    except GraphUnavailable as e:
        log.warning("graph_search: expansion unavailable: %s", e)
        return result

    by_id = {e.entity_id: e.name for e in result.entities}
    for hop in hops:
        node = hop.get("node")
        to_id, to_name = _id_of(node), _name_of(node)
        from_id = hop.get("from_id") or ""
        from_name = hop.get("from_name") or by_id.get(from_id, from_id)
        # Report the edge in its true direction rather than the traversal direction:
        # "A EMPLOYS B" and "B EMPLOYS A" are different facts.
        if hop.get("start_id") == to_id:
            from_name, to_name = to_name, from_name
            from_id, to_id = to_id, from_id
        result.relationships.append(GraphRelationship(
            from_name=from_name, rel_type=str(hop.get("rel_type") or "RELATED"),
            to_name=to_name, from_id=from_id, to_id=to_id))

    return result


async def graph_search(query: str, *, tenant_id: str, limit: int = 10,
                       expand: bool = True) -> GraphSearchResult:
    """Structured entry point. Used by the tool handler and available to callers
    (a future Knowledge Agent, the observability UI) that want the objects rather
    than the rendered text."""
    if not tenant_id:
        # Belt and braces: the boundary refuses this first. If that ever stops
        # being true, an unscoped graph read must still not happen.
        raise PermissionError("graph_search requires a tenant context")
    lim = max(1, min(int(limit or 10), MAX_LIMIT))
    return await asyncio.to_thread(_search_sync, query, tenant_id, lim, bool(expand))


def render_for_model(result: GraphSearchResult) -> str:
    if not result.available:
        return ("The knowledge graph is not available right now. Say so rather than "
                "guessing at relationships.")
    if result.is_empty:
        return (f"No entity matching '{result.query}' is in the knowledge graph. "
                f"Do not invent connections — say nothing is recorded, or try "
                f"knowledge_search for document evidence instead.")

    lines: list[str] = []
    if result.entities:
        lines.append("ENTITIES:")
        for e in result.entities:
            labels = f" [{', '.join(e.labels)}]" if e.labels else ""
            degree = f" ({e.degree} connection(s))" if e.degree else ""
            lines.append(f"- {e.name}{labels}{degree}")
    if result.relationships:
        lines.append("")
        lines.append("RELATIONSHIPS:")
        for r in result.relationships:
            lines.append(f"- {r.from_name} --{r.rel_type}--> {r.to_name}")
    lines.append("")
    lines.append(f"{len(result.entities)} entity(ies) and {len(result.relationships)} "
                 f"relationship(s) from the knowledge graph. State only what is listed here.")
    return "\n".join(lines)


async def _graph_search(ctx, query: str, limit: int = 10, expand: bool = True) -> str:
    """Executor handler. Read-only, no egress, so it runs inline in the loop."""
    tenant_id = (ctx.get("tenant_id") or "").strip()
    if not tenant_id:
        return ("error: graph search requires a tenant context and this call has none.")
    if not (query or "").strip():
        return "What should I look up in the knowledge graph? Give a name or a topic."
    try:
        result = await graph_search(query.strip(), tenant_id=tenant_id,
                                    limit=limit, expand=expand)
    except PermissionError as e:
        return f"error: {e}"
    except Exception as e:  # noqa: BLE001 — a graph outage is not a turn failure
        log.warning("graph_search failed: %s", e)
        return f"Knowledge graph search unavailable ({e})."
    return render_for_model(result)


_registered = False

_TOOLS = [
    Tool(
        "graph_search",
        "Look an entity up in the company knowledge graph and see what it is connected "
        "to — people, projects, documents, organisations, meetings and the relationships "
        "between them. Use for 'who works on X', 'what is X related to', 'how are X and Y "
        "connected'. For policy or document CONTENT use knowledge_search instead.",
        {"type": "object",
         "properties": {
             "query": {"type": "string",
                       "description": "an entity name or topic, in plain language"},
             "limit": {"type": "integer",
                       "description": f"max entities to return (1-{MAX_LIMIT}, default 10)"},
             "expand": {"type": "boolean",
                        "description": "also return relationships (default true)"},
         },
         "required": ["query"]},
        _graph_search,
        "knowledge.graph.read",
    ),
]

_NAMES = [t.name for t in _TOOLS]


def register_graph_tools() -> list[str]:
    """Idempotently register the graph tools."""
    global _registered
    if _registered:
        return _NAMES
    for t in _TOOLS:
        register(t)
    _registered = True
    return _NAMES
