"""
Per-user provider token management (Google + Microsoft Graph).

Reads/writes the `provider_connections` table via the Supabase service-role
client (bypasses RLS, server-side only), with the encrypted JSON file store as
a durable fallback. Returns valid access tokens and transparently refreshes
expired ones. Never logs access/refresh tokens.

Both providers share one code path — they differ only in the token endpoint,
the client credentials, and whether `scope` must be echoed on refresh (Microsoft
requires it; Google does not).
"""
from __future__ import annotations

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from backend.auth.supabase_client import get_supabase_admin
from backend.services.http_client import api_request

log = logging.getLogger("aria.provider.tokens")

# Load .env HERE rather than relying on an earlier `import config.settings`.
# These constants are read once at import; if this module is imported first (a
# script, a worker, a test), unloaded env would leave every client_id empty and
# `which_provider` would report "nothing connected" for a fully connected user.
try:
    from pathlib import Path as _Path
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).resolve().parents[2] / ".env")
except Exception:  # noqa: BLE001 — dotenv is optional in container deploys
    pass

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

M365_CLIENT_ID = os.getenv("M365_CLIENT_ID", "")
M365_CLIENT_SECRET = os.getenv("M365_CLIENT_SECRET", "")
M365_TENANT = os.getenv("M365_TENANT_ID", "common")
M365_TOKEN_URL = f"https://login.microsoftonline.com/{M365_TENANT}/oauth2/v2.0/token"

# Delegated Graph permissions we ask for. offline_access is what yields a refresh
# token; it is never echoed back in the granted-scope list, so it lives here only.
MS_SCOPES = [
     "openid", "email", "profile",
    "User.Read", "Mail.Send", "Mail.ReadWrite",
    "Calendars.ReadWrite",
]

# When creds are absent the app still loads (dev mode); services degrade to "not connected".
GOOGLE_CONFIGURED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
MS_CONFIGURED = bool(M365_CLIENT_ID and M365_CLIENT_SECRET)

PROVIDERS = ("microsoft", "google")

_CONFIG = {
    "google": {
        "token_url": GOOGLE_TOKEN_URL,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_scope": None,                     # Google infers it from the refresh token
        "label": "Google",
        "connect_url": "/auth/google/connect",
    },
    "microsoft": {
        "token_url": M365_TOKEN_URL,
        "client_id": M365_CLIENT_ID,
        "client_secret": M365_CLIENT_SECRET,
        "refresh_scope": " ".join(MS_SCOPES),      # Graph requires scope on refresh
        "label": "Microsoft 365",
        "connect_url": "/auth/microsoft/connect",
    },
}


def not_connected(provider: str) -> dict:
    c = _CONFIG[provider]
    return {
        "error": f"{provider}_not_connected",
        "message": f"Connect your {c['label']} account in Settings → Connected Apps",
        "connect_url": c["connect_url"],
    }


def token_expired(provider: str) -> dict:
    c = _CONFIG[provider]
    return {
        "error": f"{provider}_token_expired",
        "message": f"Your {c['label']} connection expired — please reconnect.",
        "connect_url": c["connect_url"],
    }


# Kept as module constants because callers pattern-match on `detail["error"]`.
NOT_CONNECTED = not_connected("google")
TOKEN_EXPIRED = token_expired("google")


def normalize_scopes(scopes) -> list[str]:
    """
    Strip the Graph resource prefix so granted scopes compare equal to the bare
    permission names used everywhere else. Microsoft returns
    'https://graph.microsoft.com/Mail.Read'; we store 'Mail.Read'.
    Google scopes are full URIs by design and pass through untouched.
    """
    if isinstance(scopes, str):
        scopes = scopes.split()
    out = []
    for s in scopes or []:
        if s.startswith("https://graph.microsoft.com/"):
            s = s.rsplit("/", 1)[-1]
        out.append(s)
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


from backend.services import _token_file_store as _file
from backend.services import token_crypto


async def _fetch_connection(user_id: str, provider: str) -> dict | None:
    """Load the user's row for `provider`. Tries Supabase first; falls back to the JSON
    file store. IDENTITY-AWARE: the OAuth connect flow stores tokens under the Supabase sub,
    but the agent executor looks them up by the internal alias (e.g. "user_1"). If the
    direct lookup misses, resolve the identity to its canonical Supabase uid and retry —
    otherwise a connected mailbox reads as "not connected" for the aliased user. Tokens
    are decrypted before returning."""
    def _q(uid: str):
        try:
            sb = get_supabase_admin()
            r = (sb.table("provider_connections").select("*")
                 .eq("user_id", uid).eq("provider", provider).limit(1).execute())
            row = (r.data or [None])[0]
            if row:
                return row
        except Exception as e:
            log.info("Supabase fetch failed, using file store: %s", e)
        return _file.fetch(uid, provider)
    row = await asyncio.to_thread(_q, user_id)
    if not row:
        for alt in await _alias_candidates(user_id):
            row = await asyncio.to_thread(_q, alt)
            if row:
                break
    return token_crypto.dec_row(row) if row else None


