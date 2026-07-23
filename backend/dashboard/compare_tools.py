"""compare_periods tool: compare a metric between two named periods, returning both
values + delta + % change (consistent period definitions, so the agent doesn't hand-roll)."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

from backend.orchestrator.registry import Tool, register
from backend.dashboard import coreshare_db

_METRIC = {
    "expenditure": ("SELECT SUM(a.SuggestedAssistanceAmount) AS v FROM DataShare.VRequestAttributes a "
                    "JOIN DataShare.VRequestsApproved r ON r.RequestID = a.RequestId "
                    "WHERE r.SubmitDate >= '{s}' AND r.SubmitDate < '{e}'"),
    "requests": "SELECT COUNT(*) AS v FROM DataShare.VRequests WHERE SubmitDate >= '{s}' AND SubmitDate < '{e}'",
    "approved": "SELECT COUNT(*) AS v FROM DataShare.VRequestsApproved WHERE SubmitDate >= '{s}' AND SubmitDate < '{e}'",
}
_PERIODS = ["this_month", "last_month", "this_quarter", "last_quarter",
            "this_year", "last_year", "last_30_days", "prev_30_days"]


def _range(name: str, today: date):
    y, m = today.year, today.month
    fo = lambda yy, mm: date(yy, mm, 1)
    nxt = today + timedelta(days=1)
    if name == "last_month":
        pm = fo(y, m) - timedelta(days=1); return fo(pm.year, pm.month), fo(y, m)
    if name == "this_year":
        return date(y, 1, 1), nxt
    if name == "last_year":
        return date(y - 1, 1, 1), date(y, 1, 1)
    if name == "last_30_days":
        return today - timedelta(days=30), nxt
    if name == "prev_30_days":
        return today - timedelta(days=60), today - timedelta(days=30)
    if name in ("this_quarter", "last_quarter"):
        qs = ((m - 1) // 3) * 3 + 1
        if name == "this_quarter":
            return fo(y, qs), nxt
        pe = fo(y, qs); ps = pe - timedelta(days=1); pqs = ((ps.month - 1) // 3) * 3 + 1
        return fo(ps.year, pqs), pe
    return fo(y, m), nxt  # this_month


async def _compare_periods(ctx, metric: str = "expenditure",
                           period_a: str = "this_month", period_b: str = "last_month") -> str:
    metric = (metric or "expenditure").lower()
    if metric not in _METRIC:
        metric = "expenditure"

    def _q(sql):
        return coreshare_db.run_query(sql)

    def _work():
        row = _q("SELECT CAST(GETDATE() AS date) AS d")[0]["d"]
        today = row if isinstance(row, date) else date.fromisoformat(str(row)[:10])

        def val(period):
            p = period if period in _PERIODS else "this_month"
            s, e = _range(p, today)
            r = _q(_METRIC[metric].format(s=s.isoformat(), e=e.isoformat()))
            return float(r[0]["v"] or 0)
        return val(period_a), val(period_b)

    try:
        a, b = await asyncio.to_thread(_work)
    except Exception as ex:  # noqa: BLE001
        return f"error: could not compare periods: {str(ex)[:160]}"

    delta = a - b
    pct = (delta / b * 100) if b else None
    out = {
        "metric": metric, "unit": "AED" if metric == "expenditure" else "count",
        "a": {"period": period_a, "value": round(a)},
        "b": {"period": period_b, "value": round(b)},
        "delta": round(delta), "pct_change": round(pct, 1) if pct is not None else None,
        "note": "By SubmitDate; the current period may be partial. 'requests'/'approved' count requests, "
                "not unique people.",
    }
    return json.dumps(out, default=str)


_TOOLS = [
    Tool("compare_periods",
         "Compare a metric between two time periods; returns both values + delta + % change (consistent "
         "period definitions). metric: 'expenditure'|'requests'|'approved'. period_a/period_b one of: "
         "this_month, last_month, this_quarter, last_quarter, this_year, last_year, last_30_days, "
         "prev_30_days. USE THIS for any 'X vs Y period' / 'this month vs last month' comparison.",
         {"type": "object", "properties": {
             "metric": {"type": "string", "enum": ["expenditure", "requests", "approved"]},
             "period_a": {"type": "string"}, "period_b": {"type": "string"}}},
         _compare_periods, "charts.read"),
]

COMPARE_TOOL_NAMES = [t.name for t in _TOOLS]
_registered = False


def register_compare_tools() -> list[str]:
    global _registered
    if not _registered:
        for t in _TOOLS:
            register(t)
        _registered = True
    return COMPARE_TOOL_NAMES
