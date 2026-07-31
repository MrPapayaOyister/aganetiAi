"""
Global authentication enforcement (Phase 0 security).

Before this, every route in main.py trusted a caller-supplied `user_id` query
param (default "user_1") and required NO token — the whole API was open on the
network. This ASGI middleware closes that: a request reaches a route handler only
if it is either

  (a) a valid Supabase ES256 JWT whose `sub` maps to an ENABLED internal user, or
  (b) an internal service call presenting the correct X-Internal-Token.

On success it NORMALIZES the effective identity — the request's `user_id` in the
query string, the JSON body, and any path segment equal to the caller's Supabase
sub is rewritten to the mapped internal user_id — so no handler can be tricked
into acting on another user's data by a caller-supplied `user_id` (IDOR).

Public (no auth) paths: health checks, OAuth connect/callback, docs, preflight.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from urllib.parse import parse_qs, urlencode

from starlette.types import ASGIApp, Scope, Receive, Send, Message
from starlette.responses import JSONResponse

from backend.auth.jwt_verify import verify_supabase_jwt
from backend.service_auth import internal_token
from config.users import internal_user_for_sub, USERS

log = logging.getLogger("aganeti.auth.enforce")

# ── LOCAL DEV ONLY: skip the Supabase login ───────────────────────────────────
# Testing the provider OAuth flows against a local frontend means round-tripping
# Supabase social login, which only redirects to origins allow-listed in the
# Supabase project. This flag lets a dev instance run without any login at all.
#
# It is gated THREE ways so it cannot be switched on by accident:
#   1. DEV_AUTH_BYPASS must be explicitly truthy — it is NOT in .env/.env.example,
#      only in scripts/dev_backend.sh, so the systemd unit can never inherit it;
#   2. the request must arrive from loopback — the production DGX is fronted by a
#      remote proxy over Tailscale, so real user traffic never looks local;
#   3. a warning is logged on every startup where it is active.
# Identity normalization still runs, so handlers cannot be tricked via user_id.
DEV_AUTH_BYPASS = os.getenv("DEV_AUTH_BYPASS", "").strip().lower() in ("1", "true", "yes")
DEV_AUTH_USER = os.getenv("DEV_AUTH_USER", "user_1")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

if DEV_AUTH_BYPASS:
    log.warning("=" * 72)
    log.warning("DEV_AUTH_BYPASS IS ON — loopback requests skip authentication entirely")
    log.warning("and act as %r. Never set this on a production instance.", DEV_AUTH_USER)
    log.warning("=" * 72)

# Paths that must stay reachable without a token.
_PUBLIC_PREFIXES = (
    "/health",
    "/auth/google",       # OAuth connect + callback (browser redirect, no bearer)
    "/auth/microsoft",    # same, for the Microsoft Graph connect flow
    "/auth/provider",     # provider status/disconnect used during connect flow
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)


def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return ""
    return ""


def _consteq(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(a, b)
    except Exception:
        return False


class AuthEnforceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        method = scope.get("method", "GET")
        path = scope.get("path", "") or ""

        if method == "OPTIONS" or any(path == p or path.startswith(p + "/") or path.startswith(p)
                                      for p in _PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)

        effective_user: str | None = None
        caller_sub: str = ""

        # (c) local dev bypass — flag + loopback only (see DEV_AUTH_BYPASS above)
        if DEV_AUTH_BYPASS and (scope.get("client") or ("",))[0] in _LOOPBACK:
            effective_user = DEV_AUTH_USER if DEV_AUTH_USER in USERS else "user_1"
            return await self._forward(scope, receive, send, effective_user, "")

        # (b) internal service token
        supplied = _header(scope, b"x-internal-token")
        expected = internal_token()
        if expected and supplied and _consteq(supplied, expected):
            effective_user = _header(scope, b"x-internal-user") or "user_1"
            if effective_user not in USERS:
                effective_user = "user_1"
        else:
            # (a) Supabase bearer JWT
            authz = _header(scope, b"authorization")
            if authz[:7].lower() != "bearer ":
                return await JSONResponse({"detail": "authentication required"}, status_code=401)(scope, receive, send)
            token = authz[7:].strip()
            try:
                claims = verify_supabase_jwt(token)
            except Exception as e:
                log.warning("jwt verify failed (%s %s): %s", method, path, e)
                return await JSONResponse({"detail": "invalid or expired token"}, status_code=401)(scope, receive, send)
            caller_sub = str(claims.get("sub", ""))
            effective_user = internal_user_for_sub(caller_sub)
            if not effective_user:
                # New/multi-user path (Phase B): authorize by allowed email domain and
                # auto-provision on first login, then normalize identity to the stable
                # Supabase sub (resolve_user-compatible → approvals/tasks key to the DB
                # User). Config-registry users never reach here (short-circuited above).
                from backend.auth import identity as _identity
                email = str(claims.get("email", ""))
                name = (claims.get("user_metadata") or {}).get("full_name") or claims.get("name")
                try:
                    await _identity.resolve_or_provision(caller_sub, email, name)
                    effective_user = caller_sub
                except _identity.NotAuthorized:
                    log.warning("authenticated sub not authorized: %s (%s %s)", caller_sub, method, path)
                    return await JSONResponse({"detail": "user not authorized"}, status_code=403)(scope, receive, send)
                except Exception:  # noqa: BLE001
                    log.exception("first-login provisioning failed for sub=%s", caller_sub)
                    return await JSONResponse({"detail": "onboarding failed"}, status_code=503)(scope, receive, send)

        return await self._forward(scope, receive, send, effective_user, caller_sub)

    async def _forward(self, scope: Scope, receive: Receive, send: Send,
                       effective_user: str | None, caller_sub: str) -> None:
        """Normalize the request to the resolved identity, then hand it to the app.

        Shared by all three auth paths (JWT, internal token, dev bypass) so the IDOR
        protection can never be skipped by whichever one authenticated the caller."""
        path = scope.get("path", "") or ""
        scope = dict(scope)

        # Authoritative, non-forgeable identity: strip any client-supplied copy of the
        # trusted header and inject the resolved effective_user. Handlers read identity
        # ONLY from this header (never body/query/X-Internal-User), so a caller can
        # never act as another user or default to the seeded admin.
        _hdrs = [(k, v) for k, v in scope.get("headers", []) if k != b"x-auth-user"]
        _hdrs.append((b"x-auth-user", (effective_user or "").encode("latin-1")))
        scope["headers"] = _hdrs

        # query string
        qs = scope.get("query_string", b"").decode("latin-1")
        if qs and "user_id=" in qs:
            params = parse_qs(qs, keep_blank_values=True)
            params["user_id"] = [effective_user]
            scope["query_string"] = urlencode(params, doseq=True).encode("latin-1")

        # path segment equal to the caller's sub (e.g. /files/<uuid> -> /files/user_1)
        if caller_sub and caller_sub in path:
            new_path = path.replace(caller_sub, effective_user)
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode("latin-1")

        # JSON body: rewrite user_id (leave multipart/streaming uploads untouched)
        ctype = _header(scope, b"content-type").lower()
        if ctype.startswith("application/json"):
            body = b""
            more = True
            while more:
                msg = await receive()
                if msg.get("type") == "http.request":
                    body += msg.get("body", b"")
                    more = msg.get("more_body", False)
                else:
                    more = False
            new_body = body
            if body:
                try:
                    data = json.loads(body)
                    if isinstance(data, dict) and "user_id" in data:
                        data["user_id"] = effective_user
                        new_body = json.dumps(data).encode("utf-8")
                except Exception:
                    new_body = body  # not a JSON object we own — pass through

            # keep content-length consistent with the (possibly) rewritten body
            headers = [(k, v) for k, v in scope.get("headers", []) if k != b"content-length"]
            headers.append((b"content-length", str(len(new_body)).encode("latin-1")))
            scope["headers"] = headers

            _consumed = False

            async def _receive() -> Message:
                nonlocal _consumed
                if not _consumed:
                    _consumed = True
                    return {"type": "http.request", "body": new_body, "more_body": False}
                # Delegate to the real receive so a StreamingResponse's disconnect
                # watcher blocks on a GENUINE client disconnect. Returning a fabricated
                # http.disconnect here made Starlette abort every SSE stream instantly.
                return await receive()

            return await self.app(scope, _receive, send)

        return await self.app(scope, receive, send)
