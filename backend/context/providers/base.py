"""
The provider contract.

A provider knows how to fetch ONE kind of context and nothing else. It never
sees a prompt, never formats a section, never inspects another provider's
output. That is what makes adding an EmailProvider or a SlackProvider a
registration rather than an edit to the builder.

Two rules keep the builder honest:

  * `collect()` returns ContextItems, already sorted by whatever ranking that
    source natively has. Cross-source ranking is Phase 4 and lives nowhere yet.
  * `collect()` may raise. The builder isolates and times every provider, so a
    failure in one is recorded as a stat and the rest of the turn is unaffected.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional

from ..bundle import ContextItem


@dataclass(slots=True)
class ContextRequest:
    """Everything a provider is allowed to know about the turn.

    Deliberately narrow. A provider that needs something not here is a provider
    that is reaching into the request handler, which is the coupling this
    package exists to remove.
    """

    user_id: str
    message: str
    session_id: str = ""
    limit: Optional[int] = None
    #: organizations.id — the isolation boundary, when the caller resolved one.
    #: "" means the caller could not be placed in a tenant; a provider must then
    #: apply NO tenant predicate rather than inventing one. An empty tenant is the
    #: one value that must never be turned into a filter, because `org_id == ""`
    #: matches nothing and silently returns an empty context instead of failing.
    tenant_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ContextProvider(abc.ABC):
    """Base class for every context source."""

    #: Bundle slot this provider writes into. Must be unique across the registry.
    name: str = "provider"

    #: Seconds before the builder abandons this provider for the turn. Chosen per
    #: provider because the cost profiles differ by an order of magnitude — a
    #: SQLite task read is sub-millisecond; memory recall makes an LLM call.
    timeout: float = 5.0

    #: A disabled provider is skipped without being called at all. This is how a
    #: provider can exist in the architecture before it is allowed to change
    #: behaviour (see GraphProvider).
    enabled: bool = True

    @abc.abstractmethod
    async def collect(self, request: ContextRequest) -> list[ContextItem]:
        """Fetch context for this turn. May raise; the builder isolates it."""

    def is_applicable(self, request: ContextRequest) -> bool:
        """Cheap pre-check. Returning False skips the provider and records it as
        `skipped` rather than as an error — a provider with no input to work on
        is not a failure."""
        return True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r} timeout={self.timeout}>"
