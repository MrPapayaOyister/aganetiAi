"""Analytics agent SSE generator — the exact-number Q&A half of the dashboard.

Distinct from stream.stream_dashboard (which builds CHARTS): this agent answers in WORDS
with exact figures. It uses the SAME free-form read-only SQL engine as the chart builder
(get_database_schema + query_data against the real CORE-SHARE Azure DB), so it can answer
any question the data supports — cross-checks, ratios, time series, percentiles, and
multi-step calculations — not just a fixed metric whitelist. It is the backend of
POST /dashboard/ask.

SSE frames: token | tool_call | tool_result | done | error | [DONE].
"""
from __future__ import annotations

import json
import logging
import uuid

from backend.orchestrator import graph
from backend.dashboard.tools import register_dashboard_tools
from backend.dashboard.forecast_tools import register_forecast_tools

log = logging.getLogger("aganeti.analytics")

# Ensure the shared read-only SQL tools + the statistical forecaster exist (idempotent).
register_dashboard_tools()
register_forecast_tools()

# The /ask agent gets READ-ONLY data tools only — it answers in words, never builds or
# deletes charts. get_database_schema + query_data are SELECT-only against the real DB;
# current_time anchors relative dates like "this month".
ASK_TOOL_NAMES = ["get_database_schema", "query_data", "forecast_metric", "current_time"]


