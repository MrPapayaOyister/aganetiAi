"""SQL — intentionally empty.

The audit found no SQL source participating in the chat context path. The Azure
SQL data source is reached only by the analytics/chart lanes, which run their
own agent rather than the assistant's context assembly.

This provider exists so the slot is defined and a future SQLProvider is a body,
not a new integration. It is DISABLED, so the builder records it as `skipped`
and never calls it. Inventing behaviour here would be exactly the change this
phase forbids.
"""
from __future__ import annotations

from ..bundle import ContextItem
from .base import ContextProvider, ContextRequest


class SQLProvider(ContextProvider):
    name = "sql"
    timeout = 5.0
    enabled = False                    # nothing to query on the chat path today

    async def collect(self, request: ContextRequest) -> list[ContextItem]:
        return []
