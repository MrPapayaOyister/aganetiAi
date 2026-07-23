"""forecast_metric tool: statistically-grounded monthly forecast for the /ask agent.
Pulls the monthly series, drops the current (partial) month, fits forecast.forecast_series,
and returns the point forecast + 95% band + data vintage + honest caveats."""
from __future__ import annotations

import asyncio
import json

from backend.orchestrator.registry import Tool, register
from backend.dashboard import coreshare_db, forecast

_METRIC_SQL = {
    "requests": "SELECT FORMAT(SubmitDate,'yyyy-MM') AS m, COUNT(*) AS v "
                "FROM DataShare.VRequests GROUP BY FORMAT(SubmitDate,'yyyy-MM') ORDER BY m",
    "approved": "SELECT FORMAT(SubmitDate,'yyyy-MM') AS m, COUNT(*) AS v "
                "FROM DataShare.VRequestsApproved GROUP BY FORMAT(SubmitDate,'yyyy-MM') ORDER BY m",
    "expenditure": "SELECT FORMAT(v.SubmitDate,'yyyy-MM') AS m, SUM(a.SuggestedAssistanceAmount) AS v "
                   "FROM DataShare.VRequestAttributes a JOIN DataShare.VRequestsApproved v "
                   "ON v.RequestID = a.RequestId GROUP BY FORMAT(v.SubmitDate,'yyyy-MM') ORDER BY m",
}


async def _forecast_metric(ctx, metric: str = "expenditure", horizon_months: int = 3) -> str:
    metric = (metric or "expenditure").lower().strip()
    if metric not in _METRIC_SQL:
        metric = "expenditure"
    horizon = max(1, min(12, int(horizon_months or 3)))

    def _run():
        return coreshare_db.run_query(_METRIC_SQL[metric])
    try:
        rows = await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        return f"error: could not load the monthly series: {str(e)[:160]}"

    months = [r["m"] for r in rows]
    vals = [float(r["v"] or 0) for r in rows]
    # Drop the current (partial) month so it doesn't look like a false decline.
    partial = None
    if len(months) >= 5:
        partial, months, vals = months[-1], months[:-1], vals[:-1]

    res = forecast.forecast_series(vals, horizon)
    if "error" in res:
        return json.dumps(res)
    res.update({
        "metric": metric,
        "unit": "AED" if metric == "expenditure" else "count",
        "history_range": f"{months[0]} .. {months[-1]}" if months else "",
        "excluded_partial_month": partial,
        "caveats": ("Trend-only projection: with under 2 years of data, seasonality is NOT modelled "
                    "(any monthly highs are a tentative pattern, not confirmed seasonality). The band is a "
                    "95% prediction interval from residual scatter and widens further out. 'requests'/"
                    "'approved' count aid REQUESTS filed, not unique beneficiaries."),
    })
    if res.get("trend_strength") == "none":
        res["caveats"] = ("IMPORTANT: no significant trend was detected (r2 near 0) — present the point as a "
                          "rough recent AVERAGE with wide uncertainty, NOT a directional forecast. "
                          + res["caveats"])
    return json.dumps(res, default=str)[:4000]


_TOOLS = [
    Tool("forecast_metric",
         "Statistically forecast a monthly metric with a 95% prediction band (linear trend + residual "
         "interval, data-vintage aware). metric is 'expenditure' | 'requests' | 'approved'; horizon_months "
         "1-12. USE THIS for any forecast / projection / 'next N months' / 'expected' question instead of "
         "hand-rolling extrapolation in SQL. Report the point forecast AND the low95-high95 band, name the "
         "method, and state the history range.",
         {"type": "object", "properties": {
             "metric": {"type": "string", "enum": ["expenditure", "requests", "approved"]},
             "horizon_months": {"type": "integer"}}},
         _forecast_metric, "charts.read"),
]

FORECAST_TOOL_NAMES = [t.name for t in _TOOLS]
_registered = False


def register_forecast_tools() -> list[str]:
    global _registered
    if not _registered:
        for t in _TOOLS:
            register(t)
        _registered = True
    return FORECAST_TOOL_NAMES
