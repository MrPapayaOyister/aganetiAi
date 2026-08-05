"""
Natural-language meeting-time parsing — provider-agnostic, no I/O.

Lifted out of the retired integrations/m365_calendar.py so both the Graph and
Google calendar paths share one parser. Times are interpreted in Asia/Dubai
(UTC+4, no DST) and returned as NAIVE ISO strings, because both providers take
the zone as a separate field.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

DEFAULT_TZ_OFFSET_HOURS = 4       # Asia/Dubai

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}

_ISO_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _extract_hm(s: str) -> tuple[int, int] | None:
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


def parse_meeting_time(time_str: str,
                       tz_offset_hours: int = DEFAULT_TZ_OFFSET_HOURS) -> tuple[str, str]:
    """
    Convert natural language or ISO time → (start_iso, end_iso), naive, 1-hour default
    duration. Understands ISO input, "in 30 minutes", "tomorrow at 3pm",
    "next tuesday 10am", and bare clock times (pushed to tomorrow if already past).
    """
    local_now = datetime.now(timezone.utc) + timedelta(hours=tz_offset_hours)
    ts = time_str.strip()

    def _out(start: datetime) -> tuple[str, str]:
        end = start + timedelta(hours=1)
        return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")

    # Already ISO: "2026-06-24T10:00:00" or "2026-06-24 10:00"
    for fmt in _ISO_FORMATS:
        try:
            return _out(datetime.strptime(ts, fmt))
        except ValueError:
            pass

    ts_lower = ts.lower()

    # "in X minutes/hours"
    m = re.search(r"in\s+(\d+)\s+(minute|hour|min|hr)s?", ts_lower)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(hours=n) if ("hour" in unit or unit == "hr") else timedelta(minutes=n)
        return _out(local_now + delta)

    # Day-offset words
    day_delta = 0
    if "day after tomorrow" in ts_lower:
        day_delta = 2
    elif "tomorrow" in ts_lower:
        day_delta = 1
    elif re.search(r"next\s+(" + "|".join(_WEEKDAYS) + r")", ts_lower):
        m2 = re.search(r"(" + "|".join(_WEEKDAYS) + r")", ts_lower)
        day_delta = (_WEEKDAYS[m2.group(1)] - local_now.weekday()) % 7 or 7

    base_date = local_now + timedelta(days=day_delta)

    hm = _extract_hm(ts)
    if hm:
        start = base_date.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        # No explicit day and the time already passed → assume they mean tomorrow.
        if day_delta == 0 and start <= local_now:
            start += timedelta(days=1)
        return _out(start)

    # Fallback: an hour from now
    return _out(local_now + timedelta(hours=1))
