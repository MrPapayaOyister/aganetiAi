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
from backend.orchestrator.router import dashboard_model
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


def _strip_images(messages: list) -> list:
    """Replace multimodal content (image data-URLs) with just its text so the
    persisted approval blob stays small and images aren't re-sent on resume."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            text = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            out.append({**m, "content": text or "[image]"})
        else:
            out.append(m)
    return out


async def _sse(user_id: str, message: str, session_id: str, images: list | None = None):
    from backend.orchestrator import conversation as convo
    agent, prompt = await _load_primary(user_id)
    history = convo.load(user_id, session_id)  # short-term memory of this thread
    # Long-term memory: facts recalled ACROSS threads. Time-boxed and fail-soft —
    # an empty string when unavailable, so a turn never waits on it.
    try:
        from backend.chat import memory as _mem
        _recalled = await _mem.recall(user_id, message)
        if _recalled:
            prompt = f"{prompt}\n\n{_recalled}"
    except Exception:  # noqa: BLE001
        log.debug("memory recall skipped", exc_info=True)
    final: dict | None = None
    try:
        async for ev in graph.astream_turn(user_id=user_id, agent=agent,
                                            user_message=message, session_id=session_id,
                                            system_prompt=prompt, history=history, images=images):
            if ev["type"] == "final":
                final = ev
                continue
            if ev["type"] == "approval_required":
                continue  # persisted on final (which carries the full message state)
            yield f"data: {json.dumps(ev)}\n\n"
        # Persist the turn so the next message has context (fixes stateless re-asking).
        convo.append(user_id, session_id, "user", message)
        if final and final.get("status") == "awaiting_approval":
            aid = await store.create_approval(user_id, agent, _strip_images(final["messages"]), final["approval"])
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
    await _record_session(user_id, session_id, message)
    # Capture durable facts AFTER the answer is out — never on the critical path.
    try:
        import asyncio as _aio
        from backend.chat import memory as _mem
        if _mem.worth_extracting(message, turn_index=len(history)):
            _aio.create_task(_mem.capture_turn(user_id, message, session_id))
    except Exception:  # noqa: BLE001
        log.debug("memory capture skipped", exc_info=True)
    yield "data: [DONE]\n\n"


async def _record_session(uid: str, session_id: str, first_message: str) -> None:
    """Best-effort: upsert the chat_sessions registry row + bump count/last_activity so
    the thread appears in the history dropdown. Never raises into the SSE stream."""
    import uuid as _uuid
    from backend.orchestrator import conversation as convo
    try:
        skey = _uuid.UUID(str(session_id))
    except (ValueError, TypeError):
        return  # legacy non-uuid session — JSON store still works, just not listed
    n = convo.count(uid, session_id)
    if n <= 0:
        return
    title = (first_message or "").strip().replace("\n", " ")[:60] or "New chat"
    try:
        from backend.db.base import SessionLocal
        from backend.db import repo
        async with SessionLocal() as s:
            user = await repo.resolve_user(s, uid)
            if not user:
                return
            await repo.upsert_and_touch_chat_session(
                s, session_id=skey, user_id=user.id, org_id=user.org_id, title=title, count=n)
            await s.commit()
    except Exception:  # noqa: BLE001
        log.debug("chat session registry update failed", exc_info=True)


@router.get("/history")
async def chat_history(request: Request, session_id: str = "sess"):
    """This session's conversation history (for the UI to restore). The JSON store is
    keyed by the caller's own uid, so a caller can only ever read their own sessions."""
    from backend.orchestrator import conversation as convo
    return {"messages": convo.load_full(_uid(request), session_id)}


