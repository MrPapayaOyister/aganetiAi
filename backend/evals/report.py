"""
HTML report — one self-contained file, openable from disk.

No external CSS, JS or fonts: the report must render on a machine with no
network. It is also the regression artefact, so it stores the raw JSON alongside
the rendering — `run_evals.py --baseline` reads that JSON back rather than
re-parsing HTML.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Optional

from . import visualizer as viz

CSS = """
:root{--bg:#0f1420;--card:#161d2c;--line:#243044;--fg:#e6ecf5;--mut:#8fa0b8;
--good:#37C978;--warn:#FFB020;--bad:#FF6B6B;--acc:#4C8DFF}
@media(prefers-color-scheme:light){:root{--bg:#f6f8fc;--card:#fff;--line:#e2e8f2;
--fg:#16202e;--mut:#5b6a80}}
*{box-sizing:border-box}body{margin:0;padding:28px;background:var(--bg);color:var(--fg);
font:14px/1.55 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 10px;
text-transform:uppercase;letter-spacing:.07em;color:var(--mut)}
h3{font-size:13px;margin:16px 0 6px;color:var(--mut)}
.muted{color:var(--mut)}.wrap{max-width:1080px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.metric{font-size:26px;font-weight:600;line-height:1.1}
.metric small{font-size:12px;font-weight:400;color:var(--mut)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:14px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px;
border:1px solid var(--line)}
.svg-lab{fill:var(--mut);font-size:11px}.svg-val{fill:var(--fg);font-size:11px}
.svg-tick{fill:var(--mut);font-size:10px}.svg-axis{fill:var(--mut);font-size:11px}
.svg-grid{stroke:var(--line);stroke-width:1}.svg-legend{font-size:11px}
.svg-inbar{fill:#0b1220;font-size:10px;font-weight:600}
.legend{margin-top:6px;font-size:11px;color:var(--mut)}
.key{margin-right:12px;white-space:nowrap}
.key i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
.matrix td.cm{text-align:center;font-variant-numeric:tabular-nums}
.matrix td.good{color:var(--good)}.matrix td.bad{color:var(--bad)}
.fail{border-left:3px solid var(--bad);padding-left:10px;margin:8px 0}
.fail code{color:var(--warn)}
details{margin-top:8px}summary{cursor:pointer;color:var(--mut)}
"""


def _cls(v: float, good: float = 0.8, warn: float = 0.5) -> str:
    return "good" if v >= good else ("warn" if v >= warn else "bad")


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v*100:.0f}%"


def _card(label: str, value: str, cls: str = "") -> str:
    return (f"<div class='card'><div class='muted'>{escape(label)}</div>"
            f"<div class='metric {cls}'>{value}</div></div>")


def render_report(agg, scores, traces, *, sweep=None, weights=None,
                  baseline: Optional[dict] = None, title: str = "Context Engine Evaluation",
                  dataset_name: str = "default") -> str:
    a = agg.as_dict()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"<!-- generated {now} --><div class='wrap'>",
           f"<h1>{escape(title)}</h1>",
           f"<p class='muted'>{now} · dataset <b>{escape(dataset_name)}</b> · "
           f"{agg.cases} case(s)</p>"]

    # ── headline ──
    delta = ""
    if baseline:
        d = agg.overall - baseline.get("overall", 0)
        sign = "+" if d >= 0 else ""
        delta = (f" <small class='{'good' if d>=0 else 'bad'}'>{sign}{d*100:.1f}pp "
                 f"vs baseline</small>")
    out.append("<h2>Overall</h2><div class='cards'>")
    out.append(_card("overall score", f"{agg.overall*100:.0f}%{delta}",
                     _cls(agg.overall)))
    for family, value in a["families"].items():
        out.append(_card(family.replace("_", " "), _pct(value), _cls(value)))
    out.append("</div>")

    # ── totals ──
    t = a["totals"]
    out.append("<h2>Pipeline totals</h2><div class='cards'>")
    for label, key, is_pct in (("duplicate reduction", "duplicate_reduction", True),
                               ("compression ratio", "compression_ratio", True),
                               ("citation coverage", "citation_coverage", True),
                               ("budget utilisation", "budget_utilization", True),
                               ("avg tokens", "avg_tokens", False),
                               ("max corroboration", "corroborated_max", False)):
        v = t.get(key, 0)
        out.append(_card(label, _pct(v) if is_pct else f"{v:g}"))
    out.append("</div>")

    # ── latency ──
    lat = {k.replace("_ms", ""): v for k, v in a["latency"].items()
           if k not in ("total_ms",) and v > 0}
    out.append("<h2>Latency breakdown</h2><div class='panel'>")
    out.append(f"<p class='muted'>mean total <b>{a['latency'].get('total_ms',0):.0f}ms</b> "
               f"per case</p>")
    out.append(viz.stacked_latency(lat) if lat else "<p class='muted'>no timings</p>")
    out.append("</div>")

    # ── provider contribution ──
    out.append("<h2>Provider contribution</h2><div class='panel'>")
    if agg.provider_useful:
        names = sorted(agg.provider_useful, key=lambda n: -agg.provider_useful[n])
        out.append("<h3>How often a provider reached the final context</h3>")
        out.append(viz.bar_chart(names, [agg.provider_useful[n] for n in names],
                                 max_value=1.0, suffix=""))
        out.append("<table><tr><th>provider</th><th class='num'>useful in</th>"
                   "<th class='num'>items kept</th></tr>")
        for n in names:
            out.append(f"<tr><td>{escape(n)}</td>"
                       f"<td class='num'>{_pct(agg.provider_useful[n])}</td>"
                       f"<td class='num'>{agg.provider_contribution.get(n,0)}</td></tr>")
        out.append("</table>")
    else:
        out.append("<p class='muted'>no provider contributed</p>")
    out.append("</div>")

    # ── threshold sweep ──
    if sweep and sweep.trials:
        xs = [t.params["fusion_threshold"] for t in sweep.trials]
        out.append("<h2>Fusion threshold sweep</h2><div class='panel'>")
        out.append(viz.line_chart(xs, {
            "precision": [t.precision for t in sweep.trials],
            "recall": [t.recall for t in sweep.trials],
            "F1": [t.f1 for t in sweep.trials],
            "dedup": [t.duplicate_reduction for t in sweep.trials],
        }, y_max=1.0, x_label="fusion threshold"))
        out.append(f"<p>recommended <span class='pill good'>"
                   f"{sweep.recommended:.2f}</span> — {escape(sweep.rationale)}</p>")
        out.append("<table><tr><th>threshold</th><th class='num'>P</th><th class='num'>R</th>"
                   "<th class='num'>F1</th><th class='num'>dedup</th>"
                   "<th class='num'>cross-merges</th></tr>")
        for tr in sweep.trials:
            mark = " ←" if tr.params["fusion_threshold"] == sweep.recommended else ""
            out.append(
                f"<tr><td>{tr.params['fusion_threshold']:.2f}{mark}</td>"
                f"<td class='num'>{tr.precision:.2f}</td><td class='num'>{tr.recall:.2f}</td>"
                f"<td class='num'>{tr.f1:.2f}</td>"
                f"<td class='num'>{tr.duplicate_reduction:.0%}</td>"
                f"<td class='num'>{tr.cross_provider_merges}</td></tr>")
        out.append("</table></div>")

    # ── weight search ──
    if weights and weights.best_weights:
        w = weights.as_dict()
        out.append("<h2>Ranking weight search</h2><div class='panel'>")
        out.append(f"<p class='muted'>coordinate descent · {w['evaluations']} evaluation(s) "
                   f"· {w['took_s']}s · baseline {w['baseline_score']:.4f} → "
                   f"best <b>{w['best_score']:.4f}</b> "
                   f"(<span class='{'good' if w['improvement']>=0 else 'bad'}'>"
                   f"{w['improvement']:+.4f}</span>)</p>")
        names = list(w["best_weights"])
        out.append(viz.bar_chart(names, [w["best_weights"][n] for n in names], suffix=""))
        out.append("<details><summary>best weight set (paste into RankingWeights)</summary>"
                   f"<pre>{escape(json.dumps(w['best_weights'], indent=2))}</pre></details>")
        out.append("</div>")

    # ── per-case table ──
    out.append("<h2>Cases</h2><div class='panel'><table>")
    out.append("<tr><th>case</th><th class='num'>overall</th><th class='num'>entity F1</th>"
               "<th class='num'>graph R</th><th class='num'>qdrant R@k</th>"
               "<th class='num'>corrob</th><th class='num'>tokens</th>"
               "<th class='num'>ms</th></tr>")
    for s in sorted(scores, key=lambda x: x.overall):
        out.append(
            f"<tr><td title='{escape(s.question)}'>{escape(s.case_id)}</td>"
            f"<td class='num {_cls(s.overall)}'>{s.overall*100:.0f}%</td>"
            f"<td class='num'>{_pct(s.entity_f1)}</td>"
            f"<td class='num'>{_pct(s.graph_node_recall)}</td>"
            f"<td class='num'>{_pct(s.qdrant_recall_at_k)}</td>"
            f"<td class='num'>{s.max_corroboration}</td>"
            f"<td class='num'>{s.tokens_used}</td>"
            f"<td class='num'>{s.latency_ms:.0f}</td></tr>")
    out.append("</table></div>")

    # ── confusion matrices ──
    tp = sum(int(round((s.entity_precision or 0) * 1)) for s in scores)
    ent = [s for s in scores if s.entity_f1 is not None]
    if ent:
        TP = sum(1 for s in ent if (s.entity_recall or 0) >= 0.999)
        FN = sum(1 for s in ent if (s.entity_recall or 0) < 0.999)
        FP = sum(1 for s in ent if (s.entity_precision or 1) < 0.999)
        out.append("<h2>Entity resolution confusion</h2><div class='panel'>")
        out.append(viz.confusion_matrix(TP, FP, FN,
                                        labels=("expected entity", "other")))
        out.append("<p class='muted'>per case: fully-recalled vs partially/not "
                   "recalled</p></div>")

    # ── infrastructure ──
    if getattr(agg, "infrastructure_errors", None):
        out.append("<h2>Infrastructure</h2><div class='panel'>"
                   "<p class='muted'>environment problems — counted separately from "
                   "expectation failures</p><table>")
        for name, n in sorted(agg.infrastructure_errors.items(), key=lambda kv: -kv[1]):
            out.append(f"<tr><td class='warn'>{escape(name)}</td>"
                       f"<td class='num'>{n} case(s)</td></tr>")
        out.append("</table></div>")

    # ── failures ──
    if agg.failing_cases:
        out.append("<h2>Failures</h2><div class='panel'>")
        for f in agg.failing_cases:
            out.append(f"<div class='fail'><b>{escape(f['case_id'])}</b> "
                       f"<span class='muted'>({f['overall']*100:.0f}%)</span><br>"
                       f"<span class='muted'>{escape(f['question'])}</span>")
            for msg in f["failures"]:
                out.append(f"<br><code>{escape(str(msg))}</code>")
            for msg in f["errors"]:
                out.append(f"<br><span class='bad'>{escape(str(msg))}</span>")
            out.append("</div>")
        out.append("</div>")

    out.append("</div>")
    body = "".join(out)
    return f"<style>{CSS}</style>{body}"


def write_report(path: "str | Path", html: str, payload: dict) -> Path:
    """Write the HTML and the machine-readable JSON side by side."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    p.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str),
                                      encoding="utf-8")
    return p
