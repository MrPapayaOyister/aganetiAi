"""
Operational event logging + analytics aggregation (P5).

A single append-only `events` table in tasks.db records what the agent does:
messages, tool calls (with latency + success), tasks, voice sessions,
initiatives, delegations. The /analytics/* endpoints aggregate over it.

Logging is best-effort and never raises into the request path — a failed
insert must not break a chat turn.
"""
from __future__ import annotations

import sqlite3
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import BASE_DIR

log = logging.getLogger("aria.events")

DB_PATH = str(BASE_DIR / "tasks" / "tasks.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,          -- ISO8601 UTC
    user_id     TEXT,
    kind        TEXT NOT NULL,          -- message_user | message_agent | tool_called |
                                        -- task_created | task_completed | voice_session |
                                        -- initiative_sent | delegation_sent |
                                        -- delegation_completed | memory_query
    name        TEXT,                   -- tool name / metric / sub-type
    success     INTEGER,                -- 1/0/NULL
    duration_ms INTEGER,                -- for tool/voice/response timings
    meta        TEXT                    -- JSON blob
);
CREATE INDEX IF NOT EXISTS events_ts_idx   ON events (ts);
CREATE INDEX IF NOT EXISTS events_kind_idx ON events (kind);
CREATE INDEX IF NOT EXISTS events_user_idx ON events (user_id);
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
        log.warning("events.init failed: %s", e)


def log_event(kind: str, *, user_id: str | None = None, name: str | None = None,
              success: bool | None = None, duration_ms: int | None = None,
              meta: dict | None = None) -> None:
    """Append one event. Best-effort — swallows all errors."""
    try:
        init()
        with _conn() as c:
            c.execute(
                "INSERT INTO events (ts, user_id, kind, name, success, duration_ms, meta) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    user_id, kind, name,
                    (None if success is None else int(success)),
                    duration_ms,
                    (json.dumps(meta) if meta else None),
                ),
            )
    except Exception as e:  # noqa: BLE001
        log.debug("log_event(%s) failed: %s", kind, e)


