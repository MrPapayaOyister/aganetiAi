"""
Google Calendar integration (per-user, via OAuth tokens).

Returns event shapes the dashboard expects. Async over the shared httpx client.
Never logs tokens; falls back to mock data when unconfigured.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from backend.services.provider_tokens import get_google_headers, GOOGLE_CONFIGURED
from backend.services.http_client import google_request

log = logging.getLogger("aria.gcal")

CAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

_MOCK_AGENDA: list[dict] = []  # no fake data — routes return connected:False when Google is unconfigured


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_event(ev: dict) -> dict:
    start = ev.get("start", {})
    end = ev.get("end", {})
    attendees = [{"name": a.get("displayName") or a.get("email", ""), "email": a.get("email", "")}
                 for a in ev.get("attendees", []) or []]
    meet_link = ev.get("hangoutLink")
    if not meet_link:
        for ep in (ev.get("conferenceData", {}).get("entryPoints", []) or []):
            if ep.get("entryPointType") == "video":
                meet_link = ep.get("uri")
                break
    return {
        "id": ev.get("id"),
        "title": ev.get("summary") or "(no title)",
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "location": ev.get("location"),
        "attendees": attendees,
        "is_online": bool(meet_link or ev.get("conferenceData")),
        "meet_link": meet_link,
        "organizer": ev.get("organizer", {}).get("email", ""),
        "description": ev.get("description"),
        "calendar": "primary",
    }


async def get_google_agenda(user_id: str, days_ahead: int = 1) -> list[dict]:
    """Return events from now through `days_ahead` days, time-ordered."""
    if not GOOGLE_CONFIGURED:
        return _MOCK_AGENDA
    headers = await get_google_headers(user_id)
    now = datetime.now(timezone.utc)
    r = await google_request("GET", CAL, headers=headers, params={
        "timeMin": _iso(now),
        "timeMax": _iso(now + timedelta(days=days_ahead)),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 20,
    })
    if r.status_code != 200:
        log.warning("gcal agenda failed for %s: %s", user_id, r.status_code)
        return []
    return [_parse_event(e) for e in (r.json().get("items") or [])]


async def create_google_event(user_id: str, title: str, start: str, end: str,
                              description: str | None = None,
                              attendees: list[str] | None = None,
                              location: str | None = None) -> dict:
    """Create an event on the user's primary calendar. start/end are ISO datetimes."""
    if not GOOGLE_CONFIGURED:
        return {"id": "mock_event", "htmlLink": None, "hangoutLink": None}
    headers = await get_google_headers(user_id)
    body: dict = {
        "summary": title,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]
    r = await google_request("POST", CAL, headers=headers, json=body,
                             params={"sendUpdates": "all"})
    if r.status_code not in (200, 201):
        log.warning("gcal create failed for %s: %s", user_id, r.status_code)
        raise RuntimeError(f"Calendar create failed ({r.status_code})")
    d = r.json()
    return {"id": d.get("id"), "htmlLink": d.get("htmlLink"), "hangoutLink": d.get("hangoutLink")}


async def get_next_event(user_id: str) -> dict | None:
    """Return the single next upcoming event from now, or None."""
    if not GOOGLE_CONFIGURED:
        return None
    headers = await get_google_headers(user_id)
    now = datetime.now(timezone.utc)
    r = await google_request("GET", CAL, headers=headers, params={
        "timeMin": _iso(now), "singleEvents": "true", "orderBy": "startTime", "maxResults": 1,
    })
    if r.status_code != 200:
        return None
    items = r.json().get("items") or []
    return _parse_event(items[0]) if items else None
