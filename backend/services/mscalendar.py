"""
Outlook Calendar over Microsoft Graph (per-user OAuth tokens).

Async twin of `backend/services/gcalendar.py`. Structured reads return the same
event shape the Google service returns (id/title/start/end/location/attendees/
meet_link/…) so the dashboard and tool layer never branch on provider. The
Telegram-facing text builders (today's agenda, meeting brief) are kept here
because they are Graph-flavoured and were previously in integrations/.

Per-user TTL caches preserved from the retired module: agenda 5 min, upcoming
events 2 min.

Time zones: every read asks Graph for the app's configured zone via
`Prefer: outlook.timezone` and emits offset-bearing ISO strings, so nothing
downstream ever sees a bare UTC wall clock. See `backend/services/user_tz.py`.
"""
from __future__ import annotations

import time
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from backend.services.provider_tokens import get_ms_headers, MS_CONFIGURED
from backend.services.http_client import api_request
from backend.services import user_tz

log = logging.getLogger("aria.mscal")

GRAPH = "https://graph.microsoft.com/v1.0"

_agenda_cache: dict[str, tuple[float, str]] = {}
AGENDA_CACHE_TTL = 300

_events_cache: dict[str, tuple[float, list]] = {}
EVENTS_CACHE_TTL = 120


def _slot_dt(slot: dict | None, tz: ZoneInfo) -> datetime | None:
    """A Graph {dateTime, timeZone} pair as an aware datetime in `tz`.

    Graph's dateTime is naive; the sibling timeZone says what it is naive *in* —
    "UTC" by default, or whatever `Prefer: outlook.timezone` asked for. Reading that
    label rather than assuming UTC keeps the conversion right even if the header is
    ever dropped or overridden upstream."""
    slot = slot or {}
    stated = user_tz.zone_or(slot.get("timeZone"), tz)
    return user_tz.localize(slot.get("dateTime"), tz, assume=stated)


def _slot_iso(slot: dict | None, tz: ZoneInfo) -> str:
    """A Graph time slot as an offset-bearing ISO string in `tz`."""
    dt = _slot_dt(slot, tz)
    return dt.isoformat() if dt else ""


def _fmt_time(slot: dict | None, tz: ZoneInfo) -> str:
    """HH:MM in the app's zone, for the Telegram/LLM agenda lines."""
    dt = _slot_dt(slot, tz)
    return dt.strftime("%H:%M") if dt else ""


def _parse_event(ev: dict, tz: ZoneInfo) -> dict:
    """Graph event → the shared event shape (same keys gcalendar emits).

    start/end come out as ISO strings carrying the configured zone's UTC offset, so
    every consumer — prompt text, dashboard, `new Date()` in the browser — reads the
    same instant without having to know Graph answered in UTC."""
    attendees = [{"name": (a.get("emailAddress") or {}).get("name", "")
                          or (a.get("emailAddress") or {}).get("address", ""),
                  "email": (a.get("emailAddress") or {}).get("address", "")}
                 for a in ev.get("attendees", []) or []]
    om = ev.get("onlineMeeting") or {}
    meet_link = om.get("joinUrl") or ev.get("onlineMeetingUrl")
    return {
        "id": ev.get("id"),
        "title": ev.get("subject") or "(no title)",
        "start": _slot_iso(ev.get("start"), tz),
        "end": _slot_iso(ev.get("end"), tz),
        "location": (ev.get("location") or {}).get("displayName") or None,
        "attendees": attendees,
        "is_online": bool(meet_link or ev.get("isOnlineMeeting")),
        "meet_link": meet_link,
        "organizer": ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address", ""),
        "description": ev.get("bodyPreview") or None,
        "calendar": "primary",
    }


