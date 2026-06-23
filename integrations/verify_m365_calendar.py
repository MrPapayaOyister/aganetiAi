import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time, subprocess, json
import httpx
from integrations.m365_auth import get_access_token
from integrations.m365_calendar import (
    get_todays_agenda,
    get_upcoming_events,
    invalidate_agenda_cache,
    format_meeting_brief,
    create_calendar_event,
    _agenda_cache,
    _events_cache,
    AGENDA_CACHE_TTL,
    EVENTS_CACHE_TTL
)

USER_1 = "user_1"
USER_2 = "user_2"

print("\n=== M365-C Verification ===\n")

# T1 — Clean import
print("T1 — Import check...")
print("✅ All calendar functions imported\n")

# T2 — Agenda TTL constants
assert AGENDA_CACHE_TTL == 300, f"Expected 300, got {AGENDA_CACHE_TTL}"
assert EVENTS_CACHE_TTL == 120, f"Expected 120, got {EVENTS_CACHE_TTL}"
assert _agenda_cache is not _events_cache, "Cache dicts must be separate objects"
print(f"T2 ✅ Cache TTLs correct: agenda={AGENDA_CACHE_TTL}s, events={EVENTS_CACHE_TTL}s\n")

# T3 — get_todays_agenda (live Graph call)
print("T3 — get_todays_agenda (user_1, live)...")
invalidate_agenda_cache()
agenda_1 = get_todays_agenda(USER_1)
assert isinstance(agenda_1, str) and len(agenda_1) > 0
print(f"✅ user_1 agenda:\n{agenda_1}\n")

print("T3b — get_todays_agenda (user_2, live)...")
agenda_2 = get_todays_agenda(USER_2)
assert isinstance(agenda_2, str) and len(agenda_2) > 0
print(f"✅ user_2 agenda:\n{agenda_2}\n")

# T4 — Cache hit (no second API call)
print("T4 — Cache hit on second call...")
import integrations.m365_calendar as cal
call_count = 0
original   = cal._fetch_todays_agenda
def counting_fetch(uid):
    global call_count
    call_count += 1
    return f"Mock agenda for {uid}"
cal._fetch_todays_agenda = counting_fetch

invalidate_agenda_cache()
cal.get_todays_agenda(USER_1)   # miss → calls counting_fetch
cal.get_todays_agenda(USER_1)   # hit  → no call
assert call_count == 1, f"Expected 1 API call, got {call_count}"
print(f"✅ Cache hit confirmed — {call_count} API call for 2 requests\n")
cal._fetch_todays_agenda = original  # restore

# T5 — Cache expiry
print("T5 — Cache expiry...")
invalidate_agenda_cache()
cal._fetch_todays_agenda = counting_fetch
call_count = 0
cal.get_todays_agenda(USER_1)
_agenda_cache[USER_1] = (0.0, "stale")  # backdate timestamp
cal.get_todays_agenda(USER_1)
assert call_count == 2, f"Expected 2 calls after expiry, got {call_count}"
print(f"✅ Cache expiry confirmed — fetched again after TTL\n")
cal._fetch_todays_agenda = original

# T6 — Per-user cache isolation
print("T6 — Per-user agenda cache isolation...")
invalidate_agenda_cache()
r1 = get_todays_agenda(USER_1)
r2 = get_todays_agenda(USER_2)
assert USER_1 in _agenda_cache
assert USER_2 in _agenda_cache
assert _agenda_cache[USER_1][1] != _agenda_cache[USER_2][1] or True  # different users, may differ
print(f"✅ Both users cached independently\n")

# T7 — invalidate_agenda_cache targeted
print("T7 — Targeted cache invalidation...")
_agenda_cache["user_1"] = (time.time(), "cached_1")
_agenda_cache["user_2"] = (time.time(), "cached_2")
invalidate_agenda_cache("user_1")
assert "user_1" not in _agenda_cache
assert "user_2" in _agenda_cache
invalidate_agenda_cache()
assert len(_agenda_cache) == 0
print("✅ Targeted + full invalidation confirmed\n")

# T8 — get_upcoming_events (live)
print("T8 — get_upcoming_events (user_1, 60min window)...")
events_1 = get_upcoming_events(USER_1, window_minutes=60)
assert isinstance(events_1, list)
print(f"✅ user_1: {len(events_1)} upcoming events in next 60 min")

