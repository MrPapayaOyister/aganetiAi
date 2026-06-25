"""
Provider protocol — every provider implements this interface.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass
from typing import Optional


class ProviderCapability(str, Enum):
    EMAIL_READ     = "email.read"
    EMAIL_SEND     = "email.send"
    CALENDAR_READ  = "calendar.read"
    CALENDAR_WRITE = "calendar.write"
    CONTACTS_READ  = "contacts.read"
    FILES_READ     = "files.read"


@dataclass
class TokenData:
    access_token: str
    refresh_token: Optional[str]
    expires_in: Optional[int]       # seconds
    scopes: list[str]


class BaseProvider(ABC):
    name: str                       # 'google' | 'microsoft'
    display_name: str
    capability_scopes: dict[ProviderCapability, list[str]]   # capability → required scopes

    @abstractmethod
    async def refresh_access_token(self, refresh_token: str) -> TokenData:
        """Exchange refresh token for a new access token."""
        ...

    @abstractmethod
    async def revoke_token(self, token: str) -> None:
        """Revoke access when user disconnects the provider."""
        ...

    def scopes_for(self, *capabilities: ProviderCapability) -> list[str]:
        """Return the union of scopes required for the given capabilities."""
        scopes: set[str] = set()
        for cap in capabilities:
            scopes.update(self.capability_scopes.get(cap, []))
        return sorted(scopes)

    def check_capability(self, granted_scopes: list[str], cap: ProviderCapability) -> bool:
        required = self.capability_scopes.get(cap, [])
        return all(s in granted_scopes for s in required)
