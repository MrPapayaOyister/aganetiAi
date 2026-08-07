#!/usr/bin/env python3
"""
Evaluation CLI.

    python run_evals.py                      # run the golden set, write a report
    python run_evals.py --seed               # create the fixture graph first
    python run_evals.py --sweep              # + fusion-threshold sweep
    python run_evals.py --tune-weights       # + ranking-weight search
    python run_evals.py --with-answers       # + LLM answer scoring (slow)
    python run_evals.py --save-baseline      # store this run as the baseline
    python run_evals.py --baseline           # compare against it, exit 1 on regression
    python run_evals.py --tags graph,alias   # subset
    python run_evals.py --teardown           # remove the fixture

Exit codes: 0 pass · 1 regression or below --min-score · 2 setup problem.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.evals import fixture  # noqa: E402
from backend.evals.benchmark import search_ranking_weights, sweep_fusion_threshold  # noqa: E402
from backend.evals.dataset import load_dataset  # noqa: E402
from backend.evals.report import render_report, write_report  # noqa: E402
from backend.evals.runner import EvaluationRunner, RunnerConfig  # noqa: E402
from backend.evals.scoring import aggregate, score_case  # noqa: E402

REPORT_DIR = Path(__file__).parent / "reports" / "evals"
BASELINE = REPORT_DIR / "baseline.json"
BOLD, DIM, GRN, YEL, RED, OFF = ("\033[1m", "\033[2m", "\033[32m",
                                 "\033[33m", "\033[31m", "\033[0m")

# A metric may dip slightly between runs (LLM mention extraction is not perfectly
# deterministic). Only a drop beyond this counts as a regression.
REGRESSION_TOLERANCE = 0.02


def _colour(v: float, good: float = 0.8, warn: float = 0.5) -> str:
    return GRN if v >= good else (YEL if v >= warn else RED)


def print_summary(agg, scores) -> None:
    print(f"\n{BOLD}Overall{OFF}")
    c = _colour(agg.overall)
    print(f"  score            {c}{agg.overall*100:5.1f}%{OFF}  over {agg.cases} case(s)")
    for family, value in agg.families.items():
        print(f"  {family:<17}{_colour(value)}{value*100:5.1f}%{OFF}")

    print(f"\n{BOLD}Pipeline{OFF}")
    for k, v in agg.totals.items():
        shown = f"{v*100:5.1f}%" if isinstance(v, float) and v <= 1 else f"{v:g}"
        print(f"  {k:<22}{shown}")

    print(f"\n{BOLD}Latency (mean per case){OFF}")
    for k, v in agg.latency.items():
        if v > 0:
            print(f"  {k:<22}{v:8.1f}ms")

    if agg.provider_useful:
        print(f"\n{BOLD}Provider contribution{OFF}")
        for name in sorted(agg.provider_useful, key=lambda n: -agg.provider_useful[n]):
            share = agg.provider_useful[name]
            print(f"  {name:<12}useful in {share*100:5.1f}% of cases "
                  f"({agg.provider_contribution.get(name,0)} item(s))")

    if agg.infrastructure_errors:
        print(f"\n{BOLD}Infrastructure (not scored as failures){OFF}")
        for name, n in sorted(agg.infrastructure_errors.items(), key=lambda kv: -kv[1]):
            print(f"  {YEL}!{OFF} {name} — {n} case(s)")

    if agg.failing_cases:
        print(f"\n{BOLD}Failures{OFF}")
        for f in agg.failing_cases[:10]:
            print(f"  {RED}✗{OFF} {f['case_id']} ({f['overall']*100:.0f}%)")
            for msg in (f["failures"] + f["errors"])[:3]:
                print(f"      {DIM}{msg}{OFF}")


def compare_baseline(agg, baseline: dict) -> "tuple[bool, list[str]]":
    """Returns (regressed, messages)."""
    msgs, regressed = [], False
    checks = [("overall", agg.overall, baseline.get("overall", 0.0))]
    for family, value in agg.families.items():
        checks.append((family, value, baseline.get("families", {}).get(family, 0.0)))
    for name, now, before in checks:
        delta = now - before
        if delta < -REGRESSION_TOLERANCE:
            regressed = True
            msgs.append(f"{RED}REGRESSION{OFF} {name}: {before*100:.1f}% → "
                        f"{now*100:.1f}% ({delta*100:+.1f}pp)")
        elif delta > REGRESSION_TOLERANCE:
            msgs.append(f"{GRN}improved{OFF}   {name}: {before*100:.1f}% → "
                        f"{now*100:.1f}% ({delta*100:+.1f}pp)")
    return regressed, msgs


async def main() -> int:
    ap = argparse.ArgumentParser(description="Context Engine evaluation")
    ap.add_argument("--seed", action="store_true", help="seed the fixture graph first")
    ap.add_argument("--teardown", action="store_true", help="remove the fixture and exit")
    ap.add_argument("--tags", default="", help="comma-separated tag filter")
    ap.add_argument("--ids", default="", help="comma-separated case ids")
    ap.add_argument("--sweep", action="store_true", help="fusion-threshold sweep")
    ap.add_argument("--tune-weights", action="store_true", help="ranking-weight search")
    ap.add_argument("--rounds", type=int, default=2, help="weight-search rounds")
    ap.add_argument("--with-answers", action="store_true", help="score LLM answers (slow)")
    ap.add_argument("--baseline", action="store_true", help="compare against the baseline")
    ap.add_argument("--save-baseline", action="store_true", help="store this run as baseline")
    ap.add_argument("--min-score", type=float, default=0.0, help="fail below this overall")
    ap.add_argument("--report", default="", help="output HTML path")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.teardown:
        print(f"removed {fixture.teardown()} fixture node(s)")
        return 0

    print(f"\n{BOLD}Context Engine evaluation{OFF}")

    if args.seed:
        try:
            print(f"{DIM}seeding fixture…{OFF}")
            print(f"{DIM}  {fixture.seed()}{OFF}")
        except Exception as e:  # noqa: BLE001
            print(f"{RED}fixture seeding failed:{OFF} {e}")
            return 2

    try:
        dataset = load_dataset()
    except Exception as e:  # noqa: BLE001
        print(f"{RED}cannot load golden cases:{OFF} {e}")
        return 2
    if args.tags or args.ids:
        dataset = dataset.filter(tags=[t for t in args.tags.split(",") if t],
                                 ids=[i for i in args.ids.split(",") if i])
    if not len(dataset):
        print(f"{RED}no cases selected{OFF}")
        return 2

    config = RunnerConfig(with_answer=args.with_answers)
    print(f"{DIM}running {len(dataset)} case(s)…{OFF}")
    started = time.perf_counter()
    traces = await EvaluationRunner(config).run_all(dataset, progress=not args.quiet)
    scores = [score_case(c, t) for c, t in zip(dataset, traces)]
    agg = aggregate(scores, traces)
    took = time.perf_counter() - started

    if not args.quiet:
        print_summary(agg, scores)

    sweep = weights = None
    if args.sweep:
        print(f"\n{BOLD}Fusion threshold sweep{OFF}")
        sweep = await sweep_fusion_threshold(dataset, base=config,
                                             progress=not args.quiet)
        print(f"  recommended {GRN}{sweep.recommended:.2f}{OFF} — {sweep.rationale}")
    if args.tune_weights:
        print(f"\n{BOLD}Ranking weight search{OFF}")
        weights = await search_ranking_weights(dataset, base=config,
                                               rounds=args.rounds,
                                               progress=not args.quiet)
        print(f"  {weights.evaluations} evaluation(s) in {weights.took_s:.0f}s · "
              f"{weights.baseline_score:.4f} → {GRN}{weights.best_score:.4f}{OFF} "
              f"({weights.improvement:+.4f})")

    payload = {
        "overall": agg.overall, "families": agg.families, "totals": agg.totals,
        "latency": agg.latency, "cases": [s.as_dict() for s in scores],
        "traces": [t.as_dict() for t in traces],
        "provider_useful": agg.provider_useful,
        "sweep": sweep.as_dict() if sweep else None,
        "weights": weights.as_dict() if weights else None,
        "took_s": round(took, 2),
    }

    exit_code = 0
    if args.baseline:
        if not BASELINE.exists():
            print(f"\n{YEL}no baseline at {BASELINE} — run --save-baseline first{OFF}")
        else:
            base = json.loads(BASELINE.read_text())
            regressed, msgs = compare_baseline(agg, base)
            print(f"\n{BOLD}Regression check{OFF}")
            for m in msgs or [f"  {DIM}no change beyond ±{REGRESSION_TOLERANCE*100:.0f}pp{OFF}"]:
                print(f"  {m}" if msgs else m)
            if regressed:
                exit_code = 1
            payload["baseline"] = base

    if args.save_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            {"overall": agg.overall, "families": agg.families,
             "totals": agg.totals, "latency": agg.latency}, indent=2))
        print(f"\nbaseline saved → {BASELINE}")

    html = render_report(agg, scores, traces, sweep=sweep, weights=weights,
                         baseline=payload.get("baseline"),
                         dataset_name=dataset.name)
    out = Path(args.report) if args.report else REPORT_DIR / "report.html"
    write_report(out, html, payload)
    print(f"\nreport → {out}")
    print(f"json   → {out.with_suffix('.json')}")

    if agg.overall < args.min_score:
        print(f"{RED}overall {agg.overall*100:.1f}% below --min-score "
              f"{args.min_score*100:.0f}%{OFF}")
        exit_code = 1

    print(f"{DIM}completed in {took:.1f}s{OFF}\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
