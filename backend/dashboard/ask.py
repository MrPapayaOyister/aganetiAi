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
import time
import uuid

from backend.orchestrator import graph
from backend.dashboard.tools import register_dashboard_tools
from backend.dashboard.forecast_tools import register_forecast_tools
from backend.dashboard.compare_tools import register_compare_tools

log = logging.getLogger("aganeti.analytics")

# Ensure the shared read-only SQL tools + forecaster + period-comparator exist (idempotent).
register_dashboard_tools()
register_forecast_tools()
register_compare_tools()

# The /ask agent gets READ-ONLY data tools only — it answers in words, never builds or
# deletes charts. get_database_schema + query_data are SELECT-only against the real DB;
# current_time anchors relative dates like "this month".
ASK_TOOL_NAMES = ["get_database_schema", "query_data", "forecast_metric", "compare_periods", "current_time"]


from backend.dashboard.metric_contract import (  # noqa: E402
    metric_contract, no_frozen_numbers)


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
        "charity's aid-request data with EXACT numbers, in clear plain language.\n\n"
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
        "- PROCESSING TIME / how long requests take: use DataShare.VRequests.DurationInDays "
        "(with SubmitDate / FinishDate). Measured: it answers these in about 1-2 seconds.\n"
        "- DataShare.VRequestSteps — per-step workflow detail (1,278,765 rows): RequestID, "
        "Name (step), StartDate, FinishDate, DurationInDays, ProviderFullName, IsStepReturned. "
        "AVOID IT for aggregates: GROUP BY over this table times out at this database's service "
        "tier no matter which column you group by (a bare COUNT(*) is fine). Answer "
        "processing-time and bottleneck questions from VRequests instead. If a question truly "
        "needs per-step detail, filter to ONE request by RequestID rather than aggregating.\n"
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
        "3. Report the figure with its context. Money is AED with thousands separators "
        "(e.g. 'AED 1,234,567').\n\n"
        "FORMATTING - match the SHAPE of the answer to the SHAPE of the data:\n"
        "- ONE number -> a single sentence with the figure and its context.\n"
        "- A comparison of 2-4 items -> one or two sentences.\n"
        "- A breakdown, ranking, or time series of 5 OR MORE rows -> a GitHub-flavoured MARKDOWN "
        "TABLE: a header row, then a |---| separator row, then one row per record. Right-align "
        "numeric columns with |---:|. Never wrap the table in a code fence, and never emit HTML.\n"
        "- With a table, write ONE sentence above it saying what it shows and over what date range, "
        "and ONE sentence below it with the single most important takeaway.\n"
        "- Cap a table at 25 rows; if you truncate, say how many rows were omitted.\n\n"
        "DATA CAVEATS (know these):\n"
        "- Roughly one aid request in seven is approved. That ratio is orientation only: "
        "if the user asks how many were approved, or for the approval rate, COUNT it.\n"
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
        "- This system has ONLY aid-request (expenditure) data — NO donations, collections, or fundraising. If "
        "asked about money received/donated, say plainly it is not available here; never substitute "
        "expenditure for it.\n\n"
        "CANONICAL DEFINITIONS (compute ONCE with exactly these sources; do not re-run against another "
        "table to 'double-check' — it causes inconsistent answers):\n"
        + metric_contract() +
        "- A category's expenditure = the same approved join plus WHERE r.Category = '<name>'.\n"
        "- Approval rate = COUNT(*) of DataShare.VRequestsApproved / COUNT(*) of DataShare.VRequests.\n\n"
        + no_frozen_numbers() + "\n"
        f"FORECASTING RULES (this data spans about {span} months; current approval rate {rate}%; average "
        f"approved grant AED {avg:,}):\n"
        "- TIME-SERIES FORECAST: for 'next N months' / projections of expenditure, requests, or approved "
        "counts over time, CALL forecast_metric — it returns a proper trend + 95% prediction band + the "
        "history range. Report the point forecast AND the low95-high95 band, and name the method. Do NOT "
        "hand-roll a SQL trend for these.\n"
        "- PERIOD COMPARISON: for 'X this period vs last period' or 'compare X between two periods', CALL "
        "compare_periods (returns both values, the delta, and % change). State both figures and the change.\n"
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


