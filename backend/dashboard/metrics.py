"""Curated, whitelisted analytics metrics over the Dar Al Ber CORE-SHARE aid-request DB.

The analytics agent NEVER writes free SQL for these — it picks a ``metric_id`` plus a
``period`` from fixed whitelists, and this module assembles a verified, hygiene-guarded
SELECT. This is the accuracy guarantee: a number is either one of these vetted queries
or it does not get stated.

Domain facts (verified against the live schema):
  - All money is the *approved* ``SuggestedAssistanceAmount`` (disbursed aid = "expenditure").
    There is NO donation/collection/income table in this database.
  - Amounts live in ``DataShare.VRequestAttributes`` and MUST be joined to the status/date
    views on RequestID (``VRequestsApproved.RequestID = VRequestAttributes.RequestId``).
  - ``SubmitDate``/``FinishDate`` are real datetimes; ``DecisionDate`` is char (avoid);
    never reference ``DurationIn*`` (full-table scan → timeout).

Every metric SQL:
  - is SELECT-only (re-checked by ``coreshare_db.validate_select_only``),
  - carries a ``WHERE 1=1`` base so the period fragment always appends with AND,
  - applies amount hygiene (``> 0 AND <= 1000000``) wherever it sums/averages money.

Periods are computed server-side with ``GETDATE()`` — no user-supplied date string is ever
interpolated — so this surface is injection-free by construction.
"""
from __future__ import annotations

from backend.dashboard import coreshare_db

# ── FROM clauses ──────────────────────────────────────────────────────────────
_APPROVED = ("DataShare.VRequestsApproved r "
             "JOIN DataShare.VRequestAttributes a ON r.RequestID = a.RequestId")
_ALL_WITH_ATTR = ("DataShare.VRequests r "
                  "LEFT JOIN DataShare.VRequestAttributes a ON r.RequestID = a.RequestId")
_ALL = "DataShare.VRequests r"
_HYGIENE = "a.SuggestedAssistanceAmount > 0 AND a.SuggestedAssistanceAmount <= 1000000"

# ── period fragments (constant strings; GETDATE() based; no interpolation) ─────
PERIODS: dict[str, str] = {
    "all": "",
    "today": "AND CAST(r.SubmitDate AS date) = CAST(GETDATE() AS date)",
    "yesterday": "AND CAST(r.SubmitDate AS date) = CAST(DATEADD(day, -1, GETDATE()) AS date)",
    "last_7_days": "AND r.SubmitDate >= DATEADD(day, -7, GETDATE())",
    "last_30_days": "AND r.SubmitDate >= DATEADD(day, -30, GETDATE())",
    "this_month": "AND r.SubmitDate >= DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)",
    "last_month": ("AND r.SubmitDate >= DATEFROMPARTS(YEAR(DATEADD(month, -1, GETDATE())), "
                   "MONTH(DATEADD(month, -1, GETDATE())), 1) "
                   "AND r.SubmitDate < DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)"),
    "this_year": "AND r.SubmitDate >= DATEFROMPARTS(YEAR(GETDATE()), 1, 1)",
}