def system_prompt() -> str:
    try:
        from backend.dashboard import coreshare_db
        fc = coreshare_db.get_forecast_context()
    except Exception:
        fc = {"approval_rate": 14.1, "span_months": 19, "avg_grant": 14878}
    rate, span, avg = fc["approval_rate"], fc["span_months"], fc["avg_grant"]
    factor = max(1, round(100.0 / rate)) if rate else 7
    return (
        "You are the analytics assistant for Dar Al Ber Society staff. You answer questions about the "
        "charity's aid-request data with EXACT numbers, in plain warm language (a sentence or two).\n\n"
        "You have a read-only connection to the real aid-request database (Microsoft SQL Server / T-SQL). "
        "You answer by writing a SELECT query and calling query_data, then reporting the result.\n\n"
        "KEY TABLES (schema-qualified — call get_database_schema for the full list / other tables):\n"
        "- DataShare.VRequests — one row per aid request (~44k). Columns: RequestID, Category, ServiceName, "
        "ServiceType, RequestStatusName, SubmitDate, FinishDate, DurationInDays, SocialWorkerName, "
        "Country_in_arabic. RequestStatusName is one of: Approved, Rejected, InProcess, Cancelled.\n"
        "- DataShare.VRequestsApproved — the approved requests only (~6.2k, same columns).\n"
        "- DataShare.VRequestAttributes — one row per request with money + applicant fields. Columns: "
        "RequestId, State (=emirate), SuggestedAssistanceAmount (the AED amount granted), "
        "RequiredAmountForAssistance, NumberOfFamilyMembers, MaritalStatus, Gender, Nationality, Religion. "
        "Join to requests on VRequestAttributes.RequestId = VRequests.RequestID.\n"
        "- DataShare.VRequestSteps — workflow steps (~422k rows): RequestID, Name (step), StartDate, "
        "FinishDate, DurationInDays, ProviderFullName, IsStepReturned. Use for processing-time / bottleneck "
        "questions.\n"
        "Real emirates in State: Ajman, Dubai, Ras Al-Khaimah, Sharjah, Umm Al Quwain. "
        "Real categories: Study Fees, Home Rent, Treatment Fees, Other, Debt, Furnishing, Electricity Bills, "
        "Maintenance.\n\n"
        "HOW YOU WORK:\n"
        "0. The KEY TABLES above are usually everything you need — do NOT call get_database_schema unless "
        "you need a table or column that is NOT listed above (e.g. a dbo.* detail table). It is expensive.\n"
        "1. Do the arithmetic IN SQL for accuracy — COUNT, SUM, AVG, ratios with CAST(... AS float), "
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY col) OVER () for medians, DATEFROMPARTS(YEAR(d),MONTH(d),1) "
        "to group by month. Use TOP n (never LIMIT). One statement per query_data call (no semicolons).\n"
        "2. For a complex question, run SEVERAL queries and combine them — e.g. compare two periods, compute a "
        "rate as approved/total, or pull a breakdown then summarise the top items.\n"
        "3. Report the figure plainly with its context. Money is AED with thousands separators "
        "(e.g. 'AED 92,139,691'). Keep it to a sentence or two.\n\n"
        "DATA CAVEATS (know these):\n"
        "- Approval rate is ~14% (about 6,200 approved of ~44,000). 'Expenditure' = SUM of "
        "SuggestedAssistanceAmount, usually for approved requests.\n"
        "- SuggestedAssistanceAmount lives ONLY in DataShare.VRequestAttributes (VRequests and "
        "VRequestsApproved do NOT have an amount column), and it is 0 for many non-approved requests. "
        "For a total/average/median grant, JOIN VRequestAttributes to VRequestsApproved (approved only) "
        "or add WHERE SuggestedAssistanceAmount > 0 — otherwise the zeros distort it (median collapses "
        "to 0). Only include all requests if the user explicitly asks.\n"
        "- RequiredAmountForAssistance (amount REQUESTED, not granted) has messy outliers (some very "
        "large/invalid values). When you SUM or AVERAGE it, filter to a sensible range (BETWEEN 1 AND 1000000).\n"
        "- Nationality / Country values are FULL ISO names ('Syrian Arab Republic', 'Sudan', 'Egypt', "
        "'Pakistan', 'Bangladesh', ...). If the user names a nationality loosely ('Syrian', 'Sudanese'), "
        "match with LIKE '%Syria%' (the country STEM) — NEVER exact-equality a shortened form, or you will "
        "wrongly get 0. If unsure of the exact stored value, run a quick SELECT DISTINCT ... LIKE probe first.\n"
        "- Each row is an aid REQUEST, not a unique person (one applicant may file several requests). If the "
        "user says 'beneficiaries', you may treat it as requests, but note that distinction when it matters.\n"
        "- NetMonthlyIncome / CurrentSalary contain corrupt values (huge and negative) — do NOT report income "
        "statistics; if asked, say the income data is unreliable in this system.\n"
        "- Filter State to the real emirates above when breaking down by emirate (a few junk rows exist).\n"
        "- This system has ONLY aid-request (expenditure) data — NO donations, collections, or fundraising. If "
        "asked about money received/donated, say plainly it is not available here; never substitute "
        "expenditure for it.\n\n"
        "CANONICAL DEFINITIONS (compute ONCE with exactly these sources; do not re-run against another "
        "table to 'double-check' — it causes inconsistent answers):\n"
        "- Total (approved) expenditure = SUM(a.SuggestedAssistanceAmount) FROM DataShare.VRequestAttributes a "
        "JOIN DataShare.VRequestsApproved r ON r.RequestID = a.RequestId  (about AED 92.1M).\n"
        "- A category's expenditure = that same JOIN plus WHERE r.Category = '<name>'.\n"
        "- Approval rate = COUNT(*) of DataShare.VRequestsApproved / COUNT(*) of DataShare.VRequests.\n\n"
        f"FORECASTING RULES (this data spans about {span} months; current approval rate {rate}%; average "
        f"approved grant AED {avg:,}):\n"
        "- TIME-SERIES FORECAST: for 'next N months' / projections of expenditure, requests, or approved "
        "counts over time, CALL forecast_metric — it returns a proper trend + 95% prediction band + the "
        "history range. Report the point forecast AND the low95-high95 band, and name the method. Do NOT "
        "hand-roll a SQL trend for these.\n"
        f"- INTAKE -> APPROVED: when projecting approved cases or spend from a change in intake, you MUST "
        f"apply the approval rate. new_approved = new_intake x {rate}%; spend_impact = new_approved x "
        f"avg_grant. Do NOT multiply raw intake by the grant — only {rate}% get approved, so that overstates "
        f"the impact ~{factor}x.\n"
        "- MATCH WINDOWS: the growth-rate baseline window MUST equal the projection horizon. Projecting N "
        "months ahead -> use the LAST N months as the baseline; never compare a 6-month lookback to a "
        "3-month projection.\n"
        f"- SEASONALITY: with only ~{span} months of data (under 24), do NOT claim confirmed 'seasonality'. "
        "Call repeated highs a 'tentative pattern (under 2 years of data — not enough to confirm "
        "seasonality)', and always state the data date-range so the forecast is auditable.\n\n"
        "HARD RULES:\n"
        "- EVERY number you state MUST come from a query_data result. Never invent, guess, or estimate.\n"
        "- If a query errors or returns nothing, fix the SQL and retry (call get_database_schema if unsure of a "
        "name); if it still has no answer, say so honestly.\n"
        "- PRIVACY: report only aggregates (counts, sums, averages, rankings). NEVER reveal an individual "
        "applicant's personal details — name, ID number, phone, email or salary — even if asked directly.\n"
        "- Never mention SQL, tables, columns, tool names, or the word 'query' in your reply. The first data "
        "call may take a few seconds while the database wakes up."
    )


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _init_state(user_id: str, message: str, history: list, model_key, run_id: str) -> dict:
    msgs: list[dict] = [{"role": "system", "content": system_prompt()}]
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": message})
    return {
        "messages": msgs, "user_id": user_id, "agent_id": "analytics",
        "allowed_tools": ASK_TOOL_NAMES, "step": 0, "awaiting": None,
        "model_key": model_key, "fallback_models": [], "has_image": False,
        "run_id": run_id,
    }


