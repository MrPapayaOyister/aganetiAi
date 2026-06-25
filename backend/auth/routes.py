"""
Auth-related API routes:
  GET  /api/me                   — current user profile + capabilities
  POST /api/auth/provider/connect — store provider tokens after Supabase OAuth
  DELETE /api/auth/provider/{name} — disconnect a provider
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from .middleware import get_current_user
from .context import UserContext
from .supabase_client import get_supabase_admin
from .providers.registry import get_provider

logger = logging.getLogger("aganeti.auth.routes")
router = APIRouter(prefix="/auth", tags=["auth"])


# ── /api/me ────────────────────────────────────────────────────

@router.get("/me")
async def me(user: UserContext = Depends(get_current_user)):
    """Return the current user's identity, plan, features, and available tools."""
    return {
        "user_id":       user.user_id,
        "email":         user.email,
        "display_name":  user.display_name,
        "plan":          user.plan,
        "features":      user.features,
        "providers": [
            {
                "provider":       p.provider,
                "email":          p.email,
                "scopes":         p.scopes,
            }
            for p in user.providers
        ],
        "available_tools": user.available_tools(),
    }


# ── Provider connect ───────────────────────────────────────────

class ConnectProviderRequest(BaseModel):
    provider: str                   # 'google' | 'microsoft'
    provider_email: Optional[str]   # email at the provider
    access_token: str
    refresh_token: Optional[str]
    expires_in: Optional[int]       # seconds until access_token expires
    scopes: list[str]               # granted scopes


@router.post("/provider/connect")
async def connect_provider(
    body: ConnectProviderRequest,
    user: UserContext = Depends(get_current_user),
):
    """
    Store provider OAuth tokens after the user completes the Supabase OAuth flow.
    Called from the frontend after Supabase returns the session with provider tokens.
    """
    if body.provider not in ("google", "microsoft"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown provider: {body.provider}")

    expiry = None
    if body.expires_in:
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=body.expires_in)).isoformat()

    supabase = get_supabase_admin()
    supabase.table("provider_connections").upsert({
        "user_id":        user.user_id,
        "provider":       body.provider,
        "provider_email": body.provider_email,
        "scopes":         body.scopes,
        "access_token":   body.access_token,
        "refresh_token":  body.refresh_token,
        "token_expiry":   expiry,
        "connected_at":   datetime.now(timezone.utc).isoformat(),
        "last_refreshed": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="user_id,provider").execute()

    logger.info("User %s connected provider %s", user.user_id, body.provider)
    return {"status": "connected", "provider": body.provider}


# ── Provider disconnect ────────────────────────────────────────

@router.delete("/provider/{provider_name}")
async def disconnect_provider(
    provider_name: str,
    user: UserContext = Depends(get_current_user),
):
    """Disconnect a provider and revoke the stored tokens."""
    if provider_name not in ("google", "microsoft"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown provider")

    supabase = get_supabase_admin()
    row = (
        supabase.table("provider_connections")
        .select("access_token")
        .eq("user_id", user.user_id)
        .eq("provider", provider_name)
        .single()
        .execute()
    )

    if row.data:
        try:
            provider = get_provider(provider_name)
            await provider.revoke_token(row.data.get("access_token", ""))
        except Exception:
            pass  # best-effort revoke

    supabase.table("provider_connections").delete().eq(
        "user_id", user.user_id
    ).eq("provider", provider_name).execute()

    logger.info("User %s disconnected provider %s", user.user_id, provider_name)
    return {"status": "disconnected", "provider": provider_name}
