"""
Google provider — Gmail + Google Calendar.

Supabase handles the OAuth dance. We only need to refresh the access token
when it expires, using the refresh_token stored in provider_connections.

Scopes requested during Supabase Google OAuth configuration:
  https://www.googleapis.com/auth/gmail.readonly
  https://www.googleapis.com/auth/gmail.send
  https://www.googleapis.com/auth/calendar.readonly
  https://www.googleapis.com/auth/calendar.events
"""
from __future__ import annotations
import os
import httpx
from .base import BaseProvider, ProviderCapability, TokenData

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_TOKEN_URL     = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL    = "https://oauth2.googleapis.com/revoke"


class GoogleProvider(BaseProvider):
    name = "google"
    display_name = "Google"

    capability_scopes = {
        ProviderCapability.EMAIL_READ: [
            "https://www.googleapis.com/auth/gmail.readonly"
        ],
        ProviderCapability.EMAIL_SEND: [
            "https://www.googleapis.com/auth/gmail.send"
        ],
        ProviderCapability.CALENDAR_READ: [
            "https://www.googleapis.com/auth/calendar.readonly"
        ],
        ProviderCapability.CALENDAR_WRITE: [
            "https://www.googleapis.com/auth/calendar.events"
        ],
        ProviderCapability.CONTACTS_READ: [
            "https://www.googleapis.com/auth/contacts.readonly"
        ],
        ProviderCapability.FILES_READ: [
            "https://www.googleapis.com/auth/drive.readonly"
        ],
    }

    async def refresh_access_token(self, refresh_token: str) -> TokenData:
        async with httpx.AsyncClient() as client:
            r = await client.post(GOOGLE_TOKEN_URL, data={
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            })
        r.raise_for_status()
        data = r.json()
        return TokenData(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),  # Google may not reissue
            expires_in=data.get("expires_in", 3600),
            scopes=data.get("scope", "").split(),
        )

    async def revoke_token(self, token: str) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(GOOGLE_REVOKE_URL, params={"token": token})
