"""Per-user dashboard notifications (the header bell).

A lightweight JSON store (same pattern as orchestrator/conversation.py) that the
frontend polls. Kinds:
  * "mail"          — a new email arrived (info); entity: {message_id, sender, subject}
  * "task_proposal" — the agent suggests a task from an email; APPROVE → creates it;
                      entity: {title, priority, source, subject}
  * "info"          — generic.
Bounded + best-effort; a notification failure must never break the mail poll.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone

from config.settings import MEMORY_DIR

_MAX = 100


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s or "anon"))[:80]


def _path(uid: str) -> str:
    return str(MEMORY_DIR / f"notif_{_safe(uid)}.json")


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
            json.dump(rows, f)
        os.replace(tmp, _path(uid))
    except Exception:
        pass


def notify(uid: str, title: str, body: str = "", kind: str = "info",
           entity: dict | None = None, dedup_key: str | None = None) -> dict | None:
    """Append a notification. dedup_key (e.g. 'mail:<msgid>') prevents duplicates."""
    rows = _load(uid)
    if dedup_key and any(r.get("dedup_key") == dedup_key for r in rows):
        return None
    row = {"id": str(uuid.uuid4()), "title": title, "body": body, "kind": kind,
           "entity": entity or {}, "read": False, "acted": False,
           "dedup_key": dedup_key, "created_at": datetime.now(timezone.utc).isoformat()}
    rows.append(row)
    _save(uid, rows[-_MAX:])
    return row


def list_notifications(uid: str, limit: int = 50) -> list[dict]:
    return list(reversed(_load(uid)))[:limit]  # newest first


def unread_count(uid: str) -> int:
    return sum(1 for r in _load(uid) if not r.get("read"))


def get(uid: str, nid: str) -> dict | None:
    return next((r for r in _load(uid) if r.get("id") == nid), None)


def _update(uid: str, nid: str, **fields) -> bool:
    rows = _load(uid)
    hit = False
    for r in rows:
        if r.get("id") == nid:
            r.update(fields)
            hit = True
    if hit:
        _save(uid, rows)
    return hit


def mark_read(uid: str, nid: str) -> bool:
    return _update(uid, nid, read=True)


def mark_all_read(uid: str) -> None:
    rows = _load(uid)
    for r in rows:
        r["read"] = True
    _save(uid, rows)


def set_acted(uid: str, nid: str) -> bool:
    return _update(uid, nid, acted=True, read=True)
