"""The single chat surface: one endpoint that routes a turn to the right agent.

The AI Insights page is being merged into the Assistant, so the Assistant must be
able to answer anything the user asks — normal conversation, an exact-number data
question, or "build me a dashboard" — without the user choosing a mode. This module
is that router.

Routing (cheap and deterministic — no LLM call just to decide whether to think):
    chart request   -> dashboard chart builder  (emits chart artifacts)
    data question   -> analytics agent          (exact numbers, markdown tables)
    everything else -> the user's primary agent (mail, calendar, docs, tasks)

Whichever path runs, the frames on the wire are the same (backend/chat/frames.py),
and any chart produced is emitted as an `artifact` frame AND persisted to
chat_artifacts so reopening the thread renders it exactly as it was.
"""
from __future__ import annotations

import logging
import re
import time

from backend.chat import frames as F
from backend.chat import store as chat_store

log = logging.getLogger("aganeti.chat.unified")

# ── routing heuristics ────────────────────────────────────────────────────────
# Deliberately conservative: when in doubt fall through to the primary agent,
# which can still answer conversationally. A false positive sends a chatty message
# to the SQL agent, which is the more annoying failure.

_CHART = re.compile(
    r"\b(dashboard|charts?|graphs?|plot|visuali[sz]e|pie chart|bar chart|line chart)\b"
    r"|\b(build|create|make|draw|generate|show)\b.{0,24}\b(dashboard|chart|graph)\b",
    re.I)

_DATA = re.compile(
    r"\b(how many|how much|total|sum|average|median|count|breakdown|distribution|"
    r"compare|comparison|versus|vs\b|trend|forecast|predict|projection|"
    r"top \d+|ranking|rank|per cent|percentage|share of|proportion|"
    r"list|enumerate|which \w+ (has|have|received|got)|"
    # grouping phrases — stems so plurals ("by emirates") match too
    r"(by|per|across) \w{0,6}(categor|emirate|status|month|year|nationalit|type|region|gender|quarter))\w*"
    # Arabic equivalents — one live thread is titled "معلومات عن معملات الزكاة"
    r"|(كم|إجمالي|متوسط|عدد|مقارنة|نسبة|توقع|قائمة|حسب)",
    re.I)

# Domain nouns that make a data question about THIS database rather than the
# user's own mailbox/calendar (which the primary agent owns).
# STEMS, not whole words: a trailing \b made "donation" match but "donations" miss,
# so plural/inflected forms silently fell through to the primary agent.
_DOMAIN = re.compile(
    r"\b(request|applicant|aid|grant|expenditur|spend|spent|beneficiar|approv|reject|"
    r"emirate|categor|zakat|donation|donor|case|charit|disburse|allocat)\w*"
    r"|(طلب|مساعدة|إنفاق|زكاة|تبرع|منح|مستفيد)",
    re.I)

# The primary agent owns these even when phrased like a data question
# ("how many emails do I have?" is an inbox question, not an analytics one).
_PERSONAL = re.compile(
    r"\b(my |me\b|inbox|email|e-mail|mail|calendar|meeting|schedule|task|todo|to-do|"
    r"reminder|draft|reply|document|upload|contact)\b", re.I)


def route(message: str) -> str:
    """'chart' | 'data' | 'primary'."""
    t = (message or "").strip()
    if not t:
        return "primary"
    if _PERSONAL.search(t) and not _CHART.search(t):
        return "primary"
    if _CHART.search(t):
        return "chart"
    if _DATA.search(t) and _DOMAIN.search(t):
        return "data"
    return "primary"


_STATUS_LABEL = {
    "get_database_schema": "Reading the data model",
    "query_data": "Querying the database",
    "run_metric": "Computing the metric",
    "list_metrics": "Listing available metrics",
    "save_chart": "Building the chart",
    "delete_chart": "Removing a chart",
    "forecast_metric": "Fitting the forecast",
    "compare_periods": "Comparing the periods",
    "current_time": "Checking the date",
}


# ── chart artifacts ───────────────────────────────────────────────────────────
async def _board_chart_ids(board_id: str) -> set[str]:
    """Chart ids currently on a board (used to diff what THIS turn created)."""
    import asyncio
    from backend.dashboard import config_db
    try:
        cfgs = await asyncio.to_thread(config_db.list_configs, board_id, False)
        return {c["id"] for c in (cfgs or [])}
    except Exception:
        return set()


async def _render_new_charts(board_id: str, before: set[str]) -> list[dict]:
    """Render charts created during this turn, newest last. Returns DashChart dicts."""
    import asyncio
    from backend.dashboard import config_db
    from backend.routes import dashboard as dash_routes
    try:
        cfgs = await asyncio.to_thread(config_db.list_configs, board_id, False)
    except Exception:
        return []
    fresh = [c for c in (cfgs or []) if c["id"] not in before]
    if not fresh:
        return []
    out = []
    for cfg in fresh:
        try:
            out.append(await dash_routes._render(cfg))
        except Exception:
            log.debug("artifact render failed for chart %s", cfg.get("id"), exc_info=True)
    return out