@router.get("/sessions")
async def list_sessions(request: Request, archived: bool = False, q: str | None = None,
                        date_from: str | None = None, date_to: str | None = None,
                        limit: int = 200):
    """This user's chat threads — for the sidebar Recents and the History page.

    Optional q (title search), date_from/date_to (ISO dates) and pinned-first
    ordering. The response is a SUPERSET of the old shape, so the previously
    deployed dropdown keeps working unchanged."""
    from datetime import datetime as _dt
    from backend.db.base import SessionLocal
    from backend.db import repo

    def _parse(v):
        if not v:
            return None
        try:
            return _dt.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None

    async with SessionLocal() as s:
        user = await repo.resolve_user(s, _uid(request))
        if not user:
            return {"sessions": []}
        rows = await repo.search_chat_sessions(
            s, user.id, archived=archived, q=(q or "").strip() or None,
            date_from=_parse(date_from), date_to=_parse(date_to), limit=max(1, min(limit, 500)))
        return {"sessions": [{
            "id": str(r.id), "title": r.title or "New chat",
            "message_count": r.message_count,
            "last_message_at": r.last_message_at.isoformat() if r.last_message_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "pinned": bool(getattr(r, "pinned", False)),
            "kind": getattr(r, "kind", "assistant"),
            "preview": getattr(r, "last_message_preview", None),
            "artifact_count": getattr(r, "artifact_count", 0) or 0,
        } for r in rows]}


@router.post("/sessions/{session_id}/pin")
async def pin_session(request: Request, session_id: str):
    """Pin/unpin a thread so it stays at the top of History. Body: {pinned: bool}."""
    import uuid as _uuid
    from backend.db.base import SessionLocal
    from backend.db import repo
    body = await request.json()
    try:
        skey = _uuid.UUID(session_id)
    except (ValueError, TypeError):
        return JSONResponse({"error": "bad session id"}, status_code=400)
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, _uid(request))
        if not user:
            return JSONResponse({"error": "unknown user"}, status_code=404)
        ok = await repo.set_chat_session_pinned(s, skey, user.id, bool(body.get("pinned", True)))
        await s.commit()
        if not ok:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "pinned": bool(body.get("pinned", True))}


# ── Long-term memory (cross-thread facts the agent recalls) ────────────────────
@router.get("/memory")
async def list_memory(request: Request, q: str | None = None, status: str = "active",
                      limit: int = 100, offset: int = 0):
    """Facts remembered about this user. Shown in Settings so nothing is stored
    invisibly — the user can read, correct or delete every item."""
    from backend.chat import memory
    return await memory.list_items(_uid(request), q=q, status=status,
                                   limit=max(1, min(limit, 200)), offset=max(0, offset))


@router.post("/memory")
async def create_memory(request: Request):
    """Explicitly remember a fact. Body: {fact, kind?}."""
    from backend.chat import memory
    body = await request.json()
    fact = (body.get("fact") or "").strip()
    if not fact:
        return JSONResponse({"error": "fact required"}, status_code=400)
    item = await memory.add(_uid(request), fact, kind=body.get("kind") or "fact", source="user")
    if item is None:
        return JSONResponse({"error": "could not store"}, status_code=500)
    return item


@router.patch("/memory/{item_id}")
async def update_memory(request: Request, item_id: str):
    """Correct a fact, or archive it. Body: {fact?} | {status: 'archived'|'active'}."""
    from backend.chat import memory
    body = await request.json()
    item = await memory.update(_uid(request), item_id, fact=body.get("fact"), status=body.get("status"))
    if item is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return item


@router.delete("/memory/{item_id}")
async def delete_memory(request: Request, item_id: str):
    """Forget a fact (soft-delete + drop its vector)."""
    from backend.chat import memory
    ok = await memory.delete(_uid(request), item_id)
    return {"ok": bool(ok)}


@router.patch("/sessions/{session_id}")
async def update_session(request: Request, session_id: str):
    """Rename a session (title_source→'user' so auto-titling never clobbers it)."""
    import uuid as _uuid
    from backend.db.base import SessionLocal
    from backend.db import repo
    body = await request.json()
    try:
        skey = _uuid.UUID(session_id)
    except (ValueError, TypeError):
        return JSONResponse({"error": "bad session id"}, status_code=400)
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, _uid(request))
        if not user:
            return JSONResponse({"error": "unknown user"}, status_code=404)
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            return JSONResponse({"error": "title required"}, status_code=400)
        ok = await repo.rename_chat_session(s, skey, user.id, title.strip()[:120])
        await s.commit()
    return {"ok": True} if ok else JSONResponse({"error": "not found"}, status_code=404)


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str):
    """Soft-delete a session row + unlink its JSON body file (ownership-scoped)."""
    import uuid as _uuid
    from backend.db.base import SessionLocal
    from backend.db import repo
    from backend.orchestrator import conversation as convo
    uid = _uid(request)
    try:
        skey = _uuid.UUID(session_id)
    except (ValueError, TypeError):
        return JSONResponse({"error": "bad session id"}, status_code=400)
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, uid)
        if not user:
            return JSONResponse({"error": "unknown user"}, status_code=404)
        ok = await repo.delete_chat_session(s, skey, user.id)
        await s.commit()
    if ok:
        convo.delete(uid, session_id)
        return {"ok": True}
    return JSONResponse({"error": "not found"}, status_code=404)


