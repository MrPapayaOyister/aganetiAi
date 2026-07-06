import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone

# Defined database path relative to tasks directory
DB_PATH = "tasks/tasks.db"

BACKEND = os.getenv("AGANETI_DATA_BACKEND", "sqlite")
_log = logging.getLogger("tasks.store")


def _pg() -> bool:
    return BACKEND == "postgres"


def _get_abs_db_path() -> str:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.path.join(project_root, DB_PATH))


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_get_abs_db_path())
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def get_conn() -> sqlite3.Connection:
    """Public connection helper used by integrations/agent_inbox.py (always SQLite)."""
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    abs_path = _get_abs_db_path()
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    conn = _get_connection()
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT 'user_1', title TEXT NOT NULL,
            source TEXT NOT NULL, status TEXT DEFAULT 'pending', priority TEXT DEFAULT 'medium',
            due_date TEXT, notes TEXT DEFAULT '', reminder_sent INTEGER DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN user_id TEXT NOT NULL DEFAULT 'user_1'")
        conn.commit()
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id TEXT PRIMARY KEY, full_name TEXT NOT NULL, email TEXT, nickname TEXT,
            department TEXT, role TEXT, is_agent INTEGER DEFAULT 0, agent_id TEXT, notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_email    ON contacts(email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_nickname ON contacts(nickname)")
    conn.commit()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_messages (
            id TEXT PRIMARY KEY, from_agent TEXT NOT NULL, to_agent TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'message', payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT DEFAULT (datetime('now')),
            read_at TEXT, resolved_at TEXT
        )""")
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_messages_to ON agent_messages (to_agent, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_messages_from ON agent_messages (from_agent, created_at)")
        conn.commit()
    except Exception:
        pass
    conn.close()


# ── Postgres helpers ──────────────────────────────────────────────────────────
def _task_dict(t, legacy_uid) -> dict:
    def _iso(d):
        return d.isoformat() if d else ""
    return {"id": str(t.id), "user_id": legacy_uid, "title": t.title, "source": t.source or "",
            "status": t.status, "priority": t.priority,
            "due_date": (t.due_date.strftime("%Y-%m-%d") if t.due_date else ""),
            "notes": t.notes or "", "reminder_sent": (1 if t.reminder_sent else 0),
            "created_at": _iso(t.created_at), "updated_at": _iso(t.updated_at)}


def _parse_due(due_date):
    if not due_date:
        return None
    s = str(due_date).strip()
    for cand in (s, s + "T00:00:00"):
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ── create_task ───────────────────────────────────────────────────────────────
def _sq_create_task(user_id, title, source, priority="medium", due_date=None, notes="") -> dict:
    task_id = str(uuid.uuid4())
    now_str = datetime.utcnow().isoformat()
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("INSERT INTO tasks (id,user_id,title,source,priority,due_date,notes,status,reminder_sent,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,'pending',0,?,?)",
                (task_id, user_id, title, source, priority, due_date, notes, now_str, now_str))
    conn.commit()
    row = cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row)


def create_task(user_id, title, source, priority="medium", due_date=None, notes="") -> dict:
    if _pg():
        try:
            from backend.db import models as M, sync as _s
            with _s.session() as ses:
                u = _s.resolve_user(ses, user_id)
                if u:
                    t = M.Task(org_id=u.org_id, user_id=u.id, title=title, source=source or "assistant",
                               priority=priority, due_date=_parse_due(due_date), notes=notes, status="pending")
                    ses.add(t)
                    ses.commit()
                    ses.refresh(t)
                    return _task_dict(t, user_id)
        except Exception as e:  # noqa: BLE001
            _log.warning("pg create_task failed → sqlite: %s", e)
    return _sq_create_task(user_id, title, source, priority, due_date, notes)


# ── get_all_tasks ─────────────────────────────────────────────────────────────
def _sq_get_all_tasks(user_id, status=None) -> list[dict]:
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    q = "SELECT * FROM tasks WHERE user_id = ?"
    params = [user_id]
    if status is not None:
        q += " AND status = ?"
        params.append(status)
    q += (" ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 "
          "WHEN 'low' THEN 3 ELSE 4 END ASC, created_at ASC")
    rows = conn.cursor().execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_tasks(user_id, status=None) -> list[dict]:
    if _pg():
        try:
            from sqlalchemy import select
            from backend.db import models as M, sync as _s
            with _s.session() as ses:
                u = _s.resolve_user(ses, user_id)
                if u:
                    q = select(M.Task).where(M.Task.user_id == u.id, M.Task.deleted_at.is_(None))
                    if status is not None:
                        q = q.where(M.Task.status == status)
                    rows = ses.execute(q).scalars().all()
                    order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
                    rows = sorted(rows, key=lambda t: (order.get((t.priority or "").lower(), 4), t.created_at or datetime.min.replace(tzinfo=timezone.utc)))
                    return [_task_dict(t, user_id) for t in rows]
        except Exception as e:  # noqa: BLE001
            _log.warning("pg get_all_tasks failed → sqlite: %s", e)
    return _sq_get_all_tasks(user_id, status)


# ── update_task ───────────────────────────────────────────────────────────────
def _sq_update_task(user_id, task_id, **kwargs) -> dict | None:
    allowed = {"title", "status", "priority", "due_date", "notes", "reminder_sent"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    fields["updated_at"] = datetime.utcnow().isoformat()
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if not cur.execute("SELECT id FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone():
        conn.close()
        return None
    setc = ", ".join(f"{k}=?" for k in fields)
    cur.execute(f"UPDATE tasks SET {setc} WHERE id=? AND user_id=?", list(fields.values()) + [task_id, user_id])
    conn.commit()
    row = cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_task(user_id, task_id, **kwargs) -> dict | None:
    if _pg():
        try:
            from backend.db import models as M, sync as _s
            with _s.session() as ses:
                u = _s.resolve_user(ses, user_id)
                if u:
                    t = ses.get(M.Task, uuid.UUID(str(task_id)))
                    if not t or t.user_id != u.id:
                        return None
                    for k in ("title", "status", "priority", "notes"):
                        if k in kwargs and kwargs[k] is not None:
                            setattr(t, k, kwargs[k])
                    if "due_date" in kwargs:
                        t.due_date = _parse_due(kwargs["due_date"])
                    if "reminder_sent" in kwargs:
                        t.reminder_sent = bool(kwargs["reminder_sent"])
                    ses.commit()
                    ses.refresh(t)
                    return _task_dict(t, user_id)
        except Exception as e:  # noqa: BLE001
            _log.warning("pg update_task failed → sqlite: %s", e)
    return _sq_update_task(user_id, task_id, **kwargs)


# ── delete_task (soft-delete on PG) ───────────────────────────────────────────
def _sq_delete_task(user_id, task_id) -> bool:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, user_id))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def delete_task(user_id, task_id) -> bool:
    if _pg():
        try:
            from sqlalchemy import func
            from backend.db import models as M, sync as _s
            with _s.session() as ses:
                u = _s.resolve_user(ses, user_id)
                if u:
                    t = ses.get(M.Task, uuid.UUID(str(task_id)))
                    if not t or t.user_id != u.id:
                        return False
                    t.deleted_at = func.now()
                    ses.commit()
                    return True
        except Exception as e:  # noqa: BLE001
            _log.warning("pg delete_task failed → sqlite: %s", e)
    return _sq_delete_task(user_id, task_id)


# ── find_task_by_title ────────────────────────────────────────────────────────
def _sq_find_task_by_title(user_id, partial_title) -> dict | None:
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    row = conn.cursor().execute(
        "SELECT * FROM tasks WHERE user_id=? AND title LIKE ? AND status!='done' ORDER BY created_at DESC LIMIT 1",
        (user_id, f"%{partial_title}%")).fetchone()
    conn.close()
    return dict(row) if row else None


def find_task_by_title(user_id, partial_title) -> dict | None:
    if _pg():
        try:
            from sqlalchemy import select
            from backend.db import models as M, sync as _s
            with _s.session() as ses:
                u = _s.resolve_user(ses, user_id)
                if u:
                    t = ses.execute(select(M.Task).where(
                        M.Task.user_id == u.id, M.Task.deleted_at.is_(None), M.Task.status != "done",
                        M.Task.title.ilike(f"%{partial_title}%")).order_by(M.Task.created_at.desc()).limit(1)).scalars().first()
                    return _task_dict(t, user_id) if t else None
        except Exception as e:  # noqa: BLE001
            _log.warning("pg find_task_by_title failed → sqlite: %s", e)
    return _sq_find_task_by_title(user_id, partial_title)


def get_pending_summary(user_id) -> str:
    tasks = get_all_tasks(user_id, status="pending")
    if not tasks:
        return "No pending tasks."
    lines = ["Pending Tasks:"]
    for t in tasks:
        line = f"- [{t['priority']}] {t['title']}"
        if t.get("due_date"):
            line += f" (due: {t['due_date']})"
        lines.append(line)
    return "\n".join(lines)


def get_unread_email_count(user_id: str) -> int:
    from pathlib import Path
    import json
    p = Path(f"email_store/{user_id}/unread.json")
    if not p.exists():
        return 0
    try:
        return len(json.loads(p.read_text()))
    except Exception:
        return 0
