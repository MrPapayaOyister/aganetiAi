"""
HybridContextFusion — many providers in, one comparable, deduplicated list out.

Three jobs, in order:

  1. NORMALIZE. Providers score on incompatible scales — Qdrant cosine ~0.3–0.9,
     graph 0–1 structural, memory and calendar nothing at all. Each provider gets
     a strategy that maps onto 0–1. `raw_score` is never overwritten.

  2. DEDUPLICATE. The same fact often arrives twice: a Qdrant chunk saying
     "Agentic AI uses LiteLLM" and a graph edge `agentic-ai -USES-> litellm`.
     Near-duplicates collapse into ONE item.

  3. CORROBORATE. A merged item keeps every provider's Evidence, so agreement
     becomes a countable signal instead of a silently-discarded duplicate. This
     is the whole reason to fuse rather than concatenate: two independent sources
     saying the same thing is the strongest quality signal available without a
     human, and concatenation throws it away.

Fusion does NOT rank (WeightedRanker does) and does NOT format (PromptBuilder
does). It also never drops information — a merged item carries the union of its
evidence, and the longer text is kept because it is the more complete statement.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .bundle import ContextBundle, ContextItem, FusionStats
from .evidence import Evidence, corroboration_of, merge_evidence, providers_of

log = logging.getLogger("aganeti.context.fusion")

# Jaccard overlap above which two items are treated as the same information.
# 0.6 is deliberately permissive: a graph edge ("agentic-ai -USES-> litellm") and
# a sentence ("Agentic AI uses LiteLLM") share few literal tokens, and missing a
# genuine cross-source match costs more than an occasional over-merge — the texts
# are kept whole either way, so a false merge loses nothing but a list slot.
DEFAULT_SIMILARITY_THRESHOLD = 0.6

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on",
         "for", "and", "or", "with", "by", "it", "that", "this"}


def _tokens(text: str) -> set[str]:
    # Graph items read "agentic-ai -USES-> litellm"; splitting on non-alphanumerics
    # turns that into {agentic, ai, uses, litellm}, which is what lets it match the
    # prose form at all.
    return {w for w in _WORD.findall((text or "").lower())
            if w not in _STOP and len(w) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / len(ta | tb)


def containment(a: str, b: str) -> float:
    """Fraction of the SHORTER item's tokens present in the longer one.

    Jaccard alone under-scores a short graph triple against a long paragraph that
    fully contains it; containment catches that asymmetric case."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small & large) / len(small)


# ── normalization strategies ─────────────────────────────────────────────────

def _norm_cosine(score: Optional[float], _all: "list[float]") -> float:
    """Qdrant cosine similarity is already 0–1 for normalized vectors."""
    return 0.0 if score is None else max(0.0, min(1.0, float(score)))


def _norm_passthrough(score: Optional[float], _all: "list[float]") -> float:
    """Graph scores are already 0–1 by construction (phase-3 ranker)."""
    return 0.0 if score is None else max(0.0, min(1.0, float(score)))


def _norm_minmax(score: Optional[float], all_scores: "list[float]") -> float:
    """Min-max within this turn's results, for unbounded provider scales."""
    if score is None:
        return 0.0
    vals = [v for v in all_scores if v is not None]
    if not vals:
        return 0.0
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return 1.0
    return (float(score) - lo) / (hi - lo)


def _norm_constant(value: float) -> Callable:
    """For providers with no score at all.

    A structured source that returned something is a FACT, not a guess, so it
    gets a high constant rather than 0 — scoring it zero would rank the user's
    own calendar below every fuzzy vector hit.
    """
    def _fn(_score: Optional[float], _all: "list[float]") -> float:
        return value
    return _fn


@dataclass(slots=True)
class NormalizationPolicy:
    """Which strategy each provider uses. Configurable, not hardcoded."""

    strategies: dict[str, Callable] = field(default_factory=lambda: {
        "corporate": _norm_cosine,
        "graph": _norm_passthrough,
        "memory": _norm_constant(0.70),     # native scoring is internal + unexposed
        "calendar": _norm_constant(0.85),   # exact, authoritative, but not query-scored
        "tasks": _norm_constant(0.85),
        "history": _norm_constant(0.50),
        "sql": _norm_constant(0.90),
    })
    default: Callable = field(default=_norm_minmax)

    def for_provider(self, name: str) -> Callable:
        return self.strategies.get(name, self.default)


