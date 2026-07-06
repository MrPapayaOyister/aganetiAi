"""Enterprise Agentic OS — HTTP surface over the real LangGraph executor.

Endpoints (all under /agent, auth-enforced by the global middleware):
  POST /agent/chat                    → SSE stream of the primary agent's turn
  GET  /agent/approvals?status=       → list this user's approvals
  POST /agent/approvals/{id}/approve  → resume the paused run, executing the action
  POST /agent/approvals/{id}/reject   → resume the paused run, skipping the action
  GET  /agent/tools                   → registered tool catalog (for the UI)
  GET  /agent/agents                  → this user's agents (primary + specialists)
  GET  /agent/agents/{id}             → one agent + its tool permissions
  POST /agent/agents                  → create an agent (from a template or free-form)
  PATCH/agent/agents/{id}             → edit persona / prompt / model / status
  PUT  /agent/agents/{id}/permissions → set the agent's tool allow-list
  DELETE /agent/agents/{id}           → archive (soft-delete) an agent

The chat path loads the user's DB-backed primary agent (persona, prompt, model,
permission-derived tool allow-list); a saved edit takes effect on the next call
(no turn caching). Falls back to the in-code template only pre-seed.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.orchestrator import graph, registry, store
from backend.orchestrator import templates

router = APIRouter(prefix="/agent", tags=["agent-os"])
log = logging.getLogger("aganeti.agent_os")

PRIMARY_PROMPT = templates.PRIMARY_PROMPT
DEFAULT_PRIMARY = {"id": "primary", "tools": list(templates.PRIMARY_TOOLS)}


def _uid(request: Request) -> str:
    """The caller's identity — read ONLY from the trusted header the auth
    middleware injects (X-Auth-User). Client-supplied user_id/body/headers are
    NEVER trusted, and there is no admin default: a missing identity fails closed."""
    u = request.headers.get("x-auth-user")
    if not u:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return u


async def _load_primary(user_id: str) -> tuple[dict, str]:
    """Resolve the user → their DB primary agent → executor agent dict + prompt.

    Falls back to the in-code template when the user or agent isn't seeded yet, so
    the endpoint never hard-fails pre-onboarding.
    """
    try:
        from backend.db.base import SessionLocal
        from backend.db import repo
        async with SessionLocal() as s:
            user = await repo.resolve_user(s, user_id)
            if user:
                ag = await repo.get_primary_agent(s, user.id)
                if ag:
                    perms = await repo.allowed_tools(s, ag.id)
                    known = set(registry.all_names())
                    # An empty list is a DELIBERATE lockdown (user revoked all tools) —
                    # honor it verbatim; never re-inject the default toolset for an
                    # agent that exists (the no-agent fallback is the outer path).
                    tools = [t for t in perms if t in known]
                    agent = {"id": "primary", "tools": tools,
                             "model_key": ag.model_key, "fallback_models": ag.fallback_models or []}
                    prompt = ag.system_prompt or PRIMARY_PROMPT
                    if ag.persona:
                        prompt = f"{prompt}\n\n{ag.persona}"
                    return agent, prompt
    except Exception:  # noqa: BLE001
        log.exception("DB primary-agent load failed for %s; using template", user_id)
    return dict(DEFAULT_PRIMARY), PRIMARY_PROMPT


async def _sse(user_id: str, message: str, session_id: str):
    from backend.orchestrator import conversation as convo
    agent, prompt = await _load_primary(user_id)
    history = convo.load(user_id, session_id)  # short-term memory of this thread
    final: dict | None = None
    try:
        async for ev in graph.astream_turn(user_id=user_id, agent=agent,
                                            user_message=message, session_id=session_id,
                                            system_prompt=prompt, history=history):
            if ev["type"] == "final":
                final = ev
                continue
            if ev["type"] == "approval_required":
                continue  # persisted on final (which carries the full message state)
            yield f"data: {json.dumps(ev)}\n\n"
        # Persist the turn so the next message has context (fixes stateless re-asking).
        convo.append(user_id, session_id, "user", message)
        if final and final.get("status") == "awaiting_approval":
            aid = await store.create_approval(user_id, agent, final["messages"], final["approval"])
            convo.append(user_id, session_id, "assistant",
                         f"(Prepared an action for your approval: {final['approval'].get('preview', '')})")
            yield f"data: {json.dumps({'type': 'approval_required', 'approval': final['approval'], 'approval_id': aid})}\n\n"
        else:
            fin = (final or {}).get("final", "") or ""
            if fin:
                convo.append(user_id, session_id, "assistant", fin)
            yield f"data: {json.dumps({'type': 'done', 'final': fin})}\n\n"
    except Exception as e:  # noqa: BLE001
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    yield "data: [DONE]\n\n"


@router.get("/history")
async def chat_history(request: Request, session_id: str = "sess"):
    """This session's conversation history (for the UI to restore on reload)."""
    from backend.orchestrator import conversation as convo
    return {"messages": convo.load_full(_uid(request), session_id)}


