"""
Microsoft 365 Calendar Operations (Microsoft Graph)

Drop-in replacement for the retired Google Calendar integration. All Graph calendar
operations with the same per-user TTL caches preserved (agenda 5 min, upcoming events 2 min).

Used by: backend/main.py (agenda in /chat, meeting-prep job, /calendar endpoints),
reports/pdf_generator.py (format_agenda_for_prompt). Tokens/scopes set up in M365-A.

Note on time zones: Graph returns calendarView times in UTC (no offset suffix), so the
HH:MM extracted by _fmt_time is UTC, and the meeting-prep window math in backend/main.py
treats event start times as UTC — kept consistent end to end.
"""

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import time
from datetime import datetime, timezone, timedelta
from integrations.m365_auth import get_access_token

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT    = 30

# Agenda cache — 5 minute TTL (per Task 14 spec)
_agenda_cache: dict[str, tuple[float, str]] = {}
AGENDA_CACHE_TTL = 300

# Upcoming events cache — 2 minute TTL (meeting prep needs fresher data)
_events_cache: dict[str, tuple[float, list]] = {}
EVENTS_CACHE_TTL = 120


def _headers(user_id: str) -> dict:
    return {
        "Authorization": f"Bearer {get_access_token(user_id)}",
        "Content-Type":  "application/json"
    }


def _fmt_time(iso: str) -> str:
    """Extract HH:MM from Graph API ISO datetime string."""
    try:
        return iso[11:16]
    except Exception:
        return iso


