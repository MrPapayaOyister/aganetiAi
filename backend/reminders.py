"""Per-user reminders that surface on the DASHBOARD bell at fire time.

A lightweight JSON store (same pattern as notifications.py). The orchestrator
`set_reminder` tool writes here; a 60s scheduler job (fire_due_all) fires due
reminders by raising a dashboard notification (kind="reminder"). Best-effort;
never raises into the scheduler / tool loop.
"""
from __future__ import annotations

import glob
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from config.settings import MEMORY_DIR

_MAX = 200

# The org's wall-clock timezone. "at 5pm" / "tomorrow 9am" are interpreted here,
# then stored as UTC. Default Asia/Dubai (GST, UTC+4); override via APP_TIMEZONE.
try:
    from zoneinfo import ZoneInfo
    APP_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Dubai"))
except Exception:  # pragma: no cover — fallback to a fixed +4 offset
    APP_TZ = timezone(timedelta(hours=4))


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s or "anon"))[:80]


def _path(uid: str) -> str:
    return str(MEMORY_DIR / f"reminders_{_safe(uid)}.json")


def _load(uid: str) -> list[dict]:
    p = _path(uid)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save(uid: str, rows: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(_path(uid)), exist_ok=True)
        tmp = _path(uid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows[-_MAX:], f)
        os.replace(tmp, _path(uid))
    except Exception:
        pass


def parse_when(when: str, now: datetime | None = None) -> datetime | None:
    """Accept an ISO 8601 datetime OR a natural phrase ('in 30 minutes',
    'in 2 hours', 'at 5pm', 'tomorrow 9am'). Returns a tz-aware UTC datetime."""
    now = now or datetime.now(timezone.utc)
    if not when:
        return None
    raw = str(when).strip()
    # 1) ISO
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        pass
    s = raw.lower()
    # 2) relative: "in N minutes/hours/days"
    m = re.search(r"in\s+(\d+)\s*(sec|second|min|minute|hour|hr|day)s?", s)
    if m:
        n = int(m.group(1)); unit = m.group(2)
        mult = {"sec": 1, "second": 1, "min": 60, "minute": 60, "hour": 3600, "hr": 3600, "day": 86400}[unit]
        return now + timedelta(seconds=n * mult)
    # 3) absolute clock time: "at 5pm", "5:30 pm", "at 17:00" (+ optional 'tomorrow').
    #    Interpreted in the ORG timezone (APP_TZ), then converted to UTC.
    tm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if tm and ("at " in s or "am" in s or "pm" in s or ":" in s or "tomorrow" in s or "today" in s):
        h = int(tm.group(1)); mm = int(tm.group(2) or 0); ap = tm.group(3)
        if ap == "pm" and h < 12: h += 12
        if ap == "am" and h == 12: h = 0
        now_local = now.astimezone(APP_TZ)
        base = now_local + timedelta(days=1) if "tomorrow" in s else now_local
        dt_local = base.replace(hour=h, minute=mm, second=0, microsecond=0)
        if "tomorrow" not in s and dt_local <= now_local:
            dt_local += timedelta(days=1)  # next occurrence
        return dt_local.astimezone(timezone.utc)
    return None


def add(uid: str, message: str, when: str) -> dict:
    """Store a reminder. Returns {ok, id?, fire_at?, error?}."""
    dt = parse_when(when)
    if not dt:
        return {"ok": False, "error": "could_not_parse_time"}
    rows = _load(uid)
    row = {
        "id": str(uuid.uuid4()),
        "message": str(message or "Reminder"),
        "fire_at": dt.isoformat(),
        "fired": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rows.append(row)
    _save(uid, rows)
    return {"ok": True, "id": row["id"], "fire_at": row["fire_at"]}


def list_reminders(uid: str, include_fired: bool = False) -> list[dict]:
    rows = _load(uid)
    return [r for r in rows if include_fired or not r.get("fired")]


def _fire_due_for_file(path: str) -> int:
    """Fire due reminders in one user's file. Returns count fired."""
    fname = os.path.basename(path)
    m = re.match(r"reminders_(.+)\.json$", fname)
    if not m:
        return 0
    uid = m.group(1)
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            return 0
    except Exception:
        return 0
    now = datetime.now(timezone.utc)
    fired = 0
    changed = False
    from backend import notifications
    for r in rows:
        if r.get("fired"):
            continue
        try:
            fa = datetime.fromisoformat(str(r.get("fire_at")).replace("Z", "+00:00"))
        except Exception:
            continue
        if fa <= now:
            try:
                notifications.notify(uid, "⏰ Reminder", r.get("message", ""),
                                     kind="reminder", dedup_key=f"reminder:{r.get('id')}")
            except Exception:
                pass
            r["fired"] = True
            r["fired_at"] = now.isoformat()
            fired += 1
            changed = True
    if changed:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f)
            os.replace(tmp, path)
        except Exception:
            pass
    return fired


def fire_due_all() -> int:
    """Scan every user's reminder file and fire due ones (scheduler entrypoint)."""
    total = 0
    try:
        for path in glob.glob(str(MEMORY_DIR / "reminders_*.json")):
            total += _fire_due_for_file(path)
    except Exception:
        pass
    return total
