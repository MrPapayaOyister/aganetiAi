"""Approval + paused-run persistence for the agent OS.

An outbound tool pauses the run; we persist the full paused state (agent config +
message history + the pending approval) so the user can approve/reject in a LATER
HTTP request and the executor resumes exactly where it left off — survives process
restarts.

Two backends, selected by env AGANETI_DATA_BACKEND (default "sqlite"):
  * sqlite   — legacy tasks/tasks.db `agent_approvals` table (unchanged).
  * postgres — the new `approvals` table. The full resume blob (agent + messages +
    the original external identity) lives in payload JSONB; the row is keyed by the
    internal User UUID + org so it lists per-user, while the ORIGINAL supabase uid
    is preserved in the blob so resume can still execute the outbound action against
    external services (Gmail/Calendar). Identity is resolved HERE, so callers keep
    passing the supabase uid exactly as before — the cutover is a flag flip.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

BACKEND = os.getenv("AGANETI_DATA_BACKEND", "sqlite")

try:
    from config.settings import BASE_DIR
    _DB = str(BASE_DIR / "tasks" / "tasks.db")
except Exception:
    _DB = "tasks/tasks.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_approvals (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    agent        TEXT NOT NULL,
    messages     TEXT NOT NULL,
    approval     TEXT NOT NULL,
    action_type  TEXT,
    preview      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    result       TEXT,
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);
CREATE INDEX IF NOT EXISTS agent_approvals_user_idx ON agent_approvals (user_id, status);
"""

_initialized = False


def _c() -> sqlite3.Connection:
    c = sqlite3.connect(_DB, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def _init() -> None:
    global _initialized
    if _initialized:
        return
    with _c() as c:
        c.executescript(_SCHEMA)
    _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── sqlite backend (legacy) ───────────────────────────────────────────────────
async def _sq_create(user_id: str, agent: dict, messages: list, approval: dict) -> str:
    def _w() -> str:
        _init()
        aid = str(uuid.uuid4())
        with _c() as c:
            c.execute(
                "INSERT INTO agent_approvals (id,user_id,agent,messages,approval,action_type,"
                "preview,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (aid, user_id, json.dumps(agent), json.dumps(messages), json.dumps(approval),
                 approval.get("action_type"), approval.get("preview"), "pending", _now()))
        return aid
    return await asyncio.to_thread(_w)


async def _sq_get(aid: str) -> dict | None:
    def _r():
        _init()
        with _c() as c:
            row = c.execute("SELECT * FROM agent_approvals WHERE id=?", (aid,)).fetchone()
        return dict(row) if row else None
    return await asyncio.to_thread(_r)


async def _sq_list(user_id: str, status: str) -> list[dict]:
    def _r():
        _init()
        with _c() as c:
            rows = c.execute(
                "SELECT id,user_id,action_type,preview,status,created_at,decided_at "
                "FROM agent_approvals WHERE user_id=? AND status=? ORDER BY created_at DESC LIMIT 100",
                (user_id, status)).fetchall()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_r)


async def _sq_decide(aid: str, status: str, result: str | None) -> None:
    def _w():
        _init()
        with _c() as c:
            c.execute("UPDATE agent_approvals SET status=?, result=COALESCE(?,result), decided_at=? WHERE id=?",
                      (status, result, _now(), aid))
    await asyncio.to_thread(_w)


# ── postgres backend (new `approvals` table) ──────────────────────────────────
async def _pg_create(identity: str, agent: dict, messages: list, approval: dict) -> str:
    from backend.db.base import SessionLocal
    from backend.db import models as M, repo
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, identity)
        if user is None:  # unknown identity: fall back so nothing breaks
            return await _sq_create(identity, agent, messages, approval)
        ap = M.Approval(
            org_id=user.org_id, user_id=user.id, agent_id=None,
            tool_key=approval.get("name") or approval.get("action_type") or "",
            action_type=approval.get("action_type"), preview=approval.get("preview"),
            status="pending",
            payload={"agent": agent, "messages": messages, "approval": approval,
                     "supabase_uid": user.supabase_uid})
        s.add(ap)
        await s.commit()
        return str(ap.id)


def _pg_row_to_legacy(ap) -> dict:
    """Shape a PG Approval row like the sqlite dict the route/resume expect."""
    p = ap.payload or {}
    return {"id": str(ap.id),
            # resume executes outbound against external svcs → needs the supabase uid
            "user_id": p.get("supabase_uid") or (str(ap.user_id) if ap.user_id else ""),
            "agent": json.dumps(p.get("agent") or {}),
            "messages": json.dumps(p.get("messages") or []),
            "approval": json.dumps(p.get("approval") or {}),
            "action_type": ap.action_type, "preview": ap.preview,
            "status": ap.status, "result": ap.result,
            "created_at": ap.created_at.isoformat() if ap.created_at else None,
            "decided_at": ap.decided_at.isoformat() if ap.decided_at else None}


async def _pg_get(aid: str) -> dict | None:
    from backend.db.base import SessionLocal
    from backend.db import models as M
    try:
        key = uuid.UUID(str(aid))
    except (ValueError, TypeError):
        return await _sq_get(aid)  # legacy sqlite id → look there
    async with SessionLocal() as s:
        ap = await s.get(M.Approval, key)
        return _pg_row_to_legacy(ap) if ap else None


async def _pg_list(identity: str, status: str) -> list[dict]:
    from sqlalchemy import select
    from backend.db.base import SessionLocal
    from backend.db import models as M, repo
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, identity)
        if user is None:
            return await _sq_list(identity, status)
        rows = (await s.execute(
            select(M.Approval).where(M.Approval.user_id == user.id, M.Approval.status == status)
            .order_by(M.Approval.created_at.desc()).limit(100))).scalars().all()
        return [{"id": str(r.id), "user_id": str(r.user_id), "action_type": r.action_type,
                 "preview": r.preview, "status": r.status,
                 "created_at": r.created_at.isoformat() if r.created_at else None,
                 "decided_at": r.decided_at.isoformat() if r.decided_at else None} for r in rows]


async def _pg_decide(aid: str, status: str, result: str | None) -> None:
    from sqlalchemy import update
    from sqlalchemy import func as sqlfunc
    from backend.db.base import SessionLocal
    from backend.db import models as M
    try:
        key = uuid.UUID(str(aid))
    except (ValueError, TypeError):
        return await _sq_decide(aid, status, result)
    async with SessionLocal() as s:
        await s.execute(update(M.Approval).where(M.Approval.id == key)
                        .values(status=status, result=result, decided_at=sqlfunc.now()))
        await s.commit()


# ── public dispatch (interface unchanged) ─────────────────────────────────────
async def create_approval(user_id: str, agent: dict, messages: list, approval: dict) -> str:
    if BACKEND == "postgres":
        return await _pg_create(user_id, agent, messages, approval)
    return await _sq_create(user_id, agent, messages, approval)


async def get_approval(aid: str) -> dict | None:
    if BACKEND == "postgres":
        return await _pg_get(aid)
    return await _sq_get(aid)


async def list_approvals(user_id: str, status: str = "pending") -> list[dict]:
    if BACKEND == "postgres":
        return await _pg_list(user_id, status)
    return await _sq_list(user_id, status)


async def decide(aid: str, status: str, result: str | None = None) -> None:
    if BACKEND == "postgres":
        return await _pg_decide(aid, status, result)
    return await _sq_decide(aid, status, result)