async def _calendar_view(user_id: str, start: datetime, end: datetime,
                         select: str, top: int = 25,
                         tz_name: str | None = None) -> list[dict]:
    """Raw calendarView query, cancelled events filtered out.

    `Prefer: outlook.timezone` is what makes Graph answer in the local wall clock;
    without it every dateTime comes back as UTC and a 09:30 Dubai meeting reads as
    05:30. Graph accepts IANA names directly (verified against Asia/Dubai and
    Europe/London), so no Windows time-zone mapping table is needed, and Graph
    applies the DST rules of the named zone per event."""
    headers = dict(await get_ms_headers(user_id))
    headers["Prefer"] = f'outlook.timezone="{tz_name or user_tz.tz_name()}"'
    r = await api_request("GET", f"{GRAPH}/me/calendarView", headers=headers, params={
        "startDateTime": start.isoformat().replace("+00:00", ""),
        "endDateTime": end.isoformat().replace("+00:00", ""),
        "$select": select,
        "$orderby": "start/dateTime",
        "$top": top,
    })
    if r.status_code != 200:
        log.warning("graph calendarView failed for %s: %s", user_id, r.status_code)
        return []
    return [e for e in (r.json().get("value") or []) if not e.get("isCancelled", False)]


# ── Structured reads (dashboard / tool layer) ─────────────────────────────────

async def get_agenda(user_id: str, days_ahead: int = 1) -> list[dict]:
    """Events from now through `days_ahead` days, time-ordered."""
    if not MS_CONFIGURED:
        return []
    tz = user_tz.tz()
    now = datetime.now(timezone.utc)
    raw = await _calendar_view(
        user_id, now, now + timedelta(days=days_ahead),
        "id,subject,start,end,location,attendees,onlineMeeting,isOnlineMeeting,"
        "organizer,bodyPreview,isAllDay,isCancelled", top=20, tz_name=str(tz))
    return [_parse_event(e, tz) for e in raw]


async def get_next_event(user_id: str) -> dict | None:
    """The single next upcoming event from now, or None."""
    events = await get_agenda(user_id, days_ahead=14)
    return events[0] if events else None


async def create_event(user_id: str, title: str, start: str, end: str,
                       description: str | None = None,
                       attendees: list[str] | None = None,
                       location: str | None = None,
                       timezone_str: str | None = None) -> dict:
    """
    Create an event on the user's default calendar. `start`/`end` are naive local
    ISO strings — Graph takes the zone separately. isOnlineMeeting=True makes
    Microsoft mint the Teams link.

    `timezone_str` defaults to the app's configured zone, matching the zone the
    reads are rendered in, so "book me 3pm tomorrow" round-trips as 3pm.
    """
    if not MS_CONFIGURED:
        raise HTTPException(status_code=503, detail={
            "error": "microsoft_not_configured",
            "message": "Microsoft OAuth is not configured on the server.",
        })
    headers = await get_ms_headers(user_id)
    tz_name = timezone_str or user_tz.tz_name()
    payload: dict = {
        "subject": title,
        "body": {"contentType": "HTML", "content": description or ""},
        "start": {"dateTime": start, "timeZone": tz_name},
        "end": {"dateTime": end, "timeZone": tz_name},
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
    }
    if location:
        payload["location"] = {"displayName": location}
    if attendees:
        payload["attendees"] = [{"emailAddress": {"address": e}, "type": "required"}
                                for e in attendees]
    r = await api_request("POST", f"{GRAPH}/me/events", headers=headers, json=payload)
    if r.status_code not in (200, 201):
        log.warning("graph event create failed for %s: %s", user_id, r.status_code)
        raise RuntimeError(f"Calendar create failed ({r.status_code})")
    d = r.json()
    invalidate_agenda_cache(user_id)
    return {
        "id": d.get("id"),
        "htmlLink": d.get("webLink"),
        "hangoutLink": (d.get("onlineMeeting") or {}).get("joinUrl"),
        "onlineMeeting": d.get("onlineMeeting") or {},
    }


