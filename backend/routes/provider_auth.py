"""
Per-user OAuth connect / callback / status / disconnect for Google and Microsoft 365.

Supabase handles login (identity); these flows connect a user's *provider data*
(mail, calendar, contacts) and store tokens in `provider_connections`.
Both providers run the same authorization-code flow — they differ only in the
endpoints, the scope list, and how the account's email is discovered.
Never logs tokens.
"""
from __future__ import annotations

import os
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse, JSONResponse

from backend.auth.supabase_client import get_supabase_admin
from backend.services.http_client import api_request
from backend.services.provider_tokens import MS_SCOPES, normalize_scopes

log = logging.getLogger("aria.provider.auth")
router = APIRouter()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
M365_CLIENT_ID = os.getenv("M365_CLIENT_ID", "")
M365_CLIENT_SECRET = os.getenv("M365_CLIENT_SECRET", "")
M365_TENANT = os.getenv("M365_TENANT_ID", "common")

BACKEND_URL = os.getenv("BACKEND_URL", "http://100.107.179.44:8000").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")

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

PROVIDERS = {
    "google": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": GOOGLE_SCOPES,
        # Google needs access_type=offline to hand back a refresh token at all.
        "extra_auth_params": {"access_type": "offline", "include_granted_scopes": "true"},
        # Google infers the scope from the code; Microsoft demands it be repeated.
        "scope_on_exchange": False,
        "id_env": "GOOGLE_CLIENT_ID",
    },
    "microsoft": {
        "client_id": M365_CLIENT_ID,
        "client_secret": M365_CLIENT_SECRET,
        "auth_url": f"https://login.microsoftonline.com/{M365_TENANT}/oauth2/v2.0/authorize",
        "token_url": f"https://login.microsoftonline.com/{M365_TENANT}/oauth2/v2.0/token",
        "scopes": MS_SCOPES,
        # offline_access (in MS_SCOPES) is what yields the refresh token.
        "extra_auth_params": {"response_mode": "query"},
        "scope_on_exchange": True,
        "id_env": "M365_CLIENT_ID",
    },
}


def _redirect_uri(provider: str) -> str:
    return f"{BACKEND_URL}/auth/{provider}/callback"


def _encode_state(user_id: str, redirect_uri: str) -> str:
    raw = json.dumps({"user_id": user_id, "redirect_uri": redirect_uri or "/"}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_state(state: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(state.encode()).decode())
    except Exception:
        return {"user_id": "", "redirect_uri": "/"}


def _jwt_claim(token: str, claim: str) -> str | None:
    """Read one claim out of an id_token payload. No signature verification — the
    token came straight from the provider's TLS token endpoint, and we only use it
    as a display-name/email fallback."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get(claim)
    except Exception:
        return None


async def _fetch_profile(provider: str, access_token: str) -> tuple[str | None, dict]:
    """Return (email, raw_profile) for the freshly connected account."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if provider == "google":
        r = await api_request("GET", "https://www.googleapis.com/oauth2/v2/userinfo",
                              headers=headers)
        if r.status_code == 200:
            p = r.json()
            return p.get("email"), p
        return None, {}
    r = await api_request("GET", "https://graph.microsoft.com/v1.0/me", headers=headers)
    if r.status_code == 200:
        p = r.json()
        # Work/school accounts expose `mail`; personal ones only userPrincipalName.
        return p.get("mail") or p.get("userPrincipalName"), p
    return None, {}


# ── Connect ────────────────────────────────────────────────────────────────────

@router.get("/auth/{provider}/connect")
async def provider_connect(provider: str, user_id: str = Query(...),
                           redirect_uri: str = Query("/")):
    """Build the provider consent URL (offline access + forced consent so a refresh
    token is always issued) and redirect the browser to it.

    ONE PROVIDER AT A TIME: if the user already has the *other* provider linked, we
    stop here — before sending them through a full consent round-trip — and bounce
    back to the UI asking them to disconnect it first. Two live mailboxes would make
    every read ambiguous."""
    cfg = PROVIDERS.get(provider)
    if not cfg:
        return JSONResponse(status_code=404, content={"error": "unknown_provider"})
    if not cfg["client_id"]:
        return JSONResponse(status_code=503, content={
            "error": f"{provider}_oauth_not_configured",
            "message": f"{cfg['id_env']} is not set on the server.",
        })

    try:
        from backend.services.provider_tokens import connected_providers
        others = [p for p in await connected_providers(user_id) if p != provider]
    except Exception as e:  # noqa: BLE001 — a lookup failure must not block connecting
        log.info("connect precheck failed for %s: %s", user_id, e)
        others = []
    if others:
        log.info("refusing %s connect for %s — %s already linked", provider, user_id, others[0])
        return RedirectResponse(
            f"{FRONTEND_URL}{redirect_uri}?connect_error=already_connected"
            f"&connected_provider={others[0]}"
        )

    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": _redirect_uri(provider),
        "response_type": "code",
        "scope": " ".join(cfg["scopes"]),
        # "prompt": "consent",
        "state": _encode_state(user_id, redirect_uri),
        **cfg["extra_auth_params"],
    }
    if provider == "google":
        params["prompt"] = "consent"
    return RedirectResponse(f"{cfg['auth_url']}?{urlencode(params)}")


