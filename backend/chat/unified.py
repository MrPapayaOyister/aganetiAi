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
# NOUNS ONLY. A bare "me"/"my" used to live here, which meant the everyday phrasings
# "give me the total", "show me the breakdown" and "tell me how many" were all read as
# questions about the user's own mailbox and sent to the primary agent, which then
# replied that it had no access to expenditure records. The pronoun says nothing about
# whose data is wanted; the noun does.
_PERSONAL = re.compile(
    r"\b(inbox|email|e-mail|mail|calendar|meeting|schedule|task|todo|to-do|"
    r"reminder|draft|reply|document|upload|contact)\b", re.I)


# POC-3 is EXPLICITLY OPT-IN, not inferred. A heuristic here would silently move
# existing traffic onto a new execution path, and "existing chat behaviour is
# unchanged for non-POC-3 requests" is only checkable if the trigger cannot fire
# by accident. A leading `/agent` (or `/verify`) is something no ordinary question
# produces, so the answer to "did this change my chat?" is provably no.
_AGENT_PREFIX = re.compile(r"^\s*/(agent|verify)\b[:\s]*", re.I)


def strip_agent_prefix(message: str) -> str:
    """The question with the opt-in marker removed — what the agent actually answers."""
    return _AGENT_PREFIX.sub("", message or "", count=1).strip()


def route(message: str) -> str:
    """'agent' | 'chart' | 'data' | 'primary'."""
    t = (message or "").strip()
    if not t:
        return "primary"
    # Checked FIRST and by prefix only: an explicit request must not be
    # re-interpreted by the heuristics below, and the heuristics must not be
    # able to claim a turn the user explicitly routed.
    if _AGENT_PREFIX.match(t):
        return "agent"
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


# ── POC-3 agent lane ──────────────────────────────────────────────────────────
# The plan→knowledge→draft→verify→finalize loop (backend/agents), surfaced over
# the SSE vocabulary that already exists in backend/chat/frames.py. NO new event
# types: pipeline steps are `stage` frames and the structured payloads are
# `artifact` frames, which is what those two frames are for. A new type would
# oblige every existing consumer to learn it.
#
# WHAT IS DELIBERATELY NOT EMITTED: the drafted answer before verification, the
# prompts, and any model reasoning. A client renders what the run PRODUCED —
# plan, evidence, verdict, answer — never how the model got there.

#: Customer-facing stage labels. The loop's node names are an implementation
#: detail; these are what a person reads.
_AGENT_STAGE_LABEL = {
    "plan": "Planning",
    "knowledge": "Searching knowledge",
    "draft": "Drafting the answer",
    "verify": "Checking the evidence",
    "finalize": "Finishing",
}

#: Verdict → what the UI should say. Kept here rather than in the frontend so the
#: wording cannot drift from the verdict that produced it.
_VERDICT_LABEL = {
    "SUPPORTED": "Supported by the evidence",
    "PARTIALLY_SUPPORTED": "Partially supported",
    "UNSUPPORTED": "Not supported by the evidence",
    "CONFLICTING": "The evidence conflicts",
}


def _public_citation(c: dict) -> dict:
    """One citation, reduced to what a customer UI should see.

    Scores, corroboration counts and the raw Evidence records stay server-side:
    they are retrieval diagnostics, and the brief is explicit that store
    internals are not exposed unless the UI already shows them. `provider` is
    mapped to a plain word for the same reason.
    """
    return {
        "kind": c.get("kind") or "document",
        "source": c.get("source") or c.get("document_id") or "",
        "text": " ".join((c.get("text") or "").split())[:400],
        "where": "document" if c.get("provider") == "corporate" else "knowledge graph",
    }


