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

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.orchestrator import graph, registry, store
from backend.orchestrator import templates

router = APIRouter(prefix="/agent", tags=["agent-os"])
log = logging.getLogger("aganeti.agent_os")

PRIMARY_PROMPT = templates.PRIMARY_PROMPT
DEFAULT_PRIMARY = {"id": "primary", "tools": list(templates.PRIMARY_TOOLS)}


def _uid(request: Request, body: dict | None = None) -> str:
    return ((body or {}).get("user_id")
            or request.query_params.get("user_id")
            or request.headers.get("X-Internal-User")
            or "user_1")


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
                    tools = [t for t in perms if t in known] or list(templates.PRIMARY_TOOLS)
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
    agent, prompt = await _load_primary(user_id)
    final: dict | None = None
    try:
        async for ev in graph.astream_turn(user_id=user_id, agent=agent,
                                            user_message=message, session_id=session_id,
                                            system_prompt=prompt):
            if ev["type"] == "final":
                final = ev
                continue
            if ev["type"] == "approval_required":
                continue  # persisted on final (which carries the full message state)
            yield f"data: {json.dumps(ev)}\n\n"
        if final and final.get("status") == "awaiting_approval":
            aid = await store.create_approval(user_id, agent, final["messages"], final["approval"])
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
        ag = await repo.create_agent(s, org_id=user.org_id, user_id=user.id, kind=kind, name=name,
                                     system_prompt=prompt, template_key=tkey,
                                     model_key=body.get("model_key"), config={"tools": tools})
        known = set(registry.all_names())
        await repo.set_permissions(s, org_id=user.org_id, agent_id=ag.id,
                                   permissions=[t for t in tools if t in known], granted_by=user.id)
        if kind == "primary":
            user.primary_agent_id = ag.id
        await s.commit()
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
