import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

# same path used by tasks/store.py
try:
    from config.settings import DB_PATH
except ImportError:
    DB_PATH = "tasks/tasks.db"

# Ensure DB_PATH is absolute relative to project root
if not os.path.isabs(DB_PATH):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = os.path.abspath(os.path.join(project_root, DB_PATH))

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def resolve_contact(name_or_email: str) -> dict | None:
    if not name_or_email or not name_or_email.strip():
        return None
    with get_conn() as conn:
        # 1. Exact email
        row = conn.execute(
            "SELECT * FROM contacts WHERE LOWER(email) = LOWER(?)",
            (name_or_email.strip(),)
        ).fetchone()
        if row:
            return dict(row)

        # 2. Exact nickname
        row = conn.execute(
            "SELECT * FROM contacts WHERE LOWER(nickname) = LOWER(?)",
            (name_or_email.strip(),)
        ).fetchone()
        if row:
            return dict(row)

        # 3. Partial full_name
        row = conn.execute(
            "SELECT * FROM contacts WHERE LOWER(full_name) LIKE LOWER(?)",
            (f"%{name_or_email.strip()}%",)
        ).fetchone()
        if row:
            return dict(row)

    return None

def create_contact(
    full_name: str,
    email: str = None,
    nickname: str = None,
    role: str = None,
    department: str = None,
    is_agent: int = 0,
    agent_id: str = None,
    notes: str = None
) -> dict:
    contact_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO contacts
                (id, full_name, email, nickname, department, role, is_agent, agent_id, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            contact_id, full_name, email, nickname,
            department, role, is_agent, agent_id, notes,
            datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()
    return resolve_contact(full_name)

def update_contact(contact_id: str, **kwargs) -> dict | None:
    allowed = {"full_name","email","nickname","role","department","notes","is_agent","agent_id"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return None
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [contact_id]
    with get_conn() as conn:
        conn.execute(
            f"UPDATE contacts SET {set_clause} WHERE id = ?", values
        )
        conn.commit()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return dict(row) if row else None

def list_contacts(is_agent: int = None) -> list[dict]:
    with get_conn() as conn:
        if is_agent is not None:
            rows = conn.execute(
                "SELECT * FROM contacts WHERE is_agent = ? ORDER BY full_name ASC",
                (is_agent,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM contacts ORDER BY full_name ASC"
            ).fetchall()
        return [dict(r) for r in rows]

def auto_create_from_email_header(from_name: str, from_email: str) -> dict:
    existing = resolve_contact(from_email)
    if existing:
        return existing
    return create_contact(
        full_name=from_name or from_email.split("@")[0],
        email=from_email,
        notes=f"Auto-created from incoming email on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    )

def format_contact_for_display(contact: dict) -> str:
    lines = [f"👤 *{contact['full_name']}*"]
    if contact.get("nickname"):
        lines.append(f"   Nickname: {contact['nickname']}")
    if contact.get("email"):
        lines.append(f"   Email: {contact['email']}")
    if contact.get("role"):
        lines.append(f"   Role: {contact['role']}")
    if contact.get("department"):
        lines.append(f"   Department: {contact['department']}")
    if contact.get("notes"):
        lines.append(f"   Notes: {contact['notes']}")
    return "\n".join(lines)
