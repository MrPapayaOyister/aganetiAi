"""Corporate knowledge — the existing Qdrant RAG, unchanged.

Wraps `backend.main.retrieve_corporate_context` semantics exactly:
`search_corporate(query, top_k=1, owner=user_id)` and the sentinel string it
returns when nothing matches. `top_k` stays 1 because raising it would change
what reaches the prompt, and this phase changes architecture only.
"""
from __future__ import annotations

import asyncio

from ..bundle import ContextItem
from .base import ContextProvider, ContextRequest

# The exact sentinel the legacy helper returns on a miss. The prompt builder
# suppresses the section on this value, so it must round-trip byte-identically.
NO_RESULT = "No specific corporate guidelines found."


class CorporateKnowledgeProvider(ContextProvider):
    name = "corporate"
    timeout = 6.0

    def __init__(self, top_k: int = 1) -> None:
        # 1 == the legacy `retrieve_corporate_context` depth. Do not raise it here:
        # more chunks is a retrieval change, not a refactor.
        self.top_k = top_k

    def is_applicable(self, request: ContextRequest) -> bool:
        return bool((request.message or "").strip())

    async def collect(self, request: ContextRequest) -> list[ContextItem]:
        from backend.ingest import search_corporate

        # search_corporate embeds and queries Qdrant synchronously.
        # tenant_id is passed straight through: `None` when the request carries no
        # tenant, which makes the org predicate absent rather than empty. See
        # search_corporate — an empty tenant would match nothing.
        hits = await asyncio.to_thread(
            search_corporate, request.message, self.top_k, request.user_id,
            None, None, (request.tenant_id or None))
        out = []
        for h in hits or []:
            text = (h.get("text") or "").strip()
            if not text:
                continue
            out.append(ContextItem(
                text=text,
                provider=self.name,
                source=h.get("source") or "",
                score=h.get("score"),
                timestamp=(str(h["timestamp"]) if h.get("timestamp") else None),
                metadata={
                    "chunk_index": h.get("chunk_index"),
                    "document_id": h.get("document_id"),
                    "source_type": h.get("source_type"),
                    "sensitivity": h.get("sensitivity"),
                    "user_id": h.get("user_id"),
                },
            ))
        # search_corporate already returns Qdrant's descending-score order.
        return out
