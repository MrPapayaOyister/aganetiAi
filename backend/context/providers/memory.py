"""Long-term memory — the existing rewrite→search path, algorithm untouched.

Reproduces `main.py::_memory_block` exactly:

    recent  = get_recent_history(session_id, n=3)      # NOT user_id
    rewrite = rewrite_query(recent, message)           # synchronous LLM call
    facts   = search_memory(session_id, rewrite, k=3)  # NOT user_id

Both lookups are keyed by **session_id**, not user_id. That is almost certainly
a defect — a new thread starts with empty long-term memory — but fixing it would
change what the model sees, and this phase must not. It is preserved verbatim
and flagged in `metadata["keyed_by"]` so the next phase can find it.

The whole block runs in one worker thread because `rewrite_query` makes a
blocking httpx call and `search_memory` does a blocking embed + Qdrant query.
"""
from __future__ import annotations

import asyncio

from ..bundle import ContextItem
from .base import ContextProvider, ContextRequest


class MemoryProvider(ContextProvider):
    name = "memory"
    # Generous: this path includes a synchronous LLM rewrite. The legacy code had
    # no timeout at all, so anything finite is stricter than before — but it must
    # be high enough that a normal turn is never cut short.
    timeout = 12.0

    def __init__(self, top_k: int = 3, rewrite: bool = True) -> None:
        self.top_k = top_k
        self.rewrite = rewrite

    def is_applicable(self, request: ContextRequest) -> bool:
        return bool((request.session_id or "").strip())

    async def collect(self, request: ContextRequest) -> list[ContextItem]:
        from memory.long_term import search_memory
        from memory.query_rewriter import rewrite_query
        from backend.main import get_recent_history

        session_id = request.session_id

        def _block() -> tuple[str, str]:
            recent_msgs = get_recent_history(session_id, n=3)
            search_query = (rewrite_query(recent_msgs, request.message)
                            if self.rewrite else request.message)
            return search_memory(session_id, search_query, top_k=self.top_k), search_query

        raw, used_query = await asyncio.to_thread(_block)
        text = (raw or "").strip()
        if not text:
            return []
        # search_memory returns ONE pre-joined "- fact (from date)" block, already
        # ranked by its own hybrid score. Splitting it into per-fact items would
        # re-rank it; keep the block whole so the prompt is byte-identical.
        return [ContextItem(
            text=text,
            provider=self.name,
            source=f"user_memory_{session_id}",
            metadata={"keyed_by": "session_id", "rewritten_query": used_query,
                      "top_k": self.top_k},
        )]
