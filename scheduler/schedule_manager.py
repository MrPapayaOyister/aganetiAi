import sqlite3
import uuid
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import os

# Resolve DB_PATH relative to the project root directory
project_root = Path(__file__).resolve().parent.parent
DB_PATH = str(project_root / "tasks" / "tasks.db")

class CronLabelsDict(dict):
    def get(self, key, default=None):
        if key in self:
            return self[key]
        
        # Try to parse dynamically
        # 1. Match "MM HH * * *"
        m = re.match(r'^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*$', key)
        if m:
            minute = int(m.group(1))
            hour = int(m.group(2))
            period = "AM"
            display_hour = hour
            if hour >= 12:
                period = "PM"
                if hour > 12:
                    display_hour = hour - 12
            elif hour == 0:
                display_hour = 12
            return f"Every day at {display_hour}:{minute:02d} {period}"

        # 2. Match "MM HH * * 1-5"
        m = re.match(r'^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+1-5$', key)
        if m:
            minute = int(m.group(1))
            hour = int(m.group(2))
            period = "AM"
            display_hour = hour
            if hour >= 12:
                period = "PM"
                if hour > 12:
                    display_hour = hour - 12
            elif hour == 0:
                display_hour = 12
            return f"Every weekday at {display_hour}:{minute:02d} {period}"

        # 3. Match "MM HH * * 1"
        m = re.match(r'^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+1$', key)
        if m:
            minute = int(m.group(1))
            hour = int(m.group(2))
            period = "AM"
            display_hour = hour
            if hour >= 12:
                period = "PM"
                if hour > 12:
                    display_hour = hour - 12
            elif hour == 0:
                display_hour = 12
            return f"Every Monday at {display_hour}:{minute:02d} {period}"

        # 4. Match "MM HH * * 2"
        m = re.match(r'^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+2$', key)
        if m:
            minute = int(m.group(1))
            hour = int(m.group(2))
            period = "AM"
            display_hour = hour
            if hour >= 12:
                period = "PM"
                if hour > 12:
                    display_hour = hour - 12
            elif hour == 0:
                display_hour = 12
            return f"Every Tuesday at {display_hour}:{minute:02d} {period}"

        # 5. Match "MM HH * * 3"
        m = re.match(r'^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+3$', key)
        if m:
            minute = int(m.group(1))
            hour = int(m.group(2))
            period = "AM"
            display_hour = hour
            if hour >= 12:
                period = "PM"
                if hour > 12:
                    display_hour = hour - 12
            elif hour == 0:
                display_hour = 12
            return f"Every Wednesday at {display_hour}:{minute:02d} {period}"

        # 6. Match "MM HH * * 4"
        m = re.match(r'^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+4$', key)
        if m:
            minute = int(m.group(1))
            hour = int(m.group(2))
            period = "AM"
            display_hour = hour
            if hour >= 12:
                period = "PM"
                if hour > 12:
                    display_hour = hour - 12
            elif hour == 0:
                display_hour = 12
            return f"Every Thursday at {display_hour}:{minute:02d} {period}"

        # 7. Match "MM HH * * 5"
        m = re.match(r'^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+5$', key)
        if m:
            minute = int(m.group(1))
            hour = int(m.group(2))
            period = "AM"
            display_hour = hour
            if hour >= 12:
                period = "PM"
                if hour > 12:
                    display_hour = hour - 12
            elif hour == 0:
                display_hour = 12
            return f"Every Friday at {display_hour}:{minute:02d} {period}"

        # 8. Match "MM HH * * 6,0"
        m = re.match(r'^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+6,0$', key)
        if m:
            minute = int(m.group(1))
            hour = int(m.group(2))
            period = "AM"
            display_hour = hour
            if hour >= 12:
                period = "PM"
                if hour > 12:
                    display_hour = hour - 12
            elif hour == 0:
                display_hour = 12
            return f"Every weekend at {display_hour}:{minute:02d} {period}"

        return default if default is not None else f"Custom: {key}"

CRON_LABELS = CronLabelsDict({
    "0 8 * * *":   "Every day at 8:00 AM",
    "0 18 * * *":  "Every day at 6:00 PM",
    "0 21 * * *":  "Every day at 9:00 PM",
    "0 * * * *":   "Every hour",
    "0 9 * * 1":   "Every Monday at 9:00 AM",
    "0 9 * * 1-5": "Every weekday at 9:00 AM",
})

def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    """Ensure that the schedules table exists in tasks.db on import."""
    # Ensure tasks directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            label TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            action_type TEXT NOT NULL,
            action_payload TEXT NOT NULL DEFAULT '{}',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

# Run database initialization
init_db()

