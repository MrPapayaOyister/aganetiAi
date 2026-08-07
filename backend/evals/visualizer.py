"""
Chart helpers — inline SVG, zero dependencies.

No matplotlib, no CDN. The report has to open from a file:// URL on a machine
with no network and no extra packages, so every chart is hand-rolled SVG
embedded in the HTML. That constraint also keeps the report a single portable
artefact you can attach to a PR.
"""
from __future__ import annotations

from html import escape
from typing import Iterable, Optional, Sequence

PALETTE = ["#4C8DFF", "#37C978", "#FFB020", "#FF6B6B", "#A78BFA",
           "#22D3EE", "#F472B6", "#94A3B8"]


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.2f}".rstrip("0").rstrip(".")


def bar_chart(labels: Sequence[str], values: Sequence[float], *,
              width: int = 560, bar_h: int = 22, gap: int = 8,
              max_value: Optional[float] = None, suffix: str = "",
              colors: Optional[Sequence[str]] = None) -> str:
    """Horizontal bars — the honest default for comparing named quantities."""
    if not labels:
        return "<p class='muted'>no data</p>"
    top = max_value if max_value is not None else (max(values) or 1.0)
    label_w, pad = 170, 60
    plot_w = width - label_w - pad
    height = len(labels) * (bar_h + gap) + gap
    parts = [f"<svg viewBox='0 0 {width} {height}' width='100%' "
             f"style='max-width:{width}px' role='img'>"]
    for i, (lab, val) in enumerate(zip(labels, values)):
        y = gap + i * (bar_h + gap)
        w = max(1, int(plot_w * (val / top))) if top else 1
        color = (colors[i] if colors and i < len(colors) else PALETTE[i % len(PALETTE)])
        parts.append(
            f"<text x='0' y='{y + bar_h * 0.72}' class='svg-lab'>{escape(str(lab))[:26]}</text>"
            f"<rect x='{label_w}' y='{y}' width='{w}' height='{bar_h}' rx='4' fill='{color}'/>"
            f"<text x='{label_w + w + 6}' y='{y + bar_h * 0.72}' class='svg-val'>"
            f"{_fmt(val)}{suffix}</text>")
    parts.append("</svg>")
    return "".join(parts)


def line_chart(x: Sequence[float], series: "dict[str, Sequence[float]]", *,
               width: int = 620, height: int = 240, y_max: Optional[float] = None,
               x_label: str = "", y_label: str = "") -> str:
    """Multi-series line — used for the threshold sweep."""
    if not x or not series:
        return "<p class='muted'>no data</p>"
    pad_l, pad_b, pad_t, pad_r = 46, 30, 14, 90
    plot_w, plot_h = width - pad_l - pad_r, height - pad_b - pad_t
    top = y_max if y_max is not None else max(
        (max(v) for v in series.values() if v), default=1.0) or 1.0
    xmin, xmax = min(x), max(x)
    span = (xmax - xmin) or 1.0

    def px(v): return pad_l + plot_w * (v - xmin) / span
    def py(v): return pad_t + plot_h * (1 - (v / top))

    parts = [f"<svg viewBox='0 0 {width} {height}' width='100%' "
             f"style='max-width:{width}px' role='img'>"]
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h * frac
        parts.append(f"<line x1='{pad_l}' y1='{y:.1f}' x2='{pad_l+plot_w}' y2='{y:.1f}' "
                     f"class='svg-grid'/>"
                     f"<text x='{pad_l-6}' y='{y+4:.1f}' class='svg-tick' "
                     f"text-anchor='end'>{_fmt(top*(1-frac))}</text>")
    for i, (name, ys) in enumerate(series.items()):
        color = PALETTE[i % len(PALETTE)]
        pts = " ".join(f"{px(xv):.1f},{py(yv):.1f}" for xv, yv in zip(x, ys))
        parts.append(f"<polyline points='{pts}' fill='none' stroke='{color}' "
                     f"stroke-width='2' stroke-linejoin='round'/>")
        for xv, yv in zip(x, ys):
            parts.append(f"<circle cx='{px(xv):.1f}' cy='{py(yv):.1f}' r='2.5' fill='{color}'/>")
        parts.append(f"<text x='{pad_l+plot_w+8}' y='{py(ys[-1])+4:.1f}' "
                     f"class='svg-legend' fill='{color}'>{escape(name)}</text>")
    for xv in x:
        parts.append(f"<text x='{px(xv):.1f}' y='{height-8}' class='svg-tick' "
                     f"text-anchor='middle'>{_fmt(xv)}</text>")
    if x_label:
        parts.append(f"<text x='{pad_l+plot_w/2:.0f}' y='{height-0}' "
                     f"class='svg-axis' text-anchor='middle'>{escape(x_label)}</text>")
    if y_label:
        parts.append(f"<text x='12' y='{pad_t+plot_h/2:.0f}' class='svg-axis' "
                     f"transform='rotate(-90 12 {pad_t+plot_h/2:.0f})' "
                     f"text-anchor='middle'>{escape(y_label)}</text>")
    parts.append("</svg>")
    return "".join(parts)


