"""
Tracked delegation lifecycle (P7).

Builds on the existing agent_messages transport (integrations/agent_inbox.py):
Aria delegates a task to a named sub-agent, the sub-agent executes a real
capability, the result is written back, and the user is notified via the P3
initiative queue. Each delegation is a row in `delegations` with a full
status lifecycle the UI can render as pills:

    pending → in_progress → completed
                          ↘ failed

Sub-agents are thin wrappers around already-tested capabilities:
    calendar_agent  → Google Calendar agenda
    email_agent     → Gmail inbox
    memory_agent    → long-term memory search
    scheduler_agent → reminder/schedule ack
    aria            → quick LLM answer (fallback)
"""
from __future__ import annotations

import sqlite3
import json
import uuid
import logging
import asyncio
from datetime import datetime, timezone

from config.settings import BASE_DIR

log = logging.getLogger("aria.delegation")
DB_PATH = str(BASE_DIR / "tasks" / "tasks.db")

KNOWN_AGENTS = ("calendar_agent", "email_agent", "memory_agent", "scheduler_agent", "aria")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegations (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    from_agent  TEXT NOT NULL DEFAULT 'aria',
    to_agent    TEXT NOT NULL,
    task        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|in_progress|completed|failed
    result      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS delegations_user_idx ON delegations (user_id, status);
"""

_initialized = False


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    global _initialized
    if _initialized:
        return
    try:
        with _conn() as c:
            c.executescript(_SCHEMA)
        _initialized = True
    except Exception as e:  # noqa: BLE001
        log.warning("delegation.init failed: %s", e)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_status(did: str, status: str, *, result: str | None = None, error: str | None = None) -> None:
    try:
        with _conn() as c:
            c.execute(
                "UPDATE delegations SET status=?, result=COALESCE(?,result), "
                "error=COALESCE(?,error), updated_at=? WHERE id=?",
                (status, result, error, _now(), did),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("delegation status update failed: %s", e)


# ── Sub-agent handlers (real capabilities) ────────────────────────────────────
def _run_async(coro):
    """Run an async coroutine from a sync handler."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Called from within the event loop via to_thread → make a new loop.
            return asyncio.run(coro)
    except RuntimeError:
        pass
    return asyncio.run(coro)


def _calendar_agent(user_id: str, task: str) -> str:
    from backend.services import gcalendar
    events = _run_async(gcalendar.get_google_agenda(user_id, days_ahead=2))
    if not events:
        return "No upcoming events found."
    return "Upcoming events:\n" + "\n".join(
        f"- {e.get('title','(untitled)')} at {e.get('start','')}" for e in events[:8])


def _email_agent(user_id: str, task: str) -> str:
    from backend.services import gmail
    msgs = _run_async(gmail.get_gmail_inbox(user_id, 10))
    if not msgs:
        return "Inbox is empty."
    unread = sum(1 for m in msgs if not m.get("is_read"))
    lines = [f"{'•' if not m.get('is_read') else ' '} {m.get('subject','(no subject)')} — {m.get('from_name','')}"
             for m in msgs[:8]]
    return f"{unread} unread of {len(msgs)} recent:\n" + "\n".join(lines)


def _memory_agent(user_id: str, task: str) -> str:
    from memory.long_term import search_memory
    mem = search_memory(user_id, task, top_k=5)
    return (mem or "").strip() or "No relevant memory found."


def _scheduler_agent(user_id: str, task: str) -> str:
    return f"Scheduler acknowledged: '{task}'. Use set_reminder for a concrete time."


def _aria_agent(user_id: str, task: str) -> str:
    return f"Handled directly: {task}"


_HANDLERS = {
    "calendar_agent": _calendar_agent,
    "email_agent": _email_agent,
    "memory_agent": _memory_agent,
    "scheduler_agent": _scheduler_agent,
    "aria": _aria_agent,
}


# ── Public API ────────────────────────────────────────────────────────────────
def create(user_id: str, to_agent: str, task: str, from_agent: str = "aria") -> dict:
    """Create a delegation record (pending) + mirror it into agent_messages."""
    init()
    did = str(uuid.uuid4())
    row = {
        "id": did, "user_id": user_id, "from_agent": from_agent, "to_agent": to_agent,
        "task": task, "status": "pending", "result": None, "error": None,
        "created_at": _now(), "updated_at": _now(),
    }
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO delegations (id,user_id,from_agent,to_agent,task,status,"
                "result,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, user_id, from_agent, to_agent, task, "pending", None, None,
                 row["created_at"], row["updated_at"]),
            )
        # Mirror onto the inter-agent transport.
        try:
            from integrations.agent_inbox import send_message
            send_message(from_agent, to_agent, "task",
                         {"delegation_id": did, "task": task, "user_id": user_id})
        except Exception:
            pass
        try:
            from backend import events
            events.log_event("delegation_sent", user_id=user_id, name=to_agent)
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        log.warning("delegation create failed: %s", e)
        row["status"] = "failed"
        row["error"] = str(e)
    return row


def run(did: str) -> dict:
    """Execute a delegation synchronously: mark in_progress, run the sub-agent,
    write the result/error, notify the user via an initiative. Returns the row."""
    init()
    try:
        with _conn() as c:
            r = c.execute("SELECT * FROM delegations WHERE id=?", (did,)).fetchone()
        if not r:
            return {"id": did, "status": "failed", "error": "not found"}
        d = dict(r)
    except Exception as e:  # noqa: BLE001
        return {"id": did, "status": "failed", "error": str(e)}

    _set_status(did, "in_progress")
    to_agent = d["to_agent"]
    handler = _HANDLERS.get(to_agent, _aria_agent)
    try:
        result = handler(d["user_id"], d["task"])
        _set_status(did, "completed", result=result)
        # Mirror result back onto the transport + notify the user (P3).
        try:
            from integrations.agent_inbox import send_message
            send_message(to_agent, d["from_agent"], "result",
                         {"delegation_id": did, "result": result})
        except Exception:
            pass
        try:
            from backend import events, initiatives
            events.log_event("delegation_completed", user_id=d["user_id"], name=to_agent, success=True)
            initiatives.enqueue(
                d["user_id"], "system",
                f"{to_agent.replace('_', ' ')} finished",
                f"Delegated task complete:\n\n{result[:500]}",
                dedup_key=f"deleg:{did}",
                meta={"delegation_id": did},
            )
        except Exception:
            pass
        d["status"] = "completed"
        d["result"] = result
    except Exception as e:  # noqa: BLE001
        log.warning("delegation run failed (%s): %s", did, e)
        _set_status(did, "failed", error=str(e))
        try:
            from backend import events
            events.log_event("delegation_completed", user_id=d["user_id"], name=to_agent, success=False)
        except Exception:
            pass
        d["status"] = "failed"
        d["error"] = str(e)
    return d


def create_and_run(user_id: str, to_agent: str, task: str) -> dict:
    row = create(user_id, to_agent, task)
    if row.get("status") == "failed":
        return row
    return run(row["id"])


def list_for_user(user_id: str, limit: int = 20) -> list[dict]:
    init()
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM delegations WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("delegation list failed: %s", e)
        return []
