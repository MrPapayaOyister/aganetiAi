"""
Ranking — turning graph signals into an order.

Five signals, each normalised to [0,1] and combined with explicit weights. The
weights live here as data rather than being baked into a formula so they can be
tuned (or A/B'd) without touching traversal.

    confidence     what the extractor claimed          → is this likely true?
    corroboration  distinct sources asserting it       → do independent sources agree?
    freshness      how recently it was last seen       → is it still current?
    importance     node degree                         → is it central to the graph?
    frequency      observation count on the edge       → how often is it restated?

`hop` is applied last as a decay: a node two hops from the question is less
likely to be about the question than one hop, regardless of how good it looks in
isolation. Without the decay, a highly-connected hub (say "Microsoft") outranks
the entity actually asked about.

Every score keeps its component breakdown, because a single opaque float is
untunable — the first thing anyone will ask is "why did THAT come first?".
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .types import Evidence


@dataclass(slots=True)
class RankingWeights:
    """Relative importance of each signal. They need not sum to 1 — the score is
    normalised by their total, so adding a signal does not rescale the others."""

    confidence: float = 1.0
    corroboration: float = 1.2      # slightly above confidence: agreement between
                                    # independent sources beats one model's self-report
    freshness: float = 0.6
    importance: float = 0.8
    frequency: float = 0.7

    # Multiplier applied per hop of distance from a seed.
    hop_decay: float = 0.55

    @property
    def total(self) -> float:
        return (self.confidence + self.corroboration + self.freshness
                + self.importance + self.frequency) or 1.0


DEFAULT_WEIGHTS = RankingWeights()

# A node seen in this many distinct sources is treated as fully corroborated.
CORROBORATION_SATURATION = 4
# Degree at which importance saturates. Log-scaled below this: the difference
# between 1 and 5 neighbours matters far more than between 50 and 54.
IMPORTANCE_SATURATION = 25
# Observations at which frequency saturates.
FREQUENCY_SATURATION = 8
# Age in days at which freshness has fully decayed.
FRESHNESS_HALFLIFE_DAYS = 30.0


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def freshness_score(last_seen: Any, now: Optional[datetime] = None) -> float:
    """Exponential decay on age. Unknown timestamp scores neutral, not zero —
    missing metadata is not evidence of staleness."""
    dt = _parse_ts(last_seen)
    if dt is None:
        return 0.5
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return float(math.exp(-age_days / FRESHNESS_HALFLIFE_DAYS))


def corroboration_score(evidence: Evidence) -> float:
    distinct = len(set(evidence.source_ids))
    if distinct <= 0:
        return 0.0
    return min(1.0, distinct / CORROBORATION_SATURATION)


def importance_score(degree: int) -> float:
    """Log-scaled degree. Linear scaling would let hubs dominate everything."""
    if degree <= 0:
        return 0.0
    return min(1.0, math.log1p(degree) / math.log1p(IMPORTANCE_SATURATION))


def frequency_score(observations: int) -> float:
    if observations <= 0:
        return 0.0
    return min(1.0, math.log1p(observations) / math.log1p(FREQUENCY_SATURATION))


def score_signals(evidence: Evidence, degree: int = 0,
                  now: Optional[datetime] = None) -> dict[str, float]:
    """The five component signals, each in [0,1]."""
    return {
        "confidence": max(0.0, min(1.0, evidence.confidence)),
        "corroboration": corroboration_score(evidence),
        "freshness": freshness_score(evidence.last_seen, now),
        "importance": importance_score(degree),
        "frequency": frequency_score(evidence.observations),
    }


def combine(signals: dict[str, float], hop: int = 0,
            weights: RankingWeights = DEFAULT_WEIGHTS) -> float:
    """Weighted mean of the signals, decayed by distance from the question."""
    weighted = (signals.get("confidence", 0.0) * weights.confidence
                + signals.get("corroboration", 0.0) * weights.corroboration
                + signals.get("freshness", 0.0) * weights.freshness
                + signals.get("importance", 0.0) * weights.importance
                + signals.get("frequency", 0.0) * weights.frequency)
    base = weighted / weights.total
    return round(base * (weights.hop_decay ** max(0, hop)), 6)


def score_node(evidence: Evidence, degree: int = 0, hop: int = 0,
               weights: RankingWeights = DEFAULT_WEIGHTS,
               now: Optional[datetime] = None) -> tuple[float, dict[str, float]]:
    signals = score_signals(evidence, degree, now)
    return combine(signals, hop, weights), signals


def score_edge(evidence: Evidence, hop: int = 0,
               weights: RankingWeights = DEFAULT_WEIGHTS,
               now: Optional[datetime] = None) -> float:
    """Edges have no degree of their own, so importance is left out entirely
    rather than defaulting to zero — a zero would drag every edge down equally
    and just add noise."""
    signals = score_signals(evidence, degree=0, now=now)
    signals.pop("importance")
    partial = RankingWeights(
        confidence=weights.confidence, corroboration=weights.corroboration,
        freshness=weights.freshness, importance=0.0, frequency=weights.frequency,
        hop_decay=weights.hop_decay)
    return combine(signals, hop, partial)