def stacked_latency(stages: "dict[str, float]", *, width: int = 620,
                    height: int = 54) -> str:
    """One stacked bar showing where a turn's milliseconds go."""
    total = sum(stages.values()) or 1.0
    parts = [f"<svg viewBox='0 0 {width} {height}' width='100%' "
             f"style='max-width:{width}px' role='img'>"]
    x = 0.0
    for i, (name, val) in enumerate(stages.items()):
        w = width * (val / total)
        color = PALETTE[i % len(PALETTE)]
        parts.append(f"<rect x='{x:.1f}' y='6' width='{max(w,0.5):.1f}' height='24' "
                     f"fill='{color}'><title>{escape(name)}: {val:.1f}ms "
                     f"({val/total*100:.0f}%)</title></rect>")
        if w > 46:
            parts.append(f"<text x='{x+w/2:.1f}' y='22' class='svg-inbar' "
                         f"text-anchor='middle'>{val:.0f}</text>")
        x += w
    legend = " ".join(
        f"<span class='key'><i style='background:{PALETTE[i%len(PALETTE)]}'></i>"
        f"{escape(n)}</span>" for i, n in enumerate(stages))
    parts.append(f"</svg><div class='legend'>{legend}</div>")
    return "".join(parts)


def confusion_matrix(tp: int, fp: int, fn: int, tn: int = 0, *,
                     labels: tuple = ("expected", "not expected")) -> str:
    """2×2 retrieval confusion matrix.

    `tn` is usually meaningless for retrieval (the set of documents NOT expected
    and NOT retrieved is unbounded), so it renders as "—" unless supplied."""
    def cell(v, cls=""):
        return f"<td class='cm {cls}'>{'—' if v is None else v}</td>"
    return (
        "<table class='matrix'>"
        f"<tr><th></th><th>retrieved</th><th>not retrieved</th></tr>"
        f"<tr><th>{escape(labels[0])}</th>{cell(tp,'good')}{cell(fn,'bad')}</tr>"
        f"<tr><th>{escape(labels[1])}</th>{cell(fp,'bad')}{cell(tn or None)}</tr>"
        "</table>")


def sparkline(values: Iterable[float], *, width: int = 120, height: int = 24) -> str:
    vals = list(values)
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = width / (len(vals) - 1)
    pts = " ".join(f"{i*step:.1f},{height-(v-lo)/span*height:.1f}"
                   for i, v in enumerate(vals))
    return (f"<svg viewBox='0 0 {width} {height}' width='{width}' height='{height}'>"
            f"<polyline points='{pts}' fill='none' stroke='{PALETTE[0]}' "
            f"stroke-width='1.5'/></svg>")
