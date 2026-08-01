"""Postgres-backed chat message store (the durable half of a conversation).

Replaces the flat-JSON store in orchestrator/conversation.py, which capped a thread
at 24 messages (silently destroying older turns) and could only hold
{role, content, ts} — so charts, tables and generated PDFs were unrepresentable.

Selected by AGANETI_CHAT_STORE:
    json  read+write the legacy JSON files only (the pre-existing behaviour)
    dual  write BOTH, read JSON            (rollout: verify parity with no risk)
    pg    read+write Postgres only         (final state; JSON left on disk as backup)

Every Postgres write is best-effort in `dual` mode: an exception is logged and
swallowed so a database hiccup can never break a live SSE turn. In `pg` mode a
read failure falls back to the JSON file rather than losing the thread.

Session identity: the frontend's session_id is usually a UUID, but the dashboard
passes a 32-hex board_id and a few legacy callers pass bare words ("analytics").
Anything non-UUID is mapped through uuid5 so the same key always lands on the same
row — see _session_uuid.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from backend.db import models as M
from backend.db import sync as dbsync

log = logging.getLogger("aganeti.chat.store")

# Namespace for deriving a stable session UUID from a non-UUID session key.
_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

_CONTEXT_TURNS_DEFAULT = 16
# Azure gpt-4.1 has 128k of context vs the local gateway's 32k, so a thread on the
# analytics agent can afford a much longer window before it needs trimming.
_CONTEXT_TURNS_WIDE = 40


def mode() -> str:
    m = (os.getenv("AGANETI_CHAT_STORE") or "json").strip().lower()
    return m if m in ("json", "dual", "pg") else "json"


def enabled() -> bool:
    """True when Postgres should be written to at all."""
    return mode() in ("dual", "pg") and os.getenv("AGANETI_DATA_BACKEND", "sqlite") == "postgres"


def reads_pg() -> bool:
    return mode() == "pg" and os.getenv("AGANETI_DATA_BACKEND", "sqlite") == "postgres"


def context_turns(agent_id: str | None = None) -> int:
    return _CONTEXT_TURNS_WIDE if agent_id in ("dashboard", "analytics") else _CONTEXT_TURNS_DEFAULT


def _session_uuid(session_key: str) -> uuid.UUID:
    """Stable UUID for a session key. Real UUIDs pass through unchanged so existing
    chat_sessions rows keep matching; everything else is derived deterministically."""
    try:
        return uuid.UUID(str(session_key))
    except (ValueError, TypeError, AttributeError):
        return uuid.uuid5(_NS, f"session:{session_key}")


def _is_board_key(session_key: str) -> bool:
    """32 hex chars with no dashes is what POST /dashboard/board-session mints."""
    s = str(session_key or "")
    return len(s) == 32 and all(c in "0123456789abcdefABCDEF" for c in s)


def _ensure_session(s, user, session_key: str):
    """Fetch or create the chat_sessions row for this key. Returns the ORM row."""
    sid = _session_uuid(session_key)
    row = s.get(M.ChatSession, sid)
    if row is not None:
        return row
    is_board = _is_board_key(session_key)
    row = M.ChatSession(
        id=sid,
        org_id=user.org_id,
        user_id=user.id,
        title=None,
        kind="analytics" if is_board else "assistant",
        # The session IS the board: keep the 32-hex id so save_chart files charts here.
        board_id=session_key if is_board else sid.hex,
    )
    s.add(row)
    s.flush()
    return row


# ── writes ────────────────────────────────────────────────────────────────────
def append(uid: str, session_key: str, role: str, content: str,
           *, agent: str | None = None, model_key: str | None = None,
           tool_calls: list | None = None, meta: dict | None = None) -> str | None:
    """Persist one turn. Returns the new message id (str) or None if not written."""
    if not enabled() or not content:
        return None
    try:
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, uid)
            if user is None:
                log.debug("chat store: unknown identity %r — skipping pg write", uid)
                return None
            sess = _ensure_session(s, user, session_key)
            nxt = s.execute(
                select(func.coalesce(func.max(M.ChatMessage.seq), 0) + 1)
                .where(M.ChatMessage.session_id == sess.id)
            ).scalar_one()
            msg = M.ChatMessage(
                org_id=user.org_id, user_id=user.id, session_id=sess.id, seq=nxt,
                role=role, content=content, agent=agent, model_key=model_key,
                tool_calls=tool_calls or [], meta=meta or {},
            )
            s.add(msg)
            # Keep the registry hot for the history list without a second round-trip.
            sess.message_count = (sess.message_count or 0) + 1
            sess.last_message_at = datetime.now(timezone.utc)
            sess.last_message_preview = content[:200]
            if not sess.title and role == "user":
                sess.title = content[:60]
            s.commit()
            return str(msg.id)
    except Exception:
        log.exception("chat store: append failed (swallowed — SSE must not break)")
        return None


def add_artifact(uid: str, session_key: str, *, kind: str, title: str | None = None,
                 spec: dict | None = None, data: list | dict | None = None,
                 uri: str | None = None, message_id: str | None = None,
                 meta: dict | None = None) -> str | None:
    """Persist a chart/table/pdf produced in a turn, so reopening the thread renders
    exactly what the user saw. `data` is capped — a snapshot, not a data warehouse."""
    if not enabled():
        return None
    try:
        snapshot = data
        m = dict(meta or {})
        if isinstance(data, list) and len(data) > 200:
            snapshot = data[:200]
            m["truncated"] = True
            m["row_count"] = len(data)
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, uid)
            if user is None:
                return None
            sess = _ensure_session(s, user, session_key)
            art = M.ChatArtifact(
                org_id=user.org_id, user_id=user.id, session_id=sess.id,
                message_id=uuid.UUID(message_id) if message_id else None,
                kind=kind, title=title, spec=spec or {}, data=snapshot, uri=uri, meta=m,
            )
            s.add(art)
            sess.artifact_count = (sess.artifact_count or 0) + 1
            s.commit()
            return str(art.id)
    except Exception:
        log.exception("chat store: add_artifact failed (swallowed)")
        return None


# ── reads ─────────────────────────────────────────────────────────────────────
def load_full(uid: str, session_key: str) -> list[dict]:
    """Every message in the thread, oldest→newest, with its artifacts attached."""
    try:
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, uid)
            if user is None:
                return []
            sid = _session_uuid(session_key)
            rows = s.execute(
                select(M.ChatMessage)
                .where(M.ChatMessage.session_id == sid,
                       M.ChatMessage.user_id == user.id,
                       M.ChatMessage.deleted_at.is_(None))
                .order_by(M.ChatMessage.seq)
            ).scalars().all()
            arts = s.execute(
                select(M.ChatArtifact)
                .where(M.ChatArtifact.session_id == sid,
                       M.ChatArtifact.deleted_at.is_(None))
                .order_by(M.ChatArtifact.created_at)
            ).scalars().all()
            by_msg: dict[str, list[dict]] = {}
            for a in arts:
                by_msg.setdefault(str(a.message_id), []).append({
                    "id": str(a.id), "kind": a.kind, "title": a.title,
                    "spec": a.spec or {}, "data": a.data, "uri": a.uri, "meta": a.meta or {},
                })
            out = []
            for r in rows:
                item = {
                    "role": r.role, "content": r.content or "",
                    "ts": r.created_at.isoformat() if r.created_at else "",
                }
                got = by_msg.get(str(r.id))
                if got:
                    item["artifacts"] = got
                out.append(item)
            return out
    except Exception:
        log.exception("chat store: load_full failed")
        return []


def load(uid: str, session_key: str, limit: int = _CONTEXT_TURNS_DEFAULT) -> list[dict]:
    """Recent turns as executor messages. The FIRST user message is always kept as
    the thread anchor, so a long conversation doesn't lose what it is about."""
    rows = load_full(uid, session_key)
    msgs = [{"role": r.get("role"), "content": r.get("content", "")}
            for r in rows if r.get("role") in ("user", "assistant") and r.get("content")]
    if len(msgs) <= limit:
        return msgs
    anchor = next((m for m in msgs if m["role"] == "user"), None)
    tail = msgs[-limit:]
    if anchor and anchor not in tail:
        return [anchor, *tail[1:]] if len(tail) > 1 else [anchor, *tail]
    return tail


def count(uid: str, session_key: str) -> int:
    try:
        with dbsync.session() as s:
            sid = _session_uuid(session_key)
            return int(s.execute(
                select(func.count()).select_from(M.ChatMessage)
                .where(M.ChatMessage.session_id == sid, M.ChatMessage.deleted_at.is_(None))
            ).scalar_one() or 0)
    except Exception:
        return 0


def delete(uid: str, session_key: str) -> None:
    """Soft-delete the thread's messages + artifacts (the session row is handled by
    the existing repo.delete path)."""
    if not enabled():
        return
    try:
        now = datetime.now(timezone.utc)
        with dbsync.session() as s:
            sid = _session_uuid(session_key)
            for model in (M.ChatMessage, M.ChatArtifact):
                for row in s.execute(select(model).where(model.session_id == sid)).scalars():
                    row.deleted_at = now
            s.commit()
    except Exception:
        log.exception("chat store: delete failed (swallowed)")
