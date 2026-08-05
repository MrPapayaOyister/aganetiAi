"""The wall clock user-facing times are rendered in, and the conversions into it.

One source of truth for every human-facing timestamp the app produces. Providers
return instants in whatever zone they please — Microsoft Graph defaults to UTC,
Google answers in the calendar's own zone — and everything downstream (the LLM
prompt, Telegram, the PDF digest, the dashboard) wants one consistent local wall
clock. That conversion happens here, once, so no caller has to know what a
provider handed back.

The zone is deployment-wide: env APP_TIMEZONE, falling back to UTC if it is unset
or unreadable. This deployment is single-region, so a per-user timezone would be a
column that is NULL for everyone and a lookup that always returns the same answer.
If people ever span regions, this is the seam to widen — every caller already goes
through `tz()`.

Always IANA names ("Asia/Dubai"), never fixed offsets. A fixed offset is wrong
twice a year in any zone that observes DST; `zoneinfo` resolves the offset at the
instant in question, so 09:00 Europe/London is +01:00 in July and +00:00 in
January without any special-casing. Asia/Dubai has no DST, but the helpers must
stay correct for a deployment that moves.
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("aria.user_tz")

UTC = timezone.utc

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Dubai")


def _zone(name: str | None) -> ZoneInfo | None:
    """ZoneInfo for an IANA name, or None if it is unset/unknown."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r — ignoring", name)
        return None


def zone_or(name: str | None, fallback: ZoneInfo) -> ZoneInfo:
    """ZoneInfo for `name`, or `fallback` when it is unset or unknown.

    Lets a caller trust a zone the provider stated for a specific value — Graph
    labels every slot with the zone it rendered it in — while still having
    somewhere to land if that label is missing or junk."""
    return _zone(name) or fallback


def tz_name() -> str:
    """The app's IANA timezone name. Falls back to UTC if APP_TIMEZONE is bad."""
    return APP_TIMEZONE if _zone(APP_TIMEZONE) else "UTC"


def tz() -> ZoneInfo:
    """The app's zone. Never raises — worst case UTC."""
    return _zone(APP_TIMEZONE) or ZoneInfo("UTC")


def to_aware(value: str | datetime | None, assume: timezone | ZoneInfo = UTC) -> datetime | None:
    """Parse a provider timestamp into an aware datetime, or None if unusable.

    Handles what the two providers actually emit: Graph's naive
    "2026-08-06T05:30:00.0000000" (7 fractional digits, no offset — UTC by
    default) and Google's RFC3339 "2026-08-06T09:30:00+04:00". A value with no
    offset is stamped with `assume`, which is the only place a guess is made and
    why callers pass the zone the provider documented rather than letting it
    default silently."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=assume)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=assume)


def localize(value: str | datetime | None, zone: ZoneInfo,
             assume: timezone | ZoneInfo = UTC) -> datetime | None:
    """Parse `value` and convert it into `zone`. None when unparseable."""
    dt = to_aware(value, assume=assume)
    return dt.astimezone(zone) if dt else None


def localize_iso(value: str | datetime | None, zone: ZoneInfo,
                 assume: timezone | ZoneInfo = UTC) -> str:
    """`value` as an ISO string in `zone`, WITH its UTC offset.

    The offset is the point: a bare "2026-08-06T05:30:00" is read by
    `new Date()` in the browser as local time and by half our own helpers as UTC,
    which is exactly how a 09:30 meeting came out as 05:30. Carrying "+04:00"
    makes the instant unambiguous to every consumer. Unparseable input is returned
    unchanged rather than dropped, so a provider oddity degrades to the old
    behaviour instead of blanking the event."""
    dt = localize(value, zone, assume=assume)
    return dt.isoformat() if dt else (value if isinstance(value, str) else "")


def fmt_hhmm(value: str | datetime | None, zone: ZoneInfo,
             assume: timezone | ZoneInfo = UTC) -> str:
    """24-hour HH:MM in `zone` — the form the agenda/brief text builders render."""
    dt = localize(value, zone, assume=assume)
    return dt.strftime("%H:%M") if dt else ""


def day_bounds(zone: ZoneInfo, when: datetime | None = None) -> tuple[datetime, datetime]:
    """UTC instants bracketing the LOCAL calendar day containing `when`.

    "Today" has to mean the local today. Bracketing the UTC day instead puts the
    boundary at 04:00 Dubai, so early meetings land on the wrong day and late ones
    vanish from the agenda entirely."""
    now_local = (when or datetime.now(UTC)).astimezone(zone)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)