async def find_free_slots(user_id: str, date_iso: str, duration_min: int = 60,
                          work_start: int = 9, work_end: int = 18) -> list[str]:
    """
    Free start times (HH:MM in the user's zone) on `date_iso` that fit `duration_min`
    without overlapping an existing event. 30-minute increments within working
    hours. Empty list means no gaps found.

    Working hours are anchored in the user's local day and converted per instant, so
    the window stays 09:00–18:00 local across a DST boundary instead of drifting by
    an hour the way a fixed offset would.
    """
    if not MS_CONFIGURED:
        return []
    tz = user_tz.tz()
    try:
        day = datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        return []
    start_local = day.replace(hour=work_start, minute=0, tzinfo=tz)
    end_local = day.replace(hour=work_end, minute=0, tzinfo=tz)

    try:
        events = await _calendar_view(user_id, start_local.astimezone(timezone.utc),
                                      end_local.astimezone(timezone.utc),
                                      "start,end,isCancelled", top=50, tz_name=str(tz))
    except Exception:  # noqa: BLE001
        return []

    busy = []
    for e in events:
        s, en = _slot_dt(e.get("start"), tz), _slot_dt(e.get("end"), tz)
        if s and en:
            busy.append((s, en))

    slots: list[str] = []
    cursor = start_local
    while cursor + timedelta(minutes=duration_min) <= end_local:
        slot_end = cursor + timedelta(minutes=duration_min)
        if not any(s < slot_end and en > cursor for s, en in busy):
            slots.append(cursor.strftime("%H:%M"))
        cursor += timedelta(minutes=30)
    return slots


# ── Text builders (Telegram briefs / LLM prompt context) ──────────────────────

async def _fetch_todays_agenda(user_id: str) -> str:
    """Today's events as a Telegram-ready block. Only called on cache miss.

    The window is the user's local day. Bracketing the UTC day instead cut it at
    04:00 Dubai, so a 01:00 event showed up under the wrong date and a 23:00 one was
    missing altogether."""
    tz = user_tz.tz()
    day_start, day_end = user_tz.day_bounds(tz)
    events = await _calendar_view(
        user_id, day_start, day_end,
        "subject,start,end,location,attendees,onlineMeeting,isAllDay,isCancelled",
        tz_name=str(tz))

    if not events:
        return "No events scheduled for today."

    lines = ["📅 Today's Calendar:\n"]
    for e in events:
        time_str = "All day" if e.get("isAllDay") else _fmt_time(e.get("start"), tz)
        subject = e.get("subject", "(no subject)")
        location = (e.get("location") or {}).get("displayName", "")
        loc_str = f" | 📍 {location}" if location else ""
        om = e.get("onlineMeeting") or {}
        meet_url = f" | 🔗 {om['joinUrl']}" if om.get("joinUrl") else ""
        attendees = e.get("attendees", []) or []
        att_str = f" | 👥 {len(attendees)}" if len(attendees) > 1 else ""
        lines.append(f"• {time_str} — {subject}{loc_str}{att_str}{meet_url}")
    return "\n".join(lines)


async def get_todays_agenda(user_id: str) -> str:
    """Today's agenda as text. Cached 5 minutes per user."""
    if not MS_CONFIGURED:
        return ""
    now = time.time()
    cached = _agenda_cache.get(user_id)
    if cached and now - cached[0] < AGENDA_CACHE_TTL:
        return cached[1]
    result = await _fetch_todays_agenda(user_id)
    _agenda_cache[user_id] = (now, result)
    return result


def invalidate_agenda_cache(user_id: str | None = None) -> None:
    """Clear the agenda cache — one user, or all when user_id is None."""
    if user_id:
        _agenda_cache.pop(user_id, None)
    else:
        _agenda_cache.clear()


async def get_upcoming_events(user_id: str, window_minutes: int = 60) -> list[dict]:
    """
    Events starting within the next `window_minutes`, in the shared structured
    shape (same as gcalendar) so the meeting-prep job is provider-agnostic.
    Cached 2 minutes per user.
    """
    if not MS_CONFIGURED:
        return []
    now = time.time()
    cached = _events_cache.get(user_id)
    if cached and now - cached[0] < EVENTS_CACHE_TTL:
        return cached[1]
    tz = user_tz.tz()
    start = datetime.now(timezone.utc)
    raw = await _calendar_view(
        user_id, start, start + timedelta(minutes=window_minutes),
        "id,subject,start,end,attendees,onlineMeeting,isOnlineMeeting,organizer,"
        "location,bodyPreview,isCancelled",
        top=10, tz_name=str(tz))
    result = [_parse_event(e, tz) for e in raw]
    _events_cache[user_id] = (now, result)
    return result