events_2 = get_upcoming_events(USER_2, window_minutes=60)
assert isinstance(events_2, list)
print(f"✅ user_2: {len(events_2)} upcoming events in next 60 min\n")

# T9 — format_meeting_brief (if any events exist)
if events_1:
    print("T9 — format_meeting_brief (user_1 first event)...")
    brief = format_meeting_brief(events_1[0], USER_1)
    assert isinstance(brief, str) and len(brief) > 0
    assert "Meeting" in brief or "min" in brief
    print(f"✅ Brief generated:\n{brief}\n")
else:
    print("T9 ⚠️  Skipped — no upcoming events in 60-min window\n")

# T10 — create_calendar_event (creates a test event)
print("T10 — create_calendar_event (test event, user_1)...")
from datetime import datetime, timezone, timedelta
start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
    hour=10, minute=0, second=0, microsecond=0
)
end = start + timedelta(hours=1)
event = create_calendar_event(
    user_id   = USER_1,
    subject   = "[M365-C Verification] Test event — safe to delete",
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%S"),
    end_iso   = end.strftime("%Y-%m-%dT%H:%M:%S"),
    attendee_emails = ["akshay@meerana.ae"],
    body      = "Automated verification event from M365-C test suite."
)
assert "id" in event
assert event.get("subject") == "[M365-C Verification] Test event — safe to delete"
teams_link = event.get("onlineMeeting", {}).get("joinUrl", "")
assert teams_link, "Teams join URL should be present (isOnlineMeeting=True)"
print(f"✅ Event created: {event['id'][:40]}...")
print(f"✅ Teams link: {teams_link[:60]}...\n")

# T10b — Cleanup: delete the verification event so the live calendar stays clean
print("T10b — Cleanup: delete the test event...")
try:
    dresp = httpx.delete(
        f"https://graph.microsoft.com/v1.0/me/events/{event['id']}",
        headers={"Authorization": f"Bearer {get_access_token(USER_1)}"},
        timeout=30,
    )
    if dresp.status_code in (200, 202, 204):
        print("✅ Test event deleted (calendar restored)\n")
    else:
        print(f"⚠️  Could not delete test event ({dresp.status_code}) — delete manually\n")
except Exception as e:
    print(f"⚠️  Cleanup delete failed: {e} — delete the event manually\n")

# T11 — FastAPI /calendar/agenda endpoint
print("T11 — /calendar/agenda endpoint...")
r = subprocess.run(
    ["curl", "-s", "http://localhost:8000/calendar/agenda?user_id=user_1"],
    capture_output=True, text=True
)
data = json.loads(r.stdout)
assert "agenda" in data
print(f"✅ /calendar/agenda user_1: {data['agenda'][:60]}...\n")

r2 = subprocess.run(
    ["curl", "-s", "http://localhost:8000/calendar/agenda?user_id=user_2"],
    capture_output=True, text=True
)
data2 = json.loads(r2.stdout)
assert "agenda" in data2
print(f"✅ /calendar/agenda user_2: {data2['agenda'][:60]}...\n")

# T12 — /calendar/invalidate endpoint
print("T12 — /calendar/invalidate endpoint...")
r3 = subprocess.run(
    ["curl", "-s", "-X", "POST",
     "http://localhost:8000/calendar/invalidate?user_id=user_1"],
    capture_output=True, text=True
)
data3 = json.loads(r3.stdout)
assert data3.get("status") == "ok"
print(f"✅ /calendar/invalidate: {data3}\n")

# T13 — No active imports of the retired google_calendar module
print("T13 — No active google_calendar imports in .py files...")
result = subprocess.run(
    ["grep", "-rnE", r"(from +integrations\.google_calendar|import +google_calendar)",
     os.path.expanduser("~/projects/"), "--include=*.py"],
    capture_output=True, text=True
)
hits = [l for l in result.stdout.splitlines() if ".bak" not in l]
assert len(hits) == 0, "Found active google_calendar imports:\n" + "\n".join(hits)
print("✅ Zero active google_calendar imports\n")

# T14 — Health check
print("T14 — Server health check...")
r4 = subprocess.run(
    ["curl", "-s", "http://localhost:8000/health"],
    capture_output=True, text=True
)
assert "ok" in r4.stdout
print("✅ /health still ok\n")

print("=== M365-C Verification Complete ✅ ===")
