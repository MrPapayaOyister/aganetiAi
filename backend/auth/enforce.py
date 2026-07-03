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
from urllib.parse import parse_qs, urlencode

from starlette.types import ASGIApp, Scope, Receive, Send, Message
from starlette.responses import JSONResponse

from backend.auth.jwt_verify import verify_supabase_jwt
from backend.service_auth import internal_token
from config.users import internal_user_for_sub, USERS

log = logging.getLogger("aganeti.auth.enforce")

# Paths that must stay reachable without a token.
_PUBLIC_PREFIXES = (
    "/health",
    "/auth/google",       # OAuth connect + callback (browser redirect, no bearer)
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
                log.warning("authenticated sub not authorized: %s (%s %s)", caller_sub, method, path)
                return await JSONResponse({"detail": "user not authorized"}, status_code=403)(scope, receive, send)

        # ── normalize identity so handlers cannot be tricked by a supplied user_id ──
        scope = dict(scope)

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
                return {"type": "http.disconnect"}

            return await self.app(scope, _receive, send)

        return await self.app(scope, receive, send)