MAX_IMAGES = 4


def _clean_images(raw) -> list:
    """Accept a list of data: URLs (or {url} objects); cap count; drop junk.
    Vision is expensive and the context is finite — 4 images per turn is plenty."""
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        u = it.get("url") if isinstance(it, dict) else it
        if isinstance(u, str) and u.startswith("data:image"):
            out.append(u)
        if len(out) >= MAX_IMAGES:
            break
    return out


@router.post("/chat")
async def agent_chat(request: Request):
    """The single chat surface.

    Routes the turn internally — conversation to the user's primary agent, exact-number
    questions to the analytics agent, chart requests to the dashboard builder — and
    emits one SSE frame vocabulary for all three (backend/chat/frames.py). Falls back
    to the primary-agent-only generator if the router is unavailable, so a problem here
    degrades the feature instead of taking chat down.
    """
    user_id = _uid(request)
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "sess")
    images = _clean_images(body.get("images"))
    try:
        from backend.chat.unified import unified_stream
        gen = unified_stream(user_id, message, session_id, images, dashboard_model())
    except Exception:  # noqa: BLE001
        log.exception("unified router unavailable — serving the primary agent only")
        gen = _sse(user_id, message, session_id, images)
    return StreamingResponse(gen, media_type="text/event-stream")


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
    # decided_by (the resolved caller) is recorded on the row + the audit event.
    won = await store.decide(aid, status, decided_by=cu.id)
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


# ── User settings (preferences) ───────────────────────────────────────────────
@router.get("/settings")
async def get_settings(request: Request):
    from backend.db.base import SessionLocal
    from backend.db import repo
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, _uid(request))
        return {"settings": (user.settings or {}) if user else {}}


@router.put("/settings")
async def put_settings(request: Request):
    body = await request.json()
    incoming = body.get("settings") if isinstance(body.get("settings"), dict) else body
    if not isinstance(incoming, dict):
        return JSONResponse({"error": "settings must be an object"}, status_code=400)
    from sqlalchemy import update as _upd
    from backend.db.base import SessionLocal
    from backend.db import models as M, repo
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, _uid(request))
        if not user:
            return JSONResponse({"error": "unknown user"}, status_code=404)
        merged = {**(user.settings or {}), **incoming}
        await s.execute(_upd(M.User).where(M.User.id == user.id).values(settings=merged))
        await s.commit()
        return {"settings": merged}


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


# ── Notifications (proactive mail alerts + task proposals for the header bell) ──
@router.get("/notifications")
async def list_notifications(request: Request):
    from backend import notifications as notif
    uid = _uid(request)
    return {"notifications": notif.list_notifications(uid), "unread": notif.unread_count(uid)}


@router.post("/notifications/{nid}/read")
async def read_notification(request: Request, nid: str):
    from backend import notifications as notif
    ok = notif.mark_read(_uid(request), nid)
    return {"ok": ok}


@router.post("/notifications/read_all")
async def read_all_notifications(request: Request):
    from backend import notifications as notif
    notif.mark_all_read(_uid(request))
    return {"ok": True}


@router.post("/notifications/{nid}/approve")
async def approve_notification(request: Request, nid: str):
    """Approve a 'task_proposal' notification → create the real task (approval-gated:
    nothing is created until the user clicks approve)."""
    from backend import notifications as notif
    uid = _uid(request)
    n = notif.get(uid, nid)
    if not n:
        return JSONResponse({"error": "not found"}, status_code=404)
    if n.get("kind") != "task_proposal":
        return JSONResponse({"error": "not a task proposal"}, status_code=400)
    ent = n.get("entity") or {}
    title = (ent.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "no task title"}, status_code=400)
    try:
        from tasks.store import create_task
        await asyncio.to_thread(create_task, uid, title=title, source="email",
                                priority=(ent.get("priority") or "Medium").lower())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"could not create task: {e}"}, status_code=500)
    notif.set_acted(uid, nid)
    return {"ok": True, "task": title}
