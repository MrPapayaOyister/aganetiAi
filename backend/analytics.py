"""Read-only analytics over the agent's own data.

A narrow, parameterized allowlist (NOT free-form SQL) safe to expose as a tool.
Task metrics read through tasks.store (dual-backend: SQLite or Postgres); email
metrics read the email log file.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from config.settings import BASE_DIR

EMAIL_LOG = BASE_DIR / "logs" / "email_log.json"

METRICS = ["summary", "completed", "created", "pending_by_priority", "top_contacts", "triage_volume"]


def _cutoff(days: int) -> str:
    return (datetime.utcnow() - timedelta(days=days)).isoformat()


def _load_email_log() -> list[dict]:
    try:
        data = json.loads(EMAIL_LOG.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _tasks(user_id: str, status: str | None = None) -> list[dict]:
    try:
        from tasks.store import get_all_tasks
        return get_all_tasks(user_id, status=status)
    except Exception:
        return []


def run_metric(metric: str, user_id: str, days: int = 7) -> dict:
    if metric not in METRICS:
        return {"error": f"unknown metric '{metric}'", "available": METRICS}

    if metric == "summary":
        allt = _tasks(user_id)
        pending = [t for t in allt if t.get("status") == "pending"]
        done = [t for t in allt if t.get("status") == "done"]
        by_pri: dict[str, int] = {}
        for t in pending:
            by_pri[t.get("priority", "medium")] = by_pri.get(t.get("priority", "medium"), 0) + 1
        return {"metric": metric, "total": len(allt), "pending": len(pending), "done": len(done),
                "pending_by_priority": by_pri,
                "human": f"{len(pending)} pending, {len(done)} done, {len(allt)} total tasks."}

    if metric == "completed":
        cut = _cutoff(days)
        titles = [t["title"] for t in _tasks(user_id, status="done") if (t.get("updated_at") or "") >= cut]
        return {"metric": metric, "days": days, "count": len(titles), "titles": titles,
                "human": f"You completed {len(titles)} task(s) in the last {days} day(s)."}

    if metric == "created":
        cut = _cutoff(days)
        titles = [t["title"] for t in _tasks(user_id) if (t.get("created_at") or "") >= cut]
        return {"metric": metric, "days": days, "count": len(titles), "titles": titles,
                "human": f"{len(titles)} task(s) were created in the last {days} day(s)."}

    if metric == "pending_by_priority":
        by_pri: dict[str, int] = {}
        for t in _tasks(user_id, status="pending"):
            by_pri[t.get("priority", "medium")] = by_pri.get(t.get("priority", "medium"), 0) + 1
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
                "human": ("Most-triaged senders: " + "; ".join(f"{s} ({n})" for s, n in top)) if top
                         else "No triaged email on record yet."}

    if metric == "triage_volume":
        cutoff = _cutoff(days)
        n = sum(1 for e in _load_email_log() if (e.get("triaged_at") or "") >= cutoff)
        return {"metric": metric, "days": days, "count": n,
                "human": f"{n} email(s) triaged in the last {days} day(s)."}

    return {"error": "unhandled metric"}
