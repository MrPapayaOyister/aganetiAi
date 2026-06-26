"""
Per-user Google token management.

Reads/writes the `provider_connections` table via the Supabase service-role
client (bypasses RLS, server-side only). Returns valid access tokens and
transparently refreshes expired ones. Never logs access/refresh tokens.
"""
from __future__ import annotations

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from backend.auth.supabase_client import get_supabase_admin
from backend.services.http_client import google_request

log = logging.getLogger("aria.google.tokens")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# When creds are absent the app still loads (dev mode); services return mock data.
GOOGLE_CONFIGURED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

NOT_CONNECTED = {
    "error": "google_not_connected",
    "message": "Connect your Google account in Settings → Connected Apps",
    "connect_url": "/auth/google/connect",
}
TOKEN_EXPIRED = {
    "error": "google_token_expired",
    "message": "Your Google connection expired — please reconnect.",
    "connect_url": "/auth/google/connect",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


from backend.services import _token_file_store as _file


async def _fetch_connection(user_id: str) -> dict | None:
    """Load the user's google row. Tries Supabase first; falls back to the
    JSON file store if Supabase is unavailable (e.g. the migration hasn't
    been run yet)."""
    def _q():
        try:
            sb = get_supabase_admin()
            r = (sb.table("provider_connections").select("*")
                 .eq("user_id", user_id).eq("provider", "google").limit(1).execute())
            row = (r.data or [None])[0]
            if row:
                return row
        except Exception as e:
            log.info("Supabase fetch failed, using file store: %s", e)
        return _file.fetch(user_id, "google")
    return await asyncio.to_thread(_q)


async def _update_tokens(row_id: str, access_token: str, expiry_iso: str,
                         refresh_token: str | None = None) -> None:
    def _u():
        patch = {"access_token": access_token, "token_expiry": expiry_iso}
        if refresh_token:
            patch["refresh_token"] = refresh_token
        # Update both stores — Supabase first (best-effort), then always JSON.
        try:
            sb = get_supabase_admin()
            sb.table("provider_connections").update({**patch,
                "updated_at": _now().isoformat()}).eq("id", row_id).execute()
        except Exception:
            pass
        _file.update(row_id, patch)
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


async def get_google_token(user_id: str) -> str:
    """
    Return a valid Google access_token for this user, refreshing if it expires
    within 5 minutes. Raises HTTPException(403) if not connected, HTTPException(401)
    if a refresh fails (the row is deleted so the user is prompted to reconnect).
    """
    row = await _fetch_connection(user_id)
    if not row:
        raise HTTPException(status_code=403, detail=NOT_CONNECTED)

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
        # Can't refresh without a refresh token — force reconnect.
        await _delete_connection(row["id"])
        raise HTTPException(status_code=401, detail=TOKEN_EXPIRED)

    resp = await google_request(
        "POST", GOOGLE_TOKEN_URL,
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if resp.status_code != 200:
        # invalid_grant → refresh token revoked/expired. Delete + reconnect.
        log.warning("Google token refresh failed for %s: %s", user_id, resp.status_code)
        await _delete_connection(row["id"])
        raise HTTPException(status_code=401, detail=TOKEN_EXPIRED)

    data = resp.json()
    new_access = data["access_token"]
    new_expiry = (_now() + timedelta(seconds=data.get("expires_in", 3600))).isoformat()
    await _update_tokens(row["id"], new_access, new_expiry,
                         refresh_token=data.get("refresh_token"))  # Google may rotate
    return new_access


async def get_google_headers(user_id: str) -> dict:
    """Return the Authorization header dict for Google API calls."""
    token = await get_google_token(user_id)
    return {"Authorization": f"Bearer {token}"}
