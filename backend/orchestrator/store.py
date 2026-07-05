"""Approval + paused-run persistence for the agent OS.

An outbound tool pauses the run; we persist the full paused state (agent config +
message history + the pending approval) so the user can approve/reject in a LATER
HTTP request and the executor resumes exactly where it left off — survives process
restarts. SQLite for now (matches the current data layer); moves to the Postgres
`approvals` + `agent_runs` tables in Phase A with the same interface.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone

try:
    from config.settings import BASE_DIR
    _DB = str(BASE_DIR / "tasks" / "tasks.db")
except Exception:
    _DB = "tasks/tasks.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_approvals (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    agent        TEXT NOT NULL,          -- json: agent config (id, tools)
    messages     TEXT NOT NULL,          -- json: full paused message history
    approval     TEXT NOT NULL,          -- json: {tool_call_id,name,args,action_type,preview}
    action_type  TEXT,
    preview      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
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


async def create_approval(user_id: str, agent: dict, messages: list, approval: dict) -> str:
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


async def get_approval(aid: str) -> dict | None:
    def _r():
        _init()
        with _c() as c:
            row = c.execute("SELECT * FROM agent_approvals WHERE id=?", (aid,)).fetchone()
        return dict(row) if row else None
    return await asyncio.to_thread(_r)


async def list_approvals(user_id: str, status: str = "pending") -> list[dict]:
    def _r():
        _init()
        with _c() as c:
            rows = c.execute(
                "SELECT id,user_id,action_type,preview,status,created_at,decided_at "
                "FROM agent_approvals WHERE user_id=? AND status=? ORDER BY created_at DESC LIMIT 100",
                (user_id, status)).fetchall()
        return [dict(r) for r in rows]
    return await asyncio.to_thread(_r)


async def decide(aid: str, status: str, result: str | None = None) -> None:
    def _w():
        _init()
        with _c() as c:
            c.execute("UPDATE agent_approvals SET status=?, result=COALESCE(?,result), decided_at=? WHERE id=?",
                      (status, result, _now(), aid))
    await asyncio.to_thread(_w)
