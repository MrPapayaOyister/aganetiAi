"""Long-term memory: facts the assistant recalls ACROSS threads.

Distinct from per-thread history (backend/chat/store.py). A preference stated in one
conversation should still be known in a brand-new one.

Design:
  * Postgres `memory_items` is the system of record — stable ordering, pagination,
    a partial UNIQUE on fact_norm for dedup, soft-delete, cascade with the user.
  * Qdrant `user_memory_{ext_uid}` stays the retrieval index (unchanged collection
    naming, so facts written by the older tooling are still found). vector_id links
    the two; if Qdrant is unavailable the Postgres row still lands and a later
    write backfills the vector. A fact is never lost because the vector store blinked.

Recall is latency-bounded: it must never delay first token. Everything here fails
soft — memory is an enhancement, not a dependency.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select

from backend.db import models as M
from backend.db import sync as dbsync

log = logging.getLogger("aganeti.chat.memory")

MAX_INJECTED = 5          # facts placed in a system prompt
MAX_INJECTED_CHARS = 600
RECALL_TIMEOUT_S = 1.5    # hard ceiling on the recall path
MAX_ACTIVE = 300          # per user, before eviction


def normalise(fact: str) -> str:
    """Dedup key: lowercase, collapse whitespace, drop a leading 'the user ' and
    trailing punctuation, so 'The user prefers X.' == 'prefers x'."""
    t = (fact or "").strip().lower()
    t = re.sub(r"^(the\s+)?user\s+", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.rstrip(" .!?,;:")


def _row_to_dict(r) -> dict:
    return {
        "id": str(r.id), "fact": r.fact, "kind": r.kind, "source": r.source,
        "status": r.status, "confidence": float(r.confidence or 0),
        "use_count": int(r.use_count or 0),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


# ── Qdrant side (best-effort) ─────────────────────────────────────────────────
def _ext_uid(identity: str) -> str:
    """The EXTERNAL id the Qdrant collection is named after (supabase uid, or the
    'user_1' alias). memory_items.user_id is the internal uuid, so the two must be
    kept related explicitly — that is what meta.ext_uid records."""
    return str(identity)


def _vector_add(ext_uid: str, fact: str) -> str | None:
    try:
        from memory import long_term
        pid = str(uuid.uuid4())
        long_term.store_memory(ext_uid, fact, point_id=pid)  # type: ignore[call-arg]
        return pid
    except TypeError:
        try:
            from memory import long_term
            long_term.store_memory(ext_uid, fact)
            return None
        except Exception:
            return None
    except Exception:
        log.debug("memory: vector add failed (row still stored)", exc_info=True)
        return None


def _vector_search(ext_uid: str, query: str, k: int) -> list[str]:
    try:
        from memory import long_term
        res = long_term.search_memory(ext_uid, query, top_k=k)
        if isinstance(res, str):
            return [res] if res.strip() else []
        return [str(x) for x in (res or []) if str(x).strip()]
    except Exception:
        log.debug("memory: vector search failed", exc_info=True)
        return []


# ── writes ────────────────────────────────────────────────────────────────────
async def add(identity: str, fact: str, *, kind: str = "fact", source: str = "auto",
              confidence: float = 0.6, session_id: str | None = None) -> dict | None:
    import asyncio
    return await asyncio.to_thread(_add_sync, identity, fact, kind, source, confidence, session_id)


def _add_sync(identity, fact, kind, source, confidence, session_id) -> dict | None:
    fact = (fact or "").strip()
    if not fact:
        return None
    norm = normalise(fact)
    try:
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, identity)
            if user is None:
                return None
            # Exact duplicate → bump usage rather than accumulating restatements.
            existing = s.execute(select(M.MemoryItem).where(
                M.MemoryItem.user_id == user.id, M.MemoryItem.fact_norm == norm,
                M.MemoryItem.status == "active", M.MemoryItem.deleted_at.is_(None),
            )).scalar_one_or_none()
            if existing is not None:
                existing.use_count = (existing.use_count or 0) + 1
                existing.updated_at = datetime.now(timezone.utc)
                s.commit()
                return _row_to_dict(existing)

            vid = _vector_add(_ext_uid(identity), fact)
            row = M.MemoryItem(
                org_id=user.org_id, user_id=user.id, fact=fact, fact_norm=norm,
                kind=kind, source=source, confidence=confidence,
                vector_id=uuid.UUID(vid) if vid else None,
                meta={"ext_uid": _ext_uid(identity)},
                source_session_id=_as_uuid(session_id),
            )
            s.add(row)
            s.commit()
            _evict_if_needed(s, user.id)
            return _row_to_dict(row)
    except Exception:
        log.exception("memory: add failed")
        return None


def _as_uuid(v):
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError, AttributeError):
        return None


def _evict_if_needed(s, user_id) -> None:
    """Keep the set bounded: archive the least-used, oldest facts past the cap."""
    try:
        n = s.execute(select(func.count()).select_from(M.MemoryItem).where(
            M.MemoryItem.user_id == user_id, M.MemoryItem.status == "active",
            M.MemoryItem.deleted_at.is_(None))).scalar_one()
        if n <= MAX_ACTIVE:
            return
        victims = s.execute(select(M.MemoryItem).where(
            M.MemoryItem.user_id == user_id, M.MemoryItem.status == "active",
            M.MemoryItem.deleted_at.is_(None),
        ).order_by(M.MemoryItem.use_count.asc(), M.MemoryItem.updated_at.asc())
         .limit(n - MAX_ACTIVE)).scalars().all()
        for v in victims:
            v.status = "archived"
        s.commit()
    except Exception:
        log.debug("memory: eviction skipped", exc_info=True)


async def update(identity: str, item_id: str, *, fact: str | None = None,
                 status: str | None = None) -> dict | None:
    import asyncio
    return await asyncio.to_thread(_update_sync, identity, item_id, fact, status)


def _update_sync(identity, item_id, fact, status) -> dict | None:
    try:
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, identity)
            iid = _as_uuid(item_id)
            if user is None or iid is None:
                return None
            row = s.execute(select(M.MemoryItem).where(
                M.MemoryItem.id == iid, M.MemoryItem.user_id == user.id)).scalar_one_or_none()
            if row is None:
                return None
            if isinstance(fact, str) and fact.strip():
                row.fact = fact.strip()
                row.fact_norm = normalise(fact)
                row.source = "user"          # a correction is authoritative
                row.confidence = 1.0
                vid = _vector_add(_ext_uid(identity), row.fact)
                if vid:
                    row.vector_id = uuid.UUID(vid)
            if status in ("active", "archived"):
                row.status = status
            row.updated_at = datetime.now(timezone.utc)
            s.commit()
            return _row_to_dict(row)
    except Exception:
        log.exception("memory: update failed")
        return None


async def delete(identity: str, item_id: str) -> bool:
    import asyncio
    return await asyncio.to_thread(_delete_sync, identity, item_id)


def _delete_sync(identity, item_id) -> bool:
    try:
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, identity)
            iid = _as_uuid(item_id)
            if user is None or iid is None:
                return False
            row = s.execute(select(M.MemoryItem).where(
                M.MemoryItem.id == iid, M.MemoryItem.user_id == user.id)).scalar_one_or_none()
            if row is None:
                return False
            row.deleted_at = datetime.now(timezone.utc)
            row.status = "archived"
            s.commit()
            try:  # drop the vector too, so it stops being recalled
                from memory import long_term
                long_term.forget(_ext_uid(identity), row.fact)  # type: ignore[attr-defined]
            except Exception:
                pass
            return True
    except Exception:
        log.exception("memory: delete failed")
        return False


# ── reads ─────────────────────────────────────────────────────────────────────
async def list_items(identity: str, *, q: str | None = None, status: str = "active",
                     limit: int = 100, offset: int = 0) -> dict:
    import asyncio
    return await asyncio.to_thread(_list_sync, identity, q, status, limit, offset)


def _list_sync(identity, q, status, limit, offset) -> dict:
    try:
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, identity)
            if user is None:
                return {"items": [], "total": 0}
            base = select(M.MemoryItem).where(
                M.MemoryItem.user_id == user.id, M.MemoryItem.deleted_at.is_(None))
            if status in ("active", "archived"):
                base = base.where(M.MemoryItem.status == status)
            if q:
                base = base.where(M.MemoryItem.fact.ilike(f"%{q}%"))
            total = s.execute(select(func.count()).select_from(base.subquery())).scalar_one()
            rows = s.execute(base.order_by(M.MemoryItem.updated_at.desc())
                             .limit(limit).offset(offset)).scalars().all()
            return {"items": [_row_to_dict(r) for r in rows], "total": int(total or 0)}
    except Exception:
        log.exception("memory: list failed")
        return {"items": [], "total": 0}


async def recall(identity: str, query: str, k: int = MAX_INJECTED) -> str:
    """Facts relevant to this turn, as a prompt block. Returns "" on any problem.

    Time-boxed: memory must never add more than RECALL_TIMEOUT_S to first token.
    """
    import asyncio
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_recall_sync, identity, query, k), timeout=RECALL_TIMEOUT_S)
    except Exception:
        return ""


def _recall_sync(identity, query, k) -> str:
    try:
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, identity)
            if user is None:
                return ""
            rows = s.execute(select(M.MemoryItem).where(
                M.MemoryItem.user_id == user.id, M.MemoryItem.status == "active",
                M.MemoryItem.deleted_at.is_(None),
            ).order_by(M.MemoryItem.updated_at.desc()).limit(MAX_ACTIVE)).scalars().all()
            if not rows:
                return ""

            # Prefer vector hits; fall back to most-recent when Qdrant is unavailable.
            hits = _vector_search(_ext_uid(identity), query, k) if query else []
            by_norm = {r.fact_norm: r for r in rows}
            chosen, seen = [], set()
            for h in hits:
                r = by_norm.get(normalise(h))
                if r is not None and r.id not in seen:
                    chosen.append(r); seen.add(r.id)
            for r in rows:
                if len(chosen) >= k:
                    break
                if r.id not in seen:
                    chosen.append(r); seen.add(r.id)

            lines, used = [], 0
            for r in chosen[:k]:
                line = f"- {r.fact}"
                if used + len(line) > MAX_INJECTED_CHARS:
                    break
                lines.append(line); used += len(line)
            if not lines:
                return ""

            ids = [r.id for r in chosen[:len(lines)]]
            now = datetime.now(timezone.utc)
            for r in rows:
                if r.id in ids:
                    r.use_count = (r.use_count or 0) + 1
                    r.last_used_at = now
            s.commit()

            return ("WHAT YOU KNOW ABOUT THIS USER (long-term memory — use it silently; "
                    "never recite it back unless asked):\n" + "\n".join(lines))
    except Exception:
        log.debug("memory: recall failed", exc_info=True)
        return ""


# ── automatic capture ─────────────────────────────────────────────────────────
# A first-person declarative is the cheap signal that a turn contains something
# durable. Gating on this avoids an LLM call on every message.
_WORTH_REMEMBERING = re.compile(
    r"\b(i (prefer|like|want|need|always|never|usually|am|work|use|report|present)"
    r"|my \w+"
    r"|we (always|usually|prefer|report|need|track)"
    r"|call me|refer to me"
    r"|remember (that|this)"
    r"|from now on|going forward|in future"
    r"|(please )?always \w+"
    r"|our (fiscal|policy|process|team|board|reporting|currency|format))\b", re.I)


def worth_extracting(message: str, turn_index: int = 0) -> bool:
    t = (message or "").strip()
    if not t:
        return False
    if re.search(r"\bremember\b", t, re.I):
        return True
    if _WORTH_REMEMBERING.search(t):
        return True
    return turn_index > 0 and turn_index % 4 == 0


async def capture_turn(identity: str, user_message: str, session_id: str | None = None) -> None:
    """Extract durable facts from a turn. Fire-and-forget AFTER the response is sent —
    it must never delay or break the stream."""
    try:
        text = (user_message or "").strip()
        if not text:
            return
        # An explicit "remember X" is stored verbatim; no LLM needed.
        m = re.search(r"\bremember(?:\s+that|\s+this)?[:,]?\s+(.{4,240})", text, re.I)
        if m:
            await add(identity, m.group(1).strip(), kind="fact", source="user",
                      confidence=1.0, session_id=session_id)
            return
        facts = await _extract_facts(text)
        for f in facts[:3]:
            await add(identity, f, kind="preference", source="auto",
                      confidence=0.6, session_id=session_id)
    except Exception:
        log.debug("memory: capture failed", exc_info=True)


async def _extract_facts(text: str) -> list[str]:
    """One cheap local call. Returns [] on any problem — never raises."""
    import json as _json
    try:
        from backend.orchestrator import llm
        msg = await llm.chat(
            [{"role": "system", "content":
              "Extract durable facts or preferences about the USER from their message — "
              "things worth recalling in a FUTURE, unrelated conversation (role, "
              "preferences, recurring constraints, how they want things done). Ignore "
              "one-off requests, questions and pleasantries. Reply with a JSON array of "
              "short third-person strings, for example: prefers figures in AED. "
              "Reply with an empty array if there is nothing durable."},
             {"role": "user", "content": text[:1500]}],
            tools=None, tier="fast", temperature=0.0, max_tokens=200,
            agent={"model_key": "gateway-fast", "fallback_models": ["gateway"]})
        raw = (msg or {}).get("content") or ""
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return []
        out = _json.loads(raw[start:end + 1])
        return [str(x).strip() for x in out if isinstance(x, str) and 3 < len(str(x).strip()) <= 240]
    except Exception:
        return []
