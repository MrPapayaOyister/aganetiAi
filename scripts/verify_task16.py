#!/usr/bin/env python3
"""
Task 16 Verification — Tests T1-T5 (unit tests, no server required).
Run from project root: python scripts/verify_task16.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force init_db so agent_messages table exists in the test DB
from tasks.store import init_db, get_conn
init_db()

# ── T1: Schema + indexes ─────────────────────────────────────────
print("Running T1: Schema + indexes...")
with get_conn() as conn:
    row = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='agent_messages'
    """).fetchone()
    assert row is not None, "agent_messages table missing"

    idx = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type='index' AND name LIKE 'idx_agent_messages%'
    """).fetchall()
    assert len(idx) == 2, f"Expected 2 indexes, got {len(idx)}: {[r['name'] for r in idx]}"

print("T1 ✅ Schema + indexes confirmed\n")

# ── T2: send_message + get_pending_messages ───────────────────────
print("Running T2: send_message + get_pending_messages...")
from integrations.agent_inbox import send_message, get_pending_messages, mark_read

msg = send_message("agent_1", "agent_2", "message", {"text": "Task 16 test"})
assert msg["status"] == "pending"
assert msg["from_agent"] == "agent_1"
assert msg["to_agent"] == "agent_2"
assert msg["payload"]["text"] == "Task 16 test"

pending = get_pending_messages("agent_2")
ids = [m["id"] for m in pending]
assert msg["id"] in ids
print(f"T2 ✅ send + get_pending confirmed. {len(pending)} pending for agent_2\n")

# ── T3: mark_read, resolve, reject ───────────────────────────────
print("Running T3: mark_read / resolve / reject...")
from integrations.agent_inbox import resolve_message, reject_message

# mark_read
m1 = send_message("agent_1", "agent_2", "message", {"text": "read test"})
mark_read(m1["id"])
pending_after = get_pending_messages("agent_2")
assert m1["id"] not in [m["id"] for m in pending_after], "marked message still pending"

# resolve
m2 = send_message("agent_1", "agent_2", "message", {"text": "resolve test"})
resolve_message(m2["id"])

# reject
m3 = send_message("agent_1", "agent_2", "message", {"text": "reject test"})
reject_message(m3["id"], reason="out of scope")

# Verify statuses in DB
with get_conn() as conn:
    r1 = dict(conn.execute("SELECT status FROM agent_messages WHERE id=?", (m1["id"],)).fetchone())
    r2 = dict(conn.execute("SELECT status FROM agent_messages WHERE id=?", (m2["id"],)).fetchone())
    r3 = dict(conn.execute("SELECT status FROM agent_messages WHERE id=?", (m3["id"],)).fetchone())
assert r1["status"] == "read", f"Expected read, got {r1['status']}"
assert r2["status"] == "resolved", f"Expected resolved, got {r2['status']}"
assert r3["status"] == "rejected", f"Expected rejected, got {r3['status']}"

print("T3 ✅ mark_read / resolve / reject confirmed\n")

# ── T4: Per-agent isolation ───────────────────────────────────────
print("Running T4: Per-agent inbox isolation...")
send_message("agent_2", "agent_1", "message", {"text": "for agent_1 only"})

pending_1 = get_pending_messages("agent_1")
pending_2 = get_pending_messages("agent_2")

for m in pending_1:
    assert m["to_agent"] == "agent_1", f"agent_1 inbox has msg for {m['to_agent']}"
for m in pending_2:
    assert m["to_agent"] == "agent_2", f"agent_2 inbox has msg for {m['to_agent']}"

print("T4 ✅ Per-agent inbox isolation confirmed\n")

# ── T5: Inbox summary counts ─────────────────────────────────────
print("Running T5: Inbox summary counts...")
from integrations.agent_inbox import get_inbox_summary

m_a = send_message("agent_2", "agent_1", "message", {"text": "summary test A"})
m_b = send_message("agent_2", "agent_1", "message", {"text": "summary test B"})
resolve_message(m_b["id"])

summary = get_inbox_summary("agent_1")
assert summary["agent_id"] == "agent_1"
assert summary["pending"] >= 1
assert summary["resolved"] >= 1
print(f"T5 ✅ Inbox summary: {summary}\n")

print("=" * 50)
print("ALL UNIT TESTS PASSED (T1–T5) ✅")
