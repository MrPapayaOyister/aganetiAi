"""Enterprise Agentic OS — HTTP surface over the real LangGraph executor.

Endpoints (all under /agent, auth-enforced by the global middleware):
  POST /agent/chat                    → SSE stream of the primary agent's turn
  GET  /agent/approvals?status=       → list this user's approvals
  POST /agent/approvals/{id}/approve  → resume the paused run, executing the action
  POST /agent/approvals/{id}/reject   → resume the paused run, skipping the action
  GET  /agent/tools                   → registered tool catalog (for the UI)

Phase B swaps DEFAULT_PRIMARY for the user's DB-backed primary agent + its
permission-derived tool allowlist; the executor contract is unchanged.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.orchestrator import graph, registry, store

router = APIRouter(prefix="/agent", tags=["agent-os"])

DEFAULT_PRIMARY = {"id": "primary", "tools": [
    "list_tasks", "get_agenda", "search_memory", "draft_email", "send_email",
    "create_calendar_event", "delegate", "current_time", "calc"]}

PRIMARY_PROMPT = (
    "You are the user's Primary AI assistant — their chief of staff. Use tools to answer "
    "with real data; delegate domain subtasks to specialist agents via the delegate tool. "
    "Sending email and creating calendar events are OUTBOUND actions requiring the user's "
    "explicit approval: call the tool and the system pauses for their approval — do not "
    "pretend you have sent anything. Never fabricate data. Be concise and professional.")


def _uid(request: Request, body: dict | None = None) -> str:
    return ((body or {}).get("user_id")
            or request.query_params.get("user_id")
            or request.headers.get("X-Internal-User")
            or "user_1")


async def _sse(user_id: str, message: str, session_id: str):
    final: dict | None = None
    try:
        async for ev in graph.astream_turn(user_id=user_id, agent=DEFAULT_PRIMARY,
                                            user_message=message, session_id=session_id,
                                            system_prompt=PRIMARY_PROMPT):
            if ev["type"] == "final":
                final = ev
                continue
            if ev["type"] == "approval_required":
                continue  # persisted on final (which carries the full message state)
            yield f"data: {json.dumps(ev)}\n\n"
        if final and final.get("status") == "awaiting_approval":
            aid = await store.create_approval(user_id, DEFAULT_PRIMARY, final["messages"], final["approval"])
            yield f"data: {json.dumps({'type': 'approval_required', 'approval': final['approval'], 'approval_id': aid})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'done', 'final': (final or {}).get('final', '')})}\n\n"
    except Exception as e:  # noqa: BLE001
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/chat")
async def agent_chat(request: Request):
    body = await request.json()
    user_id = _uid(request, body)
    message = body.get("message", "")
    session_id = body.get("session_id", "sess")
    return StreamingResponse(_sse(user_id, message, session_id), media_type="text/event-stream")


@router.get("/approvals")
async def list_approvals(request: Request, status: str = "pending"):
    return {"approvals": await store.list_approvals(_uid(request), status)}


async def _resume(aid: str, approved: bool):
    rec = await store.get_approval(aid)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    if rec["status"] != "pending":
        return JSONResponse({"error": f"already {rec['status']}"}, status_code=409)
    agent = json.loads(rec["agent"])
    messages = json.loads(rec["messages"])
    approval = json.loads(rec["approval"])
    res = await graph.resume(user_id=rec["user_id"], agent=agent, messages=messages,
                             approval=approval, approved=approved)
    await store.decide(aid, "approved" if approved else "rejected", result=res.get("final") or "")
    if res["status"] == "awaiting_approval":
        new_aid = await store.create_approval(rec["user_id"], agent, res["messages"], res["approval"])
        return {"status": "awaiting_approval", "approval_id": new_aid, "approval": res["approval"]}
    return {"status": "done", "final": res["final"]}


@router.post("/approvals/{aid}/approve")
async def approve(aid: str):
    return await _resume(aid, approved=True)


@router.post("/approvals/{aid}/reject")
async def reject(aid: str):
    return await _resume(aid, approved=False)


@router.get("/tools")
async def list_tools():
    return {"tools": [
        {"name": t.name, "description": t.description, "is_outbound": t.is_outbound,
         "required_permission": t.required_permission}
        for t in [registry.get(n) for n in registry.all_names()] if t]}
