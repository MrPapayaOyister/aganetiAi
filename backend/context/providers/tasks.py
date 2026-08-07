"""Tasks — wraps `tasks.store.get_pending_summary` (SQLite), unchanged."""
from __future__ import annotations

import asyncio

from ..bundle import ContextItem
from .base import ContextProvider, ContextRequest


class TaskProvider(ContextProvider):
    name = "tasks"
    timeout = 4.0

    async def collect(self, request: ContextRequest) -> list[ContextItem]:
        from tasks.store import get_pending_summary

        text = (await asyncio.to_thread(get_pending_summary, request.user_id) or "").strip()
        if not text:
            return []
        return [ContextItem(text=text, provider=self.name, source="tasks.db")]