# ── Analytics pipeline (Stage 1): declared figures ────────────────────────────
# Asking for the figures in the SAME response avoids a second LLM call. The block is
# stripped before the user sees the answer; it exists purely so every number can be
# recomputed from the recorded rows.
FIGURES_CONTRACT = (
    "\n\nEVIDENCE AND FIGURES (required):\n"
    "Each query_data result includes an \"evidence_id\" (q1, q2, ...). After your "
    "answer, append a fenced block listing EVERY number you stated:\n"
    "```figures\n"
    "[{\"label\": \"Treatment Fees\", \"value\": 45974876, \"unit\": \"AED\", "
    "\"evidence_id\": \"q1\", \"derivation\": \"cell\", \"column\": \"TotalExpenditure\"}]\n"
    "```\n"
    "- value MUST be a plain number: no commas, no currency symbol, no thousands words.\n"
    "- derivation is how the number came from that query: cell | sum | count | avg | "
    "max | min | ratio | delta | pct_change.\n"
    "- column is the source column when you know it.\n"
    "- Include every figure in your prose AND in any table. If a number is not in this "
    "block it will be flagged as unsourced.\n"
    "- The block is removed before the user sees your reply, so never refer to it."
)


def system_prompt_with_figures() -> str:
    return system_prompt() + FIGURES_CONTRACT


_TOOL_LABEL = {
    "query_data": "Querying the database",
    "get_database_schema": "Reading the schema",
    "run_query_cached": "Querying the database",
}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _pipeline_mode() -> str:
    """off | shadow | on — see backend/insight/evidence.py."""
    import os
    m = (os.getenv("INSIGHT_PIPELINE") or "off").strip().lower()
    return m if m in ("off", "shadow", "on") else "off"


def _init_state(user_id: str, message: str, history: list, model_key, run_id: str,
                tenant_id: str = "", session_id: str = "") -> dict:
    _prompt = system_prompt_with_figures() if _pipeline_mode() != "off" else system_prompt()
    msgs: list[dict] = [{"role": "system", "content": _prompt}]
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": message})
    # One state factory (§7.1). This used to be an AgentState literal, and was one
    # of the three the audit found — adding a field to AgentState without editing
    # here raised KeyError in the analytics lane, which has nothing to do with the
    # change that caused it. `run_id` is this lane's own key and is not part of
    # AgentState, so it is added after rather than pushed into the factory.
    return {**graph.new_state(
        messages=msgs, user_id=user_id, tenant_id=tenant_id, session_id=session_id,
        agent_id="analytics", allowed_tools=ASK_TOOL_NAMES, model_key=model_key,
        has_image=False,
    ), "run_id": run_id}


