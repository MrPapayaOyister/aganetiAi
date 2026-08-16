"""
Per-user OAuth connect / callback / status / disconnect for Google and Microsoft 365.

Supabase handles login (identity); these flows connect a user's *provider data*
(mail, calendar, contacts) and store tokens in `provider_connections`.
Both providers run the same authorization-code flow — they differ only in the
endpoints, the scope list, and how the account's email is discovered.
Never logs tokens.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse, JSONResponse

from backend.auth import tenant
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


# ── OAuth `state` — signed, bound to the authenticated user, short-lived ───────
# The state used to be plain base64 JSON. Two consequences, both exploited by the
# same attack (audit R2):
#
#   * /connect took `user_id` from the query string while sitting on the public
#     prefix list, so an anonymous caller could mint a consent URL naming a victim;
#   * even with /connect authenticated, an unsigned state could simply be EDITED
#     before the browser reached the provider — the callback trusted whatever came
#     back and stored the attacker's tokens under the victim's id.
#
# Fixing only the first would have left the second. The state is therefore now an
# HMAC-signed envelope carrying the SERVER'S view of who initiated the flow, plus
# an issue time and a nonce. The callback accepts nothing else.
STATE_TTL_SECONDS = int(os.getenv("OAUTH_STATE_TTL", "600"))  # 10 minutes


def _state_secret() -> bytes:
    """Key for the state HMAC.

    Prefers a dedicated secret; falls back to INTERNAL_API_TOKEN so an existing
    deployment is protected without a new variable. If neither is set the signature
    is unforgeable-by-nobody, so `_sign_state` refuses rather than issuing a token
    that only looks signed.
    """
    from backend.service_auth import internal_token
    return (os.getenv("OAUTH_STATE_SECRET", "") or internal_token()).encode("utf-8")


class StateUnsigned(RuntimeError):
    """No signing key configured — refuse to start an OAuth flow at all."""


def _sign(payload: bytes) -> str:
    key = _state_secret()
    if not key:
        raise StateUnsigned(
            "neither OAUTH_STATE_SECRET nor INTERNAL_API_TOKEN is set; refusing to "
            "issue an unsigned OAuth state")
    return base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode().rstrip("=")


def _encode_state(user_id: str, redirect_uri: str) -> str:
    body = json.dumps({
        "user_id": user_id,
        "redirect_uri": redirect_uri or "/",
        "iat": int(time.time()),
        "nonce": secrets.token_urlsafe(8),
    }, separators=(",", ":"), sort_keys=True).encode()
    b = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{b}.{_sign(body)}"


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _decode_state(state: str) -> dict:
    """Verify and decode. Returns {} on ANY failure — a bad signature, a tampered
    payload, an expired flow or a malformed token are all the same answer, so the
    callback has one path for "not a state I issued"."""
    try:
        b, sig = state.rsplit(".", 1)
        body = _b64d(b)
        if not hmac.compare_digest(sig, _sign(body)):
            log.warning("oauth state signature mismatch")
            return {}
        data = json.loads(body)
        age = int(time.time()) - int(data.get("iat", 0))
        if age < -60 or age > STATE_TTL_SECONDS:
            log.warning("oauth state expired (age=%ss, ttl=%ss)", age, STATE_TTL_SECONDS)
            return {}
        if not data.get("user_id"):
            return {}
        return data
    except StateUnsigned:
        raise
    except Exception:  # noqa: BLE001
        log.warning("oauth state could not be decoded")
        return {}


def _safe_redirect(target: str) -> str:
    """Only same-app relative paths. `redirect_uri` reaches us from the client and
    is echoed into a Location header on the way back; without this an attacker could
    hand a victim a connect link that lands them on another origin afterwards."""
    t = (target or "/").strip()
    if not t.startswith("/") or t.startswith("//") or "\\" in t:
        return "/"
    return t


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
async def provider_connect(provider: str, request: Request,
                           redirect_uri: str = Query("/"),
                           mode: str = Query("json")):
    """Mint the provider consent URL for THE AUTHENTICATED CALLER.

    SECURITY (audit R2 — account-linking IDOR). This route used to be anonymous and
    take `user_id` from the query string, which it embedded in an unsigned `state`.
    An attacker could therefore start a flow naming a victim, complete consent with
    their OWN provider account, and have the callback file their tokens against the
    victim's record — after which the victim's agent read the attacker's mailbox and,
    worse, the attacker's mailbox became a channel into the victim's assistant.

    Two changes close it, and both are needed:
      1. the subject is now `TenantContext.supabase_uid` and the query parameter is
         ignored entirely;
      2. the `state` is HMAC-signed (see `_encode_state`), so it cannot be edited in
         flight — authenticating this endpoint alone would not have been enough.

    The OAuth redirect flow itself is preserved. Because a top-level browser
    navigation cannot carry a bearer token, the default response is now JSON
    carrying the consent URL: the SPA fetches it with its token and then navigates.
    `mode=redirect` keeps the 302 behaviour for a caller that can authenticate the
    navigation itself (e.g. a future cookie session); it is not used by the SPA.
    """
    ctx = await tenant.require(request)
    user_id = ctx.supabase_uid or str(ctx.user_id)
    redirect_uri = _safe_redirect(redirect_uri)

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
        # Strictly this identity: the token layer lets a user with no connection of
        # their own fall back to the sole live account, and inheriting it here would
        # block them from ever linking one.
        others = [p for p in await connected_providers(user_id)
                  if p != provider]
    except Exception as e:  # noqa: BLE001 — a lookup failure must not block connecting
        log.info("connect precheck failed for %s: %s", user_id, e)
        others = []
    if others:
        log.info("refusing %s connect for %s — %s already linked", provider, user_id, others[0])
        bounce = (f"{FRONTEND_URL}{redirect_uri}?connect_error=already_connected"
                  f"&connected_provider={others[0]}")
        if mode == "redirect":
            return RedirectResponse(bounce)
        return JSONResponse(status_code=409, content={
            "error": "already_connected", "connected_provider": others[0],
            "redirect_url": bounce})

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
    authorize_url = f"{cfg['auth_url']}?{urlencode(params)}"
    if mode == "redirect":
        return RedirectResponse(authorize_url)
    return {"authorize_url": authorize_url, "provider": provider}


# ── Callback ───────────────────────────────────────────────────────────────────

@router.get("/auth/{provider}/callback")
async def provider_callback(provider: str, code: str = Query(None), state: str = Query(""),
                            error: str = Query(None),
                            error_description: str = Query(None)):
    """Exchange the code for tokens, fetch the profile, upsert the connection,
    then redirect back to the frontend."""
    cfg = PROVIDERS.get(provider)
    # The subject comes from the SIGNED state and from nowhere else. A tampered,
    # forged, expired or absent state decodes to {}, so `user_id` is empty and the
    # branch below bounces the browser without touching any token store. This is
    # the second half of the R2 fix: authenticating /connect stops an attacker
    # MINTING a hostile state, and the signature stops them EDITING a legitimate one.
    st = _decode_state(state)
    user_id = st.get("user_id", "")
    redirect_uri = _safe_redirect(st.get("redirect_uri", "/"))
    fail = f"{FRONTEND_URL}{redirect_uri}?connect_error={provider}"

    if not cfg:
        return JSONResponse(status_code=404, content={"error": "unknown_provider"})
    if error or not code or not user_id:
        log.warning("%s callback error=%s (%s) code?=%s state_valid?=%s", provider, error,
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
        "token_expiry": expiry,
        "scopes": granted_scopes or normalize_scopes(cfg["scopes"]),
        "raw_profile": profile,
    }
    # Only write refresh_token when one actually came back. A silent re-consent can
    # return none, and storing that None would overwrite the working refresh token
    # already on the row — turning a live connection into one that can only ever
    # report "expired". Omitting the key leaves the stored value untouched in both
    # the file store (dict.update) and the Supabase upsert (DO UPDATE SET on
    # supplied columns only).
    if refresh_token:
        payload["refresh_token"] = refresh_token
    # Encrypt the OAuth tokens once, at rest, before either store sees them.
    from backend.services import token_crypto
    epayload = token_crypto.enc_row(payload)
    # PostgreSQL is the system of record (migration d4e91a3b7c62); Supabase and
    # the JSON file are written too so the fallbacks stay current. A connect is
    # only reported as failed when NO store accepted it — otherwise the user is
    # sent back to re-authorise an account that is, in fact, connected.
    stored_ok = False
    try:
        from backend.services import _token_pg_store as _pg
        await _pg.upsert(user_id, provider, epayload)
        stored_ok = True
    except Exception as e:  # noqa: BLE001
        log.warning("%s connection upsert (Postgres) failed for %s — trying fallbacks: %s",
                    provider, user_id, e)
    try:
        sb = get_supabase_admin()
        sb.table("provider_connections").upsert({
            **epayload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id,provider").execute()
        stored_ok = True
    except Exception as e:
        log.info("%s connection upsert (Supabase) failed for %s — using file fallback: %s",
                 provider, user_id, e)
    try:
        from backend.services import _token_file_store as _file
        _file.upsert(user_id, provider, epayload)
        stored_ok = True
    except Exception as e:
        if not stored_ok:
            log.warning("%s connection upsert failed (all stores) for %s: %s", provider, user_id, e)
            return RedirectResponse(fail)

    if not refresh_token:
        # Any previously stored refresh token was preserved; if there wasn't one, the
        # connection dies at the first token expiry (~1h).
        log.warning("%s connected for %s WITHOUT a refresh token (kept any existing one) "
                    "— check offline_access / access_type=offline and that consent was "
                    "re-prompted.", provider, user_id)

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


async def _self_identity(request: Request) -> str:
    """The caller's OWN external identity, for the two self-service provider routes.

    SECURITY (audit S1/S2): these routes used to take `user_id` from the query
    string while sitting on the middleware's public-prefix list, so an anonymous
    caller could name any victim. Identity now comes from X-Auth-User — injected by
    AuthEnforceMiddleware from the verified token and stripped from the inbound
    request, so it cannot be forged — and the query parameter is ignored entirely.

    `TenantContext.require` additionally refuses an identity that does not resolve
    to an ACTIVE user in a tenant. The supabase uid is what the token stores are
    keyed by, so that is what we hand to `_identity_ids`.
    """
    ctx = await tenant.require(request)
    return ctx.supabase_uid or str(ctx.user_id)


@router.get("/auth/provider/status")
async def provider_status(request: Request):
    """Report which providers THE CALLER has connected. Reads from Supabase
    first, falls back to the file store; checks every id this identity maps to.

    Always self-scoped: there is no way to ask about another user."""
    user_id = await _self_identity(request)
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
async def disconnect_provider(provider: str, request: Request):
    """Delete THE CALLER'S stored connection for a provider, under every id this
    identity maps to, from BOTH stores.

    Always self-scoped (audit S1): this route was anonymous and took the victim's
    id from the query string, so any unauthenticated caller could destroy any
    user's Google/Microsoft credentials. The target is now the authenticated
    caller and nobody else.

    Note: this revokes our copy of the credentials, not the grant itself —
    Microsoft has no simple per-app revoke endpoint, so an already-issued Graph
    access token stays valid until it expires (≤1h)."""
    if provider not in PROVIDERS:
        return JSONResponse(status_code=404, content={"disconnected": False,
                                                      "error": "unknown provider"})
    user_id = await _self_identity(request)
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
