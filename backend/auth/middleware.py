"""
JWT validation middleware for Supabase-issued tokens.

Supabase signs JWTs with RS256 using a project-specific key pair.
We validate locally (no network call) using the public JWKS endpoint
or the static JWT secret from the Supabase dashboard.

Set in .env:
  SUPABASE_URL=https://xxxx.supabase.co
  SUPABASE_JWT_SECRET=your-jwt-secret   # from Project Settings → API → JWT Secret
  SUPABASE_SERVICE_KEY=...              # for backend-to-Supabase DB calls (service role)
"""
from __future__ import annotations

import os
import logging
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .context import UserContext, ProviderContext
from .supabase_client import get_supabase_admin
from backend.auth.jwt_verify import verify_supabase_jwt

logger = logging.getLogger("aganeti.auth")

_bearer = HTTPBearer(auto_error=False)


def _decode_jwt(token: str) -> dict:
    """Validate a Supabase ES256 JWT via JWKS. Raises HTTPException on failure."""
    try:
        return verify_supabase_jwt(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}")
    except Exception as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}")


async def _build_user_context(user_id: str, email: str) -> UserContext:
    """
    Load profile, feature flags, and provider connections from Supabase
    and assemble a UserContext. Single DB call pattern using select().
    """
    # Resilient: any missing table / DB hiccup degrades to a minimal context
    # rather than 500-ing the request (the global auth middleware already gate-kept it).
    profile: dict = {}
    flags: list[str] = []
    providers: list[ProviderContext] = []
    try:
        supabase = get_supabase_admin()
        try:
            profile = supabase.table("profiles").select("*").eq("id", user_id).single().execute().data or {}
        except Exception:
            profile = {}
        try:
            flags_resp = supabase.table("feature_flags").select("flag").eq("user_id", user_id).eq("enabled", True).execute()
            flags = [r["flag"] for r in (flags_resp.data or [])]
        except Exception:
            flags = []
        try:
            providers_resp = (
                supabase.table("provider_connections")
                .select("provider, provider_email, scopes, token_expiry")
                .eq("user_id", user_id).execute()
            )
            providers = [
                ProviderContext(
                    provider=r["provider"], email=r.get("provider_email", ""),
                    scopes=r.get("scopes") or [], token_expiry=r.get("token_expiry"),
                )
                for r in (providers_resp.data or [])
            ]
        except Exception:
            providers = []
    except Exception as e:
        logger.warning("build_user_context degraded for %s: %s", user_id, e)

    return UserContext(
        user_id=user_id,
        email=email,
        display_name=profile.get("display_name") or (email.split("@")[0] if email else user_id),
        plan=profile.get("plan", "free"),
        features=flags,
        providers=providers,
    )


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> UserContext:
    """FastAPI dependency — validates JWT and returns UserContext."""
    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    payload = _decode_jwt(credentials.credentials)
    user_id: str = payload["sub"]
    email: str = payload.get("email", "")

    ctx = await _build_user_context(user_id, email)
    return ctx


async def optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[UserContext]:
    """Like get_current_user but returns None instead of 401 for unauthenticated."""
    if not credentials:
        return None
    try:
        payload = _decode_jwt(credentials.credentials)
        return await _build_user_context(payload["sub"], payload.get("email", ""))
    except HTTPException:
        return None