def _fetch_todays_agenda(user_id: str) -> str:
    """
    Fetches today's calendar events from Microsoft Graph.
    Called only on cache miss — never call directly from other modules.
    """
    now   = datetime.now(timezone.utc)
    start = now.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
    end   = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    url    = f"{GRAPH_BASE}/me/calendarView"
    params = {
        "startDateTime": start,
        "endDateTime":   end,
        "$select":       "subject,start,end,location,attendees,onlineMeeting,isAllDay,isCancelled",
        "$orderby":      "start/dateTime",
        "$top":          25
    }

    resp = httpx.get(url, headers=_headers(user_id), params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    events = resp.json().get("value", [])

    # Filter cancelled events
    events = [e for e in events if not e.get("isCancelled", False)]

    if not events:
        return "No events scheduled for today."

    lines = ["📅 Today's Calendar:\n"]
    for e in events:
        if e.get("isAllDay"):
            time_str = "All day"
        else:
            time_str = _fmt_time(e.get("start", {}).get("dateTime", ""))

        subject  = e.get("subject", "(no subject)")
        location = e.get("location", {}).get("displayName", "")
        loc_str  = f" | 📍 {location}" if location else ""

        # Teams/online meeting link
        meet_url = ""
        om = e.get("onlineMeeting")
        if om and om.get("joinUrl"):
            meet_url = f" | 🔗 {om['joinUrl']}"

        # Attendee count
        attendees = e.get("attendees", [])
        att_str   = f" | 👥 {len(attendees)}" if len(attendees) > 1 else ""

        lines.append(f"• {time_str} — {subject}{loc_str}{att_str}{meet_url}")

    return "\n".join(lines)


def get_todays_agenda(user_id: str) -> str:
    """
    Returns today's calendar agenda. Cached for 5 minutes per user.
    Drop-in replacement for the retired Google Calendar get_todays_agenda().
    """
    now = time.time()
    if user_id in _agenda_cache:
        cached_at, result = _agenda_cache[user_id]
        if now - cached_at < AGENDA_CACHE_TTL:
            return result

    result = _fetch_todays_agenda(user_id)
    _agenda_cache[user_id] = (now, result)
    return result


def invalidate_agenda_cache(user_id: str = None) -> None:
    """
    Clear agenda cache.
    user_id=None clears all users. Specific user_id clears only that user.
    """
    if user_id:
        _agenda_cache.pop(user_id, None)
    else:
        _agenda_cache.clear()


def format_agenda_for_prompt(user_id: str = "user_1") -> str:
    """
    Formats today's agenda for inclusion in an LLM prompt / PDF report.
    Drop-in replacement for the retired Google Calendar format_agenda_for_prompt() —
    delegates to the cached get_todays_agenda() so callers (backend/main.py,
    reports/pdf_generator.py) are unchanged in behaviour.
    """
    return get_todays_agenda(user_id)


def _fetch_upcoming_events(user_id: str, window_minutes: int = 60) -> list:
    """
    Fetches calendar events starting within the next window_minutes.
    Called only on cache miss.
    """
    now   = datetime.now(timezone.utc)
    end   = now + timedelta(minutes=window_minutes)

    url    = f"{GRAPH_BASE}/me/calendarView"
    params = {
        "startDateTime": now.isoformat(),
        "endDateTime":   end.isoformat(),
        "$select":       "id,subject,start,end,attendees,onlineMeeting,location,bodyPreview,isCancelled",
        "$orderby":      "start/dateTime",
        "$top":          10
    }

    resp = httpx.get(url, headers=_headers(user_id), params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    events = resp.json().get("value", [])

    # Filter cancelled
    return [e for e in events if not e.get("isCancelled", False)]


def get_upcoming_events(user_id: str, window_minutes: int = 60) -> list:
    """
    Returns upcoming events within window_minutes. Cached 2 minutes per user.
    Drop-in replacement for the retired Google Calendar get_upcoming_events().
    NOTE: returns RAW Graph event dicts (start/end as {dateTime,timeZone}, attendees as
    Graph objects), unlike the old module's flattened shape — callers updated accordingly.
    """
    now = time.time()
    if user_id in _events_cache:
        cached_at, result = _events_cache[user_id]
        if now - cached_at < EVENTS_CACHE_TTL:
            return result

    result = _fetch_upcoming_events(user_id, window_minutes)
    _events_cache[user_id] = (now, result)
    return result


def format_meeting_brief(event: dict, user_id: str) -> str:
    """
    Formats a single Graph calendar event into a Telegram-ready meeting brief.
    Called by the meeting prep APScheduler job.
    """
    subject  = event.get("subject", "(no subject)")
    start    = _fmt_time(event.get("start", {}).get("dateTime", ""))
    location = event.get("location", {}).get("displayName", "")
    preview  = event.get("bodyPreview", "")[:200]

    attendees = event.get("attendees", [])
    att_lines = []
    for a in attendees[:5]:  # cap at 5 for Telegram readability
        addr = a.get("emailAddress", {})
        att_lines.append(f"  • {addr.get('name', addr.get('address', ''))}")

    meet_url = ""
    om = event.get("onlineMeeting")
    if om and om.get("joinUrl"):
        meet_url = f"\n🔗 Join: {om['joinUrl']}"

    loc_str = f"\n📍 {location}" if location else ""
    att_str = ("\n👥 Attendees:\n" + "\n".join(att_lines)) if att_lines else ""
    pre_str = f"\n📝 {preview}" if preview else ""

    return (
        f"⏰ Meeting in ~30 min:\n"
        f"*{subject}*\n"
        f"🕐 {start}"
        f"{loc_str}"
        f"{meet_url}"
        f"{att_str}"
        f"{pre_str}"
    )


def create_calendar_event(
    user_id:         str,
    subject:         str,
    start_iso:       str,   # e.g. "2026-06-22T14:00:00"
    end_iso:         str,   # e.g. "2026-06-22T15:00:00"
    attendee_emails: list[str] = None,
    body:            str = "",
    timezone_str:    str = "Asia/Dubai"
) -> dict:
    """
    Creates a calendar event. Returns the created event dict from Graph.
    isOnlineMeeting=True → Teams link auto-generated by Microsoft.
    """
    url     = f"{GRAPH_BASE}/me/events"
    payload = {
        "subject": subject,
        "body":    {"contentType": "HTML", "content": body or ""},
        "start":   {"dateTime": start_iso, "timeZone": timezone_str},
        "end":     {"dateTime": end_iso,   "timeZone": timezone_str},
        "isOnlineMeeting":      True,
        "onlineMeetingProvider": "teamsForBusiness",
    }
    if attendee_emails:
        payload["attendees"] = [
            {"emailAddress": {"address": email}, "type": "required"}
            for email in attendee_emails
        ]

    resp = httpx.post(url, headers=_headers(user_id), json=payload, timeout=TIMEOUT)
    resp.raise_for_status()

    # Invalidate agenda cache — new event just added
    invalidate_agenda_cache(user_id)
    return resp.json()


def parse_meeting_time(time_str: str, tz_offset_hours: int = 4) -> tuple[str, str]:
    """
    Convert natural language or ISO time → (start_iso, end_iso) suitable for Graph API.
    Times are expressed in Asia/Dubai (UTC+4) local time. Returns naive ISO strings
    (no offset) because Graph API takes timeZone separately.
    Default duration: 1 hour.
    """
    import re
    from datetime import datetime, timezone, timedelta

    local_now = datetime.now(timezone.utc) + timedelta(hours=tz_offset_hours)

    def _apply_time(base: datetime, h: int, m: int) -> datetime:
        return base.replace(hour=h, minute=m, second=0, microsecond=0)

    def _extract_hm(s: str):
        match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s.lower())
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        period = match.group(3)
        if period == "pm" and hour != 12:
            hour += 12
        if period == "am" and hour == 12:
            hour = 0
        return hour, minute

    ts = time_str.strip()

    # Already ISO: "2026-06-24T10:00:00" or "2026-06-24 10:00"
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            start = datetime.strptime(ts, fmt)
            end = start + timedelta(hours=1)
            return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass

    ts_lower = ts.lower()

    # "in X minutes/hours"
    m = re.search(r"in\s+(\d+)\s+(minute|hour|min|hr)s?", ts_lower)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(hours=n) if "hour" in unit or unit == "hr" else timedelta(minutes=n)
        start = local_now + delta
        end = start + timedelta(hours=1)
        return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")

    # Day offset words
    day_delta = 0
    if "tomorrow" in ts_lower:
        day_delta = 1
    elif "day after tomorrow" in ts_lower:
        day_delta = 2
    elif re.search(r"next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", ts_lower):
        day_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                   "friday": 4, "saturday": 5, "sunday": 6}
        m2 = re.search(r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", ts_lower)
        target_wd = day_map[m2.group(1)]
        current_wd = local_now.weekday()
        day_delta = (target_wd - current_wd) % 7 or 7

    base_date = local_now + timedelta(days=day_delta)

    hm = _extract_hm(ts)
    if hm:
        start = _apply_time(base_date, hm[0], hm[1])
        # If no day offset and time is in the past, push to tomorrow
        if day_delta == 0 and start <= local_now:
            start += timedelta(days=1)
        end = start + timedelta(hours=1)
        return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")

    # Fallback: 1 hour from now
    start = local_now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")


def find_free_slots(user_id: str, date_iso: str, duration_min: int = 60,
                    work_start: int = 9, work_end: int = 18) -> list[str]:
    """
    Return list of free start times (HH:MM, Asia/Dubai local) on date_iso that fit
    duration_min without overlapping existing events. Slots are in 30-min increments
    within working hours. Empty list means no gaps found.
    """
    from datetime import date, datetime, timezone, timedelta
    TZ_OFFSET = 4

    day = datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_utc = day.replace(hour=work_start - TZ_OFFSET, minute=0)
    end_utc   = day.replace(hour=work_end   - TZ_OFFSET, minute=0)

    url    = f"{GRAPH_BASE}/me/calendarView"
    params = {
        "startDateTime": start_utc.isoformat().replace("+00:00", ""),
        "endDateTime":   end_utc.isoformat().replace("+00:00", ""),
        "$select":       "start,end,isCancelled",
        "$top":          50,
    }
    try:
        resp = httpx.get(url, headers=_headers(user_id), params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        events = [e for e in resp.json().get("value", []) if not e.get("isCancelled")]
    except Exception:
        return []

    busy = []
    for e in events:
        s = datetime.fromisoformat(e["start"]["dateTime"].replace("Z", "+00:00") if "Z" in e["start"]["dateTime"] else e["start"]["dateTime"] + "+00:00")
        en = datetime.fromisoformat(e["end"]["dateTime"].replace("Z", "+00:00") if "Z" in e["end"]["dateTime"] else e["end"]["dateTime"] + "+00:00")
        busy.append((s, en))

    slots = []
    cursor = start_utc
    while cursor + timedelta(minutes=duration_min) <= end_utc:
        slot_end = cursor + timedelta(minutes=duration_min)
        conflict = any(s < slot_end and en > cursor for s, en in busy)
        if not conflict:
            local_hour = (cursor.hour + TZ_OFFSET) % 24
            slots.append(f"{local_hour:02d}:{cursor.minute:02d}")
        cursor += timedelta(minutes=30)
    return slots
