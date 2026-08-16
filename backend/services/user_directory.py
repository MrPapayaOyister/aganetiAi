"""The user directory, read from the database.

Replaces `config/users.py`, a two-entry dict built from USER_1_* / USER_2_* env
vars. That registry decided who could log in, what their Telegram chat id was,
and which agent spoke for them — so onboarding meant editing .env and restarting
the process, and the ceiling was however many entries someone would hand-write.
It had already drifted: seven rows in `users`, two in the registry.

Identity here is the Supabase subject (`users.supabase_uid`), and that choice is
load-bearing rather than incidental:

  * it is what the auth middleware already resolves a bearer token to, so no
    translation layer sits between "who called" and "whose data";
  * every row in every user-scoped table is already keyed by it — the audit that
    preceded this change found exactly one row in the whole database using a
    legacy alias, and it was migrated (scripts/fix_miskeyed_connection.py);
  * it is stable for the life of the account, unlike an email.

`users.id` (the internal UUID) remains the foreign key that other TABLES point
at; it is exposed here as `uid` for the queries that need to join.

Caching
-------
A TTL snapshot, because several callers are synchronous — the morning brief, the
APScheduler job registrar, document drafting — and cannot await a query. Those
read `snapshot()`/`name_for()` and tolerate a cold cache by degrading to the id.
Anything where a cold cache would be a CORRECTNESS problem is async and awaits
`refresh()` first: `by_telegram_id` in particular, since a miss there refuses a
legitimate user's message.

Call `await refresh(force=True)` after provisioning or changing a user so the
change is visible before the TTL lapses.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("aganeti.user_directory")

# Short enough that a newly-onboarded user appears without a restart; long enough
# that the per-message Telegram path is not a query per message.
_TTL = 60.0

_cache: dict[str, dict] = {}
_loaded_at: float = 0.0


def _entry(row) -> dict:
    """One directory record.

    `user_id` is the canonical id used everywhere in the app; `uid` is the
    internal UUID other tables' foreign keys point at. Both are strings so callers
    never have to care which flavour they hold.
    """
    return {
        "user_id": str(row.supabase_uid),
        "uid": str(row.id),
        "org_id": str(row.org_id) if row.org_id else None,
        "name": row.full_name or (row.email or "").split("@")[0] or str(row.supabase_uid),
        "email": row.email or "",
        "role": row.role or "employee",
        "status": row.status or "active",
        "telegram_chat_id": int(row.telegram_chat_id) if row.telegram_chat_id else 0,
        # The agent that speaks for this user in agent-to-agent messaging. Was a
        # hardcoded "agent_1"/"agent_2"; now the real row in `agents`.
        "agent_id": str(row.primary_agent_id) if row.primary_agent_id else None,
        "is_agent": True,
    }


async def refresh(force: bool = False) -> dict[str, dict]:
    """Reload the snapshot if stale. Never raises.

    A failed refresh keeps serving the previous snapshot rather than emptying it:
    losing the directory mid-request would log every user out of Telegram and
    strip names from every digest, which is a far worse failure than briefly
    stale data.
    """
    global _loaded_at
    now = time.monotonic()
    if not force and _cache and now - _loaded_at < _TTL:
        return _cache

    try:
        from sqlalchemy import text
        from backend.db.base import SessionLocal

        async with SessionLocal() as s:
            rows = (await s.execute(text(
                "select id, org_id, supabase_uid, email, full_name, role, status, "
                "       telegram_chat_id, primary_agent_id "
                "from users "
                "where status = 'active' and deleted_at is null "
                "  and supabase_uid is not null and supabase_uid <> '' "
                "order by created_at"
            ))).all()
    except Exception as e:  # noqa: BLE001
        log.warning("user directory refresh failed, serving %d cached entr%s: %s",
                    len(_cache), "y" if len(_cache) == 1 else "ies", e)
        return _cache

    fresh = {}
    for r in rows:
        try:
            e = _entry(r)
            fresh[e["user_id"]] = e
        except Exception:  # noqa: BLE001 — one malformed row must not blank the directory
            log.exception("skipping malformed user row")
    _cache.clear()
    _cache.update(fresh)
    _loaded_at = now
    return _cache


def snapshot() -> dict[str, dict]:
    """The cached directory, keyed by canonical user_id. SYNC — may be cold.

    Shaped like the dict it replaces so the call sites that iterated `USERS`
    keep working; prefer the accessors below for new code.
    """
    return dict(_cache)


def known(user_id: str) -> bool:
    """Whether this id is an active user. SYNC — cold cache answers False.

    Only ever used to reject, never to grant: authentication happens in
    backend/auth/enforce.py against a verified JWT, so a cold cache here can
    refuse a valid caller but can never admit an invalid one.
    """
    return bool(user_id) and user_id in _cache


def name_for(user_id: str) -> str:
    """Display name, degrading to the id itself. SYNC — safe on a cold cache."""
    return (_cache.get(user_id) or {}).get("name") or user_id


async def get(user_id: str) -> dict | None:
    await refresh()
    return _cache.get(user_id)


async def all_user_ids() -> list[str]:
    """Every active user. Used by the schedulers to fan digests out."""
    await refresh()
    return list(_cache)


async def by_telegram_id(chat_id: int) -> dict | None:
    """Resolve an inbound Telegram chat to a user, or None if unlinked.

    This is the authorisation check on the Telegram side — callers MUST treat
    None as "unauthorised" and refuse. Forced refresh on a miss so a user who
    linked Telegram moments ago is not turned away for up to a TTL; a hit is
    served from cache, keeping the common path query-free.
    """
    if not chat_id:
        return None
    await refresh()
    hit = _by_chat(chat_id)
    if hit is None:
        await refresh(force=True)
        hit = _by_chat(chat_id)
    return hit


def _by_chat(chat_id: int) -> dict | None:
    for e in _cache.values():
        if e.get("telegram_chat_id") == chat_id:
            return e
    return None


async def find_by_name(fragment: str) -> dict | None:
    """Fuzzy lookup for "delegate this to Akshay". None when ambiguous.

    Refuses on multiple matches rather than picking the first: delegation hands
    a task — and the notification that goes with it — to whoever is chosen, so an
    arbitrary tie-break would quietly route one person's work to another.
    """
    frag = (fragment or "").strip().lower()
    if not frag:
        return None
    await refresh()
    hits = [e for e in _cache.values()
            if frag in e["name"].lower() or frag in e["email"].lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        # Prefer an exact name match before giving up.
        exact = [e for e in hits if e["name"].lower() == frag]
        if len(exact) == 1:
            return exact[0]
        log.info("find_by_name(%r) matched %d users — refusing to guess", fragment, len(hits))
    return None