async def _alias_candidates(user_id: str) -> list[str]:
    """Other ids this user may be stored under (internal alias ⇄ Supabase uid)."""
    out: list[str] = []
    try:
        from backend.db.base import SessionLocal
        from backend.db import repo
        async with SessionLocal() as s:
            u = await repo.resolve_user(s, user_id)
        alt = getattr(u, "supabase_uid", None) if u else None
        if alt and alt != user_id:
            out.append(alt)
    except Exception:  # noqa: BLE001
        pass
    if not out:
        # Static registry fallback for the alias users that predate the DB bridge.
        try:
            from config.users import USERS
            alt = (USERS.get(user_id) or {}).get("supabase_uid", "")
            if alt and alt != user_id:
                out.append(alt)
        except Exception:  # noqa: BLE001
            pass
    return out


async def _update_tokens(row_id: str, access_token: str, expiry_iso: str,
                         refresh_token: str | None = None) -> None:
    def _u():
        patch = {"access_token": access_token, "token_expiry": expiry_iso}
        if refresh_token:
            patch["refresh_token"] = refresh_token
        epatch = token_crypto.enc_row(patch)  # encrypt tokens before either store
        # Update both stores — Supabase first (best-effort), then always JSON.
        try:
            sb = get_supabase_admin()
            sb.table("provider_connections").update({**epatch,
                "updated_at": _now().isoformat()}).eq("id", row_id).execute()
        except Exception:
            pass
        _file.update(row_id, epatch)
    await asyncio.to_thread(_u)


async def _delete_connection(row_id: str) -> None:
    def _d():
        try:
            sb = get_supabase_admin()
            sb.table("provider_connections").delete().eq("id", row_id).execute()
        except Exception:
            pass
        _file.delete(row_id)
    await asyncio.to_thread(_d)


async def get_provider_token(user_id: str, provider: str) -> str:
    """
    Return a valid access_token for this user + provider, refreshing if it expires
    within 5 minutes. Raises HTTPException(403) if not connected, HTTPException(401)
    if a refresh fails (the row is KEPT so status stays truthful and reconnecting
    simply overwrites it).
    """
    if provider not in _CONFIG:
        raise HTTPException(status_code=400, detail={"error": "unknown_provider",
                                                    "message": f"Unknown provider {provider!r}"})
    cfg = _CONFIG[provider]
    row = await _fetch_connection(user_id, provider)
    if not row:
        raise HTTPException(status_code=403, detail=not_connected(provider))

    access_token: str = row.get("access_token", "")
    refresh_token: str | None = row.get("refresh_token")
    expiry_raw: str | None = row.get("token_expiry")

    needs_refresh = True
    if expiry_raw and access_token:
        try:
            exp = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            needs_refresh = exp <= _now() + timedelta(minutes=5)
        except Exception:
            needs_refresh = True

    if not needs_refresh:
        return access_token

    if not refresh_token:
        raise HTTPException(status_code=401, detail=token_expired(provider))

    form = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if cfg["refresh_scope"]:
        form["scope"] = cfg["refresh_scope"]

    resp = await api_request("POST", cfg["token_url"], data=form)
    if resp.status_code != 200:
        # invalid_grant → refresh token revoked/expired (Google apps in "Testing" mode
        # expire them after 7 days; Graph after 90 days of disuse). Ask for a reconnect.
        log.warning("%s token refresh failed for %s: %s %s",
                    provider, user_id, resp.status_code, resp.text[:120])
        raise HTTPException(status_code=401, detail=token_expired(provider))

    data = resp.json()
    new_access = data["access_token"]
    new_expiry = (_now() + timedelta(seconds=data.get("expires_in", 3600))).isoformat()
    await _update_tokens(row["id"], new_access, new_expiry,
                         refresh_token=data.get("refresh_token"))  # both may rotate
    return new_access


async def get_provider_headers(user_id: str, provider: str) -> dict:
    """Authorization header dict for the given provider's API."""
    token = await get_provider_token(user_id, provider)
    return {"Authorization": f"Bearer {token}"}


async def which_provider(user_id: str) -> str | None:
    """
    Which mailbox/calendar provider this user has connected. A user may only have
    ONE at a time — the connect route refuses a second while one is live — so this
    normally finds exactly one row. The Microsoft-first order is only a tiebreak for
    legacy rows predating that rule. Returns None when neither is connected.
    """
    for p in PROVIDERS:
        if not _CONFIG[p]["client_id"]:
            continue
        if await _fetch_connection(user_id, p):
            return p
    return None


async def connected_providers(user_id: str) -> list[str]:
    """All providers this user has a stored connection for."""
    return [p for p in PROVIDERS if await _fetch_connection(user_id, p)]


# ── Back-compat wrappers (Google-only call sites) ──────────────────────────────

async def get_google_token(user_id: str) -> str:
    return await get_provider_token(user_id, "google")


async def get_google_headers(user_id: str) -> dict:
    return await get_provider_headers(user_id, "google")


async def get_ms_token(user_id: str) -> str:
    return await get_provider_token(user_id, "microsoft")


async def get_ms_headers(user_id: str) -> dict:
    """Authorization + JSON content-type header for Microsoft Graph."""
    token = await get_provider_token(user_id, "microsoft")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
