"""Operational event logging + analytics aggregation (P5) — dual-backend.

Append-only `events` records what the agent does (messages, tool calls, tasks,
voice, initiatives). /analytics/* aggregates over it. Selected by env
AGANETI_DATA_BACKEND: sqlite (tasks.db) or postgres (the events table, keyed by the
internal User UUID). Logging is best-effort and never raises into the request path.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from config.settings import BASE_DIR

log = logging.getLogger("aria.events")
DB_PATH = str(BASE_DIR / "tasks" / "tasks.db")
BACKEND = os.getenv("AGANETI_DATA_BACKEND", "sqlite")


def _pg() -> bool:
    return BACKEND == "postgres"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user_id TEXT, kind TEXT NOT NULL,
    name TEXT, success INTEGER, duration_ms INTEGER, meta TEXT);
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
    if _pg():
        try:
            from backend.db import models as M, sync as _s
            with _s.session() as ses:
                uid = org = None
                agent_id = None
                if user_id:
                    u = _s.resolve_user(ses, user_id)
                    if u:
                        uid, org = u.id, u.org_id
                # a UUID agent id can ride in meta for per-agent analytics
                ai = (meta or {}).get("agent_uuid")
                if ai:
                    import uuid as _u
                    try:
                        agent_id = _u.UUID(str(ai))
                    except (ValueError, TypeError):
                        agent_id = None
                ses.add(M.Event(kind=kind, name=name, org_id=org, user_id=uid, agent_id=agent_id,
                                success=success, duration_ms=duration_ms,
                                cost_micros=(meta or {}).get("cost_micros"), meta=meta or {}))
                ses.commit()
            return
        except Exception as e:  # noqa: BLE001
            log.debug("pg log_event(%s) failed → sqlite: %s", kind, e)
    try:
        init()
        with _conn() as c:
            c.execute("INSERT INTO events (ts,user_id,kind,name,success,duration_ms,meta) VALUES (?,?,?,?,?,?,?)",
                      (datetime.now(timezone.utc).isoformat(), user_id, kind, name,
                       (None if success is None else int(success)), duration_ms,
                       (json.dumps(meta) if meta else None)))
    except Exception as e:  # noqa: BLE001
        log.debug("log_event(%s) failed: %s", kind, e)


class timed:
    def __init__(self, kind: str, **fields):
        self.kind, self.fields, self._t0, self._success = kind, fields, 0.0, None

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def ok(self, success: bool = True):
        self._success = success

    def __exit__(self, exc_type, exc, tb):
        dur = int((time.monotonic() - self._t0) * 1000)
        success = self._success if self._success is not None else (exc is None)
        log_event(self.kind, duration_ms=dur, success=success, **self.fields)
        return False


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── Postgres aggregation helper ───────────────────────────────────────────────
def _pg_rows(sql: str, params: dict) -> list[dict]:
    from sqlalchemy import text
    from backend.db import sync as _s
    with _s.engine().connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params).fetchall()]


def _pg_uid(user_id: str | None):
    if not user_id:
        return None
    from backend.db import sync as _s
    uid, _ = _s.resolve_ids(user_id)
    return uid


# ── Aggregations for /analytics/* ─────────────────────────────────────────────
def summary(user_id: str | None, days: int = 7) -> dict:
    out = {"period_days": days, "messages_user": 0, "messages_agent": 0, "tool_calls": 0,
           "tasks_created": 0, "tasks_completed": 0, "voice_sessions": 0, "initiatives": 0,
           "avg_response_ms": None, "avg_tool_ms": None}
    _map = {"message_user": "messages_user", "message_agent": "messages_agent",
            "tool_called": "tool_calls", "llm_call": "tool_calls", "task_created": "tasks_created",
            "task_completed": "tasks_completed", "voice_session": "voice_sessions",
            "initiative_sent": "initiatives"}
    try:
        if _pg():
            uid = _pg_uid(user_id)
            uf = "AND user_id = :uid" if uid else ""
            p = {"cut": datetime.now(timezone.utc) - timedelta(days=days), "uid": uid}
            for r in _pg_rows(f"SELECT kind, COUNT(*) n FROM events WHERE ts >= :cut {uf} GROUP BY kind", p):
                if r["kind"] in _map:
                    out[_map[r["kind"]]] += r["n"]
            ar = _pg_rows(f"SELECT AVG(duration_ms) a FROM events WHERE ts>=:cut {uf} AND kind IN ('message_agent','llm_call') AND duration_ms IS NOT NULL", p)
            out["avg_response_ms"] = round(ar[0]["a"]) if ar and ar[0]["a"] is not None else None
            tr = _pg_rows(f"SELECT AVG(duration_ms) a FROM events WHERE ts>=:cut {uf} AND kind IN ('tool_called','llm_call') AND duration_ms IS NOT NULL", p)
            out["avg_tool_ms"] = round(tr[0]["a"]) if tr and tr[0]["a"] is not None else None
            return out
        init()
        cut = _cutoff(days)
        with _conn() as c:
            by = {r["kind"]: r["n"] for r in c.execute("SELECT kind, COUNT(*) n FROM events WHERE ts>=? GROUP BY kind", (cut,))}
            for k, dest in _map.items():
                out[dest] += by.get(k, 0)
            r = c.execute("SELECT AVG(duration_ms) a FROM events WHERE ts>=? AND kind='message_agent' AND duration_ms IS NOT NULL", (cut,)).fetchone()
            out["avg_response_ms"] = round(r["a"]) if r and r["a"] is not None else None
            r = c.execute("SELECT AVG(duration_ms) a FROM events WHERE ts>=? AND kind='tool_called' AND duration_ms IS NOT NULL", (cut,)).fetchone()
            out["avg_tool_ms"] = round(r["a"]) if r and r["a"] is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("summary failed: %s", e)
    return out


def tools(user_id: str | None, days: int = 30) -> dict:
    items = []
    try:
        if _pg():
            uid = _pg_uid(user_id)
            uf = "AND user_id = :uid" if uid else ""
            p = {"cut": datetime.now(timezone.utc) - timedelta(days=days), "uid": uid}
            rows = _pg_rows(f"SELECT name, COUNT(*) calls, SUM(CASE WHEN success THEN 1 ELSE 0 END) ok, "
                            f"AVG(duration_ms) avg_ms FROM events WHERE ts>=:cut {uf} AND kind IN ('tool_called','llm_call') "
                            f"AND name IS NOT NULL GROUP BY name ORDER BY calls DESC", p)
        else:
            init()
            cut = _cutoff(days)
            with _conn() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT name, COUNT(*) calls, SUM(COALESCE(success,0)) ok, AVG(duration_ms) avg_ms "
                    "FROM events WHERE ts>=? AND kind='tool_called' AND name IS NOT NULL GROUP BY name ORDER BY calls DESC", (cut,))]
        for r in rows:
            calls = r["calls"] or 0
            items.append({"name": r["name"], "calls": calls,
                          "success_rate": round((r["ok"] or 0) / calls, 3) if calls else 0,
                          "avg_ms": round(r["avg_ms"]) if r["avg_ms"] is not None else None})
    except Exception as e:  # noqa: BLE001
        log.warning("tools agg failed: %s", e)
    return {"period_days": days, "tools": items}


def tasks_funnel(user_id: str | None, days: int = 30) -> dict:
    out = {"period_days": days, "created": 0, "completed": 0, "completion_rate": 0.0}
    try:
        if _pg():
            uid = _pg_uid(user_id)
            uf = "AND user_id = :uid" if uid else ""
            p = {"cut": datetime.now(timezone.utc) - timedelta(days=days), "uid": uid}
            rows = _pg_rows(f"SELECT kind, COUNT(*) n FROM events WHERE ts>=:cut {uf} AND kind IN ('task_created','task_completed') GROUP BY kind", p)
            by = {r["kind"]: r["n"] for r in rows}
            out["created"], out["completed"] = by.get("task_created", 0), by.get("task_completed", 0)
        else:
            init()
            cut = _cutoff(days)
            with _conn() as c:
                out["created"] = c.execute("SELECT COUNT(*) n FROM events WHERE ts>=? AND kind='task_created'", (cut,)).fetchone()["n"]
                out["completed"] = c.execute("SELECT COUNT(*) n FROM events WHERE ts>=? AND kind='task_completed'", (cut,)).fetchone()["n"]
        out["completion_rate"] = round(out["completed"] / out["created"], 3) if out["created"] else 0.0
    except Exception as e:  # noqa: BLE001
        log.warning("tasks funnel failed: %s", e)
    return out


def response_quality(user_id: str | None, days: int = 7) -> dict:
    out = {"period_days": days, "avg_response_ms": None, "p50_ms": None, "p95_ms": None,
           "voice_sessions": 0, "avg_voice_ms": None}
    try:
        if _pg():
            uid = _pg_uid(user_id)
            uf = "AND user_id = :uid" if uid else ""
            p = {"cut": datetime.now(timezone.utc) - timedelta(days=days), "uid": uid}
            durs = [r["duration_ms"] for r in _pg_rows(
                f"SELECT duration_ms FROM events WHERE ts>=:cut {uf} AND kind IN ('message_agent','llm_call') "
                f"AND duration_ms IS NOT NULL ORDER BY duration_ms", p)]
            vr = _pg_rows(f"SELECT COUNT(*) n, AVG(duration_ms) a FROM events WHERE ts>=:cut {uf} AND kind='voice_session'", p)
            vn, va = (vr[0]["n"], vr[0]["a"]) if vr else (0, None)
        else:
            init()
            cut = _cutoff(days)
            with _conn() as c:
                durs = [r["duration_ms"] for r in c.execute("SELECT duration_ms FROM events WHERE ts>=? AND kind='message_agent' AND duration_ms IS NOT NULL ORDER BY duration_ms", (cut,))]
                r = c.execute("SELECT COUNT(*) n, AVG(duration_ms) a FROM events WHERE ts>=? AND kind='voice_session'", (cut,)).fetchone()
                vn, va = r["n"] or 0, r["a"]
        if durs:
            out["avg_response_ms"] = round(sum(durs) / len(durs))
            out["p50_ms"] = durs[len(durs) // 2]
            out["p95_ms"] = durs[min(len(durs) - 1, int(len(durs) * 0.95))]
        out["voice_sessions"] = vn or 0
        out["avg_voice_ms"] = round(va) if va is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("response_quality failed: %s", e)
    return out


def active_hours(user_id: str | None, days: int = 30) -> dict:
    buckets = [0] * 24
    try:
        if _pg():
            uid = _pg_uid(user_id)
            uf = "AND user_id = :uid" if uid else ""
            p = {"cut": datetime.now(timezone.utc) - timedelta(days=days), "uid": uid}
            for r in _pg_rows(f"SELECT EXTRACT(HOUR FROM ts) h, COUNT(*) n FROM events WHERE ts>=:cut {uf} "
                              f"AND kind IN ('message_user','message_agent','llm_call') GROUP BY 1", p):
                hh = int(r["h"])
                if 0 <= hh < 24:
                    buckets[hh] = r["n"]
        else:
            init()
            cut = _cutoff(days)
            with _conn() as c:
                for r in c.execute("SELECT ts FROM events WHERE ts>=? AND kind IN ('message_user','message_agent')", (cut,)):
                    try:
                        buckets[datetime.fromisoformat(r["ts"]).hour] += 1
                    except Exception:
                        pass
    except Exception as e:  # noqa: BLE001
        log.warning("active_hours failed: %s", e)
    return {"period_days": days, "by_hour": buckets}


def per_agent(user_id: str | None, days: int = 30) -> dict:
    """Per-agent performance from llm_call events (agent id in meta) — for /analytics/agents."""
    items = []
    try:
        if _pg():
            uid = _pg_uid(user_id)
            uf = "AND user_id = :uid" if uid else ""
            p = {"cut": datetime.now(timezone.utc) - timedelta(days=days), "uid": uid}
            rows = _pg_rows(
                f"SELECT COALESCE(meta->>'agent_id','primary') agent, COUNT(*) calls, "
                f"SUM(CASE WHEN success THEN 1 ELSE 0 END) ok, AVG(duration_ms) avg_ms, "
                f"SUM(COALESCE((meta->>'tokens_in')::int,0)) tin, SUM(COALESCE((meta->>'tokens_out')::int,0)) tout, "
                f"SUM(COALESCE(cost_micros,0)) cost FROM events WHERE ts>=:cut {uf} AND kind='llm_call' GROUP BY 1 ORDER BY calls DESC", p)
            for r in rows:
                calls = r["calls"] or 0
                items.append({"agent": r["agent"], "calls": calls,
                              "success_rate": round((r["ok"] or 0) / calls, 3) if calls else 0,
                              "avg_ms": round(r["avg_ms"]) if r["avg_ms"] is not None else None,
                              "tokens_in": r["tin"] or 0, "tokens_out": r["tout"] or 0,
                              "cost_micros": r["cost"] or 0})
    except Exception as e:  # noqa: BLE001
        log.warning("per_agent failed: %s", e)
    return {"period_days": days, "agents": items}
