"""
Which tool groups are switched off, per user and per session.

Two scopes, because one is not enough:

  * a USER default is what people actually want ("I never use email here"), and
  * a SESSION override is what makes it useful ("this thread is just research").

Session-only would mean re-toggling every new chat; user-only would mean you
cannot scope a single thread. Resolution is session ?? user ?? nothing-disabled.
The session value is an OVERRIDE, not a union: a thread that explicitly enables
email must win over a user default that disables it, and a union could not
express that.

Stored as the NEGATIVE — the disabled list, not the enabled one. Absent means
enabled, so shipping a 20th tool does not require touching anyone's stored
preferences, and a stale row can never hide a tool that did not exist when it
was written. Storing the positive would silently withhold every new tool from
every existing user.

Both homes already exist, so there is no migration:
    users.settings          JSONB, annotated "UI/user prefs"
    chat_sessions.meta      JSONB, where the session `source` marker lives

Shape in both: {"tools": {"disabled": ["email", "media"]}}

NOT localStorage: this has to survive a device change, for the same reason the
embeds do.
"""

from __future__ import annotations

import logging

log = logging.getLogger("aria.tool_prefs")

_KEY = "tools"
_FIELD = "disabled"


def _clean(value) -> list[str] | None:
    """Normalise a stored value to a list of known group ids, or None if unset.

    None and [] mean different things: None is "no preference at this scope"
    (fall through to the next one), [] is "explicitly nothing disabled" (stop
    here). Collapsing them would make a session that re-enables everything
    indistinguishable from a session with no opinion.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    from backend.tools import GROUP_IDS
    return [g for g in value if isinstance(g, str) and g in GROUP_IDS]


def _from_blob(blob) -> list[str] | None:
    if not isinstance(blob, dict):
        return None
    section = blob.get(_KEY)
    if not isinstance(section, dict):
        return None
    return _clean(section.get(_FIELD))


def get_disabled(user_id: str, session_id: str | None = None) -> list[str]:
    """The effective disabled groups: session override, else user default, else none."""
    try:
        from backend.chat import store as chat_store
        from backend.db import models as M
        from backend.db import sync as dbsync
        if not chat_store.enabled():
            return []
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            if user is None:
                return []
            if session_id:
                sess = s.get(M.ChatSession, chat_store._session_uuid(session_id))
                if sess is not None:
                    override = _from_blob(sess.meta)
                    if override is not None:
                        return override
            return _from_blob(user.settings) or []
    except Exception:
        # A preference lookup must never cost the user their tools: on any
        # failure fall back to everything enabled, which is the safe direction.
        log.exception("tool prefs: read failed — defaulting to all tools enabled")
        return []


def set_disabled(user_id: str, groups: list[str], session_id: str | None = None) -> list[str]:
    """Write the disabled list. With `session_id`, sets the session override;
    without, sets the user default. Returns what was stored."""
    from backend.tools import GROUP_IDS
    clean = sorted({g for g in (groups or []) if g in GROUP_IDS})
    try:
        from backend.chat import store as chat_store
        from backend.db import models as M
        from backend.db import sync as dbsync
        if not chat_store.enabled():
            log.warning("tool prefs: postgres disabled — preference NOT saved")
            return clean
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            if user is None:
                log.warning("tool prefs: identity %r did not resolve — NOT saved", user_id)
                return clean
            # Merge INTO the tools section, never replace it: `last_offered`
            # lives beside `disabled`, and clobbering it here would erase the
            # only record of what the previous turn saw — so the very act of
            # toggling would hide the change from note_availability_change.
            if session_id:
                sess = chat_store._ensure_session(s, user, session_id, "chat")
                meta = dict(sess.meta or {})
                meta[_KEY] = {**(meta.get(_KEY) or {}), _FIELD: clean}
                sess.meta = meta
            else:
                settings = dict(user.settings or {})
                settings[_KEY] = {**(settings.get(_KEY) or {}), _FIELD: clean}
                user.settings = settings
            s.commit()
    except Exception:
        log.exception("tool prefs: write failed")
    return clean


def clear_session_override(user_id: str, session_id: str) -> None:
    """Drop the session override so the thread follows the user default again."""
    try:
        from backend.chat import store as chat_store
        from backend.db import models as M
        from backend.db import sync as dbsync
        if not chat_store.enabled():
            return
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            if user is None:
                return
            sess = s.get(M.ChatSession, chat_store._session_uuid(session_id))
            if sess is None or not isinstance(sess.meta, dict):
                return
            meta = dict(sess.meta)
            meta.pop(_KEY, None)
            sess.meta = meta
            s.commit()
    except Exception:
        log.exception("tool prefs: clearing session override failed")


_LAST = "last_offered"


def note_availability_change(user_id: str, session_id: str, disabled: list[str]) -> bool:
    """Record the current disabled set and report whether it CHANGED since the
    last turn of this session.

    A standing "these tools are available" line in the system prompt is not
    enough on its own: when the user asks the same question verbatim, the model
    answers from its own previous reply and never re-calls. Measured — disable a
    group, ask, re-enable, ask again, and the tool is never called a third time.

    The transition is the signal that actually moves it, and it is worth a
    pointed instruction precisely because it is rare. Only written when it
    changes, so this is not a per-turn database write.
    """
    if not session_id:
        return False
    current = sorted(disabled or [])
    try:
        from backend.chat import store as chat_store
        from backend.db import models as M
        from backend.db import sync as dbsync
        if not chat_store.enabled():
            return False
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            if user is None:
                return False
            sess = s.get(M.ChatSession, chat_store._session_uuid(session_id))
            if sess is None:
                return False
            meta = dict(sess.meta or {})
            section = dict(meta.get(_KEY) or {})
            previous = section.get(_LAST)
            if previous == current:
                return False
            section[_LAST] = current
            meta[_KEY] = section
            sess.meta = meta
            s.commit()
            # First turn of a session has nothing to compare against — that is
            # not a change, and announcing one would be noise.
            return previous is not None
    except Exception:
        log.exception("tool prefs: availability-change check failed")
        return False
