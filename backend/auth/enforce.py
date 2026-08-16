"""
Global authentication enforcement (Phase 0 security).

Before this, every route in main.py trusted a caller-supplied `user_id` query
param (default "user_1") and required NO token — the whole API was open on the
network. This ASGI middleware closes that: a request reaches a route handler only
if it is either

  (a) a valid Supabase ES256 JWT whose `sub` resolves to an ACTIVE user, or
  (b) an internal service call presenting the correct X-Internal-Token.

On success it NORMALIZES the effective identity — the request's `user_id` in the
query string, the JSON body, and any path segment equal to the caller's Supabase
sub is rewritten to the resolved user_id — so no handler can be tricked into
acting on another user's data by a caller-supplied `user_id` (IDOR).

The effective identity IS the Supabase sub, for every user, with no alias table
in between. An earlier version mapped a hand-listed pair of subs (USER_n_* in
.env) onto "user_1"/"user_2" and sent everyone else through under their raw sub.
Two id schemes for one user base meant data written under one was invisible to
lookups under the other; see backend/services/user_directory.py.

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
DEV_AUTH_USER = os.getenv("DEV_AUTH_USER", "").strip()
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

if DEV_AUTH_BYPASS:
    log.warning("=" * 72)
    log.warning("DEV_AUTH_BYPASS IS ON — loopback requests skip authentication entirely")
    log.warning("and act as %r. Never set this on a production instance.", DEV_AUTH_USER)
    log.warning("=" * 72)

# Paths that must stay reachable without a token.
#
# "/auth/provider" WAS on this list, which made GET /auth/provider/status and
# DELETE /auth/provider/{provider} anonymous — and because the public branch
# returns before _forward(), the caller-supplied ?user_id= was never rewritten
# either. Any unauthenticated caller could therefore read which providers an
# arbitrary user had connected (audit S2) and delete that user's stored OAuth
# credentials from both stores (audit S1). Those two routes now authenticate like
# everything else and take their identity from the trusted header.
#
# Only the provider CALLBACKS stay public. They genuinely cannot carry a bearer:
# the provider redirects the user's browser to them and controls the request.
#
# /auth/{provider}/connect is NOT public any more. It was, and it accepted a
# caller-supplied `user_id` which it embedded in an unsigned `state` — so an
# anonymous caller could start a consent flow naming a victim and have their own
# provider account linked into that victim's record (audit R2). The SPA now fetches
# the consent URL with its bearer and navigates to the result; the redirect flow is
# unchanged from the provider's point of view.
#
# The callbacks are safe to leave open because the ONLY thing they trust is the
# HMAC-signed `state` this server issued (backend/routes/provider_auth.py).
_PUBLIC_PREFIXES = (
    "/health",
    "/auth/google/callback",      # provider -> browser -> us; no bearer possible
    "/auth/microsoft/callback",   # same, Microsoft Graph
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
            if not DEV_AUTH_USER:
                return await JSONResponse(
                    {"detail": "DEV_AUTH_BYPASS is on but DEV_AUTH_USER is unset"},
                    status_code=500)(scope, receive, send)
            return await self._forward(scope, receive, send, DEV_AUTH_USER, "")

        # (b) internal service token
        supplied = _header(scope, b"x-internal-token")
        expected = internal_token()
        if expected and supplied and _consteq(supplied, expected):
            # The caller declares whom it is acting for. This used to fall back to a
            # hardcoded "user_1" when the header was missing or unrecognised, which
            # meant any internal call that forgot to name a user silently ran as the
            # seeded admin — against that person's mail, calendar and tasks. Absent
            # is now an error, not a default.
            effective_user = _header(scope, b"x-internal-user").strip()
            if not effective_user:
                log.warning("internal call to %s %s omitted X-Internal-User", method, path)
                return await JSONResponse(
                    {"detail": "X-Internal-User required"}, status_code=400)(scope, receive, send)
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
            # ONE identity path for everybody. There used to be a short-circuit here:
            # if the sub matched a USER_n_SUPABASE_UID in .env, the request became
            # "user_1" and skipped the checks below. That split the user base in two —
            # a couple of people on a hand-edited alias, everyone else on their sub —
            # and the two halves keyed their data differently, which is how one
            # account's OAuth tokens ended up stored under an id nothing resolved.
            #
            # Now every authenticated caller is normalized to their Supabase sub, and
            # authorization (domain allowlist, disabled accounts, auto-provisioning on
            # first login) is decided in exactly one place.
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

        # query string — ALWAYS set, never merely corrected.
        #
        # This used to rewrite user_id only `if "user_id=" in qs`, which quietly made
        # the protection opt-in from the client's side: dozens of handlers are
        # declared `user_id: str = "user_1"`, so a request that simply OMITTED the
        # parameter skipped the rewrite and ran against the seeded admin's data. The
        # header below was trustworthy, but only the handlers that bothered to read
        # it benefited. Injecting unconditionally makes every one of those defaults
        # dead code — FastAPI ignores the extra parameter on routes that declare no
        # user_id, so this is safe to apply to every request.
        qs = scope.get("query_string", b"").decode("latin-1")
        params = parse_qs(qs, keep_blank_values=True) if qs else {}
        params["user_id"] = [effective_user]
        scope["query_string"] = urlencode(params, doseq=True).encode("latin-1")

        # path segment equal to the caller's sub (normalized to the effective id)
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
                    # Set unconditionally, for the same reason as the query string:
                    # a body that omits user_id would otherwise fall through to a
                    # handler default. No model in this app declares extra="forbid",
                    # so an added field is ignored by routes that do not want it.
                    if isinstance(data, dict):
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
