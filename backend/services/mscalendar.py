"""
Outlook Calendar over Microsoft Graph (per-user OAuth tokens).

Async twin of `backend/services/gcalendar.py`. Structured reads return the same
event shape the Google service returns (id/title/start/end/location/attendees/
meet_link/…) so the dashboard and tool layer never branch on provider. The
Telegram-facing text builders (today's agenda, meeting brief) are kept here
because they are Graph-flavoured and were previously in integrations/.

Per-user TTL caches preserved from the retired module: agenda 5 min, upcoming
events 2 min.

Time zones: Graph returns calendarView times in UTC without an offset suffix, so
`_fmt_time` yields UTC HH:MM and the meeting-prep window math treats event start
times as UTC — consistent end to end.
"""
from __future__ import annotations

import time
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from backend.services.provider_tokens import get_ms_headers, MS_CONFIGURED
from backend.services.http_client import api_request

log = logging.getLogger("aria.mscal")

GRAPH = "https://graph.microsoft.com/v1.0"
DEFAULT_TZ = "Asia/Dubai"
TZ_OFFSET_HOURS = 4          # Asia/Dubai, no DST

_agenda_cache: dict[str, tuple[float, str]] = {}
AGENDA_CACHE_TTL = 300

_events_cache: dict[str, tuple[float, list]] = {}
EVENTS_CACHE_TTL = 120


def _fmt_time(iso: str) -> str:
    """Extract HH:MM from a Graph ISO datetime string."""
    try:
        return iso[11:16]
    except Exception:
        return iso


def _graph_dt(value: str) -> datetime:
    """Parse a Graph dateTime (naive = UTC) into an aware datetime."""
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_event(ev: dict) -> dict:
    """Graph event → the shared event shape (same keys gcalendar emits)."""
    attendees = [{"name": (a.get("emailAddress") or {}).get("name", "")
                          or (a.get("emailAddress") or {}).get("address", ""),
                  "email": (a.get("emailAddress") or {}).get("address", "")}
                 for a in ev.get("attendees", []) or []]
    om = ev.get("onlineMeeting") or {}
    meet_link = om.get("joinUrl") or ev.get("onlineMeetingUrl")
    return {
        "id": ev.get("id"),
        "title": ev.get("subject") or "(no title)",
        "start": (ev.get("start") or {}).get("dateTime", ""),
        "end": (ev.get("end") or {}).get("dateTime", ""),
        "location": (ev.get("location") or {}).get("displayName") or None,
        "attendees": attendees,
        "is_online": bool(meet_link or ev.get("isOnlineMeeting")),
        "meet_link": meet_link,
        "organizer": ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address", ""),
        "description": ev.get("bodyPreview") or None,
        "calendar": "primary",
    }


async def _calendar_view(user_id: str, start: datetime, end: datetime,
                         select: str, top: int = 25) -> list[dict]:
    """Raw calendarView query, cancelled events filtered out."""
    headers = await get_ms_headers(user_id)
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
    now = datetime.now(timezone.utc)
    raw = await _calendar_view(
        user_id, now, now + timedelta(days=days_ahead),
        "id,subject,start,end,location,attendees,onlineMeeting,isOnlineMeeting,"
        "organizer,bodyPreview,isAllDay,isCancelled", top=20)
    return [_parse_event(e) for e in raw]


async def get_next_event(user_id: str) -> dict | None:
    """The single next upcoming event from now, or None."""
    events = await get_agenda(user_id, days_ahead=14)
    return events[0] if events else None


async def create_event(user_id: str, title: str, start: str, end: str,
                       description: str | None = None,
                       attendees: list[str] | None = None,
                       location: str | None = None,
                       timezone_str: str = DEFAULT_TZ) -> dict:
    """
    Create an event on the user's default calendar. `start`/`end` are naive local
    ISO strings — Graph takes the zone separately. isOnlineMeeting=True makes
    Microsoft mint the Teams link.
    """
    if not MS_CONFIGURED:
        raise HTTPException(status_code=503, detail={
            "error": "microsoft_not_configured",
            "message": "Microsoft OAuth is not configured on the server.",
        })
    headers = await get_ms_headers(user_id)
    payload: dict = {
        "subject": title,
        "body": {"contentType": "HTML", "content": description or ""},
        "start": {"dateTime": start, "timeZone": timezone_str},
        "end": {"dateTime": end, "timeZone": timezone_str},
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
    Free start times (HH:MM, Asia/Dubai local) on `date_iso` that fit `duration_min`
    without overlapping an existing event. 30-minute increments within working
    hours. Empty list means no gaps found.
    """
    if not MS_CONFIGURED:
        return []
    try:
        day = datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return []
    start_utc = day.replace(hour=work_start - TZ_OFFSET_HOURS, minute=0)
    end_utc = day.replace(hour=work_end - TZ_OFFSET_HOURS, minute=0)

    try:
        events = await _calendar_view(user_id, start_utc, end_utc,
                                      "start,end,isCancelled", top=50)
    except Exception:  # noqa: BLE001
        return []

    busy = []
    for e in events:
        try:
            busy.append((_graph_dt(e["start"]["dateTime"]), _graph_dt(e["end"]["dateTime"])))
        except (KeyError, ValueError):
            continue

    slots: list[str] = []
    cursor = start_utc
    while cursor + timedelta(minutes=duration_min) <= end_utc:
        slot_end = cursor + timedelta(minutes=duration_min)
        if not any(s < slot_end and en > cursor for s, en in busy):
            local_hour = (cursor.hour + TZ_OFFSET_HOURS) % 24
            slots.append(f"{local_hour:02d}:{cursor.minute:02d}")
        cursor += timedelta(minutes=30)
    return slots


# ── Text builders (Telegram briefs / LLM prompt context) ──────────────────────

async def _fetch_todays_agenda(user_id: str) -> str:
    """Today's events as a Telegram-ready block. Only called on cache miss."""
    now = datetime.now(timezone.utc)
    events = await _calendar_view(
        user_id,
        now.replace(hour=0, minute=0, second=0, microsecond=0),
        now.replace(hour=23, minute=59, second=59, microsecond=0),
        "subject,start,end,location,attendees,onlineMeeting,isAllDay,isCancelled")

    if not events:
        return "No events scheduled for today."

    lines = ["📅 Today's Calendar:\n"]
    for e in events:
        time_str = "All day" if e.get("isAllDay") else _fmt_time((e.get("start") or {}).get("dateTime", ""))
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
    start = datetime.now(timezone.utc)
    raw = await _calendar_view(
        user_id, start, start + timedelta(minutes=window_minutes),
        "id,subject,start,end,attendees,onlineMeeting,isOnlineMeeting,organizer,"
        "location,bodyPreview,isCancelled",
        top=10)
    result = [_parse_event(e) for e in raw]
    _events_cache[user_id] = (now, result)
    return result
