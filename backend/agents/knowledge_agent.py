"""Knowledge Agent — the only retrieval path. (POC-3)

It calls `backend.orchestrator.knowledge.knowledge_search` and nothing else. It
does NOT import qdrant_client, does not import the Neo4j driver, and does not
reach into `backend.ingest` or `backend.knowledge_graph`. That is a structural
property, asserted by a test that inspects this module's imports rather than
trusting the docstring.

The reason is not tidiness. `knowledge_search` is where the tenant predicate is
applied for BOTH stores (POC-2). An agent that queried Qdrant directly would be
correctly scoped only by accident, and the day someone added a second direct
call the isolation would regress silently — with no failing test, because the
tenancy tests all sit behind knowledge_search.

PROVENANCE IS CARRIED, NOT REBUILT. `KnowledgeCitation` already carries source,
document_id, entity_id, graph_edge, scores, corroboration and the raw Evidence
records. This agent reshapes none of it; it passes the dicts through and adds
only the tenant and the enforcement claim. A second citation format would be a
second thing to keep in sync with the retriever.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .contract import AgentRequest, AgentResult, AgentSpec, AgentStatus, BaseAgent

log = logging.getLogger("aganeti.agents.knowledge")

#: Below this, the evidence is treated as insufficient rather than thin. Zero is
#: the honest threshold for a POC: "we found nothing" is the only claim that can
#: be made without judgement, and any higher bar would be a relevance heuristic
#: invented here rather than measured.
MIN_CITATIONS = 1


@dataclass
class Evidence:
    """What retrieval produced for one step."""

    query: str
    tenant_id: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    #: Which providers applied a real tenant predicate to THIS result. Carried
    #: from knowledge_search unchanged — a populated tenant is not an enforced
    #: one, and the verification step is entitled to know which it got.
    tenant_enforced_by: list[str] = field(default_factory=list)
    providers_used: list[str] = field(default_factory=list)

    @property
    def document_citations(self) -> list[dict]:
        return [c for c in self.citations if c.get("provider") == "corporate"]

    @property
    def graph_citations(self) -> list[dict]:
        return [c for c in self.citations if c.get("provider") == "graph"]

    @property
    def is_sufficient(self) -> bool:
        return len(self.citations) >= MIN_CITATIONS

    def as_dict(self) -> dict[str, Any]:
        return {"query": self.query, "tenant_id": self.tenant_id,
                "citations": list(self.citations),
                "tenant_enforced_by": list(self.tenant_enforced_by),
                "providers_used": list(self.providers_used),
                "counts": {"total": len(self.citations),
                           "documents": len(self.document_citations),
                           "graph": len(self.graph_citations)}}


def render_for_prompt(evidence: Evidence, max_items: int = 10,
                      max_chars: int = 500) -> str:
    """Evidence as numbered, cited text for a model to read.

    Markers match knowledge_search's own convention ([D#] documents, [G#] graph)
    so a citation the model emits can be traced back to the structured record at
    the same index — which is what makes the answer's citations checkable rather
    than decorative.
    """
    if not evidence.citations:
        return "NO EVIDENCE FOUND."
    lines: list[str] = []
    for n, c in enumerate(evidence.document_citations[:max_items], 1):
        text = " ".join((c.get("text") or "").split())[:max_chars]
        lines.append(f"[D{n}] ({c.get('source') or c.get('document_id') or '?'}) {text}")
    for n, c in enumerate(evidence.graph_citations[:max_items], 1):
        text = " ".join((c.get("text") or "").split())[:max_chars]
        lines.append(f"[G{n}] ({c.get('kind') or 'graph'}) {text}")
    return "\n".join(lines)


class KnowledgeAgent(BaseAgent):
    spec = AgentSpec(
        name="knowledge",
        description="Retrieves cited evidence from the corporate corpus and knowledge graph.",
        capabilities=("retrieval", "citations", "graph"),
    )

    async def _run(self, request: AgentRequest) -> AgentResult:
        # Imported here, and this is the ONLY retrieval import in the module.
        from backend.orchestrator.knowledge import knowledge_search

        result = await knowledge_search(
            request.task,
            user_id=request.user_id,
            session_id=request.session_id,
            # The tenant reaches Qdrant and Neo4j through this argument and no
            # other. POC-2 owns what happens after it.
            tenant_id=request.tenant_id,
        )

        evidence = Evidence(
            query=request.task,
            tenant_id=result.tenant_id,
            citations=[c.as_dict() for c in result.citations],
            tenant_enforced_by=list(result.tenant_enforced_by),
            providers_used=list(result.providers_used),
        )

        if not evidence.is_sufficient:
            # A correct run that found nothing. INSUFFICIENT, not FAILED — the
            # difference is what lets verification refuse honestly instead of
            # reporting a broken retriever.
            return AgentResult(agent=self.spec.name, status=AgentStatus.INSUFFICIENT,
                               output=evidence, evidence=[])

        return AgentResult(agent=self.spec.name, status=AgentStatus.OK,
                           output=evidence, evidence=evidence.citations)