class HybridContextFusion:
    """Normalize → deduplicate → corroborate."""

    def __init__(self, policy: Optional[NormalizationPolicy] = None,
                 similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
                 cross_provider_only: bool = False) -> None:
        self.policy = policy or NormalizationPolicy()
        self.threshold = similarity_threshold
        # When True, only merge items from DIFFERENT providers — useful for
        # measuring cross-source agreement without collapsing a provider's own
        # deliberate ranking.
        self.cross_provider_only = cross_provider_only

    # ── 1. normalize ─────────────────────────────────────────────────────────

    def normalize(self, bundle: ContextBundle) -> "list[ContextItem]":
        """Give every item a comparable `normalized_score`, preserving `raw_score`."""
        items: list[ContextItem] = []
        for name in list(bundle._FIELDS) + list(bundle.extra):
            group = bundle.get(name)
            if not group:
                continue
            scores = [i.score for i in group]
            strategy = self.policy.for_provider(name)
            for item in group:
                item.raw_score = item.score
                item.normalized_score = round(strategy(item.score, scores), 6)
                if not item.evidence:
                    item.evidence = self._seed_evidence(item)
                item.corroboration = corroboration_of(item.evidence)
                item.providers = providers_of(item.evidence) or [item.provider]
                items.append(item)
        return items

    @staticmethod
    def _seed_evidence(item: ContextItem) -> "list[Evidence]":
        """Every item starts with at least one attestation — its own provider."""
        if item.provider == "graph":
            return Evidence.from_graph_metadata(item.metadata, item.score or 0.0)
        meta = item.metadata or {}
        return [Evidence(
            provider=item.provider,
            source_id=item.source or "",
            document_id=meta.get("document_id"),
            conversation_id=meta.get("conversation_id"),
            message_id=meta.get("message_id"),
            confidence=float(item.normalized_score or 0.0),
            timestamp=item.timestamp,
            metadata={k: v for k, v in meta.items() if k != "signals"},
        )]

    # ── 2 + 3. deduplicate and corroborate ───────────────────────────────────

    def _same_information(self, a: ContextItem, b: ContextItem) -> bool:
        if self.cross_provider_only and a.provider == b.provider:
            return False
        return (jaccard(a.text, b.text) >= self.threshold
                or containment(a.text, b.text) >= 0.85)

    def _merge_into(self, keeper: ContextItem, other: ContextItem) -> None:
        """Fold `other` into `keeper`, losing nothing.

        The LONGER text survives: a paragraph that contains a triple states
        strictly more than the triple does, and dropping it would trade detail
        for a tidier list."""
        if len(other.text) > len(keeper.text):
            keeper.text = other.text
        keeper.evidence = merge_evidence([keeper.evidence, other.evidence])
        keeper.providers = providers_of(keeper.evidence)
        keeper.corroboration = corroboration_of(keeper.evidence)
        # Keep the strongest normalized signal — a weaker duplicate is not
        # evidence the item is worse, only that another source scored it lower.
        keeper.normalized_score = max(keeper.normalized_score, other.normalized_score)
        keeper.metadata.setdefault("merged_from", []).append(
            {"provider": other.provider, "source": other.source,
             "raw_score": other.raw_score})
        if not keeper.timestamp and other.timestamp:
            keeper.timestamp = other.timestamp

    def fuse(self, bundle: ContextBundle) -> "tuple[list[ContextItem], FusionStats]":
        """Full fusion pass. Returns (items, stats)."""
        started = time.perf_counter()
        stats = FusionStats()

        items = self.normalize(bundle)
        stats.items_in = len(items)

        # Highest normalized score first, so the strongest statement becomes the
        # keeper and weaker duplicates fold into it rather than the reverse.
        items.sort(key=lambda i: -i.normalized_score)

        kept: list[ContextItem] = []
        for item in items:
            match = next((k for k in kept if self._same_information(k, item)), None)
            if match is None:
                kept.append(item)
                continue
            before = set(match.providers)
            self._merge_into(match, item)
            stats.duplicates_merged += 1
            if set(match.providers) - before:
                stats.cross_provider_merges += 1

        stats.items_after_fusion = len(kept)
        stats.corroborated_items = sum(1 for i in kept if i.corroboration > 1)
        stats.fusion_ms = (time.perf_counter() - started) * 1000

        log.debug("fusion: %d → %d items (%d merged, %d cross-provider, "
                  "%d corroborated) in %.1fms",
                  stats.items_in, stats.items_after_fusion, stats.duplicates_merged,
                  stats.cross_provider_merges, stats.corroborated_items, stats.fusion_ms)
        return kept, stats
