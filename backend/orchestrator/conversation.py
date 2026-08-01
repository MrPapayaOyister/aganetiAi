"""Per-session conversation history for the agent OS.

Gives the executor short-term memory of the CURRENT chat thread so follow-ups
resolve ("my email" after "fetch my mails", "send that one" after a draft).

STORAGE: this module is now a thin facade. The durable store is Postgres
(backend/chat/store.py: chat_messages + chat_artifacts); the original flat-JSON
files remain as a fallback and a rollback path. Which one is authoritative is
chosen by AGANETI_CHAT_STORE:

    json  JSON only                       (legacy behaviour, the default)
    dual  write both, read JSON           (rollout — verify parity risk-free)
    pg    read+write Postgres             (final state; JSON kept on disk)

The public signatures below are unchanged, because three live callers depend on
them: routes/agent_os.py, dashboard/ask.py and dashboard/stream.py.

Note the JSON path still trims to _MAX_TURNS to keep those files small. The
Postgres path does NOT trim — losing history is the bug this replaces.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from config.settings import MEMORY_DIR

_MAX_TURNS = 24      # messages persisted per session (JSON path only)
_CONTEXT_TURNS = 16  # messages fed back to the model as context


def _store():
    """Imported lazily so a database problem can never break this module's import
    (dashboard/ask.py imports it at module scope on the live SSE path)."""
    try:
        from backend.chat import store
        return store
    except Exception:
        return None


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s or "anon"))[:80]


def _path(uid: str, session_id: str) -> str:
    return str(MEMORY_DIR / f"chat_{_safe(uid)}_{_safe(session_id)}.json")


# ── JSON implementation (unchanged behaviour) ────────────────────────────────
def _json_load_full(uid: str, session_id: str) -> list[dict]:
    path = _path(uid, session_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _json_append(uid: str, session_id: str, role: str, content: str) -> None:
    path = _path(uid, session_id)
    rows = _json_load_full(uid, session_id)
    rows.append({"role": role, "content": content, "ts": datetime.now(timezone.utc).isoformat()})
    rows = rows[-_MAX_TURNS:]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
    except Exception:
        pass


# ── public API ────────────────────────────────────────────────────────────────
def load_full(uid: str, session_id: str) -> list[dict]:
    """Full stored history (with timestamps, and artifacts on the pg path) — for
    the UI history endpoint."""
    st = _store()
    if st is not None and st.reads_pg():
        rows = st.load_full(uid, session_id)
        if rows:
            return rows
        # An empty result on a thread that predates the backfill: fall back to JSON
        # so no user ever sees their history vanish mid-rollout.
    return _json_load_full(uid, session_id)


def load(uid: str, session_id: str, limit: int = _CONTEXT_TURNS) -> list[dict]:
    """Prior turns as clean executor messages ({role, content}), oldest→newest."""
    st = _store()
    if st is not None and st.reads_pg():
        rows = st.load(uid, session_id, limit=limit)
        if rows:
            return rows
    rows = _json_load_full(uid, session_id)
    out = [{"role": r.get("role"), "content": r.get("content", "")}
           for r in rows if r.get("role") in ("user", "assistant") and r.get("content")]
    return out[-limit:]


def append(uid: str, session_id: str, role: str, content: str, **kw) -> str | None:
    """Persist one turn. Returns the Postgres message id when there is one, so the
    caller can attach artifacts to it. Extra kwargs (agent, model_key, tool_calls,
    meta) are recorded on the pg path and ignored by the JSON path."""
    if not content:
        return None
    msg_id = None
    st = _store()
    if st is not None and st.enabled():
        msg_id = st.append(uid, session_id, role, content, **kw)
    if st is None or st.mode() != "pg":
        _json_append(uid, session_id, role, content)
    return msg_id


def count(uid: str, session_id: str) -> int:
    """Number of stored messages in this session (for the registry message_count)."""
    st = _store()
    if st is not None and st.reads_pg():
        n = st.count(uid, session_id)
        if n:
            return n
    return len(_json_load_full(uid, session_id))


def delete(uid: str, session_id: str) -> None:
    """Delete the session's stored body (called when a session is deleted)."""
    st = _store()
    if st is not None and st.enabled():
        st.delete(uid, session_id)
    try:
        os.remove(_path(uid, session_id))
    except OSError:
        pass