def extract_reminder_message(text: str) -> str:
    msg = text.lower().strip()
    time_pat = r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?'
    patterns = [
        r'every weekday at\s*' + time_pat,
        r'every weekend at\s*' + time_pat,
        r'every monday at\s*' + time_pat,
        r'every tuesday at\s*' + time_pat,
        r'every wednesday at\s*' + time_pat,
        r'every thursday at\s*' + time_pat,
        r'every friday at\s*' + time_pat,
        r'every day at\s*' + time_pat,
        r'every morning',
        r'every evening',
        r'every night',
        r'every hour',
        r'every week on monday'
    ]
    cleaned = text
    for pat in patterns:
        match = re.search(pat, msg)
        if match:
            start, end = match.span()
            cleaned = text[:start] + text[end:]
            break

    cleaned_msg = cleaned.strip()
    while True:
        prev = cleaned_msg
        lower = cleaned_msg.lower()
        if lower.startswith("remind me to "):
            cleaned_msg = cleaned_msg[13:]
        elif lower.startswith("remind me "):
            cleaned_msg = cleaned_msg[10:]
        elif lower.startswith("reminder to "):
            cleaned_msg = cleaned_msg[12:]
        elif lower.startswith("reminder:"):
            cleaned_msg = cleaned_msg[9:]
        elif lower.startswith("reminder "):
            cleaned_msg = cleaned_msg[9:]
        elif lower.startswith("remind to "):
            cleaned_msg = cleaned_msg[10:]
        elif lower.startswith("remind "):
            cleaned_msg = cleaned_msg[7:]
        elif lower.startswith("alert me to "):
            cleaned_msg = cleaned_msg[12:]
        elif lower.startswith("alert me "):
            cleaned_msg = cleaned_msg[9:]
        elif lower.startswith("alert "):
            cleaned_msg = cleaned_msg[6:]
        elif lower.startswith("notify me to "):
            cleaned_msg = cleaned_msg[13:]
        elif lower.startswith("notify me "):
            cleaned_msg = cleaned_msg[10:]
        elif lower.startswith("notify "):
            cleaned_msg = cleaned_msg[7:]
        elif lower.startswith("to "):
            cleaned_msg = cleaned_msg[3:]
        
        cleaned_msg = cleaned_msg.strip(",.?! ")
        if cleaned_msg == prev:
            break
            
    return cleaned_msg

def parse_schedule_from_text(text: str) -> dict | None:
    """
    Converts natural language into a cron expression + action type.
    Returns None if parsing fails.
    """
    msg = text.lower().strip()

    # Time extraction helper
    def extract_time(t_str: str) -> tuple[int, int] | None:
        time_match = re.search(
            r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', 
            t_str.lower()
        )
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            period = time_match.group(3)
            if period == "pm" and hour != 12:
                hour += 12
            if period == "am" and hour == 12:
                hour = 0
            return hour, minute
        return None

    cron_expression = None

    # Parse in exact order:
    if "every day at" in msg:
        idx = msg.find("every day at")
        t = extract_time(msg[idx + len("every day at"):])
        if t:
            hour, minute = t
            cron_expression = f"{minute} {hour} * * *"
    elif "every morning" in msg:
        cron_expression = "0 8 * * *"
    elif "every evening" in msg:
        cron_expression = "0 18 * * *"
    elif "every night" in msg:
        cron_expression = "0 21 * * *"
    elif "every hour" in msg:
        cron_expression = "0 * * * *"
    elif "every weekday at" in msg:
        idx = msg.find("every weekday at")
        t = extract_time(msg[idx + len("every weekday at"):])
        if t:
            hour, minute = t
            cron_expression = f"{minute} {hour} * * 1-5"
    elif "every monday at" in msg:
        idx = msg.find("every monday at")
        t = extract_time(msg[idx + len("every monday at"):])
        if t:
            hour, minute = t
            cron_expression = f"{minute} {hour} * * 1"
    elif "every tuesday at" in msg:
        idx = msg.find("every tuesday at")
        t = extract_time(msg[idx + len("every tuesday at"):])
        if t:
            hour, minute = t
            cron_expression = f"{minute} {hour} * * 2"
    elif "every wednesday at" in msg:
        idx = msg.find("every wednesday at")
        t = extract_time(msg[idx + len("every wednesday at"):])
        if t:
            hour, minute = t
            cron_expression = f"{minute} {hour} * * 3"
    elif "every thursday at" in msg:
        idx = msg.find("every thursday at")
        t = extract_time(msg[idx + len("every thursday at"):])
        if t:
            hour, minute = t
            cron_expression = f"{minute} {hour} * * 4"
    elif "every friday at" in msg:
        idx = msg.find("every friday at")
        t = extract_time(msg[idx + len("every friday at"):])
        if t:
            hour, minute = t
            cron_expression = f"{minute} {hour} * * 5"
    elif "every weekend at" in msg:
        idx = msg.find("every weekend at")
        t = extract_time(msg[idx + len("every weekend at"):])
        if t:
            hour, minute = t
            cron_expression = f"{minute} {hour} * * 6,0"
    elif "every week on monday" in msg:
        cron_expression = "0 9 * * 1"

    if not cron_expression:
        return None

    # Action type detection from keywords
    action_payload = {}
    if any(w in msg for w in ["remind", "reminder", "alert", "notify"]):
        action_type = "custom_reminder"
        action_payload = {"message": extract_reminder_message(text)}
    elif any(w in msg for w in ["email", "inbox", "digest", "mail"]):
        action_type = "email_digest"
    elif any(w in msg for w in ["task", "todo", "pending"]):
        action_type = "task_summary"
    elif any(w in msg for w in ["report", "summary", "pdf", "briefing"]):
        action_type = "generate_report"
    else:
        action_type = "custom_reminder"
        action_payload = {"message": extract_reminder_message(text)}

    return {
        "cron_expression": cron_expression,
        "action_type": action_type,
        "action_payload": action_payload,
        "label": text[:80]
    }

