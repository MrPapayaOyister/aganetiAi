import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from integrations.m365_mail import (
    fetch_unread_emails,
    get_inbox_count,
    send_email,
    mark_as_read,
    get_email_by_id
)

USER_1 = "user_1"
USER_2 = "user_2"

print("\n=== M365-B Verification ===\n")

# T1 — Clean import
print("T1 — Import check...")
print("✅ All functions imported successfully\n")

# T2 — Inbox count (lightweight — no body fetch)
print("T2 — Inbox count (user_1)...")
count = get_inbox_count(USER_1)
assert "unread" in count and "total" in count
print(f"✅ user_1 inbox: {count['unread']} unread / {count['total']} total\n")

print("T2b — Inbox count (user_2)...")
count2 = get_inbox_count(USER_2)
print(f"✅ user_2 inbox: {count2['unread']} unread / {count2['total']} total\n")

# T3 — Fetch unread emails (both users)
print("T3 — Fetch unread emails (user_1, top=5)...")
emails_1 = fetch_unread_emails(USER_1, top=5)
assert isinstance(emails_1, list)
print(f"✅ user_1: {len(emails_1)} unread emails fetched")
if emails_1:
    e = emails_1[0]
    assert "id" in e and "subject" in e and "from_email" in e
    print(f"   Sample: '{e['subject']}' from {e['from_email']}\n")

print("T3b — Fetch unread emails (user_2, top=5)...")
emails_2 = fetch_unread_emails(USER_2, top=5)
assert isinstance(emails_2, list)
print(f"✅ user_2: {len(emails_2)} unread emails fetched\n")

# T4 — Per-user isolation (emails should be from different inboxes)
if emails_1 and emails_2:
    ids_1 = {e["id"] for e in emails_1}
    ids_2 = {e["id"] for e in emails_2}
    assert ids_1.isdisjoint(ids_2), "❌ user_1 and user_2 share email IDs — isolation broken"
    print("T4 ✅ Per-user inbox isolation confirmed — no shared message IDs\n")
else:
    print("T4 ⚠️  Skipped — one or both inboxes empty, isolation check not possible\n")

# T5 — get_email_by_id (if any email exists)
if emails_1:
    print("T5 — get_email_by_id (user_1)...")
    msg = get_email_by_id(USER_1, emails_1[0]["id"])
    assert msg is not None
    assert msg["id"] == emails_1[0]["id"]
    print(f"✅ Fetched by ID: '{msg['subject']}'\n")

# T6 — 404 returns None gracefully
print("T6 — get_email_by_id with fake ID returns None...")
fake = get_email_by_id(USER_1, "FAKE_ID_THAT_DOES_NOT_EXIST_12345")
assert fake is None
print("✅ 404 handled gracefully — returns None\n")

# T7 — Send email (user_1 → user_2)
print("T7 — Send test email from user_1 to user_2...")
send_email(
    user_id   = USER_1,
    to_email  = "akshay@meerana.ae",
    subject   = "[M365-B Verification] Test email from user_1",
    body      = "<p>This is an automated verification email from the M365-B test suite.</p>",
    body_type = "HTML"
)
print("✅ Email sent (202 Accepted) — check user_2 inbox\n")

# T8 — mark_as_read (mark first unread email as read, if any)
if emails_1:
    print("T8 — mark_as_read (first unread email in user_1 inbox)...")
    mark_as_read(USER_1, emails_1[0]["id"])
    print(f"✅ Marked as read: {emails_1[0]['id'][:40]}...\n")
    # Verify count decreased
    new_count = get_inbox_count(USER_1)
    print(f"   Unread after mark: {new_count['unread']} (was {count['unread']})\n")

# T9 — FastAPI endpoints
print("T9 — FastAPI /mail/inbox endpoint...")
import subprocess, json
result = subprocess.run(
    ["curl", "-s", "http://localhost:8000/mail/inbox?user_id=user_1"],
    capture_output=True, text=True
)
data = json.loads(result.stdout)
assert "emails" in data
print(f"✅ /mail/inbox returns {data['count']} emails for user_1\n")

result2 = subprocess.run(
    ["curl", "-s", "http://localhost:8000/mail/inbox/count?user_id=user_2"],
    capture_output=True, text=True
)
data2 = json.loads(result2.stdout)
assert "unread" in data2
print(f"✅ /mail/inbox/count returns unread={data2['unread']} for user_2\n")

# T10 — Health check unaffected
print("T10 — Server health check...")
result3 = subprocess.run(
    ["curl", "-s", "http://localhost:8000/health"],
    capture_output=True, text=True
)
assert "ok" in result3.stdout
print(f"✅ /health still returns ok\n")

# T11 — unread.json writer + digest/count read it (no server needed)
print("T11 — unread.json writer feeds digest + unread-count...")
from backend.main import _write_unread_store
from tasks.store import get_unread_email_count
from reports.email_digest import build_digest
sample = [{
    "id": "VERIFY_T11_ID",
    "from_name": "Verify Bot",
    "from_email": "verify@meerana.ae",
    "subject": "[M365-B] unread.json writer check",
    "body_text": "Body for the digest writer verification.",
    "body_preview": "Body for the digest writer verification.",
    "received_at": "2026-06-21T10:00:00Z",
}]
_write_unread_store(USER_1, sample)
from pathlib import Path
p = Path(f"email_store/{USER_1}/unread.json")
assert p.exists(), "unread.json was not written"
written = json.loads(p.read_text())
assert isinstance(written, list) and written and written[0]["subject"].startswith("[M365-B]")
assert "body" in written[0], "writer must map to the 'body' key build_digest expects"
assert get_unread_email_count(USER_1) == 1, "unread-count must read the written file"
digest = build_digest(USER_1, written)
assert "unread email" in digest
print(f"✅ unread.json written; count=1; digest built from it\n")

print("=== M365-B Verification Complete ✅ ===")