@router.post("/chat")
async def agent_chat(request: Request):
    user_id = _uid(request)
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "sess")
    return StreamingResponse(_sse(user_id, message, session_id), media_type="text/event-stream")


@router.get("/approvals")
async def list_approvals(request: Request, status: str = "pending"):
    return {"approvals": await store.list_approvals(_uid(request), status)}


async def _resume(request: Request, aid: str, approved: bool):
    caller = _uid(request)
    rec = await store.get_approval(aid)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    # OWNERSHIP: the caller must own this approval. Resolve both sides to the
    # internal User and compare — a non-owner cannot see or trip another user's gate.
    from backend.db.base import SessionLocal
    from backend.db import repo
    async with SessionLocal() as s:
        cu = await repo.resolve_user(s, caller)
        ou = await repo.resolve_user(s, rec["user_id"])
    if not cu or not ou or cu.id != ou.id:
        return JSONResponse({"error": "not found"}, status_code=404)  # don't leak existence
    if rec["status"] != "pending":
        return JSONResponse({"error": f"already {rec['status']}"}, status_code=409)
    status = "approved" if approved else "rejected"
    # ATOMIC CLAIM (compare-and-swap pending->decided) BEFORE executing. Only the
    # winner proceeds, so the outbound tool runs EXACTLY once under a double-click /
    # concurrent approve+reject race — preserving the single-execution guarantee.
    won = await store.decide(aid, status)
    if not won:
        return JSONResponse({"error": "already decided"}, status_code=409)
    agent = json.loads(rec["agent"])
    messages = json.loads(rec["messages"])
    approval = json.loads(rec["approval"])
    res = await graph.resume(user_id=rec["user_id"], agent=agent, messages=messages,
                             approval=approval, approved=approved)
    await store.set_result(aid, res.get("final") or "")
    if res["status"] == "awaiting_approval":
        new_aid = await store.create_approval(rec["user_id"], agent, res["messages"], res["approval"])
        return {"status": "awaiting_approval", "approval_id": new_aid, "approval": res["approval"]}
    return {"status": "done", "final": res["final"]}


@router.post("/approvals/{aid}/approve")
async def approve(request: Request, aid: str):
    return await _resume(request, aid, approved=True)


@router.post("/approvals/{aid}/reject")
async def reject(request: Request, aid: str):
    return await _resume(request, aid, approved=False)


@router.get("/tools")
async def list_tools():
    return {"tools": [
        {"name": t.name, "description": t.description, "is_outbound": t.is_outbound,
         "required_permission": t.required_permission}
        for t in [registry.get(n) for n in registry.all_names()] if t]}


@router.get("/analytics")
async def agent_analytics(request: Request, days: int = 30):
    """Per-agent performance (calls, latency, tokens, cost) from the events spine."""
    from backend import events
    return await asyncio.to_thread(events.per_agent, _uid(request), days)


# ── Agent registry CRUD (Agent Matrix) ────────────────────────────────────────
def _agent_json(ag, perms: list) -> dict:
    return {"id": str(ag.id), "kind": ag.kind, "name": ag.name, "persona": ag.persona,
            "template_key": ag.template_key, "system_prompt": ag.system_prompt,
            "model_key": ag.model_key, "fallback_models": ag.fallback_models or [],
            "status": ag.status, "tools": [p.permission for p in perms],
            "outbound": [p.permission for p in perms if p.is_outbound]}


async def _resolve_user(s, user_id: str):
    from backend.db import repo
    return await repo.resolve_user(s, user_id)


@router.get("/agents")
async def list_agents(request: Request):
    from backend.db.base import SessionLocal
    from backend.db import repo
    async with SessionLocal() as s:
        user = await _resolve_user(s, _uid(request))
        if not user:
            return {"agents": []}
        out = []
        for ag in await repo.list_agents(s, user.id):
            perms = await repo.list_permissions(s, ag.id)
            out.append(_agent_json(ag, perms))
        return {"agents": out}


async def _get_owned(s, user, agent_id: str):
    """Return the agent iff it belongs to this user (org+user scoped), else None."""
    import uuid as _uuid
    from backend.db import repo
    try:
        aid = _uuid.UUID(str(agent_id))
    except (ValueError, TypeError):
        return None
    ag = await repo.get_agent(s, aid)
    if ag and ag.user_id == user.id:
        return ag
    return None