def create_schedule(user_id: str, parsed: dict) -> dict:
    """Inserts parsed schedule into db and returns the full schedule dictionary."""
    schedule_id = str(uuid.uuid4())[:8]
    conn = _get_connection()
    conn.execute("""
        INSERT INTO schedules (id, user_id, label, cron_expression, action_type, action_payload, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
    """, (
        schedule_id,
        user_id,
        parsed["label"],
        parsed["cron_expression"],
        parsed["action_type"],
        json.dumps(parsed.get("action_payload", {})),
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    conn.close()
    return {"id": schedule_id, **parsed}

def list_schedules(user_id: str) -> list[dict]:
    """Returns all active schedules for a user."""
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, label, cron_expression, action_type, action_payload, is_active
        FROM schedules
        WHERE user_id = ? AND is_active = 1
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()

    result = []
    for row in rows:
        d = dict(row)
        if isinstance(d.get("action_payload"), str):
            try:
                d["action_payload"] = json.loads(d["action_payload"])
            except Exception:
                d["action_payload"] = {}
        result.append(d)
    return result

def delete_schedule(user_id: str, schedule_id: str) -> bool:
    """Sets is_active = 0 for a given schedule_id and user_id. Returns True if updated."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE schedules
        SET is_active = 0
        WHERE id = ? AND user_id = ? AND is_active = 1
    """, (schedule_id, user_id))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated

def load_all_active_schedules() -> list[dict]:
    """Returns all active schedules across all users."""
    conn = _get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, label, cron_expression, action_type, action_payload, is_active
        FROM schedules
        WHERE is_active = 1
    """)
    rows = cursor.fetchall()
    conn.close()

    result = []
    for row in rows:
        d = dict(row)
        if isinstance(d.get("action_payload"), str):
            try:
                d["action_payload"] = json.loads(d["action_payload"])
            except Exception:
                d["action_payload"] = {}
        result.append(d)
    return result

def format_schedules_for_display(schedules: list[dict]) -> str:
    """Formats schedules as a Markdown string for Telegram bot."""
    if not schedules:
        return "📅 You have no active schedules. Say _\"remind me every day at 8am to check emails\"_ to create one."

    lines = ["📅 *Your Active Schedules*", ""]
    for idx, sched in enumerate(schedules, 1):
        cron = sched["cron_expression"]
        time_label = CRON_LABELS.get(cron, f"Custom: {cron}")
        
        action_type = sched["action_type"]
        if action_type == "email_digest":
            action_label = "Email Digest"
        elif action_type == "task_summary":
            action_label = "Task Summary"
        elif action_type == "generate_report":
            action_label = "Scheduled Report"
        elif action_type == "custom_reminder":
            payload = sched.get("action_payload", {})
            msg_text = payload.get("message", "") if isinstance(payload, dict) else ""
            if not msg_text:
                msg_text = "Scheduled Reminder"
            action_label = f"Reminder: {msg_text}"
        else:
            action_label = action_type.replace("_", " ").title()

        lines.append(f"{idx}. {time_label} — {action_label} (ID: {sched['id']})")

    lines.append("")
    # Display help for deletion using the first schedule ID
    first_id = schedules[0]["id"]
    lines.append(f'To delete a schedule, say: _"delete schedule {first_id}"_')
    return "\n".join(lines)