# ── the unified generator ─────────────────────────────────────────────────────
async def unified_stream(user_id: str, message: str, session_id: str,
                         images: list | None = None, model_key=None):
    """SSE generator for the merged Assistant. Yields `data: ...` strings."""
    lane = "primary" if images else route(message)   # a vision turn is never analytical
    t0 = time.time()

    yield F.sse(F.start(session_id, agent=lane))
    yield F.sse(F.stage("router", "done", label="Routing", summary=lane,
                        ms=int((time.time() - t0) * 1000)))

    if lane == "primary":
        from backend.routes import agent_os
        async for chunk in agent_os._sse(user_id, message, session_id, images):
            yield chunk
        return

    # ── analytics lanes ───────────────────────────────────────────────────────
    # The session IS the board, so charts land on this thread's board and the
    # analytics agent shares the thread's short-term memory.
    board_id = _board_id_for(session_id)
    before = await _board_chart_ids(board_id) if lane == "chart" else set()
    stage_name = "query" if lane == "data" else "analyst"
    yield F.sse(F.stage(stage_name, "running",
                        label="Analysing your data" if lane == "data" else "Building the dashboard"))

    final_text = ""
    artifact_ids: list[str] = []
    try:
        if lane == "chart":
            from backend.dashboard import stream as dash_stream
            gen = dash_stream.stream_dashboard(user_id, message, board_id, model_key, persist=False)
        else:
            from backend.dashboard import ask as dash_ask
            gen = dash_ask.ask_stream(user_id, message, None, model_key, session_id, persist=False)

        async for raw in gen:
            ev = _parse(raw)
            if ev is None:
                continue
            t = ev.get("type")
            if t == "tool_call":
                name = ev.get("name", "")
                yield F.sse(F.tool_call(name, stage_name=stage_name))
                yield F.sse(F.stage(stage_name, "running", label=_STATUS_LABEL.get(name, "Working")))
            elif t == "tool_result":
                yield F.sse(F.tool_result(ev.get("name", ""), bool(ev.get("ok", True)),
                                          stage_name=stage_name))
            elif t == "chart_saved":
                pass  # artifacts are emitted together once the turn settles
            elif t == "token":
                # /dashboard/* tokens are DELTAS; accumulate then re-emit whole so the
                # frame stays replace-semantics like every other surface.
                final_text += ev.get("content", "")
                yield F.sse(F.token(final_text))
            elif t == "done":
                final_text = ev.get("final") or final_text
            elif t == "error":
                yield F.sse(F.error(ev.get("message", "error"), stage_name=stage_name))
    except Exception as e:  # noqa: BLE001
        log.exception("unified stream failed")
        yield F.sse(F.error(str(e), stage_name=stage_name))

    yield F.sse(F.stage(stage_name, "done", ms=int((time.time() - t0) * 1000)))

    # Persist the turn, then attach any charts it produced.
    msg_id = None
    try:
        from backend.orchestrator import conversation as convo
        convo.append(user_id, session_id, "user", message)
        if final_text:
            msg_id = convo.append(user_id, session_id, "assistant", final_text,
                                  agent=("dashboard" if lane == "chart" else "analytics"))
    except Exception:
        log.debug("unified: persist failed", exc_info=True)

    if lane == "chart":
        for ch in await _render_new_charts(board_id, before):
            aid = chat_store.add_artifact(
                user_id, session_id, kind="chart", title=ch.get("title"),
                spec={"chart_id": ch.get("id"), "chart_type": ch.get("type"), "board_id": board_id},
                data=ch.get("data"), message_id=msg_id,
                meta={"error": ch.get("error")} if ch.get("error") else None,
            ) or ch.get("id")
            artifact_ids.append(aid)
            yield F.sse(F.artifact(id=aid, kind="chart", title=ch.get("title"),
                                   spec={"chart_id": ch.get("id"), "chart_type": ch.get("type")},
                                   data=ch.get("data"),
                                   meta={"error": ch.get("error")} if ch.get("error") else {}))

    yield F.sse(F.done(final_text, message_id=msg_id, artifacts=artifact_ids))

    # Long-term memory capture. The primary lane does this in routes/agent_os._sse,
    # but the analytics lanes return before that runs — which meant data questions
    # (most real usage) never contributed a fact. Fire-and-forget AFTER `done`.
    try:
        import asyncio as _aio
        from backend.chat import memory as _mem
        if _mem.worth_extracting(message):
            _aio.create_task(_mem.capture_turn(user_id, message, session_id))
    except Exception:  # noqa: BLE001
        log.debug("memory capture skipped", exc_info=True)

    yield F.DONE_SENTINEL


def _board_id_for(session_id: str) -> str:
    """The 32-hex board id for a thread. A session id IS the board id."""
    import uuid
    try:
        return uuid.UUID(str(session_id)).hex
    except (ValueError, TypeError, AttributeError):
        return uuid.uuid5(chat_store._NS, f"session:{session_id}").hex


def _parse(raw: str) -> dict | None:
    """Decode one 'data: {...}' line from a downstream generator."""
    import json
    if not raw or not raw.startswith("data:"):
        return None
    body = raw[len("data:"):].strip()
    if not body or body == "[DONE]":
        return None
    try:
        return json.loads(body)
    except Exception:
        return None
