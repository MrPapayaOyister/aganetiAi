"""Conversation history — the per-session JSON store, unchanged.

Kept as a provider so the bundle is a complete picture of a turn's context, but
note it is NOT consumed as a prompt SECTION: history goes into the `messages`
array as real turns, not into the system prompt. The prompt builder therefore
ignores this provider, and the caller reads `bundle.history` directly.
"""
from __future__ import annotations

import asyncio

from ..bundle import ContextItem
from .base import ContextProvider, ContextRequest


class HistoryProvider(ContextProvider):
    name = "history"
    timeout = 3.0

    def is_applicable(self, request: ContextRequest) -> bool:
        return bool((request.session_id or "").strip())

    async def collect(self, request: ContextRequest) -> list[ContextItem]:
        from memory.store import load_history

        rows = await asyncio.to_thread(load_history, request.session_id) or []
        out = []
        for i, m in enumerate(rows):
            content = (m.get("content") or "").strip()
            if not content:
                continue
            out.append(ContextItem(
                text=content, provider=self.name, source=request.session_id,
                timestamp=m.get("timestamp"),
                # `role` and `index` are what let the caller rebuild the exact
                # messages array without re-reading the store.
                metadata={"role": m.get("role", "user"), "index": i},
            ))
        return out
