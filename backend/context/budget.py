"""
ContextBudget — spend a finite token window deliberately instead of by top_k.

`top_k` is the wrong control: 5 calendar lines and 5 document chunks cost wildly
different numbers of tokens, so a fixed count either wastes the window or blows
it. The budget works in tokens, reserves what is non-negotiable first, and then
fills the remainder highest-ranked-first.

    window
      − system prompt reservation
      − history reservation
      − response reservation
      = available for retrieved context

Allocation is a per-provider CEILING, not a quota: a share exists so one chatty
provider cannot consume the whole window, but unclaimed share is redistributed
rather than wasted. Rank always wins inside a share.

Token counts are estimated (chars / 4), not tokenized. A real tokenizer would
mean importing the model's vocabulary into the context path for a number used
only to decide when to stop — the estimate is deliberately conservative instead.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .bundle import CHARS_PER_TOKEN, ContextItem

log = logging.getLogger("aganeti.context.budget")


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // CHARS_PER_TOKEN)


@dataclass(slots=True)
class BudgetPolicy:
    """Window and reservations. Every number is configurable."""

    window: int = 8192
    reserve_system: int = 1200        # identity, rules, tools — measured, not guessed
    reserve_history: int = 1500       # prior turns go in `messages`, not the prompt
    reserve_response: int = 1024      # what the model needs to answer in

    #: Per-provider ceilings as a FRACTION of the available context budget.
    #: They intentionally sum to > 1: these are caps, and unclaimed share is
    #: redistributed, so summing to 1 would leave the window under-used whenever
    #: any provider returns little.
    shares: dict[str, float] = field(default_factory=lambda: {
        "corporate": 0.35,
        "graph": 0.25,
        "memory": 0.20,
        "calendar": 0.15,
        "tasks": 0.15,
        "history": 0.10,
        "sql": 0.20,
    })
    default_share: float = 0.10

    #: Never truncate mid-item: a half sentence is worse than no sentence.
    allow_partial_items: bool = False

    @property
    def available(self) -> int:
        return max(0, self.window - self.reserve_system
                   - self.reserve_history - self.reserve_response)

    def ceiling_for(self, provider: str) -> int:
        return int(self.available * self.shares.get(provider, self.default_share))


@dataclass(slots=True)
class BudgetResult:
    items: "list[ContextItem]"
    tokens_used: int = 0
    tokens_available: int = 0
    dropped: int = 0
    allocation: dict[str, int] = field(default_factory=dict)   # provider → tokens spent
    contribution: dict[str, int] = field(default_factory=dict)  # provider → items kept
    took_ms: float = 0.0

    @property
    def utilization(self) -> float:
        return 0.0 if not self.tokens_available else self.tokens_used / self.tokens_available


class ContextBudget:
    """Fills the window highest-ranked-first, respecting per-provider ceilings."""

    def __init__(self, policy: Optional[BudgetPolicy] = None) -> None:
        self.policy = policy or BudgetPolicy()

    def apply(self, ranked_items: "list[ContextItem]") -> BudgetResult:
        """Take items in RANK ORDER until the window is full.

        Two passes so an unclaimed share is not wasted: the first respects each
        provider's ceiling, the second offers the leftover to whatever was
        skipped, still in rank order.
        """
        started = time.perf_counter()
        available = self.policy.available
        result = BudgetResult(items=[], tokens_available=available)

        spent_by_provider: dict[str, int] = {}
        total = 0
        deferred: list[ContextItem] = []

        for item in ranked_items:
            cost = estimate_tokens(item.text)
            # The owning provider pays. A fused item is charged to the provider
            # that ranked highest for it, which is the one that put it in front.
            owner = item.provider
            ceiling = self.policy.ceiling_for(owner)
            spent = spent_by_provider.get(owner, 0)

            if total + cost > available:
                deferred.append(item)
                continue
            if spent + cost > ceiling:
                deferred.append(item)          # over its share — retry in pass 2
                continue

            result.items.append(item)
            spent_by_provider[owner] = spent + cost
            result.contribution[owner] = result.contribution.get(owner, 0) + 1
            total += cost

        # Pass 2: redistribute whatever the ceilings left unspent.
        for item in deferred:
            cost = estimate_tokens(item.text)
            if total + cost > available:
                result.dropped += 1
                continue
            owner = item.provider
            result.items.append(item)
            spent_by_provider[owner] = spent_by_provider.get(owner, 0) + cost
            result.contribution[owner] = result.contribution.get(owner, 0) + 1
            total += cost

        # Rank order must survive pass 2, which appended out of order.
        result.items.sort(key=lambda i: -i.final_score)

        result.tokens_used = total
        result.allocation = spent_by_provider
        result.took_ms = (time.perf_counter() - started) * 1000

        if result.dropped:
            log.info("budget: %d item(s) dropped — %d/%d tokens used (%.0f%%)",
                     result.dropped, total, available, result.utilization * 100)
        return result
