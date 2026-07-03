"""
Shared Supabase JWT verification (Phase 0 security).

Supabase (2024+) signs auth JWTs with **ES256** using a per-project key pair and
publishes the public keys at `${SUPABASE_URL}/auth/v1/.well-known/jwks.json`.
The old HS256-shared-secret path is legacy and is NOT used by this project
(SUPABASE_JWT_SECRET is empty; the live JWKS advertises alg=ES256).

This module is the single place that validates a bearer token, used by both the
global enforcement middleware (backend/auth/enforce.py) and the per-route
dependency (backend/auth/middleware.get_current_user).
"""
from __future__ import annotations

import os
import threading
import jwt
from jwt import PyJWKClient

_jwk_client: PyJWKClient | None = None
_lock = threading.Lock()


def _supabase_url() -> str:
    # read lazily so .env (loaded by config.settings) is already applied
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def _client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        with _lock:
            if _jwk_client is None:
                url = _supabase_url()
                if not url:
                    raise RuntimeError("SUPABASE_URL not set — cannot verify JWTs")
                jwks_url = f"{url}/auth/v1/.well-known/jwks.json"
                # cache keys for an hour; PyJWKClient refetches on unknown kid
                _jwk_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
    return _jwk_client


def verify_supabase_jwt(token: str) -> dict:
    """
    Validate a Supabase ES256 access token via JWKS.
    Returns the decoded claims dict on success; raises on any failure
    (expired, bad signature, wrong audience/issuer, missing claims).
    """
    signing_key = _client().get_signing_key_from_jwt(token)
    url = _supabase_url()
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256"],
        audience="authenticated",
        issuer=f"{url}/auth/v1",
        options={"require": ["sub", "exp", "aud"]},
    )
