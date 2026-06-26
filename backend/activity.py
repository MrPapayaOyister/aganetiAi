"""
Activity-aware scheduling (P6).

Reads the P5 events log to know when a user was last active, then decides
how often background work (email checks) should actually run — instead of a
blind fixed interval. Also records job-run results for /scheduler/status.

Cadence policy (per user, evaluated each 30s poll tick):
    active now (msg < 30 min)          → email check every 2 min
    working hours, recently seen (<4h) → every 5 min
    working hours, idle 4h+            → every 15 min
    outside working hours              → every 30 min
    deep idle (no activity 24h+)       → every 60 min
"""
from __future__ import annotations

import sqlite3
import time
import logging
from datetime import datetime, timezone

from config.settings import BASE_DIR

log = logging.getLogger("aria.activity")
DB_PATH = str(BASE_DIR / "tasks" / "tasks.db")

# Local working hours (24h clock). Configurable later via prefs.
WORK_START = 8
WORK_END = 20

# Per-user last-run timestamps for each adaptive job (monotonic seconds).
_last_run: dict[str, float] = {}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def last_active(user_id: str) -> datetime | None:
    """Most recent message_user timestamp for this user, or None."""
    try:
        with _conn() as c:
            r = c.execute(
                "SELECT ts FROM events WHERE kind='message_user' AND user_id=? "
                "ORDER BY ts DESC LIMIT 1", (user_id,),
            ).fetchone()
        if r and r["ts"]:
            dt = datetime.fromisoformat(r["ts"])
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception as e:  # noqa: BLE001
        log.debug("last_active failed: %s", e)
    return None


def minutes_since_active(user_id: str) -> float | None:
    la = last_active(user_id)
    if not la:
        return None
    return (datetime.now(timezone.utc) - la).total_seconds() / 60.0


def is_working_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return WORK_START <= now.hour < WORK_END


def email_interval_minutes(user_id: str) -> float:
    """Desired minutes between email checks for this user, given activity."""
    mins = minutes_since_active(user_id)
    if mins is not None and mins < 30:
        return 2.0
    working = is_working_hours()
    if mins is None or mins >= 24 * 60:
        return 60.0
    if working and mins < 4 * 60:
        return 5.0
    if working:
        return 15.0
    return 30.0


def should_run(job_key: str, user_id: str, interval_minutes: float) -> bool:
    """Rate-limit a job per (job_key,user) to the adaptive interval. Called from
    the 30s poll tick — returns True only when enough time has elapsed."""
    k = f"{job_key}:{user_id}"
    now = time.monotonic()
    last = _last_run.get(k, 0.0)
    if (now - last) >= interval_minutes * 60.0:
        _last_run[k] = now
        return True
    return False


def record_job_run(name: str, *, user_id: str | None = None,
                   result: str = "", error: str | None = None) -> None:
    """Persist a job run to the events table (kind='job_run') for /scheduler/status."""
    try:
        from backend import events
        events.log_event("job_run", user_id=user_id, name=name,
                         success=(error is None), meta={"result": result[:300], "error": error})
    except Exception:  # noqa: BLE001
        pass


def job_status() -> list[dict]:
    """Last run + error count per job name, from the events log."""
    out: list[dict] = []
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT name, COUNT(*) runs, "
                "       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) errors, "
                "       MAX(ts) last_run "
                "FROM events WHERE kind='job_run' AND name IS NOT NULL "
                "GROUP BY name ORDER BY last_run DESC"
            ).fetchall()
            out = [{"name": r["name"], "runs": r["runs"], "errors": r["errors"] or 0,
                    "last_run": r["last_run"]} for r in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("job_status failed: %s", e)
    return out
