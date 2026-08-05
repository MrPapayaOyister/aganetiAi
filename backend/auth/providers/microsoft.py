"""
Microsoft provider — Outlook, Calendar, Contacts.

Capability → scope map for the UserContext capability checks. The live OAuth
flow (connect/callback) is in backend/routes/provider_auth.py and the token
refresh in backend/services/provider_tokens.py; this class is the declarative
half that decides which tools a granted scope set unlocks.

Scope names here are BARE Graph permissions ("Mail.Read"). Graph hands back
fully-qualified ones ("https://graph.microsoft.com/Mail.Read"), which is why
every stored scope list is passed through provider_tokens.normalize_scopes()
first — without that, every capability check below silently returns False.
offline_access is deliberately absent: it is requested at consent time but is
never echoed in the granted-scope list, so requiring it here would also fail.
"""
from __future__ import annotations
import os
import httpx
from .base import BaseProvider, ProviderCapability, TokenData

M365_CLIENT_ID     = os.getenv("M365_CLIENT_ID", "")
M365_CLIENT_SECRET = os.getenv("M365_CLIENT_SECRET", "")
M365_TENANT        = os.getenv("M365_TENANT_ID", "common")
M365_TOKEN_URL     = f"https://login.microsoftonline.com/{M365_TENANT}/oauth2/v2.0/token"


class MicrosoftProvider(BaseProvider):
    name = "microsoft"
    display_name = "Microsoft 365"

    capability_scopes = {
        # Mail.ReadWrite implies Mail.Read; either alone unlocks reading, so the
        # check in UserContext accepts the granted set containing any of them.
        ProviderCapability.EMAIL_READ:     ["Mail.ReadWrite"],
        ProviderCapability.EMAIL_SEND:     ["Mail.Send"],
        ProviderCapability.CALENDAR_READ:  ["Calendars.ReadWrite"],
        ProviderCapability.CALENDAR_WRITE: ["Calendars.ReadWrite"],
        # ProviderCapability.CONTACTS_READ:  ["Contacts.Read"],
    }

    async def refresh_access_token(self, refresh_token: str) -> TokenData:
        from backend.services.provider_tokens import MS_SCOPES, normalize_scopes
        async with httpx.AsyncClient() as client:
            r = await client.post(M365_TOKEN_URL, data={
                "client_id":     M365_CLIENT_ID,
                "client_secret": M365_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
                "scope":         " ".join(MS_SCOPES),
            })
        r.raise_for_status()
        data = r.json()
        return TokenData(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            expires_in=data.get("expires_in", 3600),
            scopes=normalize_scopes(data.get("scope", "")),
        )

    async def revoke_token(self, token: str) -> None:
        # Graph has no per-application token-revoke endpoint. Disconnecting deletes
        # our stored credentials; an already-issued access token stays valid until
        # it expires (≤1h). Revoking the whole session needs
        # POST /users/{id}/revokeSignInSessions, which requires admin consent.
        return None