async def ask_stream(user_id: str, message: str, history: list | None = None, model_key=None,
                     session_id: str = "analytics"):
    """Async generator of SSE 'data:' lines for POST /dashboard/ask.

    session_id keys the short-term memory: the frontend passes the board_id so /ask and
    /chat (which also keys by board_id) share ONE conversation thread — follow-ups like
    'what about just Ajman?' or 'now chart that' resolve across both."""
    convo = None
    hist = history or []
    if history is None:
        try:
            from backend.orchestrator import conversation as convo
            hist = convo.load(user_id, session_id)
        except Exception:
            convo = None

    run_id = uuid.uuid4().hex
    state = _init_state(user_id, message, hist, model_key, run_id)
    cfg = {"recursion_limit": 4 * graph.STEP_BUDGET}
    final_text = ""
    try:
        async for _mode, chunk in graph.GRAPH.astream(state, cfg, stream_mode=["updates"]):
            for _node, upd in chunk.items():
                if not upd:
                    continue
                for m in upd.get("messages", []):
                    role = m.get("role")
                    if role == "assistant":
                        for tc in (m.get("tool_calls") or []):
                            yield _sse({"type": "tool_call", "name": tc["function"]["name"]})
                        content = m.get("content")
                        if content:
                            final_text = content
                            yield _sse({"type": "token", "content": content})
                    elif role == "tool":
                        name = m.get("name", "")
                        content = str(m.get("content", ""))
                        yield _sse({"type": "tool_result", "name": name,
                                    "ok": not content.startswith("error")})
        try:
            if convo is not None:
                convo.append(user_id, session_id, "user", message)
                if final_text:
                    convo.append(user_id, session_id, "assistant", final_text)
        except Exception:
            pass
        yield _sse({"type": "done", "final": final_text})
    except Exception as e:  # noqa: BLE001
        log.exception("analytics ask stream failed")
        yield _sse({"type": "error", "message": str(e)})
    yield "data: [DONE]\n\n"
