"""Calendar — wraps `mailbox.agenda_text`, which already routes Microsoft/Google."""
from __future__ import annotations

from ..bundle import ContextItem
from .base import ContextProvider, ContextRequest


class CalendarProvider(ContextProvider):
    name = "calendar"
    timeout = 8.0                      # a cold provider token can mean a refresh round trip

    async def collect(self, request: ContextRequest) -> list[ContextItem]:
        from backend.services import mailbox

        text = (await mailbox.agenda_text(request.user_id) or "").strip()
        if not text:
            return []
        return [ContextItem(text=text, provider=self.name, source="agenda")]
