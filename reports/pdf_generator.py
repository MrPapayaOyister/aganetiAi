import os
import json
import re
import httpx
from pathlib import Path
from datetime import datetime, timezone
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

# Resolve and load USERS config with a robust default fallback
try:
    from config.users import USERS
except (ImportError, ModuleNotFoundError):
    USERS = {
        "user_1": {
            "name": "User One",
            "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
            "report_style": {
                "font_family": "Inter",
                "primary_color": "#01696f",
                "tone": "formal",
                "include_header": True,
                "include_footer": True,
                "logo_path": None
            }
        },
        "user_2": {
            "name": "User Two",
            "telegram_chat_id": "",
            "report_style": {
                "font_family": "Inter",
                "primary_color": "#01696f",
                "tone": "formal",
                "include_header": True,
                "include_footer": True,
                "logo_path": None
            }
        }
    }

def get_user_report_style(user_id: str) -> dict:
    """
    Reads report_style from USERS[user_id] if present.
    Returns dict with defaults filled in for any missing keys.
    """
    user_data = USERS.get(user_id, {})
    style = user_data.get("report_style", {})
    if not isinstance(style, dict):
        style = {}

    defaults = {
        "font_family": "Inter",
        "primary_color": "#01696f",
        "tone": "formal",
        "include_header": True,
        "include_footer": True,
        "logo_path": None
    }

    # Merging keeping existing keys intact
    merged_style = {**defaults}
    for k, v in style.items():
        if v is not None:
            merged_style[k] = v

    return merged_style

def parse_calendar_prompt(prompt_str: str) -> list[dict]:
    """
    Extracts structured events from the formatted agenda string.
    Returns a list of dicts: {"title": ..., "time": ..., "location": ...}
    """
    items = []
    for line in prompt_str.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        try:
            line_content = line[2:].strip()
            if ": " in line_content:
                time_part, rest = line_content.split(": ", 1)
            else:
                time_part = "All Day"
                rest = line_content
            
            loc_match = re.search(r'\(Location:\s*(.*?)\)', rest)
            location = loc_match.group(1).strip() if loc_match else ""
            if loc_match:
                rest = re.sub(r'\(Location:\s*(.*?)\)', '', rest).strip()
                
            meet_match = re.search(r'\(Google Meet:\s*(.*?)\)', rest)
            if meet_match:
                rest = re.sub(r'\(Google Meet:\s*(.*?)\)', '', rest).strip()
            
            title = rest.strip()
            items.append({
                "title": title,
                "time": time_part,
                "location": location
            })
        except Exception:
            pass
    return items

def collect_section_data(user_id: str, sections: list[str], query: str = "") -> dict:
    """
    Collects section data for pdf generation.
    """
    data = {}
    for sec in sections:
        if sec == "calendar":
            try:
                from backend.services.mailbox import agenda_text_sync
                agenda_str = agenda_text_sync(user_id)
                
                if not agenda_str or "No events scheduled" in agenda_str:
                    items = []
                else:
                    items = parse_calendar_prompt(agenda_str)
            except Exception:
                items = [{"title": "See calendar for details", "time": "", "location": ""}]
            data["calendar"] = {"items": items}
            
        elif sec == "tasks":
            try:
                # Use localhost loopback safely
                response = httpx.get(f"http://localhost:8000/tasks?user_id={user_id}&status=pending", timeout=10.0)
                if response.status_code == 200:
                    items = response.json()
                else:
                    items = []
            except Exception:
                items = []
            data["tasks"] = {"items": items}
            
        elif sec == "emails":
            emails_path = Path(f"email_store/{user_id}/unread.json")
            if emails_path.exists():
                try:
                    emails = json.loads(emails_path.read_text())
                    if not isinstance(emails, list):
                        emails = []
                except Exception:
                    emails = []
            else:
                emails = []
                
            try:
                from reports.email_digest import summarise_email
                for email in emails:
                    subject = email.get("subject", "")
                    body = email.get("body", "")
                    email["summary"] = summarise_email(subject, body)
            except Exception as e:
                print(f"Error summarising emails: {e}")
                for email in emails:
                    email["summary"] = email.get("subject", "")
            data["emails"] = {"items": emails}
            
        elif sec == "memory":
            try:
                from memory.long_term import search_memory
                mem_str = search_memory(user_id, query or "recent decisions tasks deadlines", top_k=5)
                items = [line.lstrip("- ").strip() for line in mem_str.splitlines() if line.strip()]
            except Exception:
                items = []
            data["memory"] = {"items": items}
            
        elif sec == "custom":
            data["custom"] = {"content": query}
            
    return data

def generate_pdf(user_id: str, sections: list[str], report_title: str = "Report", query: str = "") -> Path:
    """
    Renders HTML from base template and compiles to PDF using WeasyPrint.
    """
    # TODO(security): Strict user_id validation to prevent directory traversal paths
    user_id = os.path.basename(user_id)
    if user_id not in ["user_1", "user_2"]:
        user_id = "user_1"

    style = get_user_report_style(user_id)
    section_data = collect_section_data(user_id, sections, query)

    # TODO(security): Enable autoescaping explicitly in Jinja2 Environment to block HTML/XSS injection
    env = Environment(
        loader=FileSystemLoader("reports/templates"),
        autoescape=True
    )
    template = env.get_template("report_base.html")

    user_name = USERS.get(user_id, {}).get("name", "User")

    html_content = template.render(
        report_title=report_title,
        user_name=user_name,
        company_name=os.getenv("COMPANY_NAME", "The Company"),
        generated_at=datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC"),
        sections=section_data,
        font_family=style["font_family"],
        primary_color=style["primary_color"],
        tone=style["tone"],
        include_header=style["include_header"],
        include_footer=style["include_footer"],
    )

    output_dir = Path(f"temp/{user_id}/reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"report_{timestamp}.pdf"

    HTML(string=html_content).write_pdf(str(output_path))
    return output_path

def parse_report_intent(user_message: str) -> dict:
    """
    Parses what the user wants in the report from natural language.
    """
    sections = []
    msg = user_message.lower()

    if any(w in msg for w in ["calendar", "meetings", "schedule", "events"]):
        sections.append("calendar")
    if any(w in msg for w in ["task", "todo", "pending", "to-do"]):
        sections.append("tasks")
    if any(w in msg for w in ["email", "inbox", "mail", "unread"]):
        sections.append("emails")
    if any(w in msg for w in ["memory", "decisions", "history", "context", "notes"]):
        sections.append("memory")

    # If nothing detected, default to all four main sections
    if not sections:
        sections = ["calendar", "tasks", "emails", "memory"]

    # Title inference
    if "daily" in msg or "today" in msg:
        title = "Daily Summary"
    elif "weekly" in msg or "week" in msg:
        title = "Weekly Summary"
    elif "task" in msg and len(sections) == 1:
        title = "Task Report"
    elif "email" in msg and len(sections) == 1:
        title = "Email Report"
    else:
        title = "Report"

    return {"sections": sections, "title": title, "query": user_message}
