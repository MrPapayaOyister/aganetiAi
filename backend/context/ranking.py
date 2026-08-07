"""
WeightedRanker — one comparable score per item, from eight signals.

Providers score on incompatible scales: Qdrant returns cosine similarity, the
graph returns a weighted structural score, memory and calendar return nothing at
all. Ordering them by their native numbers is meaningless, so fusion normalises
first (see fusion.py) and this module then combines *signals*, not raw scores.

Every weight is data, not code. Nothing here is hardcoded — pass a different
`RankingWeights` and the ordering changes with no edit to the algorithm.

    final = Σ(signal × weight) / Σ(weights present)

Signals absent for an item are EXCLUDED from the denominator rather than scored
zero, because a zero would penalise an item for a signal that simply does not
apply to its source (calendar has no graph centrality; a task has no depth).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .evidence import corroboration_of, providers_of

# Saturation points — where a signal stops growing. Log-scaled below them so the
# difference between 1 and 3 sources matters far more than between 20 and 22.
CORROBORATION_SATURATION = 4
CENTRALITY_SATURATION = 25
FRESHNESS_HALFLIFE_DAYS = 30.0


@dataclass(slots=True)
class RankingWeights:
    """Relative importance of each signal. Need not sum to 1."""

    query_relevance: float = 1.4      # highest: does this answer THIS question?
    provider_confidence: float = 1.0
    corroboration: float = 1.2        # independent agreement beats one self-report
    freshness: float = 0.6
    importance: float = 0.5
    provider_reliability: float = 0.8
    graph_centrality: float = 0.5
    relationship_depth: float = 0.4   # applied as a penalty (1 - depth)

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class ProviderReliability:
    """How much a source is trusted a priori, independent of any one item.

    These are PRIORS, not measurements. Structured stores are exact and stated
    highest; extraction-derived graph content sits below verbatim text because a
    model produced it. Tune from observed answer quality, not intuition.
    """

    scores: dict[str, float] = field(default_factory=lambda: {
        "tasks": 1.00,        # structured, exact, user-authored
        "calendar": 1.00,     # structured, exact, provider-authoritative
        "corporate": 0.90,    # verbatim document text
        "memory": 0.75,       # user-stated but LLM-extracted
        "graph": 0.70,        # LLM-extracted relationships
        "history": 0.85,      # verbatim conversation
        "sql": 0.95,
    })
    default: float = 0.6

    def of(self, provider: str) -> float:
        return self.scores.get(provider, self.default)


DEFAULT_WEIGHTS = RankingWeights()
DEFAULT_RELIABILITY = ProviderReliability()

_STOP = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "for",
         "and", "or", "what", "how", "who", "which", "when", "where", "why", "do",
         "does", "did", "our", "my", "i", "we", "it", "this", "that", "with", "by"}


def _terms(text: str) -> set[str]:
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split()
            if w and w not in _STOP and len(w) > 2}


def query_relevance(query: str, text: str) -> float:
    """Lexical overlap between the question and the item, 0–1.

    Deliberately NOT another embedding call: the corporate provider already paid
    for semantic matching, and re-embedding every candidate would add latency to
    ranking for a signal the vector store has effectively already given us. This
    is the cheap tie-breaker that stops a structurally-strong but topically
    irrelevant graph node outranking the chunk that answers the question.
    """
    q, t = _terms(query), _terms(text)
    if not q or not t:
        return 0.0
    return len(q & t) / len(q)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def freshness(timestamp: Any, now: Optional[datetime] = None) -> Optional[float]:
    """Exponential decay on age. None when unknown — the caller then excludes the
    signal rather than guessing, so undated calendar text is not called stale."""
    dt = _parse_ts(timestamp)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return float(math.exp(-age_days / FRESHNESS_HALFLIFE_DAYS))


def corroboration(item) -> float:
    n = max(item.corroboration, corroboration_of(item.evidence) if item.evidence else 1)
    return min(1.0, n / CORROBORATION_SATURATION)


def centrality(item) -> Optional[float]:
    """Graph degree, log-scaled. None for non-graph items."""
    degree = item.metadata.get("degree")
    if degree is None:
        signals = item.metadata.get("signals") or {}
        if "importance" in signals:
            return float(signals["importance"])
        return None
    return min(1.0, math.log1p(int(degree)) / math.log1p(CENTRALITY_SATURATION))


def depth_penalty(item) -> Optional[float]:
    """1.0 at the seed, decaying with hop distance. None for non-graph items."""
    hop = item.metadata.get("hop")
    if hop is None:
        return None
    return 1.0 / (1.0 + max(0, int(hop)))


class WeightedRanker:
    """Scores fused items into one comparable ordering."""

    def __init__(self, weights: Optional[RankingWeights] = None,
                 reliability: Optional[ProviderReliability] = None) -> None:
        self.weights = weights or DEFAULT_WEIGHTS
        self.reliability = reliability or DEFAULT_RELIABILITY

    def signals_for(self, item, query: str,
                    now: Optional[datetime] = None) -> dict[str, float]:
        """Every applicable signal, each 0–1. Absent signals are simply omitted."""
        out: dict[str, float] = {
            "query_relevance": query_relevance(query, item.text),
            # The normalized provider score IS the provider's confidence, on a
            # scale fusion made comparable.
            "provider_confidence": max(0.0, min(1.0, item.normalized_score)),
            "corroboration": corroboration(item),
            "provider_reliability": max(
                self.reliability.of(p) for p in (item.providers or [item.provider])),
        }
        f = freshness(item.timestamp, now)
        if f is not None:
            out["freshness"] = f
        c = centrality(item)
        if c is not None:
            out["graph_centrality"] = c
        d = depth_penalty(item)
        if d is not None:
            out["relationship_depth"] = d
        imp = item.metadata.get("importance")
        if imp is not None:
            out["importance"] = max(0.0, min(1.0, float(imp)))
        return out

    def score(self, item, query: str, now: Optional[datetime] = None) -> float:
        signals = self.signals_for(item, query, now)
        weights = self.weights.as_dict()
        num = sum(v * weights.get(k, 0.0) for k, v in signals.items())
        den = sum(weights.get(k, 0.0) for k in signals)
        item.signals = {k: round(v, 4) for k, v in signals.items()}
        return round(num / den, 6) if den else 0.0

    def rank(self, items: "list", query: str,
             now: Optional[datetime] = None) -> "list":
        """Score every item and return them highest-first.

        Ties break on corroboration then provider count — when two items look
        equally relevant, the one more sources agree on should win.
        """
        for item in items:
            item.final_score = self.score(item, query, now)
        return sorted(
            items,
            key=lambda i: (-i.final_score, -i.corroboration,
                           -len(providers_of(i.evidence) or i.providers), i.text[:40]),
        )
