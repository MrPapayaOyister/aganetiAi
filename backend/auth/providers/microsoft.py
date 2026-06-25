"""
Microsoft provider — Outlook, Calendar, Contacts.

Replaces the current MSAL device-flow approach with a proper web OAuth flow
via Supabase. The existing m365_auth.py device flow is preserved for
backward compatibility during migration but new users go through Supabase.

Key change: tokens now live in provider_connections table (encrypted),
not in tokens/{user_id}_m365_token.json files.
"""
from __future__ import annotations
import os
import httpx
from .base import BaseProvider, ProviderCapability, TokenData

M365_CLIENT_ID     = os.getenv("M365_CLIENT_ID", "")
M365_CLIENT_SECRET = os.getenv("M365_CLIENT_SECRET", "")
M365_TENANT        = os.getenv("M365_TENANT_ID", "common")
M365_TOKEN_URL     = f"https://login.microsoftonline.com/{M365_TENANT}/oauth2/v2.0/token"
M365_REVOKE_URL    = f"https://login.microsoftonline.com/{M365_TENANT}/oauth2/v2.0/logout"


class MicrosoftProvider(BaseProvider):
    name = "microsoft"
    display_name = "Microsoft 365"

    capability_scopes = {
        ProviderCapability.EMAIL_READ: [
            "Mail.Read",
            "offline_access",
        ],
        ProviderCapability.EMAIL_SEND: [
            "Mail.Send",
            "offline_access",
        ],
        ProviderCapability.CALENDAR_READ: [
            "Calendars.Read",
            "offline_access",
        ],
        ProviderCapability.CALENDAR_WRITE: [
            "Calendars.ReadWrite",
            "offline_access",
        ],
        ProviderCapability.CONTACTS_READ: [
            "Contacts.Read",
            "offline_access",
        ],
    }

    async def refresh_access_token(self, refresh_token: str) -> TokenData:
        async with httpx.AsyncClient() as client:
            r = await client.post(M365_TOKEN_URL, data={
                "client_id":     M365_CLIENT_ID,
                "client_secret": M365_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
                "scope":         "offline_access Mail.Read Mail.Send Calendars.ReadWrite Contacts.Read",
            })
        r.raise_for_status()
        data = r.json()
        return TokenData(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            expires_in=data.get("expires_in", 3600),
            scopes=data.get("scope", "").split(),
        )

    async def revoke_token(self, token: str) -> None:
        # Microsoft doesn't have a simple revoke endpoint; token expiry is relied upon.
        # For a real implementation, delete from provider_connections and let tokens expire.
        pass
