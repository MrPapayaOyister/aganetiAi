"""
Parameter search — fusion threshold sweep and ranking weight search.

Both re-run the SAME golden cases with one parameter varied, so a comparison is
apples to apples. Retrieval is not re-executed per trial where it can be avoided:
the threshold sweep needs fusion re-run (that IS the parameter) but everything
upstream is deterministic per case, so a trial is cheap once the first pass has
warmed the stores.

Weight search is **coordinate descent**, not full grid or Bayesian. Eight weights
at five values each is 390k evaluations — untenable against a live Neo4j and
Qdrant. Coordinate descent tunes one axis at a time, holding the rest, and
converges in `axes × values × rounds` evaluations (a few hundred). It can settle
in a local optimum; for a smooth, weakly-coupled objective like this one that is
an acceptable trade for finishing in minutes rather than days.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from backend.context.ranking import RankingWeights

from .dataset import GoldenDataset
from .runner import EvaluationRunner, RunnerConfig
from .scoring import aggregate, score_case

log = logging.getLogger("aganeti.evals.benchmark")

DEFAULT_THRESHOLDS = [round(0.40 + 0.05 * i, 2) for i in range(12)]   # 0.40 … 0.95


@dataclass(slots=True)
class TrialResult:
    label: str
    params: dict[str, Any]
    overall: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    duplicate_reduction: float = 0.0
    cross_provider_merges: int = 0
    corroborated_items: int = 0
    latency_ms: float = 0.0
    families: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _evaluate(dataset: GoldenDataset, config: RunnerConfig,
                    label: str, params: dict) -> TrialResult:
    runner = EvaluationRunner(config)
    traces = await runner.run_all(dataset)
    scores = [score_case(c, t) for c, t in zip(dataset, traces)]
    agg = aggregate(scores, traces)

    # Fusion precision/recall: did merging keep the right SOURCES together?
    prec = [s.source_precision for s in scores if s.source_precision is not None]
    rec = [s.source_recall for s in scores if s.source_recall is not None]
    p = sum(prec) / len(prec) if prec else 0.0
    r = sum(rec) / len(rec) if rec else 0.0
    f1 = 0.0 if (p + r) == 0 else 2 * p * r / (p + r)

    return TrialResult(
        label=label, params=params, overall=agg.overall,
        precision=round(p, 4), recall=round(r, 4), f1=round(f1, 4),
        duplicate_reduction=agg.totals.get("duplicate_reduction", 0.0),
        cross_provider_merges=sum(t.cross_provider_merges for t in traces),
        corroborated_items=sum(t.corroborated_items for t in traces),
        latency_ms=agg.latency.get("total_ms", 0.0),
        families=dict(agg.families),
    )


# ── fusion threshold sweep ───────────────────────────────────────────────────

@dataclass(slots=True)
class ThresholdSweep:
    trials: list[TrialResult] = field(default_factory=list)
    recommended: float = 0.6
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"trials": [t.as_dict() for t in self.trials],
                "recommended": self.recommended, "rationale": self.rationale}


async def sweep_fusion_threshold(dataset: GoldenDataset,
                                 thresholds: Optional[list] = None,
                                 base: Optional[RunnerConfig] = None,
                                 progress: bool = False) -> ThresholdSweep:
    """Evaluate every threshold and recommend one."""
    thresholds = thresholds or DEFAULT_THRESHOLDS
    base = base or RunnerConfig()
    sweep = ThresholdSweep()

    for th in thresholds:
        cfg = RunnerConfig(**{**asdict_config(base), "fusion_threshold": th})
        if progress:
            print(f"  threshold {th:.2f} …", flush=True)
        sweep.trials.append(await _evaluate(dataset, cfg, f"th={th:.2f}",
                                            {"fusion_threshold": th}))

    # Selection: highest F1, and among near-ties (within 1%) prefer the threshold
    # that merged MORE cross-provider duplicates — corroboration is the signal the
    # hybrid engine exists to produce, so it breaks ties in its favour.
    if sweep.trials:
        best_f1 = max(t.f1 for t in sweep.trials)
        near = [t for t in sweep.trials if t.f1 >= best_f1 - 0.01]
        best = max(near, key=lambda t: (t.cross_provider_merges, -t.params["fusion_threshold"]))
        sweep.recommended = best.params["fusion_threshold"]
        sweep.rationale = (
            f"F1={best.f1:.3f} (best={best_f1:.3f}), "
            f"{best.cross_provider_merges} cross-provider merge(s), "
            f"dedup={best.duplicate_reduction:.0%}")
    return sweep


def asdict_config(cfg: RunnerConfig) -> dict:
    """RunnerConfig → kwargs, without dataclasses.asdict (which would deep-copy
    the injected provider objects and break identity for fixture runs)."""
    return {f: getattr(cfg, f) for f in RunnerConfig.__slots__}


# ── ranking weight search ────────────────────────────────────────────────────

WEIGHT_AXES = ("query_relevance", "provider_confidence", "corroboration",
               "freshness", "importance", "provider_reliability",
               "graph_centrality", "relationship_depth")
WEIGHT_VALUES = (0.0, 0.25, 0.5, 0.8, 1.0, 1.4, 2.0)


@dataclass(slots=True)
class WeightSearch:
    trials: list[TrialResult] = field(default_factory=list)
    best_weights: dict[str, float] = field(default_factory=dict)
    best_score: float = 0.0
    baseline_score: float = 0.0
    evaluations: int = 0
    rounds: int = 0
    took_s: float = 0.0

    @property
    def improvement(self) -> float:
        return round(self.best_score - self.baseline_score, 4)

    def as_dict(self) -> dict[str, Any]:
        return {"trials": [t.as_dict() for t in self.trials],
                "best_weights": self.best_weights, "best_score": self.best_score,
                "baseline_score": self.baseline_score,
                "improvement": self.improvement, "evaluations": self.evaluations,
                "rounds": self.rounds, "took_s": round(self.took_s, 1)}


def _objective(trial: TrialResult) -> float:
    """What the search maximises.

    Ranking quality specifically — provider-order correlation and ranking nDCG —
    rather than the overall score, because retrieval recall does not change with
    ranking weights and including it would dilute the signal the search is
    trying to follow."""
    fam = trial.families
    parts = [v for k, v in fam.items() if k in ("ranking", "answer")]
    return round(sum(parts) / len(parts), 6) if parts else trial.overall


async def search_ranking_weights(dataset: GoldenDataset,
                                 base: Optional[RunnerConfig] = None,
                                 rounds: int = 2,
                                 axes: Optional[tuple] = None,
                                 values: Optional[tuple] = None,
                                 progress: bool = False) -> WeightSearch:
    """Coordinate descent over the ranking weights."""
    base = base or RunnerConfig()
    axes = axes or WEIGHT_AXES
    values = values or WEIGHT_VALUES
    started = time.perf_counter()
    search = WeightSearch(rounds=rounds)

    current = RankingWeights()
    baseline = await _evaluate(dataset, RunnerConfig(**{**asdict_config(base),
                                                       "weights": current}),
                               "baseline", current.as_dict())
    search.trials.append(baseline)
    search.evaluations += 1
    search.baseline_score = _objective(baseline)
    best_score = search.baseline_score

    for rnd in range(rounds):
        improved = False
        for axis in axes:
            original = getattr(current, axis)
            best_value, best_axis_score = original, best_score
            for value in values:
                if value == original:
                    continue
                candidate = RankingWeights(**{**current.as_dict(), axis: value})
                # A weight set that is all zeros has no denominator; skip it.
                if sum(candidate.as_dict().values()) <= 0:
                    continue
                trial = await _evaluate(
                    dataset, RunnerConfig(**{**asdict_config(base), "weights": candidate}),
                    f"{axis}={value}", candidate.as_dict())
                search.trials.append(trial)
                search.evaluations += 1
                score = _objective(trial)
                if score > best_axis_score + 1e-9:
                    best_axis_score, best_value = score, value
            if best_value != original:
                setattr(current, axis, best_value)
                best_score = best_axis_score
                improved = True
                if progress:
                    print(f"  round {rnd+1}: {axis} {original} → {best_value} "
                          f"(score {best_score:.4f})", flush=True)
        if not improved:
            if progress:
                print(f"  round {rnd+1}: converged", flush=True)
            break

    search.best_weights = current.as_dict()
    search.best_score = best_score
    search.took_s = time.perf_counter() - started
    return search
