"""knowledge_search — the frozen GraphRAG pipeline, reachable from the executor. (POC-1)

The authoritative LangGraph runtime could not reach GraphRAG. Its only knowledge
tool, `search_documents`, calls `ingest.search_corporate` — plain Qdrant vector
search, no graph, no fusion. The frozen hybrid pipeline (`build_ranked_context`)
was wired only into the legacy chat path, the observability probes and the
evaluation harness.

This module closes that gap and does nothing else. It is a WRAPPER:

    knowledge_search  →  backend.context.build_ranked_context  →  Qdrant + Neo4j

No retrieval logic is implemented here. No GraphRAG parameter is passed, read or
overridden — the six frozen values (resolver 0.82, depth 1, graph_top_k 8,
hop_decay 0.55, fusion 0.60, max_nodes 100) are already the defaults of the
production provider registry, so the only way to preserve them is to pass
nothing and let the pipeline configure itself exactly as it does in production.
Passing them explicitly is how a freeze gets accidentally re-tuned, so the call
below deliberately supplies neither depth, nor top_k, nor any threshold.

`search_documents` is untouched and still registered: this tool is additive.

Two consumers, one retrieval:

  * the executor gets `_knowledge_search(ctx, query) -> str` — cited text the
    model can read, capped for the tool-call protocol;
  * a future Knowledge Agent and the citation UI (POC-13, POC-23) get
    `knowledge_search(...) -> KnowledgeSearchResult` — the full structured
    result, built from the EXISTING ContextItem/Evidence provenance types
    rather than a new provenance system.

TENANCY SEAM (POC-2). `ctx` already carries `tenant_id` (graph.py:85-86); today
it is the empty string. This tool reads it, records it on the result, and passes
it to no filter yet — because no retrieval surface accepts a tenant filter yet.
When POC-2 lands, the tenant is threaded at the retrieval call below and the
tool's input schema, output shape and callers do not change.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Optional

from .registry import Tool, register

log = logging.getLogger("aganeti.orchestrator.knowledge")

#: The two knowledge sources this tool federates: the Qdrant corpus and the
#: Neo4j graph. `only=` is an existing first-class narrowing argument of
#: build_context (builder.py:98-100) that SELECTS PROVIDERS — it is not one of
#: the frozen retrieval parameters and changes no threshold, depth or top_k.
#:
#: The other registered providers (memory, calendar, tasks, sql, history) are
#: personal/session context, not knowledge. Including them would put a user's
#: calendar into the answer to a policy question and make a citation list
#: meaningless.
KNOWLEDGE_PROVIDERS = ("corporate", "graph")

#: Providers that apply a real tenant predicate when a tenant is supplied.
#:   "corporate" — Qdrant, filtered on the `org_id` payload key
#:                 (backend/ingest.py::tenant_clause).
#:   "graph"     — Neo4j, filtered on the node property GRAPH_TENANT_PROPERTY
#:                 (backend/knowledge_graph/retrieval/api.py::_tenant_visible).
#:
#: A name belongs here ONLY when a predicate stands behind it. Both do now; when
#: only Qdrant did, "graph" was deliberately absent rather than aspirational,
#: because a caller that trusts this list has no other way to tell.
#:
#: NOTE ON WHAT THE GRAPH PREDICATE CURRENTLY BUYS: it is enforced, but no node in
#: the live graph carries a tenant yet, so in lenient mode every node is treated
#: as shared and nothing is excluded. The mechanism is real and the data is not
#: there — see the migration reported at the end of POC-2.
TENANT_ENFORCING_PROVIDERS = ("corporate", "graph")

#: Rendering cap only — how many citations are written into the model-facing
#: string. Retrieval itself is bounded by the frozen pipeline, never by this.
MAX_RENDERED_CITATIONS = 12

#: Per-citation excerpt cap in the rendered string. The executor truncates tool
#: output at MAX_TOOL_OUTPUT (6000); trimming per citation keeps a long first
#: hit from starving the rest of the evidence.
MAX_EXCERPT_CHARS = 600


def _evidence_as_dicts(evidence: Any) -> list[dict]:
    """Serialise the existing Evidence dataclasses without importing them.

    Kept defensive: `ContextItem.evidence` is typed as a bare list, and an item
    that never went through fusion carries none at all.
    """
    out: list[dict] = []
    for ev in (evidence or []):
        if is_dataclass(ev) and not isinstance(ev, type):
            out.append(asdict(ev))
        elif isinstance(ev, dict):
            out.append(dict(ev))
    return out


@dataclass(slots=True)
class KnowledgeCitation:
    """One retrieved, cited piece of evidence.

    A flattened projection of ContextItem — the fields a Knowledge Agent or a
    citation UI needs, without either of them having to import the context
    package or know how fusion works.
    """

    text: str
    provider: str                       # "corporate" (Qdrant) | "graph" (Neo4j)
    kind: str                           # "document" | "entity" | "relationship"
    source: str = ""                    # file name, or the graph's source ids
    score: Optional[float] = None       # provider-native; NOT comparable across providers
    final_score: float = 0.0            # after fusion + ranking; comparable
    normalized_score: float = 0.0
    corroboration: int = 1              # distinct sources behind it
    providers: list[str] = field(default_factory=list)   # every provider attesting it
    document_id: Optional[str] = None
    entity_id: Optional[str] = None
    graph_edge: Optional[str] = None    # "start -REL-> end"
    timestamp: Optional[str] = None
    evidence: list[dict] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_item(cls, item: Any) -> "KnowledgeCitation":
        meta = dict(getattr(item, "metadata", None) or {})
        provider = getattr(item, "provider", "") or ""

        # The graph provider labels its own items ("relationship" | "entity",
        # providers/graph.py:86,101). Anything else here is a corpus chunk.
        kind = meta.get("kind") or ("document" if provider == "corporate" else "unknown")

        graph_edge = None
        if kind == "relationship" and meta.get("start_id") and meta.get("end_id"):
            graph_edge = f"{meta['start_id']} -{meta.get('rel_type', 'REL')}-> {meta['end_id']}"

        return cls(
            text=getattr(item, "text", "") or "",
            provider=provider,
            kind=kind,
            source=getattr(item, "source", "") or "",
            score=getattr(item, "score", None),
            final_score=float(getattr(item, "final_score", 0.0) or 0.0),
            normalized_score=float(getattr(item, "normalized_score", 0.0) or 0.0),
            corroboration=int(getattr(item, "corroboration", 1) or 1),
            providers=list(getattr(item, "providers", None) or ([provider] if provider else [])),
            document_id=meta.get("document_id"),
            entity_id=meta.get("entity_id"),
            graph_edge=graph_edge,
            timestamp=getattr(item, "timestamp", None),
            evidence=_evidence_as_dicts(getattr(item, "evidence", None)),
            metadata=meta,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class KnowledgeSearchResult:
    """The structured result: what was asked, what was found, and where from."""

    query: str
    user_id: str
    citations: list[KnowledgeCitation] = field(default_factory=list)
    tenant_id: str = ""                 # organizations.id, "" when unresolved
    providers_used: list[str] = field(default_factory=list)
    provider_status: dict[str, str] = field(default_factory=dict)
    fusion: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    #: Which of the providers actually applied a tenant predicate to THIS result.
    #: Stated per-result rather than assumed, because the two knowledge stores are
    #: at different stages: Qdrant filters on `org_id`, while the graph provider
    #: calls GraphRetrievalAPI.retrieve(), which takes no tenant argument at all.
    #: A caller that needs a hard boundary must check this rather than trust
    #: `tenant_id` being populated — a carried tenant is not an enforced one, and
    #: conflating the two is how a filter comes to look safer than it is.
    tenant_enforced_by: list[str] = field(default_factory=list)

    @property
    def document_citations(self) -> list[KnowledgeCitation]:
        """Qdrant corpus evidence."""
        return [c for c in self.citations if c.provider == "corporate"]

    @property
    def graph_citations(self) -> list[KnowledgeCitation]:
        """Neo4j entity/relationship evidence."""
        return [c for c in self.citations if c.provider == "graph"]

    @property
    def is_empty(self) -> bool:
        return not self.citations

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "providers_used": list(self.providers_used),
            "tenant_enforced_by": list(self.tenant_enforced_by),
            "provider_status": dict(self.provider_status),
            "citations": [c.as_dict() for c in self.citations],
            "counts": {
                "total": len(self.citations),
                "documents": len(self.document_citations),
                "graph": len(self.graph_citations),
            },
            "fusion": dict(self.fusion),
            "stats": dict(self.stats),
        }


async def knowledge_search(query: str, *, user_id: str, session_id: str = "",
                           tenant_id: str = "") -> KnowledgeSearchResult:
    """Run the frozen GraphRAG pipeline and return structured, cited evidence.

    The single retrieval call. `build_ranked_context` is invoked with the
    caller's identity and the query, and with NO retrieval configuration — the
    production provider registry already holds the frozen values, so supplying
    any of them here is the one way this wrapper could change GraphRAG
    behaviour. It does not.

    `user_id` is passed through unchanged and reaches
    `search_corporate(query, top_k, user_id)` via ContextRequest, which is the
    existing scoping semantics (caller's own documents + the shared org corpus,
    never another user's).
    """
    from backend.context import build_ranked_context

    ranked = await build_ranked_context(
        user_id, query, session_id,
        only=KNOWLEDGE_PROVIDERS,
        # The tenant boundary. Threaded here and nowhere else, exactly as the
        # POC-1 seam promised. It reaches Qdrant as an org predicate; the graph
        # provider currently ignores it — see the honesty note on the result.
        tenant_id=tenant_id,
    )

    citations = [KnowledgeCitation.from_item(i) for i in (ranked.items or [])]

    try:
        stats = ranked.stats.as_dict()
        provider_status = {n: s.get("status", "") for n, s in (stats.get("providers") or {}).items()}
    except Exception:  # noqa: BLE001 — observability must never fail a lookup
        stats, provider_status = {}, {}

    try:
        fusion = ranked.fusion.as_dict()
    except Exception:  # noqa: BLE001
        fusion = {}

    return KnowledgeSearchResult(
        query=query,
        user_id=user_id,
        tenant_id=tenant_id,
        citations=citations,
        providers_used=list(KNOWLEDGE_PROVIDERS),
        # WHAT THIS FIELD MEANS — and what it does NOT.
        #
        # It names the providers where a tenant predicate RAN. It is not, and
        # must not be read as, an assertion that the data was isolated.
        #
        # Both stores default to LENIENT mode (QDRANT_TENANT_STRICT and
        # GRAPH_TENANT_STRICT, both off), and lenient means an UNSTAMPED record
        # is treated as shared and returned to everyone. On a corpus that is
        # mostly unstamped — which is the state this was written in — the
        # predicate is close to a no-op. It runs, it is honest that it ran, and
        # it filters almost nothing.
        #
        # So `tenant_enforced_by: ["graph"]` in a log or an eval means "a graph
        # tenant predicate was applied", NOT "no other tenant's entities could
        # have been returned". Reading it as the latter is the mistake this
        # comment exists to prevent, because the field will look like a guarantee
        # to anyone who meets it six months from now without this context.
        #
        # It becomes a guarantee only when BOTH hold for a store:
        #   1. every record is stamped (writer stamps at ingestion — see
        #      knowledge_graph/builder.py — AND the backfill has run to zero
        #      unstamped), and
        #   2. that store's *_TENANT_STRICT flag is on, so unstamped means
        #      nobody's rather than everybody's.
        # Until then this is provenance, not proof.
        #
        # Enforced only where a tenant was actually supplied AND the provider has
        # a predicate. No tenant means no filter anywhere, and saying so is the
        # point.
        tenant_enforced_by=([p for p in KNOWLEDGE_PROVIDERS
                             if p in TENANT_ENFORCING_PROVIDERS] if tenant_id else []),
        provider_status=provider_status,
        fusion=fusion,
        stats=stats,
    )


def render_for_model(result: KnowledgeSearchResult) -> str:
    """The model-facing rendering: cited excerpts, documents before graph.

    Markers are stable and greppable — [D1] for a corpus chunk, [G1] for graph
    evidence — so a later citation UI can correlate what the model cited with
    the structured citation at the same index.
    """
    if result.is_empty:
        return ("No matching knowledge found in the corpus or the knowledge graph "
                "for that query. Do not guess — say what is missing, or ask for the "
                "document to be uploaded.")

    lines: list[str] = []
    docs = result.document_citations[:MAX_RENDERED_CITATIONS]
    graph = result.graph_citations[:MAX_RENDERED_CITATIONS]

    if docs:
        lines.append("DOCUMENT EVIDENCE (corpus):")
        for n, c in enumerate(docs, 1):
            excerpt = " ".join((c.text or "").split())[:MAX_EXCERPT_CHARS]
            src = c.source or c.document_id or "unknown source"
            corr = f", corroborated x{c.corroboration}" if c.corroboration > 1 else ""
            lines.append(f"[D{n}] ({src}{corr}) {excerpt}")

    if graph:
        if lines:
            lines.append("")
        lines.append("GRAPH EVIDENCE (knowledge graph):")
        for n, c in enumerate(graph, 1):
            excerpt = " ".join((c.text or "").split())[:MAX_EXCERPT_CHARS]
            src = c.source or "graph"
            corr = f", corroborated x{c.corroboration}" if c.corroboration > 1 else ""
            lines.append(f"[G{n}] ({c.kind}, from {src}{corr}) {excerpt}")

    lines.append("")
    lines.append(f"Cite these markers ([D#]/[G#]) for every claim. "
                 f"{len(result.document_citations)} document and "
                 f"{len(result.graph_citations)} graph item(s) retrieved. "
                 f"Say so plainly if they do not answer the question.")
    return "\n".join(lines)


async def _knowledge_search(ctx, query: str) -> str:
    """Executor handler. Read-only, no egress, so it runs inline in the loop."""
    try:
        result = await knowledge_search(
            query,
            user_id=ctx.get("user_id") or "",
            session_id=ctx.get("session_id") or "",
            tenant_id=ctx.get("tenant_id") or "",
        )
    except Exception as e:  # noqa: BLE001 — a retrieval outage is not a turn failure
        log.warning("knowledge_search failed: %s", e)
        return f"Knowledge search unavailable ({e})."
    return render_for_model(result)


register(Tool(
    "knowledge_search",
    "Search company knowledge: the document corpus AND the knowledge graph, fused and "
    "ranked, returning cited evidence ([D#] documents, [G#] graph). Use for policies, "
    "procedures, requirements, entities and how things relate. Prefer this over "
    "search_documents when the question needs relationships, corroboration or citations.",
    {"type": "object",
     "properties": {"query": {"type": "string",
                              "description": "the question or topic to look up"}},
     "required": ["query"]},
    _knowledge_search,
    "documents.read",
))
