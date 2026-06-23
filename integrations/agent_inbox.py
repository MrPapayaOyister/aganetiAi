"""
integrations/agent_inbox.py — Task 16: Agent Inbox & Inter-Agent Messaging

Provides CRUD operations over the agent_messages SQLite table.
All queries use parameterized statements (no string concatenation) to
prevent SQL injection (CWE-89).

Message status lifecycle:
    pending → read → resolved
    pending → rejected  (Task 17 delegation refusal)

Security notes:
    - Status transitions are enforced server-side only.
    - Payload is serialized as JSON; deserialization errors are caught and
      default to {} rather than propagating raw untrusted data.
    - TODO(security): Add authentication/authorization checks to all
      public endpoints that call these functions. Currently all agent
      message operations are trusted internal calls only.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import uuid
from datetime import datetime, timezone
from tasks.store import get_conn  # reuse existing connection helper


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    """
    Convert a sqlite3.Row to a plain dict and deserialize the JSON payload.
    Payload deserialization errors are caught and default to {} to avoid
    propagating raw untrusted data to callers.
    """
    d = dict(row)
    # Deserialize payload JSON string back to dict
    try:
        d["payload"] = json.loads(d["payload"])
    except Exception:
        d["payload"] = {}
    return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_message(
    from_agent: str,
    to_agent:   str,
    type:       str,   # noqa: A002  (shadows built-in, matches spec)
    payload:    dict
) -> dict:
    """
    Send a message from one agent to another.

    Args:
        from_agent: Sender identifier, e.g. "agent_1" or "system".
        to_agent:   Recipient identifier, e.g. "agent_2".
        type:       Message type from the type registry (see module docstring).
        payload:    Type-specific data dict — serialized to JSON for storage.

    Returns:
        The created message as a dict with status="pending".
    """
    msg_id = str(uuid.uuid4())
    now    = datetime.now(timezone.utc).isoformat()

    # Serialize payload; default to "{}" if serialization fails.
    try:
        payload_json = json.dumps(payload)
    except (TypeError, ValueError):
        payload_json = "{}"

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO agent_messages
                (id, from_agent, to_agent, type, payload, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, (msg_id, from_agent, to_agent, type, payload_json, now))
        conn.commit()

    return {
        "id":         msg_id,
        "from_agent": from_agent,
        "to_agent":   to_agent,
        "type":       type,
        "payload":    payload,
        "status":     "pending",
        "created_at": now,
    }


def get_pending_messages(agent_id: str, limit: int = 20) -> list[dict]:
    """
    Returns all pending messages for agent_id, oldest first.
    Called by the APScheduler poll job every 30s.

    Args:
        agent_id: The receiving agent identifier.
        limit:    Maximum number of messages to return (default 20).

    Returns:
        List of message dicts with deserialized payloads.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM agent_messages
            WHERE to_agent = ? AND status = 'pending'
            ORDER BY created_at ASC
            LIMIT ?
        """, (agent_id, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_read(message_id: str) -> None:
    """
    Transition a message from 'pending' to 'read'.
    Does not resolve it — recipient may still need to act.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("""
            UPDATE agent_messages
            SET status='read', read_at=?
            WHERE id=?
        """, (now, message_id))
        conn.commit()


def resolve_message(message_id: str) -> None:
    """Mark a message as fully handled (status='resolved')."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("""
            UPDATE agent_messages
            SET status='resolved', resolved_at=?
            WHERE id=?
        """, (now, message_id))
        conn.commit()


def reject_message(message_id: str, reason: str = "") -> None:
    """
    Mark a message as rejected.
    Used by Task 17 delegation refusal flow.
    Appends rejection_reason to the stored payload via json_set.

    Args:
        message_id: UUID of the message to reject.
        reason:     Human-readable rejection reason (stored in payload).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("""
            UPDATE agent_messages
            SET status='rejected', resolved_at=?, payload=json_set(payload, '$.rejection_reason', ?)
            WHERE id=?
        """, (now, reason, message_id))
        conn.commit()


def get_inbox_summary(agent_id: str) -> dict:
    """
    Returns count breakdown for dashboard inbox panel.

    Args:
        agent_id: The agent whose inbox to summarize.

    Returns:
        Dict with agent_id, per-status counts, and total.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT status, COUNT(*) as count
            FROM agent_messages
            WHERE to_agent = ?
            GROUP BY status
        """, (agent_id,)).fetchall()

    counts = {"pending": 0, "read": 0, "resolved": 0, "rejected": 0}
    for row in rows:
        status = row["status"]
        if status in counts:
            counts[status] = row["count"]

    return {"agent_id": agent_id, **counts, "total": sum(counts.values())}


def get_sent_messages(agent_id: str, limit: int = 20) -> list[dict]:
    """
    Returns messages sent BY agent_id — for outbox view.

    Args:
        agent_id: The sending agent identifier.
        limit:    Maximum number of messages to return (default 20).

    Returns:
        List of message dicts ordered newest first.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM agent_messages
            WHERE from_agent = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (agent_id, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]
