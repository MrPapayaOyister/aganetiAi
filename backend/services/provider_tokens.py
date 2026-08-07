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
    "offline_access", "openid", "email", "profile",
    "User.Read", "Mail.Send", "Mail.ReadWrite",
    "Calendars.ReadWrite","Contacts.Read"
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
from backend.services import _token_pg_store as _pg
from backend.services import token_crypto


async def _fetch_connection(user_id: str, provider: str, *,
                            live_fallback: bool = True) -> dict | None:
    """Load the user's row for `provider`. Tries Supabase first; falls back to the JSON
    file store. Tokens are decrypted before returning.

    IDENTITY-AWARE, in three steps. The OAuth connect flow stores tokens under the
    Supabase sub, but the agent executor and the schedulers look them up by whatever id
    they happen to hold, so a direct hit is not guaranteed:

      1. the id as given;
      2. the same identity's other ids, resolved from the users table;
      3. `live_fallback` — the single live connection for this provider, whoever owns it.

    Step 3 is what keeps the app bound to *whichever account is actually connected*
    rather than to any configured id. It deliberately refuses to act when more than one
    account is live: picking between two mailboxes would silently hand one user another
    user's mail. Pass live_fallback=False where the answer must be about this identity
    alone — the connect precheck, status, disconnect."""
    def _q_sync(uid: str):
        """Supabase, then the JSON file — both are synchronous clients."""
        try:
            sb = get_supabase_admin()
            r = (sb.table("provider_connections").select("*")
                 .eq("user_id", uid).eq("provider", provider).limit(1).execute())
            row = (r.data or [None])[0]
            if row:
                return row
        except Exception as e:
            log.debug("Supabase fetch failed, using file store: %s", e)
        return _file.fetch(uid, provider)

    async def _q(uid: str):
        """PostgreSQL first — it became the system of record in migration
        d4e91a3b7c62. Supabase and the JSON file remain behind it, tried in that
        order, so a Postgres outage degrades to the old behaviour instead of
        logging every connected user out. Never raises: a lookup that threw would
        500 whichever chat request triggered it."""
        try:
            row = await _pg.fetch(uid, provider)
            if row:
                return row
        except Exception as e:  # noqa: BLE001
            log.warning("Postgres token store unavailable, falling back: %s", e)
        return await asyncio.to_thread(_q_sync, uid)

    row = await _q(user_id)
    # A row with no refresh_token is a dead credential — it can only ever raise
    # "token expired". Treat it like a miss and keep looking under the user's other
    # ids, so one stale row cannot shadow a live connection stored under the alias's
    # counterpart. The dead row is still returned as a last resort, so `status` and
    # the connect precheck stay truthful about a connection existing.
    if not row or not row.get("refresh_token"):
        for alt in await _alias_candidates(user_id):
            alt_row = await _q(alt)
            if alt_row and alt_row.get("refresh_token"):
                row = alt_row
                break
            row = row or alt_row
    if live_fallback and (not row or not row.get("refresh_token")):
        row = await _sole_live_connection(provider) or row
    return token_crypto.dec_row(row) if row else None


async def _all_rows_for_provider(provider: str) -> list[dict]:
    """Every stored row for one provider, across all user ids and all stores.

    Postgres first, falling back to the JSON file — the same precedence the read
    path uses, so health reports on the rows that would actually be served.
    Rows come back ENCRYPTED: callers only inspect metadata (expiry, email,
    presence of a refresh token), and decrypting here would put plaintext tokens
    in reach of a health endpoint for no reason."""
    try:
        rows = await _pg.fetch_all_for_provider(provider)
        if rows:
            return rows
    except Exception as e:  # noqa: BLE001
        log.debug("provider rows: Postgres unavailable for %s: %s", provider, e)
    try:
        return await asyncio.to_thread(_file.fetch_all_for_provider, provider)
    except Exception as e:  # noqa: BLE001
        log.debug("provider rows: file store unavailable for %s: %s", provider, e)
        return []


async def _sole_live_connection(provider: str) -> dict | None:
    """The one connection for `provider` that can still be refreshed, or None.

    "Live" means it holds a refresh token — a row without one is a dead credential
    that can only ever raise "token expired". Returns None when zero or more than one
    qualify: with two connected accounts there is no non-arbitrary answer, and guessing
    would cross mailboxes between users."""
    def _q():
        rows: list[dict] = []
        try:
            sb = get_supabase_admin()
            rows = (sb.table("provider_connections").select("*")
                    .eq("provider", provider).execute()).data or []
        except Exception as e:
            log.debug("Supabase provider scan failed, using file store: %s", e)
        if not rows:
            rows = _file.fetch_all_for_provider(provider)
        return [r for r in rows if r.get("refresh_token")]

    live = await asyncio.to_thread(_q)
    if len(live) == 1:
        return live[0]
    if len(live) > 1:
        log.debug("%s: %d live connections — no sole account to fall back to",
                  provider, len(live))
    return None


async def _alias_candidates(user_id: str) -> list[str]:
    """Other ids this user may be stored under, resolved from the users table.

    Deliberately DB-only: the configured USER_n_SUPABASE_UID registry used to be
    consulted here, which pinned token lookups to whoever was named in .env instead of
    to the account actually connected. Connections are now found by their real owner,
    with `_sole_live_connection` covering ids the table doesn't know."""
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
    return out


async def _update_tokens(row_id: str, access_token: str, expiry_iso: str,
                         refresh_token: str | None = None) -> None:
    patch = {"access_token": access_token, "token_expiry": expiry_iso}
    if refresh_token:
        patch["refresh_token"] = refresh_token
    epatch = token_crypto.enc_row(patch)  # encrypt tokens before ANY store

    # Postgres is the system of record, so its failure is the one worth logging.
    try:
        await _pg.update(row_id, epatch)
    except Exception as e:  # noqa: BLE001
        log.warning("Postgres token update failed for row %s: %s", row_id, e)

    def _u():
        # Supabase best-effort, then always JSON. Writing all three keeps the
        # fallbacks warm: if Postgres is lost, the file store still holds a token
        # refreshed minutes ago rather than one from whenever it was last written.
        try:
            sb = get_supabase_admin()
            sb.table("provider_connections").update({**epatch,
                "updated_at": _now().isoformat()}).eq("id", row_id).execute()
        except Exception:
            pass
        _file.update(row_id, epatch)
    await asyncio.to_thread(_u)


async def _delete_connection(row_id: str) -> None:
    # Delete from every store. A row surviving in ANY of them would resurrect the
    # connection on the next read and make "disconnect" a lie.
    try:
        await _pg.delete(row_id)
    except Exception as e:  # noqa: BLE001
        log.warning("Postgres token delete failed for row %s: %s", row_id, e)

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


async def connected_providers(user_id: str, *, live_fallback: bool = True) -> list[str]:
    """All providers this user has a stored connection for.

    Pass live_fallback=False to ask strictly about THIS identity — otherwise a user with
    no connection of their own inherits the sole live one, and the connect route would
    refuse to link them an account of their own."""
    return [p for p in PROVIDERS
            if await _fetch_connection(user_id, p, live_fallback=live_fallback)]


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