class timed:
    """Context manager that logs a tool/operation event with its duration.

    with timed("tool_called", name="get_emails", user_id=uid) as t:
        ... ; t.ok()   # mark success
    """
    def __init__(self, kind: str, **fields):
        self.kind = kind
        self.fields = fields
        self._t0 = 0.0
        self._success: bool | None = None

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def ok(self, success: bool = True):
        self._success = success

    def __exit__(self, exc_type, exc, tb):
        dur = int((time.monotonic() - self._t0) * 1000)
        success = self._success if self._success is not None else (exc is None)
        log_event(self.kind, duration_ms=dur, success=success, **self.fields)
        return False  # never suppress


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── Aggregations for /analytics/* ─────────────────────────────────────────────
def summary(user_id: str | None, days: int = 7) -> dict:
    init()
    cut = _cutoff(days)
    out = {
        "period_days": days,
        "messages_user": 0, "messages_agent": 0,
        "tool_calls": 0, "tasks_created": 0, "tasks_completed": 0,
        "voice_sessions": 0, "initiatives": 0,
        "avg_response_ms": None, "avg_tool_ms": None,
    }
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT kind, COUNT(*) n FROM events WHERE ts >= ? GROUP BY kind", (cut,)
            ).fetchall()
            by = {r["kind"]: r["n"] for r in rows}
            out["messages_user"]   = by.get("message_user", 0)
            out["messages_agent"]  = by.get("message_agent", 0)
            out["tool_calls"]      = by.get("tool_called", 0)
            out["tasks_created"]   = by.get("task_created", 0)
            out["tasks_completed"] = by.get("task_completed", 0)
            out["voice_sessions"]  = by.get("voice_session", 0)
            out["initiatives"]     = by.get("initiative_sent", 0)
            r = c.execute(
                "SELECT AVG(duration_ms) a FROM events "
                "WHERE ts >= ? AND kind='message_agent' AND duration_ms IS NOT NULL", (cut,)
            ).fetchone()
            out["avg_response_ms"] = round(r["a"]) if r and r["a"] is not None else None
            r = c.execute(
                "SELECT AVG(duration_ms) a FROM events "
                "WHERE ts >= ? AND kind='tool_called' AND duration_ms IS NOT NULL", (cut,)
            ).fetchone()
            out["avg_tool_ms"] = round(r["a"]) if r and r["a"] is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("summary failed: %s", e)
    return out


def tools(user_id: str | None, days: int = 30) -> dict:
    init()
    cut = _cutoff(days)
    items = []
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT name, COUNT(*) calls, "
                "       SUM(COALESCE(success,0)) ok, AVG(duration_ms) avg_ms "
                "FROM events WHERE ts >= ? AND kind='tool_called' AND name IS NOT NULL "
                "GROUP BY name ORDER BY calls DESC", (cut,)
            ).fetchall()
            for r in rows:
                calls = r["calls"] or 0
                items.append({
                    "name": r["name"],
                    "calls": calls,
                    "success_rate": round((r["ok"] or 0) / calls, 3) if calls else 0,
                    "avg_ms": round(r["avg_ms"]) if r["avg_ms"] is not None else None,
                })
    except Exception as e:  # noqa: BLE001
        log.warning("tools agg failed: %s", e)
    return {"period_days": days, "tools": items}


def tasks_funnel(user_id: str | None, days: int = 30) -> dict:
    init()
    cut = _cutoff(days)
    out = {"period_days": days, "created": 0, "completed": 0, "completion_rate": 0.0}
    try:
        with _conn() as c:
            created = c.execute(
                "SELECT COUNT(*) n FROM events WHERE ts>=? AND kind='task_created'", (cut,)
            ).fetchone()["n"]
            completed = c.execute(
                "SELECT COUNT(*) n FROM events WHERE ts>=? AND kind='task_completed'", (cut,)
            ).fetchone()["n"]
            out["created"] = created
            out["completed"] = completed
            out["completion_rate"] = round(completed / created, 3) if created else 0.0
    except Exception as e:  # noqa: BLE001
        log.warning("tasks funnel failed: %s", e)
    return out


def response_quality(user_id: str | None, days: int = 7) -> dict:
    init()
    cut = _cutoff(days)
    out = {"period_days": days, "avg_response_ms": None, "p50_ms": None, "p95_ms": None,
           "voice_sessions": 0, "avg_voice_ms": None}
    try:
        with _conn() as c:
            durs = [r["duration_ms"] for r in c.execute(
                "SELECT duration_ms FROM events WHERE ts>=? AND kind='message_agent' "
                "AND duration_ms IS NOT NULL ORDER BY duration_ms", (cut,)
            ).fetchall()]
            if durs:
                out["avg_response_ms"] = round(sum(durs) / len(durs))
                out["p50_ms"] = durs[len(durs) // 2]
                out["p95_ms"] = durs[min(len(durs) - 1, int(len(durs) * 0.95))]
            vr = c.execute(
                "SELECT COUNT(*) n, AVG(duration_ms) a FROM events "
                "WHERE ts>=? AND kind='voice_session'", (cut,)
            ).fetchone()
            out["voice_sessions"] = vr["n"] or 0
            out["avg_voice_ms"] = round(vr["a"]) if vr["a"] is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("response_quality failed: %s", e)
    return out


def active_hours(user_id: str | None, days: int = 30) -> dict:
    """Message count by hour-of-day (0-23) — drives the activity heatmap."""
    init()
    cut = _cutoff(days)
    buckets = [0] * 24
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT ts FROM events WHERE ts>=? AND kind IN ('message_user','message_agent')",
                (cut,)
            ).fetchall()
            for r in rows:
                try:
                    h = datetime.fromisoformat(r["ts"]).hour
                    buckets[h] += 1
                except Exception:
                    pass
    except Exception as e:  # noqa: BLE001
        log.warning("active_hours failed: %s", e)
    return {"period_days": days, "by_hour": buckets}