async def _agent_lane(user_id: str, message: str, session_id: str):
    """Run the POC-3 loop and stream it as stage + artifact + done frames."""
    import backend.chat.frames as F
    from backend.agents import run_agent_loop

    question = strip_agent_prefix(message)
    if not question:
        yield F.sse(F.error("Ask a question after /agent.", stage_name="plan"))
        yield F.DONE_SENTINEL
        return

    # TENANT: resolved from the AUTHENTICATED user, never from the message body.
    # Same call the dashboard lane already uses, so this introduces no second
    # tenant mechanism. A failure to resolve yields "" — which knowledge_search
    # treats as "no predicate", exactly as it does for the eval harness — rather
    # than a guessed tenant.
    tenant_id = ""
    try:
        from backend.auth import tenant as _tenant
        tenant_id = await _tenant.resolve_tenant_id(user_id)
    except Exception:  # noqa: BLE001 — a directory outage must not fail the turn
        log.warning("agent lane: could not resolve a tenant for %s", user_id)

    for node, label in _AGENT_STAGE_LABEL.items():
        if node == "plan":
            yield F.sse(F.stage(node, "running", label=label))
            break

    t0 = time.time()
    try:
        state = await run_agent_loop(request=question, user_id=user_id,
                                     tenant_id=tenant_id, session_id=session_id,
                                     agent_id="agent")
    except Exception as e:  # noqa: BLE001
        log.exception("agent lane failed")
        yield F.sse(F.error("The agent could not complete this request.",
                            stage_name="plan", recoverable=True))
        yield F.sse(F.done("I could not complete this request."))
        yield F.DONE_SENTINEL
        return

    # ── stages, from the trace the loop already recorded ──────────────────────
    stages: list[dict] = []
    for entry in state.get("trace") or []:
        node = entry.get("agent", "")
        # `planner`/`verification` are the AGENT names; the node names differ.
        key = {"planner": "plan", "verification": "verify",
               "finalize": "finalize"}.get(node, node)
        label = _AGENT_STAGE_LABEL.get(key, key.replace("_", " ").title())
        status = {"ok": "done", "skipped": "skipped",
                  "insufficient": "done", "failed": "error"}.get(entry.get("status"), "done")
        frame = F.stage(key, status, label=label, ms=int(entry.get("took_ms") or 0))
        stages.append(frame)
        yield F.sse(frame)

    plan = state.get("plan") or {}
    evidence = state.get("evidence") or {}
    verification = state.get("verification") or {}
    citations = [_public_citation(c) for c in (evidence.get("citations") or [])]

    # ── artifacts: what it planned, what it found, what it concluded ──────────
    yield F.sse(F.artifact(
        id=f"{session_id}:plan", kind="agent_plan", title="Plan",
        data={"goal": plan.get("goal", ""),
              "steps": [{"id": s.get("id"), "agent": s.get("agent"),
                         "task": s.get("task")} for s in (plan.get("steps") or [])]}))

    yield F.sse(F.artifact(
        id=f"{session_id}:evidence", kind="agent_evidence", title="Evidence",
        data={"citations": citations, "counts": evidence.get("counts") or {}},
        # `tenant_scoped` rather than the org id: whether isolation applied is
        # useful to show, the tenant's identifier is not.
        meta={"tenant_scoped": bool(evidence.get("tenant_enforced_by"))}))

    verdict = str(verification.get("verdict") or "UNSUPPORTED")
    yield F.sse(F.artifact(
        id=f"{session_id}:verification", kind="agent_verification", title="Verification",
        data={"verdict": verdict,
              "label": _VERDICT_LABEL.get(verdict, verdict.replace("_", " ").title()),
              "explanation": verification.get("explanation") or "",
              "missing": verification.get("missing") or [],
              "conflicts": verification.get("conflicts") or [],
              "supported": bool(verification.get("releasable"))}))

    final = state.get("final_answer") or "I could not answer this from the available evidence."
    yield F.sse(F.token(final))
    yield F.sse(F.done(final, artifacts=[f"{session_id}:plan", f"{session_id}:evidence",
                                         f"{session_id}:verification"],
                       # `verified` already exists on the done frame. It means
                       # "released because the evidence supported it".
                       verified=bool(verification.get("releasable")), stages=stages))
    log.info("agent lane: %s verdict=%s citations=%d tenant_scoped=%s %.0fms",
             question[:48], verdict, len(citations),
             bool(evidence.get("tenant_enforced_by")), (time.time() - t0) * 1000)
    yield F.DONE_SENTINEL


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

    if lane == "agent":
        async for chunk in _agent_lane(user_id, message, session_id):
            yield chunk
        return

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
    # The data lane (ask.py) now emits its own per-stage frames with real per-call
    # timings. Emitting generic ones here too would double up, and the terminal frame
    # below timed the WHOLE TURN and labelled it "query" -- the live trace showed
    # "query done ms=9987" for a query that took about a second.
    _self_staged = (lane == "data")
    if not _self_staged:
        yield F.sse(F.stage(stage_name, "running", label="Building the dashboard"))

    final_text = ""
    artifact_ids: list[str] = []
    _verified = None
    _verification = None
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
                if not _self_staged:
                    yield F.sse(F.stage(stage_name, "running",
                                        label=_STATUS_LABEL.get(name, "Working")))
            elif t == "tool_result":
                # carry the agent's own row count and duration through
                yield F.sse(F.tool_result(ev.get("name", ""), bool(ev.get("ok", True)),
                                          stage_name=stage_name,
                                          rows=ev.get("rows"), ms=ev.get("ms")))
            elif t == "stage":
                # The analytics agent emits its own stage frames, including the
                # critic verdict. Forward them so the client can show verification
                # progress and distinguish verified from unverified answers.
                yield F.sse(ev)
            elif t == "chart_saved":
                pass  # artifacts are emitted together once the turn settles
            elif t == "token":
                # Per lane, because the two agents differ: ask.py (data) yields the
                # COMPLETE message each time, so accumulating it would concatenate --
                # and with Stage 2's retry that meant the corrected answer was appended
                # to the wrong one instead of replacing it. stream.py (chart) yields
                # deltas. Either way what leaves here is replace-semantics.
                if _self_staged:
                    final_text = ev.get("content", "") or final_text
                else:
                    final_text += ev.get("content", "")
                yield F.sse(F.token(final_text))
            elif t == "done":
                final_text = ev.get("final") or final_text
                # True / False / None(unverified) -- see backend/insight/evidence.py
                _verified = ev.get("verified", _verified)
                _verification = ev.get("verification") or _verification
            elif t == "error":
                yield F.sse(F.error(ev.get("message", "error"), stage_name=stage_name))
    except Exception as e:  # noqa: BLE001
        log.exception("unified stream failed")
        yield F.sse(F.error(str(e), stage_name=stage_name))

    if not _self_staged:
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

    _done = F.done(final_text, message_id=msg_id, artifacts=artifact_ids)
    if _verification is not None:
        _done["verified"] = _verified
        _done["verification"] = _verification
    yield F.sse(_done)

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