@router.get("/agents/{agent_id}")
async def get_agent(request: Request, agent_id: str):
    from backend.db.base import SessionLocal
    from backend.db import repo
    async with SessionLocal() as s:
        user = await _resolve_user(s, _uid(request))
        if not user:
            return JSONResponse({"error": "unknown user"}, status_code=404)
        ag = await _get_owned(s, user, agent_id)
        if not ag:
            return JSONResponse({"error": "not found"}, status_code=404)
        return _agent_json(ag, await repo.list_permissions(s, ag.id))


@router.post("/agents")
async def create_agent(request: Request):
    body = await request.json()
    from backend.db.base import SessionLocal
    from backend.db import repo
    async with SessionLocal() as s:
        user = await _resolve_user(s, _uid(request))
        if not user:
            return JSONResponse({"error": "unknown user"}, status_code=404)
        tkey = body.get("template_key")
        tmpl = next((t for t in templates.specialist_templates() if t["template_key"] == tkey), None)
        kind = body.get("kind") or (tmpl["kind"] if tmpl else "specialist")
        name = body.get("name") or (tmpl["name"] if tmpl else "New Agent")
        prompt = body.get("system_prompt") or (tmpl["system_prompt"] if tmpl else "You are a helpful specialist agent.")
        tools = body.get("tools") or (tmpl["tools"] if tmpl else [])
        if kind == "primary" and await repo.get_primary_agent(s, user.id):
            return JSONResponse({"error": "primary agent already exists"}, status_code=409)
        from sqlalchemy.exc import IntegrityError
        try:
            ag = await repo.create_agent(s, org_id=user.org_id, user_id=user.id, kind=kind, name=name,
                                         system_prompt=prompt, template_key=tkey,
                                         model_key=body.get("model_key"), config={"tools": tools})
            known = set(registry.all_names())
            await repo.set_permissions(s, org_id=user.org_id, agent_id=ag.id,
                                       permissions=[t for t in tools if t in known], granted_by=user.id)
            if kind == "primary":
                user.primary_agent_id = ag.id
            await s.commit()
        except IntegrityError:
            # Concurrent create raced us past the pre-check; the DB partial-unique
            # index (one primary per user) rejected the duplicate.
            await s.rollback()
            return JSONResponse({"error": "primary agent already exists"}, status_code=409)
        return _agent_json(ag, await repo.list_permissions(s, ag.id))


@router.patch("/agents/{agent_id}")
async def patch_agent(request: Request, agent_id: str):
    body = await request.json()
    from backend.db.base import SessionLocal
    from backend.db import repo
    async with SessionLocal() as s:
        user = await _resolve_user(s, _uid(request))
        if not user:
            return JSONResponse({"error": "unknown user"}, status_code=404)
        ag = await _get_owned(s, user, agent_id)
        if not ag:
            return JSONResponse({"error": "not found"}, status_code=404)
        await repo.update_agent(s, ag.id, name=body.get("name"), persona=body.get("persona"),
                                system_prompt=body.get("system_prompt"), model_key=body.get("model_key"),
                                fallback_models=body.get("fallback_models"), status=body.get("status"))
        await s.commit()
        ag2 = await repo.get_agent(s, ag.id)
        return _agent_json(ag2, await repo.list_permissions(s, ag2.id))


@router.put("/agents/{agent_id}/permissions")
async def set_agent_permissions(request: Request, agent_id: str):
    body = await request.json()
    tools = body.get("tools") or []
    from backend.db.base import SessionLocal
    from backend.db import repo
    async with SessionLocal() as s:
        user = await _resolve_user(s, _uid(request))
        if not user:
            return JSONResponse({"error": "unknown user"}, status_code=404)
        ag = await _get_owned(s, user, agent_id)
        if not ag:
            return JSONResponse({"error": "not found"}, status_code=404)
        known = set(registry.all_names())
        await repo.set_permissions(s, org_id=user.org_id, agent_id=ag.id,
                                   permissions=[t for t in tools if t in known], granted_by=user.id)
        await s.commit()
        return _agent_json(ag, await repo.list_permissions(s, ag.id))


@router.delete("/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str):
    from backend.db.base import SessionLocal
    from backend.db import repo
    async with SessionLocal() as s:
        user = await _resolve_user(s, _uid(request))
        if not user:
            return JSONResponse({"error": "unknown user"}, status_code=404)
        ag = await _get_owned(s, user, agent_id)
        if not ag:
            return JSONResponse({"error": "not found"}, status_code=404)
        if ag.kind == "primary":
            return JSONResponse({"error": "cannot delete the primary agent"}, status_code=400)
        await repo.soft_delete_agent(s, ag.id)
        await s.commit()
        return {"status": "archived", "id": str(ag.id)}
