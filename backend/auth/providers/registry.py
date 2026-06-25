"""
Provider registry — look up a provider and retrieve a valid access token.
Handles transparent refresh and persists updated tokens to Supabase.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from .base import BaseProvider
from .google import GoogleProvider
from .microsoft import MicrosoftProvider
from ..supabase_client import get_supabase_admin

logger = logging.getLogger("aganeti.providers")

_REGISTRY: dict[str, BaseProvider] = {
    "google":    GoogleProvider(),
    "microsoft": MicrosoftProvider(),
}


def get_provider(name: str) -> BaseProvider:
    p = _REGISTRY.get(name)
    if not p:
        raise ValueError(f"Unknown provider: {name!r}")
    return p


async def get_provider_token(user_id: str, provider_name: str) -> Optional[str]:
    """
    Returns a valid access token for the given user + provider.
    Transparently refreshes if the token is expired or expires within 5 minutes.
    Returns None if the user has no connection for this provider.
    """
    supabase = get_supabase_admin()
    resp = (
        supabase.table("provider_connections")
        .select("access_token, refresh_token, token_expiry")
        .eq("user_id", user_id)
        .eq("provider", provider_name)
        .single()
        .execute()
    )

    if not resp.data:
        return None

    row = resp.data
    access_token: str = row.get("access_token", "")
    refresh_token: str = row.get("refresh_token", "")
    expiry_str: Optional[str] = row.get("token_expiry")

    # Check if we need to refresh
    needs_refresh = False
    if expiry_str:
        expiry = datetime.fromisoformat(expiry_str)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        needs_refresh = expiry <= datetime.now(timezone.utc) + timedelta(minutes=5)
    elif not access_token:
        needs_refresh = True

    if needs_refresh and refresh_token:
        try:
            provider = get_provider(provider_name)
            new_tokens = await provider.refresh_access_token(refresh_token)
            new_expiry = (
                datetime.now(timezone.utc) + timedelta(seconds=new_tokens.expires_in or 3600)
            ).isoformat()
            # Persist refreshed tokens
            supabase.table("provider_connections").update({
                "access_token":   new_tokens.access_token,
                "refresh_token":  new_tokens.refresh_token or refresh_token,
                "token_expiry":   new_expiry,
                "last_refreshed": datetime.now(timezone.utc).isoformat(),
            }).eq("user_id", user_id).eq("provider", provider_name).execute()
            return new_tokens.access_token
        except Exception as e:
            logger.warning("Token refresh failed for %s/%s: %s", user_id, provider_name, e)
            return None

    return access_token or None