async def ask_stream(user_id: str, message: str, history: list | None = None, model_key=None,
                     session_id: str = "analytics", persist: bool = True):
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
    # NOT _mode: the astream loop below binds _mode to the LangGraph stream mode.
    _pmode = _pipeline_mode()
    _led = None
    if _pmode != "off":
        try:
            from backend.insight import evidence as _ev
            _led = _ev.start_ledger()
        except Exception:  # noqa: BLE001
            _led = None
    # Resolved HERE rather than passed in — see the same note in dashboard/stream.py.
    # "" is refused at the authorization boundary, which is the correct failure for
    # a turn whose tenant cannot be established.
    from backend.auth import tenant as _tenant
    _tenant_id = await _tenant.resolve_tenant_id(user_id)

    state = _init_state(user_id, message, hist, model_key, run_id, _tenant_id, session_id)
    cfg = {"recursion_limit": 4 * graph.STEP_BUDGET}
    final_text = ""
    # Stage timing. Every frame below is derived from work this turn actually does;
    # nothing here asks the model for anything extra.
    _t_stage = time.monotonic()
    _attempt = 1
    _step = None

    # _init_state has already run: it builds the system prompt via system_prompt(),
    # which calls get_forecast_context() -- real Azure round-trips on a cache miss.
    yield _sse({"type": "stage", "stage": "prepare", "status": "done",
                "label": "Preparing", "ms": int((time.monotonic() - _t_stage) * 1000),
                "detail": {"mode": _pmode}})

    async def _run_graph(st):
        """One full agent pass. Yields SSE frames; records the last assistant content."""
        nonlocal final_text, _t_stage
        _t_tool = None
        yield _sse({"type": "stage", "stage": "analyst", "status": "running",
                    "label": "Reading the question"})
        _t_stage = time.monotonic()
        async for _mode, chunk in graph.GRAPH.astream(st, cfg, stream_mode=["updates"]):
            for _node, upd in chunk.items():
                if not upd:
                    continue
                for m in upd.get("messages", []):
                    role = m.get("role")
                    if role == "assistant":
                        for tc in (m.get("tool_calls") or []):
                            _tname = tc["function"]["name"]
                            yield _sse({"type": "tool_call", "name": _tname})
                            _t_tool = time.monotonic()
                            yield _sse({"type": "stage", "stage": "query",
                                        "status": "running",
                                        "label": _TOOL_LABEL.get(_tname, "Working")})
                        content = m.get("content")
                        if content:
                            final_text = content
                            _shown = content
                            if _led is not None:
                                try:
                                    from backend.insight import evidence as _ev
                                    _shown, _ = _ev.extract_figures(content)
                                except Exception:  # noqa: BLE001
                                    _shown = content
                            yield _sse({"type": "token", "content": _shown})
                            yield _sse({"type": "stage", "stage": "analyst",
                                        "status": "done", "label": "Writing the answer",
                                        "ms": int((time.monotonic() - _t_stage) * 1000)})
                    elif role == "tool":
                        name = m.get("name", "")
                        content = str(m.get("content", ""))
                        _rows = None
                        _tool_ms = None
                        try:
                            _j = json.loads(content)
                            if isinstance(_j, dict):
                                _rows = _j.get("row_count")
                                # per-call truth, looked up by the id in the payload
                                _eid = _j.get("evidence_id")
                                if _eid and _led is not None:
                                    _r = _led.get(_eid)
                                    _tool_ms = getattr(_r, "ms", None) if _r else None
                            elif isinstance(_j, list):
                                _rows = len(_j)   # capped at 50 when no ledger: a floor
                        except Exception:  # noqa: BLE001 — a count is not worth a failure
                            pass
                        # The tool times itself; parallel calls arrive in one update, so
                        # a timer out here would report the batch for each of them.
                        _ms = _tool_ms if _tool_ms is not None else (
                            int((time.monotonic() - _t_tool) * 1000)
                            if _t_tool is not None else None)
                        _ok = not content.startswith("error")
                        _res = {"type": "tool_result", "name": name, "ok": _ok}
                        if _rows is not None:
                            _res["rows"] = _rows
                        if _ms is not None:
                            _res["ms"] = _ms
                        yield _sse(_res)
                        yield _sse({"type": "stage", "stage": "query",
                                    "status": "done" if _ok else "failed",
                                    "ms": _ms,
                                    "summary": (f"{_rows} row(s)" if _rows is not None
                                                else None),
                                    "detail": {"tool": name}})
                        _t_stage = time.monotonic()

    try:
        async for _frame in _run_graph(state):
            yield _frame

        # ── retry: a figure was stated but nothing was queried ────────────────
        # The ledger is empty only if query_data never ran. If the answer nonetheless
        # contains substantial numbers, they came from the prompt or from an earlier
        # turn, and neither is a measurement. Re-ask once, insisting on a query.
        if _led is not None and final_text and _led.is_empty():
            try:
                from backend.insight import evidence as _ev
                _p0, _ = _ev.extract_figures(final_text)
                if _ev._prose_numbers(_p0):
                    log.info("insight.retry: figures stated with no query; re-asking")
                    _attempt = 2
                    yield _sse({"type": "stage", "stage": "critic", "status": "retry",
                                "summary": "answer stated figures without querying",
                                "detail": {"attempt": _attempt}})
                    _retry_state = _init_state(user_id, message, hist, model_key, run_id,
                                               _tenant_id, session_id)
                    _retry_state["messages"].append({
                        "role": "system",
                        "content": ("Your previous answer stated a figure without running "
                                    "a query. Figures from earlier in the conversation or "
                                    "from your instructions are NOT measurements and may "
                                    "be stale. Call query_data now and answer only from "
                                    "its result."),
                    })
                    async for _frame in _run_graph(_retry_state):
                        yield _frame
            except Exception:  # noqa: BLE001 — a retry must never break the turn
                log.exception("insight: retry pass failed")
        # ── deterministic verification ────────────────────────────────────────
        _verdict = None
        if _led is not None and final_text:
            yield _sse({"type": "stage", "stage": "critic", "status": "running",
                        "label": "Verifying the figures"})
            _t_crit = time.monotonic()
            try:
                from backend.insight import evidence as _ev
                _prose, _figs = _ev.extract_figures(final_text)
                _res = _ev.verify(_prose, _figs, _led)
                _verdict = _res.as_dict()
                final_text = _prose
                log.info("insight.verify mode=%s ok=%s checked=%d issues=%s",
                         _pmode, _res.ok, _res.checked,
                         [i.kind for i in _res.issues])
                _st = _res.status
                _frame_status = {"verified": "done", "traced": "done",
                                 "unverified": "skipped"}.get(_st, "failed")
                _summary = {
                    "verified": f"{_res.checked} figure(s) recomputed",
                    "traced": f"{_res.traced} figure(s) traced to query results",
                    "unverified": "no figures to check",
                }.get(_st, f"{len(_res.issues)} issue(s)")
                yield _sse({"type": "stage", "stage": "critic",
                            "status": _frame_status, "summary": _summary,
                            "ms": int((time.monotonic() - _t_crit) * 1000),
                            "detail": _verdict})
                if _pmode == "on" and not _res.ok:
                    kinds = {i.kind for i in _res.issues}
                    if kinds & {"numeric_mismatch", "hallucinated_source",
                                "internal_inconsistency"}:
                        final_text += ("\n\n_Some figures above could not be reconciled "
                                       "against the query results — please treat them as "
                                       "provisional._")
            except Exception:  # noqa: BLE001 — verification must never break a turn
                log.exception("insight: verification failed")
            finally:
                try:
                    from backend.insight import evidence as _ev
                    _ev.clear_ledger()
                except Exception:  # noqa: BLE001
                    pass

        try:
            # persist=False when a caller (the unified chat router) owns persistence,
            # so an analytics turn is not written to the thread twice.
            if convo is not None and persist:
                convo.append(user_id, session_id, "user", message)
                if final_text:
                    convo.append(user_id, session_id, "assistant", final_text)
        except Exception:
            pass
        _done = {"type": "done", "final": final_text}
        if _verdict is not None:
            _st = _verdict.get("status")
            # True   every figure reconciled — recomputed ("verified") or traced back
            #        to a recorded value ("traced")
            # None   nothing numeric to check. NOT an assurance.
            # False  something did not reconcile.
            _done["verified"] = (True if _st in ("verified", "traced")
                                 else None if _st == "unverified" else False)
            _done["verification"] = _verdict
        yield _sse(_done)
    except Exception as e:  # noqa: BLE001
        log.exception("analytics ask stream failed")
        yield _sse({"type": "error", "message": str(e)})
    yield "data: [DONE]\n\n"