# ── Callback ───────────────────────────────────────────────────────────────────

@router.get("/auth/{provider}/callback")
async def provider_callback(provider: str, code: str = Query(None), state: str = Query(""),
                            error: str = Query(None),
                            error_description: str = Query(None)):
    """Exchange the code for tokens, fetch the profile, upsert the connection,
    then redirect back to the frontend."""
    cfg = PROVIDERS.get(provider)
    st = _decode_state(state)
    user_id = st.get("user_id", "")
    redirect_uri = st.get("redirect_uri", "/")
    fail = f"{FRONTEND_URL}{redirect_uri}?connect_error={provider}"

    if not cfg:
        return JSONResponse(status_code=404, content={"error": "unknown_provider"})
    if error or not code or not user_id:
        log.warning("%s callback error=%s (%s) code?=%s user?=%s", provider, error,
                    (error_description or "")[:160], bool(code), bool(user_id))
        return RedirectResponse(fail)

    # 1) Exchange code → tokens
    form = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "redirect_uri": _redirect_uri(provider),
        "grant_type": "authorization_code",
    }
    if cfg["scope_on_exchange"]:
        form["scope"] = " ".join(cfg["scopes"])

    tok = await api_request("POST", cfg["token_url"], data=form)
    if tok.status_code != 200:
        log.warning("%s token exchange failed: %s %s", provider, tok.status_code, tok.text[:200])
        return RedirectResponse(fail)
    td = tok.json()
    access_token = td.get("access_token")
    refresh_token = td.get("refresh_token")
    expires_in = td.get("expires_in", 3600)
    granted_scopes = normalize_scopes(td.get("scope") or "")

    # 2) Profile — API first, id_token as a fallback for the email only
    email, profile = await _fetch_profile(provider, access_token)
    if not email and td.get("id_token"):
        email = _jwt_claim(td["id_token"], "email") or _jwt_claim(td["id_token"], "preferred_username")

    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    # 3) Upsert connection — Supabase first (best-effort), and ALWAYS the JSON file
    # store so OAuth works even when the Supabase migration hasn't been run yet.
    payload = {
        "user_id": user_id,
        "provider": provider,
        "provider_email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expiry": expiry,
        "scopes": granted_scopes or normalize_scopes(cfg["scopes"]),
        "raw_profile": profile,
    }
    # Encrypt the OAuth tokens once, at rest, before either store sees them.
    from backend.services import token_crypto
    epayload = token_crypto.enc_row(payload)
    supabase_ok = False
    try:
        sb = get_supabase_admin()
        sb.table("provider_connections").upsert({
            **epayload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id,provider").execute()
        supabase_ok = True
    except Exception as e:
        log.info("%s connection upsert (Supabase) failed for %s — using file fallback: %s",
                 provider, user_id, e)
    try:
        from backend.services import _token_file_store as _file
        _file.upsert(user_id, provider, epayload)
    except Exception as e:
        if not supabase_ok:
            log.warning("%s connection upsert failed (both stores) for %s: %s", provider, user_id, e)
            return RedirectResponse(fail)

    if not refresh_token:
        # Without one, the connection dies at the first token expiry (~1h).
        log.warning("%s connected for %s WITHOUT a refresh token — check offline_access "
                    "/ access_type=offline and that consent was re-prompted.", provider, user_id)

    log.info("%s connected for user %s (%s)", provider, user_id, email)
    return RedirectResponse(f"{FRONTEND_URL}{redirect_uri}?connected={provider}")


# ── Status / disconnect ────────────────────────────────────────────────────────

async def _identity_ids(user_id: str) -> list[str]:
    """Every id this user's rows may be stored under: the one supplied plus the
    internal-alias ⇄ Supabase-uid mapping. Status and disconnect MUST use the same
    resolution the token layer uses — otherwise status reports "not connected" for a
    row that `connect` then refuses to overwrite, and Disconnect silently no-ops."""
    ids = [user_id]
    try:
        from backend.services.provider_tokens import _alias_candidates
        ids += [i for i in await _alias_candidates(user_id) if i not in ids]
    except Exception as e:  # noqa: BLE001
        log.info("alias resolution failed for %s: %s", user_id, e)
    return ids


@router.get("/auth/provider/status")
async def provider_status(user_id: str = Query(...)):
    """Report which providers this user has connected. Reads from Supabase
    first, falls back to the file store; checks every id this identity maps to."""
    out = {p: {"connected": False, "configured": bool(PROVIDERS[p]["client_id"])}
           for p in PROVIDERS}
    rows: list[dict] = []
    for uid in await _identity_ids(user_id):
        found: list[dict] = []
        try:
            sb = get_supabase_admin()
            found = (sb.table("provider_connections")
                     .select("provider, provider_email, scopes, created_at")
                     .eq("user_id", uid).execute()).data or []
        except Exception as e:
            log.info("provider status (Supabase) failed for %s — using file fallback: %s", uid, e)
        if not found:
            try:
                from backend.services import _token_file_store as _file
                found = _file.fetch_all(uid)
            except Exception:
                pass
        rows += found
    for r in rows:
        if r.get("provider") in out and not out[r["provider"]]["connected"]:
            out[r["provider"]] = {
                "connected": True,
                "configured": True,
                "email": r.get("provider_email"),
                "scopes": r.get("scopes") or [],
                "connected_at": r.get("created_at"),
            }
    return out


@router.delete("/auth/provider/{provider}")
async def disconnect_provider(provider: str, user_id: str = Query(...)):
    """Delete the stored connection for a provider, under every id this identity
    maps to, from BOTH stores.

    Note: this revokes our copy of the credentials, not the grant itself —
    Microsoft has no simple per-app revoke endpoint, so an already-issued Graph
    access token stays valid until it expires (≤1h)."""
    errors: list[str] = []
    for uid in await _identity_ids(user_id):
        sb_ok = False
        try:
            sb = get_supabase_admin()
            sb.table("provider_connections").delete().eq("user_id", uid).eq("provider", provider).execute()
            sb_ok = True
        except Exception as e:
            log.info("disconnect %s (Supabase) failed for %s — using file fallback: %s", provider, uid, e)
        try:
            from backend.services import _token_file_store as _file
            _file.delete_by_user_provider(uid, provider)
        except Exception as e:
            if not sb_ok:
                errors.append(f"{uid}: {e}")
    if errors:
        return JSONResponse(status_code=500,
                            content={"disconnected": False, "error": "; ".join(errors)})
    return {"disconnected": True}
