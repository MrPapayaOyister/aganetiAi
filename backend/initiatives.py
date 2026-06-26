"""
Agent initiative queue (P3 — proactive behavior).

Background jobs (morning brief, pre-meeting prep, task follow-ups, email
surfacing) enqueue "initiative" items here. The frontend polls /initiatives,
renders them as proactive (Aria-initiated) messages, and acknowledges them.
Telegram delivery is fire-and-forget alongside.

One append-only table in tasks.db. All operations best-effort.
"""
from __future__ import annotations

import sqlite3
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR

log = logging.getLogger("aria.initiatives")
DB_PATH = str(BASE_DIR / "tasks" / "tasks.db")

# Initiative categories the user can later filter on.
CATEGORIES = ("briefing", "meeting_prep", "task_followup", "email_surfacing", "system")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS initiatives (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    dedup_key   TEXT,                 -- skip if an unacked item with same key exists
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | delivered | acknowledged | dismissed
    created_at  TEXT NOT NULL,
    acted_at    TEXT,
    meta        TEXT
);
CREATE INDEX IF NOT EXISTS initiatives_user_idx   ON initiatives (user_id, status);
CREATE INDEX IF NOT EXISTS initiatives_dedup_idx  ON initiatives (dedup_key);
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
        log.warning("initiatives.init failed: %s", e)


def enqueue(user_id: str, category: str, title: str, body: str,
            dedup_key: str | None = None, meta: dict | None = None) -> str | None:
    """Add an initiative. If dedup_key is set and an un-acted item with the same
    key already exists, skip (returns None). Returns the new id otherwise."""
    init()
    try:
        with _conn() as c:
            if dedup_key:
                existing = c.execute(
                    "SELECT 1 FROM initiatives WHERE dedup_key=? AND status IN "
                    "('pending','delivered') LIMIT 1", (dedup_key,)
                ).fetchone()
                if existing:
                    return None
            iid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO initiatives (id,user_id,category,title,body,dedup_key,"
                "status,created_at,meta) VALUES (?,?,?,?,?,?,?,?,?)",
                (iid, user_id, category, title, body, dedup_key, "pending",
                 datetime.now(timezone.utc).isoformat(),
                 json.dumps(meta) if meta else None),
            )
        try:
            from backend import events
            events.log_event("initiative_sent", user_id=user_id, name=category)
        except Exception:
            pass
        return iid
    except Exception as e:  # noqa: BLE001
        log.warning("initiative enqueue failed: %s", e)
        return None


def pending(user_id: str, limit: int = 10) -> list[dict]:
    """Return un-acknowledged initiatives for a user (newest first), and mark
    them delivered."""
    init()
    out: list[dict] = []
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM initiatives WHERE user_id=? AND status IN "
                "('pending','delivered') ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            out = [dict(r) for r in rows]
            ids = [r["id"] for r in rows if r["status"] == "pending"]
            if ids:
                c.executemany("UPDATE initiatives SET status='delivered' WHERE id=?",
                              [(i,) for i in ids])
    except Exception as e:  # noqa: BLE001
        log.warning("initiative pending failed: %s", e)
    return out


def acknowledge(initiative_id: str, dismissed: bool = False) -> bool:
    init()
    try:
        with _conn() as c:
            c.execute(
                "UPDATE initiatives SET status=?, acted_at=? WHERE id=?",
                ("dismissed" if dismissed else "acknowledged",
                 datetime.now(timezone.utc).isoformat(), initiative_id),
            )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("initiative ack failed: %s", e)
        return False
