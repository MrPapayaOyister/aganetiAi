"""Identity bridge — map a verified Supabase JWT (sub+email) onto the internal
Postgres User, auto-provisioning allowed new users.

Authorization policy (domain-allowlist auto-provision):
  1. If the sub already resolves to a User row  -> allowed (already provisioned).
  2. Else if the email domain is in ALLOWED_EMAIL_DOMAINS, or the email is in
     ALLOWED_EMAILS -> provision on the spot (onboarding.ensure_onboarded), allow.
  3. Else -> NotAuthorized (caller returns 403).

An in-process TTL cache keeps repeated requests off the DB after first resolution.
EVERY authenticated caller reaches here — the middleware used to short-circuit a
hand-listed pair of subs into "user_1"/"user_2" before calling this, so the
authorization rules below silently did not apply to them.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("aganeti.identity")

ALLOWED_DOMAINS = {d.strip().lower() for d in
                   os.getenv("ALLOWED_EMAIL_DOMAINS", "meerana.ae").split(",") if d.strip()}
ALLOWED_EMAILS = {e.strip().lower() for e in
                  os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()}

_TTL = 300.0
_cache: dict[str, tuple[float, dict]] = {}


class NotAuthorized(Exception):
    pass


def _email_allowed(email: str) -> bool:
    e = (email or "").strip().lower()
    if not e:
        return False
    if e in ALLOWED_EMAILS:
        return True
    dom = e.rsplit("@", 1)[-1] if "@" in e else ""
    return dom in ALLOWED_DOMAINS


async def resolve_or_provision(supabase_uid: str, email: str = "",
                               full_name: str | None = None) -> dict:
    """Return {uid, org_id, supabase_uid, email} or raise NotAuthorized."""
    if not supabase_uid:
        raise NotAuthorized("no subject")
    now = time.monotonic()
    hit = _cache.get(supabase_uid)
    if hit and now - hit[0] < _TTL:
        return hit[1]

    from backend.db.base import SessionLocal
    from backend.db import repo

    async with SessionLocal() as s:
        user = await repo.resolve_user(s, supabase_uid)

    if user is not None:
        # An existing user whose login was revoked (status != active) is refused.
        if (getattr(user, "status", "active") or "active") != "active":
            raise NotAuthorized(f"user {user.id} is not active")
    else:
        # New sub: gate on the domain allowlist. Revoking access is `users.status`
        # (checked above for anyone already provisioned) — it used to ALSO be an
        # `enabled: False` flag in config/users.py, a second switch that only worked
        # for the two people listed there and was silently ignored for everyone else.
        if not _email_allowed(email):
            raise NotAuthorized(f"{email or supabase_uid} not permitted")
        from backend import onboarding
        await onboarding.ensure_onboarded(supabase_uid, email, full_name)
        async with SessionLocal() as s:
            user = await repo.resolve_user(s, supabase_uid)
        if user is None:
            raise NotAuthorized("provisioning failed")

    ident = {"uid": str(user.id), "org_id": str(user.org_id),
             "supabase_uid": user.supabase_uid or supabase_uid, "email": user.email or email}
    _cache[supabase_uid] = (now, ident)
    return ident


def invalidate(supabase_uid: str) -> None:
    _cache.pop(supabase_uid, None)