# ── the metric whitelist ──────────────────────────────────────────────────────
# chart_type: kpi -> row {value}; bar/pie/line -> rows {x, y}. value_kind drives phrasing
# (currency = AED amount; count = number of requests).
METRICS: dict[str, dict] = {
    "total_expenditure": {
        "title": "Total expenditure (approved aid)",
        "description": "Sum of approved SuggestedAssistanceAmount — money disbursed as aid (AED).",
        "chart_type": "kpi", "value_kind": "currency",
        "sql": f"SELECT SUM(a.SuggestedAssistanceAmount) AS value FROM {_APPROVED} "
               f"WHERE 1=1 AND {_HYGIENE} {{period}}",
    },
    "average_grant": {
        "title": "Average approved grant",
        "description": "Average approved SuggestedAssistanceAmount per approved request (AED).",
        "chart_type": "kpi", "value_kind": "currency",
        "sql": f"SELECT AVG(a.SuggestedAssistanceAmount) AS value FROM {_APPROVED} "
               f"WHERE 1=1 AND {_HYGIENE} {{period}}",
    },
    "approved_requests": {
        "title": "Approved requests",
        "description": "Count of approved aid requests.",
        "chart_type": "kpi", "value_kind": "count",
        "sql": f"SELECT COUNT(*) AS value FROM DataShare.VRequestsApproved r WHERE 1=1 {{period}}",
    },
    "total_requests": {
        "title": "Total requests",
        "description": "Count of ALL aid requests, any status.",
        "chart_type": "kpi", "value_kind": "count",
        "sql": f"SELECT COUNT(*) AS value FROM {_ALL} WHERE 1=1 {{period}}",
    },
    "expenditure_by_category": {
        "title": "Expenditure by category",
        "description": "Approved aid amount grouped by service category (AED).",
        "chart_type": "bar", "value_kind": "currency",
        "sql": f"SELECT r.Category AS x, SUM(a.SuggestedAssistanceAmount) AS y FROM {_APPROVED} "
               f"WHERE 1=1 AND {_HYGIENE} {{period}} GROUP BY r.Category ORDER BY y DESC",
    },
    "expenditure_by_emirate": {
        "title": "Expenditure by emirate",
        "description": "Approved aid amount grouped by applicant emirate/State (AED).",
        "chart_type": "bar", "value_kind": "currency",
        "sql": f"SELECT a.State AS x, SUM(a.SuggestedAssistanceAmount) AS y FROM {_APPROVED} "
               f"WHERE 1=1 AND {_HYGIENE} {{period}} GROUP BY a.State ORDER BY y DESC",
    },
    "requests_by_status": {
        "title": "Requests by status",
        "description": "Count of requests grouped by status (Approved/Rejected/InProcess/Cancelled).",
        "chart_type": "pie", "value_kind": "count",
        "sql": f"SELECT r.RequestStatusName AS x, COUNT(*) AS y FROM {_ALL} "
               f"WHERE 1=1 {{period}} GROUP BY r.RequestStatusName ORDER BY y DESC",
    },
    "requests_by_category": {
        "title": "Requests by category",
        "description": "Count of requests grouped by service category.",
        "chart_type": "bar", "value_kind": "count",
        "sql": f"SELECT r.Category AS x, COUNT(*) AS y FROM {_ALL} "
               f"WHERE 1=1 {{period}} GROUP BY r.Category ORDER BY y DESC",
    },
    "requests_by_emirate": {
        "title": "Requests by emirate",
        "description": "Count of requests grouped by applicant emirate/State.",
        "chart_type": "bar", "value_kind": "count",
        "sql": f"SELECT a.State AS x, COUNT(*) AS y FROM {_ALL_WITH_ATTR} "
               f"WHERE 1=1 {{period}} GROUP BY a.State ORDER BY y DESC",
    },
    "monthly_request_trend": {
        "title": "Monthly request trend",
        "description": "Count of requests per calendar month (YYYY-MM) by SubmitDate.",
        "chart_type": "line", "value_kind": "count",
        "sql": f"SELECT CONVERT(char(7), r.SubmitDate, 126) AS x, COUNT(*) AS y FROM {_ALL} "
               f"WHERE 1=1 AND r.SubmitDate IS NOT NULL {{period}} "
               f"GROUP BY CONVERT(char(7), r.SubmitDate, 126) ORDER BY x",
    },
    "monthly_expenditure_trend": {
        "title": "Monthly expenditure trend",
        "description": "Approved aid amount per calendar month (YYYY-MM) by SubmitDate (AED).",
        "chart_type": "line", "value_kind": "currency",
        "sql": f"SELECT CONVERT(char(7), r.SubmitDate, 126) AS x, SUM(a.SuggestedAssistanceAmount) AS y "
               f"FROM {_APPROVED} WHERE 1=1 AND {_HYGIENE} AND r.SubmitDate IS NOT NULL {{period}} "
               f"GROUP BY CONVERT(char(7), r.SubmitDate, 126) ORDER BY x",
    },
}


# ── public API ────────────────────────────────────────────────────────────────
def list_metric_catalog() -> list[dict]:
    """The catalog the agent reads via list_metrics — no data, just what exists."""
    return [{"id": k, "title": v["title"], "description": v["description"],
             "chart_type": v["chart_type"], "value_kind": v["value_kind"]}
            for k, v in METRICS.items()]


def build_sql(metric_id: str, period: str = "all") -> str:
    if metric_id not in METRICS:
        raise ValueError(f"unknown metric_id '{metric_id}'. Valid: {', '.join(METRICS)}")
    if period not in PERIODS:
        raise ValueError(f"unknown period '{period}'. Valid: {', '.join(PERIODS)}")
    return METRICS[metric_id]["sql"].format(period=PERIODS[period])


def _confidence(chart_type: str, rows: list[dict]) -> dict:
    """Coarse data-completeness signal derived from the returned rows (not model-invented)."""
    if chart_type == "kpi":
        val = rows[0].get("value") if rows else None
        if not rows or val is None:
            return {"level": "low", "reasons": ["no matching records for this period"]}
        return {"level": "high", "reasons": ["exact aggregate over the full matched set"]}
    if not rows:
        return {"level": "low", "reasons": ["no matching records for this period"]}
    n = len(rows)
    null_x = sum(1 for r in rows if r.get("x") in (None, ""))
    reasons = [f"{n} group(s)"]
    level = "high" if n >= 2 else "medium"
    if null_x:
        reasons.append(f"{null_x} group(s) missing a label")
        if null_x / n > 0.2:
            level = "medium"
    return {"level": level, "reasons": reasons}


def run(metric_id: str, period: str = "all") -> dict:
    """Assemble + run one whitelisted metric. Returns rows + a computed confidence.

    Raises ValueError on a bad metric_id/period; lets coreshare_db errors propagate so the
    tool layer can render a friendly "warming/failed" message.
    """
    meta = METRICS.get(metric_id)
    if meta is None:
        raise ValueError(f"unknown metric_id '{metric_id}'. Valid: {', '.join(METRICS)}")
    sql = build_sql(metric_id, period)
    coreshare_db.validate_select_only(sql)  # defence in depth on top of the read-only grant
    rows = coreshare_db.run_query(sql)
    return {
        "metric_id": metric_id, "title": meta["title"], "chart_type": meta["chart_type"],
        "value_kind": meta["value_kind"], "period": period,
        "row_count": len(rows), "rows": rows, "confidence": _confidence(meta["chart_type"], rows),
    }
