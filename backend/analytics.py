"""
Read-only analytics over the agent's own data (tasks.db + email_log.json).

A narrow, parameterized allowlist — NOT free-form text-to-SQL — so it can be safely
exposed to the LLM as a tool ("how many tasks did I finish last week?").
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import BASE_DIR

# settings.DB_PATH points at BASE_DIR/"tasks.db" which is wrong (the real DB lives in
# tasks/tasks.db); use the correct path here.
DB_PATH = str(BASE_DIR / "tasks" / "tasks.db")
EMAIL_LOG = BASE_DIR / "logs" / "email_log.json"

METRICS = [
    "summary",              # totals + pending-by-priority
    "completed",            # tasks completed in the last N days
    "created",              # tasks created in the last N days
    "pending_by_priority",  # open tasks grouped by priority
    "top_contacts",         # most-triaged senders (from email_log)
    "triage_volume",        # emails triaged in the last N days
]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _cutoff(days: int) -> str:
    return (datetime.utcnow() - timedelta(days=days)).isoformat()


def _load_email_log() -> list[dict]:
    try:
        data = json.loads(EMAIL_LOG.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def run_metric(metric: str, user_id: str, days: int = 7) -> dict:
    if metric not in METRICS:
        return {"error": f"unknown metric '{metric}'", "available": METRICS}

    if metric == "summary":
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM tasks WHERE user_id=?", (user_id,)).fetchone()[0]
            pending = c.execute("SELECT COUNT(*) FROM tasks WHERE user_id=? AND status='pending'", (user_id,)).fetchone()[0]
            done = c.execute("SELECT COUNT(*) FROM tasks WHERE user_id=? AND status='done'", (user_id,)).fetchone()[0]
            by_pri = {r["priority"]: r["n"] for r in c.execute(
                "SELECT priority, COUNT(*) n FROM tasks WHERE user_id=? AND status='pending' GROUP BY priority", (user_id,))}
        return {"metric": metric, "total": total, "pending": pending, "done": done,
                "pending_by_priority": by_pri,
                "human": f"{pending} pending, {done} done, {total} total tasks."}

    if metric == "completed":
        with _conn() as c:
            rows = c.execute(
                "SELECT title, updated_at FROM tasks WHERE user_id=? AND status='done' AND updated_at>=? ORDER BY updated_at DESC",
                (user_id, _cutoff(days))).fetchall()
        titles = [r["title"] for r in rows]
        return {"metric": metric, "days": days, "count": len(titles), "titles": titles,
                "human": f"You completed {len(titles)} task(s) in the last {days} day(s)."}

    if metric == "created":
        with _conn() as c:
            rows = c.execute(
                "SELECT title FROM tasks WHERE user_id=? AND created_at>=? ORDER BY created_at DESC",
                (user_id, _cutoff(days))).fetchall()
        titles = [r["title"] for r in rows]
        return {"metric": metric, "days": days, "count": len(titles), "titles": titles,
                "human": f"{len(titles)} task(s) were created in the last {days} day(s)."}

    if metric == "pending_by_priority":
        with _conn() as c:
            by_pri = {r["priority"]: r["n"] for r in c.execute(
                "SELECT priority, COUNT(*) n FROM tasks WHERE user_id=? AND status='pending' GROUP BY priority", (user_id,))}
        order = ["urgent", "high", "medium", "low"]
        parts = [f"{by_pri[p]} {p}" for p in order if p in by_pri]
        return {"metric": metric, "pending_by_priority": by_pri,
                "human": "Open tasks: " + (", ".join(parts) if parts else "none") + "."}

    if metric == "top_contacts":
        counts: dict[str, int] = {}
        for e in _load_email_log():
            s = (e.get("sender") or "").strip()
            if s:
                counts[s] = counts.get(s, 0) + 1
        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return {"metric": metric, "top": [{"sender": s, "count": n} for s, n in top],
                "human": ("Most-triaged senders: " + "; ".join(f"{s} ({n})" for s, n in top))
                         if top else "No triaged email on record yet."}

    if metric == "triage_volume":
        cutoff = _cutoff(days)
        n = sum(1 for e in _load_email_log() if (e.get("triaged_at") or "") >= cutoff)
        return {"metric": metric, "days": days, "count": n,
                "human": f"{n} email(s) triaged in the last {days} day(s)."}

    return {"error": "unhandled metric"}
