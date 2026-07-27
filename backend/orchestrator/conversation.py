"""Per-session conversation history for the agent OS.

Gives the executor short-term memory of the CURRENT chat thread so follow-ups
resolve ("my email" after "fetch my mails", "send that one" after a draft). Stored
as a small JSON file per (user, session), separate from long-term Qdrant memory,
and bounded so the prompt + file stay small. This is the fix for the "stateless,
keeps re-asking" behaviour.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from config.settings import MEMORY_DIR

_MAX_TURNS = 24      # messages persisted per session
_CONTEXT_TURNS = 16  # messages fed back to the model as context


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s or "anon"))[:80]


def _path(uid: str, session_id: str) -> str:
    return str(MEMORY_DIR / f"chat_{_safe(uid)}_{_safe(session_id)}.json")


def load_full(uid: str, session_id: str) -> list[dict]:
    """Full stored history (with timestamps) — for the UI history endpoint."""
    path = _path(uid, session_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def load(uid: str, session_id: str, limit: int = _CONTEXT_TURNS) -> list[dict]:
    """Prior turns as clean executor messages ({role, content}), oldest→newest."""
    rows = load_full(uid, session_id)
    out = [{"role": r.get("role"), "content": r.get("content", "")}
           for r in rows if r.get("role") in ("user", "assistant") and r.get("content")]
    return out[-limit:]


def append(uid: str, session_id: str, role: str, content: str) -> None:
    if not content:
        return
    path = _path(uid, session_id)
    rows = load_full(uid, session_id)
    rows.append({"role": role, "content": content, "ts": datetime.now(timezone.utc).isoformat()})
    rows = rows[-_MAX_TURNS:]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
    except Exception:
        pass


def count(uid: str, session_id: str) -> int:
    """Number of stored messages in this session (for the registry message_count)."""
    return len(load_full(uid, session_id))


def delete(uid: str, session_id: str) -> None:
    """Unlink the session's JSON body file (called when a session is deleted)."""
    try:
        os.remove(_path(uid, session_id))
    except OSError:
        pass
