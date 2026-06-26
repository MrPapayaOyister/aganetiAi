"""
Per-user Google OAuth connect / callback / status / disconnect.

Supabase handles login (identity); this flow connects a user's *Google data*
(Gmail, Calendar, Contacts) and stores tokens in `provider_connections`.
Never logs tokens.
"""
from __future__ import annotations

import os
import json
import base64
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse, JSONResponse

from backend.auth.supabase_client import get_supabase_admin
from backend.services.http_client import google_request

log = logging.getLogger("aria.google.auth")
router = APIRouter()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://100.107.179.44:8000").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
REDIRECT_URI = f"{BACKEND_URL}/auth/google/callback"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

GOOGLE_SCOPES = [
    "openid", "email", "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/directory.readonly",
]


def _encode_state(user_id: str, redirect_uri: str) -> str:
    raw = json.dumps({"user_id": user_id, "redirect_uri": redirect_uri or "/"}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_state(state: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(state.encode()).decode())
    except Exception:
        return {"user_id": "", "redirect_uri": "/"}


@router.get("/auth/google/connect")
async def google_connect(user_id: str = Query(...), redirect_uri: str = Query("/")):
    """Build the Google consent URL (offline + consent for a refresh token) and redirect."""
    if not GOOGLE_CLIENT_ID:
        return JSONResponse(status_code=503, content={
            "error": "google_oauth_not_configured",
            "message": "GOOGLE_CLIENT_ID is not set on the server.",
        })
    from urllib.parse import urlencode
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": _encode_state(user_id, redirect_uri),
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/auth/google/callback")
async def google_callback(code: str = Query(None), state: str = Query(""),
                          error: str = Query(None)):
    """Exchange the code for tokens, fetch the profile, upsert the connection,
    then redirect back to the frontend."""
    st = _decode_state(state)
    user_id = st.get("user_id", "")
    redirect_uri = st.get("redirect_uri", "/")
    fail = f"{FRONTEND_URL}{redirect_uri}?connect_error=google"

    if error or not code or not user_id:
        log.warning("google callback error=%s code?=%s user?=%s", error, bool(code), bool(user_id))
        return RedirectResponse(fail)

    # 1) Exchange code → tokens
    tok = await google_request("POST", GOOGLE_TOKEN_URL, data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })
    if tok.status_code != 200:
        log.warning("google token exchange failed: %s", tok.status_code)
        return RedirectResponse(fail)
    td = tok.json()
    access_token = td.get("access_token")
    refresh_token = td.get("refresh_token")
    expires_in = td.get("expires_in", 3600)
    granted_scopes = (td.get("scope") or "").split()

    # 2) Profile (userinfo)
    email, profile = None, {}
    prof = await google_request("GET", GOOGLE_USERINFO_URL,
                                headers={"Authorization": f"Bearer {access_token}"})
    if prof.status_code == 200:
        profile = prof.json()
        email = profile.get("email")
    if not email and td.get("id_token"):
        try:  # fallback: decode id_token payload (no verification, email only)
            payload = td["id_token"].split(".")[1]
            payload += "=" * (-len(payload) % 4)
            email = json.loads(base64.urlsafe_b64decode(payload)).get("email")
        except Exception:
            pass

    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    # 3) Upsert connection — Supabase first (best-effort), fall back to
    # the JSON file store so OAuth works even when the Supabase migration
    # hasn't been run yet.
    payload = {
        "user_id": user_id,
        "provider": "google",
        "provider_email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expiry": expiry,
        "scopes": granted_scopes or GOOGLE_SCOPES,
        "raw_profile": profile,
    }
    supabase_ok = False
    try:
        sb = get_supabase_admin()
        sb.table("provider_connections").upsert({
            **payload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id,provider").execute()
        supabase_ok = True
    except Exception as e:
        log.info("google connection upsert (Supabase) failed for %s — using file fallback: %s", user_id, e)
    # ALWAYS write to the file store too — guaranteed durability.
    try:
        from backend.services import _token_file_store as _file
        _file.upsert(user_id, "google", payload)
    except Exception as e:
        if not supabase_ok:
            log.warning("google connection upsert failed (both stores) for %s: %s", user_id, e)
            return RedirectResponse(fail)

    log.info("Google connected for user %s (%s)", user_id, email)
    return RedirectResponse(f"{FRONTEND_URL}{redirect_uri}?connected=google")


@router.get("/auth/provider/status")
async def provider_status(user_id: str = Query(...)):
    """Report which providers this user has connected. Reads from Supabase
    first, falls back to the file store."""
    out = {"google": {"connected": False}, "microsoft": {"connected": False}}
    rows: list[dict] = []
    try:
        sb = get_supabase_admin()
        rows = (sb.table("provider_connections")
                .select("provider, provider_email, scopes, created_at")
                .eq("user_id", user_id).execute()).data or []
    except Exception as e:
        log.info("provider status (Supabase) failed for %s — using file fallback: %s", user_id, e)
    if not rows:
        try:
            from backend.services import _token_file_store as _file
            rows = _file.fetch_all(user_id)
        except Exception:
            pass
    for r in rows:
        if r.get("provider") in out:
            out[r["provider"]] = {
                "connected": True,
                "email": r.get("provider_email"),
                "scopes": r.get("scopes") or [],
                "connected_at": r.get("created_at"),
            }
    return out


@router.delete("/auth/provider/{provider}")
async def disconnect_provider(provider: str, user_id: str = Query(...)):
    """Delete the stored connection for a provider. Deletes from BOTH stores."""
    sb_ok = False
    try:
        sb = get_supabase_admin()
        sb.table("provider_connections").delete().eq("user_id", user_id).eq("provider", provider).execute()
        sb_ok = True
    except Exception as e:
        log.info("disconnect %s (Supabase) failed for %s — using file fallback: %s", provider, user_id, e)
    try:
        from backend.services import _token_file_store as _file
        _file.delete_by_user_provider(user_id, provider)
    except Exception as e:
        if not sb_ok:
            return JSONResponse(status_code=500, content={"disconnected": False, "error": str(e)})
    return {"disconnected": True}
