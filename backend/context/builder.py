"""
ContextBuilder — the ONLY place context is gathered before an LLM call.

    build_context(user_id, message, session_id) -> ContextBundle

Every provider runs CONCURRENTLY under `asyncio.gather`, each with its own
timeout and its own exception boundary. That is the substantive change: the
legacy path ran corporate → calendar → tasks → memory strictly in sequence, so
the turn paid the sum of four latencies and one slow source delayed everything
after it. Now the turn pays the maximum, and a provider that fails, times out or
is disabled costs the others nothing.

What this does NOT do, on purpose:
  * no cross-provider ranking or merging — each provider sorts its own items
  * no de-duplication between sources
  * no prompt formatting — that is prompt.py's job, and providers never see it
Those are Phase 4.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable, Optional

from .budget import BudgetPolicy, ContextBudget
from .bundle import (
    BundleStats, ContextBundle, ContextItem, FusionStats, ProviderStat,
    RankedContextBundle, now_iso,
)
from .compression import ContextCompressor
from .fusion import HybridContextFusion
from .providers import ContextProvider, ContextRequest
from .providers import registry as provider_registry
from .ranking import RankingWeights, WeightedRanker

log = logging.getLogger("aganeti.context.builder")

# Ceiling for the WHOLE gather. Individual provider timeouts should always be
# lower; this is the backstop against a provider that ignores cancellation.
TOTAL_TIMEOUT = 15.0


class ContextBuilder:
    """Runs registered providers concurrently and assembles a ContextBundle."""

    def __init__(self, providers: Optional[Iterable[ContextProvider]] = None,
                 *, graph: Optional[bool] = None,
                 total_timeout: float = TOTAL_TIMEOUT,
                 fusion: Optional[HybridContextFusion] = None,
                 ranker: Optional[WeightedRanker] = None,
                 compressor: Optional[ContextCompressor] = None,
                 budget: Optional[ContextBudget] = None,
                 weights: Optional[RankingWeights] = None,
                 budget_policy: Optional[BudgetPolicy] = None) -> None:
        """`providers=None` uses the global registry (the normal case).

        `graph=True/False` overrides the GraphProvider for this builder without
        touching the deployment-wide env flag.

        The four hybrid stages are injectable so each can be tested — and
        disabled — in isolation."""
        self._providers = list(providers) if providers is not None else None
        self._graph_override = graph
        self.total_timeout = total_timeout
        self.fusion = fusion or HybridContextFusion()
        self.ranker = ranker or WeightedRanker(weights=weights)
        self.compressor = compressor or ContextCompressor()
        self.budget = budget or ContextBudget(budget_policy)

    def providers(self) -> "list[ContextProvider]":
        provs = (self._providers if self._providers is not None
                 else provider_registry.all_providers())
        if self._graph_override is not None:
            for p in provs:
                if p.name == "graph":
                    p.enabled = self._graph_override
        return provs

    # ── the entry point ──────────────────────────────────────────────────────

    async def build_context(self, user_id: str, message: str,
                            session_id: str = "", *,
                            only: Optional[Iterable[str]] = None,
                            metadata: Optional[dict] = None,
                            tenant_id: str = "") -> ContextBundle:
        """Gather every applicable provider's context for one turn.

        Never raises. A provider that fails is recorded in `bundle.stats` and
        contributes nothing; the bundle is always usable.

        `tenant_id` is carried to every provider on the request. Providers that can
        enforce it do; the ones that cannot yet (graph) ignore it, so adding it here
        is not itself a guarantee — see the per-store notes in each provider.
        """
        started = time.perf_counter()
        request = ContextRequest(user_id=user_id, message=message,
                                 session_id=session_id, tenant_id=str(tenant_id or ""),
                                 metadata=dict(metadata or {}))
        bundle = ContextBundle()
        bundle.metadata = {"user_id": user_id, "session_id": session_id,
                           "built_at": now_iso(), **(metadata or {})}

        wanted = set(only) if only is not None else None
        selected = [p for p in self.providers()
                    if wanted is None or p.name in wanted]

        tasks = [self._run_one(p, request) for p in selected]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.total_timeout)
        except asyncio.TimeoutError:
            # The per-provider timeouts should make this unreachable; if it is
            # reached, an empty bundle beats a hung request.
            log.error("context: total timeout after %.1fs — returning partial bundle",
                      self.total_timeout)
            results = []

        stats = BundleStats()
        for outcome in results:
            if isinstance(outcome, BaseException):     # gather itself misbehaved
                log.warning("context: provider task raised: %s", outcome)
                continue
            stat, items = outcome
            stats.providers[stat.provider] = stat
            if items:
                bundle.put(stat.provider, items)

        stats.total_ms = (time.perf_counter() - started) * 1000
        stats.total_items = sum(s.items for s in stats.providers.values())
        stats.approx_tokens = sum(s.approx_tokens for s in stats.providers.values())
        bundle.stats = stats

        if stats.failed or stats.timed_out:
            log.warning("context: built in %.0fms — %d items, failed=%s timed_out=%s",
                        stats.total_ms, stats.total_items, stats.failed, stats.timed_out)
        else:
            log.debug("context: built in %.0fms — %d items from %d provider(s)",
                      stats.total_ms, stats.total_items, len(stats.providers))
        return bundle

    # ── isolation ────────────────────────────────────────────────────────────

    async def _run_one(self, provider: ContextProvider,
                       request: ContextRequest) -> "tuple[ProviderStat, list[ContextItem]]":
        """Run one provider under its own timeout and exception boundary.

        Returns rather than raises, so `gather` never short-circuits and one
        provider's failure can never affect another's result.
        """
        stat = ProviderStat(provider=provider.name)

        if not provider.enabled:
            stat.skipped = True
            return stat, []
        try:
            if not provider.is_applicable(request):
                stat.skipped = True
                return stat, []
        except Exception as e:  # noqa: BLE001 — a bad pre-check is not a turn failure
            stat.ok, stat.error = False, f"is_applicable: {e}"
            return stat, []

        started = time.perf_counter()
        try:
            items = await asyncio.wait_for(provider.collect(request),
                                           timeout=provider.timeout)
        except asyncio.TimeoutError:
            stat.latency_ms = (time.perf_counter() - started) * 1000
            stat.ok, stat.timed_out = False, True
            stat.error = f"timed out after {provider.timeout}s"
            log.warning("context: provider %s timed out after %.1fs",
                        provider.name, provider.timeout)
            return stat, []
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — isolation is the whole point
            stat.latency_ms = (time.perf_counter() - started) * 1000
            stat.ok, stat.error = False, f"{type(e).__name__}: {e}"
            log.warning("context: provider %s failed: %s", provider.name, e)
            return stat, []

        items = [i for i in (items or []) if not i.is_empty]
        stat.latency_ms = (time.perf_counter() - started) * 1000
        stat.items = len(items)
        stat.approx_tokens = sum(i.approx_tokens for i in items)
        return stat, items


    # ── the hybrid pipeline (phase 4) ────────────────────────────────────────

    async def build_ranked_context(self, user_id: str, message: str,
                                   session_id: str = "", *,
                                   only: Optional[Iterable[str]] = None,
                                   metadata: Optional[dict] = None,
                                   compress: bool = True,
                                   apply_budget: bool = True,
                                   tenant_id: str = "") -> RankedContextBundle:
        """Retrieve → fuse → rank → compress → budget.

        Returns a RankedContextBundle: one ranked list of fused items, PLUS the
        per-provider grouping the existing PromptBuilder still reads. Ordering is
        applied to both, so a section shows its provider's best content first.

        Every stage is skippable and none can fail the turn — a fusion error
        falls back to the unfused bundle rather than losing the context.
        """
        bundle = await self.build_context(user_id, message, session_id,
                                          only=only, metadata=metadata,
                                          tenant_id=tenant_id)
        ranked = RankedContextBundle(bundle=bundle)
        stats = FusionStats()

        try:
            items, fusion_stats = self.fusion.fuse(bundle)
            stats = fusion_stats
        except Exception as e:  # noqa: BLE001 — never lose context to a fusion bug
            log.warning("context: fusion failed (%s) — falling back to raw bundle", e)
            ranked.items = bundle.all_items
            ranked.fusion = stats
            return ranked

        t0 = time.perf_counter()
        try:
            items = self.ranker.rank(items, message)
        except Exception as e:  # noqa: BLE001
            log.warning("context: ranking failed (%s) — keeping fusion order", e)
        stats.ranking_ms = (time.perf_counter() - t0) * 1000

        if compress:
            try:
                result = self.compressor.compress(items)
                items, stats.compression_ms = result.items, result.took_ms
                # Compression rewrites text, so re-rank: a collapsed chain is a
                # different string and deserves its own relevance score.
                items = self.ranker.rank(items, message)
            except Exception as e:  # noqa: BLE001
                log.warning("context: compression failed (%s) — using uncompressed", e)
        stats.items_after_compression = len(items)

        if apply_budget:
            try:
                budgeted = self.budget.apply(items)
                items = budgeted.items
                stats.budget_ms = budgeted.took_ms
                stats.tokens_available = budgeted.tokens_available
                stats.tokens_used = budgeted.tokens_used
                stats.dropped_for_budget = budgeted.dropped
                stats.allocation = budgeted.allocation
                stats.contribution = budgeted.contribution
            except Exception as e:  # noqa: BLE001
                log.warning("context: budgeting failed (%s) — using full set", e)
        stats.items_after_budget = len(items)

        ranked.items = items
        ranked.fusion = stats
        self._regroup(ranked)

        log.info("context hybrid: %d → %d items (%d merged, %d corroborated) | "
                 "%d/%d tokens (%.0f%%) | fuse %.0fms rank %.0fms compress %.0fms budget %.0fms",
                 stats.items_in, len(items), stats.duplicates_merged,
                 stats.corroborated_items, stats.tokens_used, stats.tokens_available,
                 (stats.budget_utilization * 100), stats.fusion_ms, stats.ranking_ms,
                 stats.compression_ms, stats.budget_ms)
        return ranked

    @staticmethod
    def _regroup(ranked: RankedContextBundle) -> None:
        """Write the ranked items back into per-provider slots.

        The PromptBuilder still emits one section per provider, so the grouped
        view has to reflect fusion — otherwise a section would show items that
        budgeting already dropped. A fused item appears under the provider that
        owned it, not under every attesting provider, so it is never duplicated
        across two sections.
        """
        grouped: dict[str, list[ContextItem]] = {}
        for item in ranked.items:
            grouped.setdefault(item.provider, []).append(item)
        for name in list(ranked.bundle._FIELDS) + list(ranked.bundle.extra):
            ranked.bundle.put(name, grouped.get(name, []))


_default: Optional[ContextBuilder] = None


def get_context_builder() -> ContextBuilder:
    """Process-wide builder over the global provider registry."""
    global _default
    if _default is None:
        _default = ContextBuilder()
    return _default


async def build_context(user_id: str, message: str, session_id: str = "",
                        **kwargs) -> ContextBundle:
    """Module-level convenience — the phase-3.5 call sites use this."""
    return await get_context_builder().build_context(user_id, message, session_id, **kwargs)


async def build_ranked_context(user_id: str, message: str, session_id: str = "",
                               **kwargs) -> RankedContextBundle:
    """The hybrid entry point: fused, ranked, compressed, budget-trimmed."""
    return await get_context_builder().build_ranked_context(
        user_id, message, session_id, **kwargs)
