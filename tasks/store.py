import os
import sqlite3
import uuid
from datetime import datetime

# Defined database path relative to tasks directory
DB_PATH = "tasks/tasks.db"

def _get_abs_db_path() -> str:
    """
    Resolve DB_PATH relative to the project root directory
    to ensure portability when executing from different directories.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.path.join(project_root, DB_PATH))

def _get_connection() -> sqlite3.Connection:
    abs_path = _get_abs_db_path()
    conn = sqlite3.connect(abs_path)
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def get_conn() -> sqlite3.Connection:
    """
    Public connection helper used by integrations/agent_inbox.py.
    Sets row_factory=sqlite3.Row so callers can do dict(row) directly.
    """
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """
    Creates the tasks directory if it doesn't exist, opens a SQLite connection,
    creates the tasks table if it does not exist, commits and closes.
    """
    abs_path = _get_abs_db_path()
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    conn = _get_connection()
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'user_1',
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            priority TEXT DEFAULT 'medium',
            due_date TEXT,
            notes TEXT DEFAULT '',
            reminder_sent INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Migration — safe on an existing DB (column already present → ignored).
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN user_id TEXT NOT NULL DEFAULT 'user_1'")
        conn.commit()
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id          TEXT PRIMARY KEY,
            full_name   TEXT NOT NULL,
            email       TEXT,
            nickname    TEXT,
            department  TEXT,
            role        TEXT,
            is_agent    INTEGER DEFAULT 0,
            agent_id    TEXT,
            notes       TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contacts_email    ON contacts(email)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contacts_nickname ON contacts(nickname)"
    )
    conn.commit()

    # ── Task 16: agent_messages table + indexes ──────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id           TEXT PRIMARY KEY,
            from_agent   TEXT NOT NULL,
            to_agent     TEXT NOT NULL,
            type         TEXT NOT NULL DEFAULT 'message',
            payload      TEXT NOT NULL DEFAULT '{}',
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT DEFAULT (datetime('now')),
            read_at      TEXT,
            resolved_at  TEXT
        )
    """)
    # Indexes for fast per-agent polling
    try:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_messages_to
            ON agent_messages (to_agent, status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_messages_from
            ON agent_messages (from_agent, created_at)
        """)
        conn.commit()
    except Exception:
        pass
    conn.close()

def create_task(user_id, title, source, priority="medium", due_date=None, notes="") -> dict:
    """
    Generates a UUID, created_at, updated_at, inserts the row (scoped to user_id), and
    returns the full task.
    """
    task_id = str(uuid.uuid4())
    now_str = datetime.utcnow().isoformat()

    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO tasks (id, user_id, title, source, priority, due_date, notes, status, reminder_sent, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
    """, (task_id, user_id, title, source, priority, due_date, notes, now_str, now_str))
    conn.commit()
    
    cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row)

def get_all_tasks(user_id, status=None) -> list[dict]:
    """
    Retrieves tasks for user_id. If status is provided, filters by status.
    Orders by priority DESC (high > medium > low), then created_at ASC.
    """
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = "SELECT * FROM tasks WHERE user_id = ?"
    params = [user_id]
    if status is not None:
        query += " AND status = ?"
        params.append(status)

    query += """
        ORDER BY 
            CASE priority 
                WHEN 'urgent' THEN 0
                WHEN 'high' THEN 1 
                WHEN 'medium' THEN 2 
                WHEN 'low' THEN 3 
                ELSE 4 
            END ASC, 
            created_at ASC
    """
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def update_task(user_id, task_id, **kwargs) -> dict | None:
    """
    Updates the specified task fields dynamically (only if the task belongs to user_id).
    Always sets updated_at to the current time.
    Returns the updated task dict, or None if the ID was not found for this user.
    """
    ALLOWED_KEYS = {"title", "status", "priority", "due_date", "notes", "reminder_sent"}
    update_fields = {k: v for k, v in kwargs.items() if k in ALLOWED_KEYS}

    now_str = datetime.utcnow().isoformat()
    update_fields["updated_at"] = now_str

    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Verify the task exists AND belongs to this user
    cursor.execute("SELECT id FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    if not cursor.fetchone():
        conn.close()
        return None

    set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
    query = f"UPDATE tasks SET {set_clause} WHERE id = ? AND user_id = ?"
    params = list(update_fields.values()) + [task_id, user_id]

    cursor.execute(query, params)
    conn.commit()

    cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_task(user_id, task_id) -> bool:
    """
    Deletes the task matching task_id, only if it belongs to user_id.
    Returns True if a row was deleted, False if not found for this user.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def get_pending_summary(user_id) -> str:
    """
    Retrieves pending tasks for user_id and formats them as a string.
    Returns 'No pending tasks.' if none exist.
    """
    tasks = get_all_tasks(user_id, status="pending")
    if not tasks:
        return "No pending tasks."

    lines = ["Pending Tasks:"]
    for t in tasks:
        line = f"- [{t['priority']}] {t['title']}"
        if t.get("due_date") is not None and t.get("due_date") != "":
            line += f" (due: {t['due_date']})"
        lines.append(line)

    return "\n".join(lines)

def find_task_by_title(user_id, partial_title: str) -> dict | None:
    """
    Finds the most recently created pending/active task containing partial_title for user_id.
    """
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND title LIKE ? AND status != 'done' ORDER BY created_at DESC LIMIT 1",
        (user_id, f"%{partial_title}%")
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_unread_email_count(user_id: str) -> int:
    """Returns count of unread emails for user."""
    from pathlib import Path
    import json
    p = Path(f"email_store/{user_id}/unread.json")
    if not p.exists():
        return 0
    try:
        return len(json.loads(p.read_text()))
    except Exception:
        return 0

