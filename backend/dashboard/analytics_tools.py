"""The analytics agent's tools: list_metrics + run_metric.

These wrap the curated ``metrics`` whitelist as registry Tools. Like the 5 chart tools,
they are registered into the shared registry but kept OUT of templates.PRIMARY_TOOLS, so
the primary Assistant (Aria) can never run analytics — only the /dashboard/ask agent, which
supplies ``ANALYTICS_TOOL_NAMES`` as its own allow-list.

Every run_metric call is audited through agenticAi's ``events`` spine
(kind='tool_called') with the metric, period, row count, computed confidence, and the
per-run ``run_id`` (minted by ask.ask_stream — the agent_runs table is currently unused,
so the run id rides in ctx and lands in events.meta, not an agent_runs row).
"""
from __future__ import annotations

import asyncio
import json

from backend.orchestrator.registry import Tool, register
from backend.dashboard import metrics

_PERIOD_ENUM = list(metrics.PERIODS.keys())


async def _list_metrics(ctx) -> str:
    cat = metrics.list_metric_catalog()
    lines = [f'- {m["id"]} [{m["chart_type"]}] — {m["title"]}: {m["description"]}' for m in cat]
    return ("Available metrics (call run_metric with one id):\n" + "\n".join(lines)
            + "\n\nPeriods you can pass: " + ", ".join(_PERIOD_ENUM))


async def _run_metric(ctx, metric_id: str, period: str = "all") -> str:
    try:
        res = await asyncio.to_thread(metrics.run, metric_id, period)
    except ValueError as e:  # bad metric_id / period — the model can correct itself
        return f"error: {e}"
    except Exception as e:  # noqa: BLE001 — Azure warming / relay drop / query failure
        return ("error: the data source did not answer (it may be waking up). "
                f"Run the SAME metric again in a moment. [{str(e)[:160]}]")
    # best-effort audit through the events spine (never raises into the turn)
    try:
        from backend import events
        events.log_event("tool_called", user_id=ctx.get("user_id"), name="run_metric",
                         success=True, meta={"agent_id": ctx.get("agent_id"), "run_id": ctx.get("run_id"),
                                             "metric_id": metric_id, "period": period,
                                             "row_count": res["row_count"],
                                             "confidence": res["confidence"]["level"]})
    except Exception:
        pass
    return json.dumps(res, default=str)[:5000]


_TOOLS = [
    Tool("list_metrics",
         "List the curated analytics metrics you can run (id, chart type, description). Call this "
         "FIRST whenever you are unsure which metric answers the question — never guess a metric id.",
         {"type": "object", "properties": {}},
         _list_metrics, "charts.read"),
    Tool("run_metric",
         "Run ONE curated metric and get the exact numbers back. metric_id must come from "
         "list_metrics. period is one of all/today/yesterday/last_7_days/last_30_days/this_month/"
         "last_month/this_year. Returns the rows plus a confidence level. EVERY number you state "
         "to the user MUST come from a run_metric result — never invent or estimate one.",
         {"type": "object", "properties": {
             "metric_id": {"type": "string"},
             "period": {"type": "string", "enum": _PERIOD_ENUM}},
          "required": ["metric_id"]},
         _run_metric, "charts.read"),
]

# current_time is already registered globally; include it so the agent can anchor "yesterday".
ANALYTICS_TOOL_NAMES = [t.name for t in _TOOLS] + ["current_time"]

_registered = False


def register_analytics_tools() -> list[str]:
    """Idempotently register the analytics tools. Called at dashboard-router import time so a
    missing Azure driver only breaks the dashboard router, never the core registry import."""
    global _registered
    if not _registered:
        for t in _TOOLS:
            register(t)
        _registered = True
    return ANALYTICS_TOOL_NAMES
