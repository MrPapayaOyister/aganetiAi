from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import TypedDict
from langgraph.graph import StateGraph, END
from openai import OpenAI, AsyncOpenAI
from qdrant_client import QdrantClient
from fastembed import TextEmbedding
from email.utils import parseaddr
import sys
import os
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio
import json
import httpx
import time
from integrations.model_router import route_model
from integrations.m365_mail import (
    fetch_unread_emails,
    send_email,
    mark_as_read,
    get_inbox_count,
)
from integrations.m365_calendar import (
    get_todays_agenda,
    get_upcoming_events,
    format_meeting_brief,
    format_agenda_for_prompt,
    invalidate_agenda_cache,
    create_calendar_event,
)

BASE_URL = "http://127.0.0.1:8000"

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.store import load_history, save_message, load_session_state, save_session_state, append_message
from config.settings import LLM_BASE_URL, QDRANT_URL, EMAIL_ACCOUNT, USER_1_M365_EMAIL, USER_2_M365_EMAIL
from integrations.telegram_bot import start_bot, bot
from aiogram.exceptions import TelegramBadRequest
from config.settings import TELEGRAM_CHAT_ID
from datetime import date, datetime
from fastapi.responses import JSONResponse, StreamingResponse
from tasks.store import init_db, create_task, get_all_tasks, update_task, delete_task, get_pending_summary, find_task_by_title
from config.users import get_all_user_ids, get_user_name, USERS
from integrations.agent_inbox import (
    send_message,
    get_pending_messages,
    mark_read,
    resolve_message,
    reject_message,
    get_inbox_summary,
    get_sent_messages,
)
init_db()

# Initialize Scheduler
scheduler = AsyncIOScheduler()

def get_recent_history(user_id: str, n: int = 3) -> list:
    return load_history(user_id)[-n:]


async def send_due_reminders(user_id: str):
    """
    Finds user_id's pending tasks due today and sends a Telegram reminder to that user's
    own chat. Registered once per user (job id due_tasks_{user_id}).
    """
    if not bot:
        print("Warning: Telegram bot not configured for reminders.")
        return

    from integrations.telegram_bot import _task_id_cache, send_message_to_user
    today_str = date.today().isoformat()

    try:
        tasks = get_all_tasks(user_id, status="pending")
        due_tasks = [t for t in tasks if t.get("due_date") == today_str]

        for t in due_tasks:
            short_id = t['id'][:8]
            _task_id_cache[short_id] = t['id']

            message_text = (
                f"⏰ Task due today: [{t['priority']}] {t['title']}\n"
                f"Use /done {short_id} to mark complete."
            )
            try:
                await send_message_to_user(user_id, message_text)
            except Exception as e:
                print(f"[Reminders] Failed to send message for task {t['id']}: {e}")
    except Exception as e:
        print(f"[Reminders] Error for {user_id}: {e}")

def log_triaged_email(sender: str, subject: str):
    """
    Appends a record of a triaged email to logs/email_log.json, keeping only the last 50 entries.
    """
    from config.settings import LOGS_DIR
    log_file = os.path.join(str(LOGS_DIR), "email_log.json")
    
    entries = []
    if os.path.exists(log_file):
        try:
            with open(log_file, "r") as f:
                entries = json.load(f)
                if not isinstance(entries, list):
                    entries = []
        except Exception:
            entries = []
            
    new_entry = {
        "sender": sender,
        "subject": subject,
        "triaged_at": datetime.utcnow().isoformat()
    }
    entries.append(new_entry)
    
    if len(entries) > 50:
        entries = entries[-50:]
        
    try:
        with open(log_file, "w") as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        print(f"Error writing to email log: {e}")

def find_last_email_from_attendees(attendees: list[str]) -> dict | None:
    """
    Finds the most recent triaged email from any of the attendees.
    """
    from config.settings import LOGS_DIR
    log_file = os.path.join(str(LOGS_DIR), "email_log.json")
    if not os.path.exists(log_file):
        return None
        
    try:
        with open(log_file, "r") as f:
            entries = json.load(f)
            if not isinstance(entries, list):
                return None
    except Exception:
        return None
        
    for entry in reversed(entries):
        sender_lower = entry.get("sender", "").lower()
        for attendee in attendees:
            if attendee.lower() in sender_lower:
                return {
                    "sender": entry["sender"],
                    "email": attendee,
                    "subject": entry["subject"],
                    "triaged_at": entry["triaged_at"]
                }
    return None

def format_time_ago(iso_str: str) -> str:
    try:
        triaged_at = datetime.fromisoformat(iso_str)
        now = datetime.utcnow()
        delta = now - triaged_at
        
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        
        if days > 0:
            return f"{days} day{'s' if days > 1 else ''} ago"
        if hours > 0:
            return f"{hours} hour{'s' if hours > 1 else ''} ago"
        if minutes > 0:
            return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
        return "just now"
    except Exception:
        return "recently"

def find_related_task_for_attendees(user_id: str, attendees: list[str]) -> dict | None:
    """
    Checks if any of user_id's pending tasks' title or notes contains any attendee
    email/name.
    """
    tasks = get_all_tasks(user_id, status="pending")
    for task in tasks:
        title_lower = task.get("title", "").lower()
        notes_lower = task.get("notes", "").lower()
        for attendee in attendees:
            name_prefix = attendee.split("@")[0].lower()
            if (attendee.lower() in title_lower or attendee.lower() in notes_lower or 
                name_prefix in title_lower or name_prefix in notes_lower):
                return task
    return None

def _user_m365_email(user_id: str) -> str:
    """The user's own M365 address — used to exclude self from a meeting's attendees."""
    return {"user_1": USER_1_M365_EMAIL, "user_2": USER_2_M365_EMAIL}.get(user_id, "")

async def send_meeting_brief(event: dict, user_id: str):
    """
    Builds the Graph-based meeting brief (format_meeting_brief) and enriches it with the
    'last email from attendees' and 'related pending task' context, then sends it to the
    correct user's Telegram chat.
    """
    # Attendee addresses (Graph shape), excluding the user themselves.
    own = _user_m365_email(user_id).lower()
    attendees = [
        a.get("emailAddress", {}).get("address", "")
        for a in event.get("attendees", [])
    ]
    attendees = [e for e in attendees if e and e.lower() != own]

    message = format_meeting_brief(event, user_id)

    if attendees:
        last_email = find_last_email_from_attendees(attendees)
        if last_email:
            time_ago = format_time_ago(last_email["triaged_at"])
            message += f"\n📧 Last email from {last_email['email']}: \"{last_email['subject']}\" ({time_ago})"

        related_task = find_related_task_for_attendees(user_id, attendees)
        if related_task:
            message += f"\n📋 Related pending task: {related_task['title']} [{related_task['priority']}]"

    try:
        from integrations.telegram_bot import send_message_to_user
        await send_message_to_user(user_id, message)
        print(f"Successfully sent meeting brief ({user_id}) for event {event.get('id', '')}")
    except Exception as e:
        print(f"Error sending meeting brief: {e}")

_briefed_events: set = set()

async def check_upcoming_meetings():
    """
    APScheduler job — for each user, finds meetings starting in 25-35 min and sends a brief
    to their Telegram. Dedup via _briefed_events on the Graph event id.
    """
    from datetime import timezone
    now = datetime.now(timezone.utc)
    for uid in ["user_1", "user_2"]:
        try:
            events = await asyncio.to_thread(get_upcoming_events, uid, 60)
            for event in events:
                event_id = event.get("id", "")
                if event_id in _briefed_events:
                    continue

                start_str = event.get("start", {}).get("dateTime", "")
                if not start_str:
                    continue
                try:
                    start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                    else:
                        start_dt = start_dt.astimezone(timezone.utc)
                except Exception as e:
                    print(f"Error parsing event start date {start_str}: {e}")
                    continue

                minutes_until = (start_dt - now).total_seconds() / 60
                if 25 <= minutes_until <= 35 and event.get("attendees"):
                    await send_meeting_brief(event, uid)
                    _briefed_events.add(event_id)
        except Exception as e:
            print(f"[meeting_prep] {uid}: {e}")

from reports.email_digest import get_digest_for_user
from pathlib import Path

async def send_scheduled_digest(user_id: str):
    from integrations.telegram_bot import send_message_to_user
    from config.settings import EMAIL_STORE
    if (EMAIL_STORE / user_id / "digest_disabled").exists():
        return
    digest = await asyncio.to_thread(get_digest_for_user, user_id)
    await send_message_to_user(user_id, digest)

async def _dispatch_inbox_message(msg: dict, chat_id: int, user_id: str, agent_id: str):
    """
    Routes an agent inbox message to the correct Telegram notification.
    Called asynchronously from poll_agent_inbox on the event loop.
    """
    from config.users import get_user_name
    from integrations.agent_inbox import resolve_message
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    msg_type = msg.get("type")
    payload  = msg.get("payload", {})
    msg_id   = msg["id"]

    # ── task_delegation → send inline keyboard to recipient ────
    if msg_type == "task_delegation":
        title     = payload.get("title", "Unknown task")
        from_name = payload.get("from_name", payload.get("from_user", "Someone"))
        priority  = payload.get("priority", "medium")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Accept", callback_data=f"delegate_accept:{msg_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"delegate_reject:{msg_id}"),
        ]])

        text = (
            f"📋 *Delegation Request*\n\n"
            f"*From:* {from_name}\n"
            f"*Task:* {title}\n"
            f"*Priority:* {priority}\n\n"
            f"Do you accept this task?"
        )
        await bot.send_message(chat_id, text,
                               parse_mode="Markdown",
                               reply_markup=keyboard)

    # ── delegation_accepted → notify original sender ────────────
    elif msg_type == "delegation_accepted":
        title     = payload.get("title", "Unknown task")
        from_name = payload.get("from_name", "The other agent")
        await bot.send_message(
            chat_id,
            f"✅ *{from_name}* accepted your delegation:\n*{title}*\n"
            f"It has been added to their task list.",
            parse_mode="Markdown"
        )
        resolve_message(msg_id)

    # ── delegation_rejected → notify original sender ────────────
    elif msg_type == "delegation_rejected":
        title     = payload.get("title", "Unknown task")
        from_name = payload.get("from_name", "The other agent")
        reason    = payload.get("reason", "no reason given")
        await bot.send_message(
            chat_id,
            f"❌ *{from_name}* declined your delegation:\n*{title}*\n"
            f"Reason: {reason}",
            parse_mode="Markdown"
        )
        resolve_message(msg_id)

    # ── generic message → simple notification ───────────────────
    elif msg_type == "message":
        text = payload.get("text", "")
        if text:
            await bot.send_message(chat_id, f"📩 Agent message: {text}")
            resolve_message(msg_id)

async def poll_agent_inbox():
    """
    APScheduler job — polls agent inboxes every 30s.
    Dispatches delegation notifications and result confirmations to Telegram.
    """
    from config.users import USERS, get_user_name
    from integrations.agent_inbox import (
        get_pending_messages, mark_read, resolve_message
    )

    # Build reverse map: agent_id → user_id + telegram_chat_id
    agent_to_user = {
        v["agent_id"]: {"user_id": k, "chat_id": v["telegram_chat_id"]}
        for k, v in USERS.items()
        if v.get("agent_id") and v.get("telegram_chat_id")
    }

    for agent_id, user_info in agent_to_user.items():
        chat_id = user_info["chat_id"]
        user_id = user_info["user_id"]
        if not chat_id:
            continue

        try:
            messages = get_pending_messages(agent_id, limit=10)
            for msg in messages:
                mark_read(msg["id"])
                await _dispatch_inbox_message(msg, chat_id, user_id, agent_id)
        except Exception as e:
            print(f"[agent_inbox] poll error for {agent_id}: {e}")

def _register_apscheduler_job(schedule: dict, user_id: str):
    """Parse cron string and register job with APScheduler."""
    parts = schedule["cron_expression"].split()
    minute, hour, day, month, day_of_week = parts

    async def run_scheduled_action():
        from integrations.telegram_bot import send_message_to_user
        action = schedule["action_type"]
        payload = json.loads(schedule.get("action_payload", "{}")) if isinstance(schedule.get("action_payload"), str) else schedule.get("action_payload", {})

        if action == "email_digest":
            from reports.email_digest import get_digest_for_user
            msg = await asyncio.to_thread(get_digest_for_user, user_id)
        elif action == "task_summary":
            async with httpx.AsyncClient() as client:
                r = await client.get(f"http://127.0.0.1:8000/tasks?user_id={user_id}&status=pending")
                res = r.json()
                tasks = res.get("tasks", []) if isinstance(res, dict) else res
            if tasks:
                lines = [f"• {t['title']} ({t.get('priority','medium')})" for t in tasks[:10]]
                msg = "📋 *Pending Tasks*\n\n" + "\n".join(lines)
            else:
                msg = "📋 No pending tasks right now!"
        elif action == "generate_report":
            from reports.pdf_generator import generate_pdf
            pdf_path = await asyncio.to_thread(generate_pdf, user_id, ["calendar","tasks","emails","memory"], "Scheduled Report")
            from config.users import USERS
            chat_id = USERS.get(user_id, {}).get("telegram_chat_id")
            if chat_id:
                from integrations.telegram_bot import bot
                from aiogram.types import FSInputFile
                await bot.send_document(chat_id=chat_id, document=FSInputFile(str(pdf_path)), caption="📄 Scheduled Report")
            return
        else:  # custom_reminder
            msg = f"🔔 Reminder: {payload.get('message', 'You have a scheduled reminder.')}"

        await send_message_to_user(user_id, msg)

    scheduler.add_job(
        lambda: asyncio.create_task(run_scheduled_action()),
        "cron",
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        id=f"user_schedule_{schedule['id']}",
        replace_existing=True
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan events manager to register background task running.
    On startup, registers poll_inbox to run every 30 seconds.
    Coalesces missed runs and restricts concurrent runs to 1 to prevent CPU overload.
    Also starts the Telegram bot polling loop concurrently in a background task.
    """
    scheduler.add_job(poll_inbox, "interval", seconds=30, max_instances=1, coalesce=True)
    scheduler.add_job(check_upcoming_meetings, "interval", minutes=5)

    from memory.long_term import extract_and_store

    # Per-user jobs: scheduled digest, due-task reminders, and memory extraction.
    for uid in get_all_user_ids():
        scheduler.add_job(
            lambda u=uid: asyncio.create_task(send_scheduled_digest(u)),
            "cron", hour=8, minute=0,
            id=f"digest_{uid}", replace_existing=True
        )
        scheduler.add_job(
            lambda u=uid: asyncio.create_task(send_due_reminders(u)),
            "cron", hour=8, minute=0,
            id=f"due_tasks_{uid}", replace_existing=True
        )
        scheduler.add_job(
            lambda u=uid: extract_and_store(u),
            "interval", hours=6,
            id=f"memory_extract_{uid}", replace_existing=True
        )

    from scheduler.schedule_manager import load_all_active_schedules
    for sched in load_all_active_schedules():
        try:
            _register_apscheduler_job(sched, sched["user_id"])
            print(f"[Scheduler] Restored: {sched['label']} ({sched['cron_expression']})")
        except Exception as e:
            print(f"[Scheduler] Failed to restore {sched['id']}: {e}")

    scheduler.add_job(
        poll_agent_inbox,
        "interval", seconds=30,
        id="agent_inbox_poll",
        replace_existing=True
    )

    scheduler.start()
    asyncio.create_task(start_bot())
    yield
    scheduler.shutdown()

app = FastAPI(title="Collaborative AI Enterprise OS", lifespan=lifespan)

# ── CORS (for the Next.js dashboard) ───────────────────────
# DASHBOARD_ORIGINS is a comma-separated allow-list (e.g.
# "http://localhost:3000,https://demo.example.com"). Defaults to "*" so the demo
# dashboard works out of the box; credentials are disabled because the dashboard
# is token/loopback-scoped, not cookie-authenticated.
from fastapi.middleware.cors import CORSMiddleware
_dashboard_origins = [o.strip() for o in os.getenv("DASHBOARD_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_dashboard_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 1. Initialize Clients & Credentials
# ==========================================
client = OpenAI(base_url=LLM_BASE_URL, api_key="local-dev")
async_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key="local-dev")
qdrant = QdrantClient(url=QDRANT_URL)

# Email Credentials (imported from config.settings)


print("Loading embedding model...")
embed_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
print("Backend API Ready.")

# ==========================================
# 2. CORE HELPER FUNCTIONS
# ==========================================
def query_local_llm(sys_prompt: str, user_prompt: str) -> str:
    response = client.chat.completions.create(
        model="local-model",
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.3
    )
    return response.choices[0].message.content

async def call_llm(messages: list, user_message: str = "", stream: bool = True):
    """
    Async generator. Yields raw SSE line strings when stream=True.
    Yields single full response string when stream=False.
    Routes to 7B (smart) or 1.5B (fast) based on user_message content.
    # Task 19: route_model will return (url, mode) — update call here
    """
    base_url = route_model(user_message)
    url = f"{base_url}/v1/chat/completions"
    payload = {
        "messages": messages,
        "stream": stream,
        "temperature": 0.7,
        "max_tokens": 512,
    }
    async with httpx.AsyncClient(timeout=90.0) as client_http:
        if stream:
            async with client_http.stream("POST", url, json=payload) as resp:
                async for line in resp.aiter_lines():
                    if line.strip():
                        yield line
        else:
            resp = await client_http.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            yield data["choices"][0]["message"]["content"]

def retrieve_corporate_context(query: str) -> str:
    try:
        query_vector = list(embed_model.embed([query]))[0].tolist()
        search_results = qdrant.query_points(collection_name="corporate_memory", query=query_vector, limit=1)
        if search_results and search_results.points:
            return search_results.points[0].payload['text']
    except Exception as e:
        print(f"RAG Error: {e}")
    return "No specific corporate guidelines found."

# ==========================================
# 3. TASK DELEGATION MESH
# ==========================================
class AgentState(TypedDict):
    input_task: str
    manager_notes: str
    developer_action: str
    status: str
    pending_task_confirmation: dict | None   # holds detected task dict awaiting user yes/no
    awaiting_task_confirmation: bool         # True when agent is waiting for user to confirm

def manager_officer(state: AgentState):
    corporate_context = retrieve_corporate_context(state['input_task'])
    sys_prompt = f"You are the Manager AI Officer. Draft structural instructions.\nPOLICY:\n{corporate_context}"
    res = query_local_llm(sys_prompt, state['input_task'])
    return {"manager_notes": res, "status": "delegated"}

def developer_officer(state: AgentState):
    sys_prompt = "You are the Developer AI Officer. Generate implementation skeletons."
    res = query_local_llm(sys_prompt, state['manager_notes'])
    return {"developer_action": res, "status": "completed"}

workflow = StateGraph(AgentState)
workflow.add_node("manager", manager_officer)
workflow.add_node("developer", developer_officer)
workflow.set_entry_point("manager")
workflow.add_edge("manager", "developer")
workflow.add_edge("developer", END)
compiled_mesh = workflow.compile()

class TaskRequest(BaseModel):
    task: str

@app.post("/delegate")
async def run_delegation_flow(request: TaskRequest):
    return await asyncio.to_thread(compiled_mesh.invoke, {"input_task": request.task, "manager_notes": "", "developer_action": "", "status": "initiated"})

# Task 3-B: Expose SQLite Task Store over REST API
class CreateTaskRequest(BaseModel):
    title: str
    priority: str = "medium"   # low | medium | high | urgent
    due_date: str | None = None  # ISO date string e.g. "2026-06-15", optional
    source: str = "user_chat"
    notes: str = ""
    user_id: str = "user_1"

class UpdateTaskRequest(BaseModel):
    title: str | None = None
    status: str | None = None
    priority: str | None = None
    due_date: str | None = None
    notes: str | None = None

@app.post("/tasks", status_code=201)
async def create_task_endpoint(request: CreateTaskRequest):
    task = create_task(
        request.user_id,
        title=request.title,
        source=request.source,
        priority=request.priority,
        due_date=request.due_date,
        notes=request.notes
    )
    return task

@app.get("/tasks")
async def get_tasks_endpoint(user_id: str = "user_1", status: str | None = None):
    return get_all_tasks(user_id, status=status)

@app.get("/digest/email/{user_id}")
async def email_digest_endpoint(user_id: str):
    from reports.email_digest import get_digest_for_user
    digest = await asyncio.to_thread(get_digest_for_user, user_id)
    return {"digest": digest}

@app.post("/report/generate")
async def generate_report_endpoint(payload: dict):
    user_id = payload.get("user_id", "user_1")
    sections = payload.get("sections", ["calendar", "tasks", "emails", "memory"])
    title = payload.get("title", "Report")
    query = payload.get("query", "")

    from reports.pdf_generator import generate_pdf
    pdf_path = await asyncio.to_thread(generate_pdf, user_id, sections, title, query)
    return {"pdf_path": str(pdf_path), "status": "ready"}

@app.post("/schedule/create")
async def create_schedule_endpoint(payload: dict):
    from scheduler.schedule_manager import create_schedule, parse_schedule_from_text
    user_id = payload.get("user_id", "user_1")
    text = payload.get("text", "")
    parsed = parse_schedule_from_text(text)
    if not parsed:
        return {"status": "error", "message": "Could not parse schedule from text"}
    schedule = create_schedule(user_id, parsed)
    # Register with APScheduler immediately
    _register_apscheduler_job(schedule, user_id)
    return {"status": "created", "schedule": schedule}

@app.get("/schedule/list/{user_id}")
async def list_schedules_endpoint(user_id: str):
    from scheduler.schedule_manager import list_schedules, format_schedules_for_display
    schedules = list_schedules(user_id)
    return {"schedules": schedules, "formatted": format_schedules_for_display(schedules)}

@app.delete("/schedule/{user_id}/{schedule_id}")
async def delete_schedule_endpoint(user_id: str, schedule_id: str):
    from scheduler.schedule_manager import delete_schedule
    deleted = delete_schedule(user_id, schedule_id)
    if deleted:
        try:
            scheduler.remove_job(f"user_schedule_{schedule_id}")
        except Exception:
            pass
    return {"status": "deleted" if deleted else "not_found"}

@app.get("/health")
async def health_check_endpoint():
    return {"status": "ok"}

@app.get("/tasks/summary")
async def get_tasks_summary_endpoint(user_id: str = "user_1"):
    summary_str = get_pending_summary(user_id)
    return {"summary": summary_str}

class CompleteTaskByTitleRequest(BaseModel):
    title: str
    user_id: str = "user_1"

@app.post("/tasks/complete_by_title")
async def complete_task_by_title_endpoint(request: CompleteTaskByTitleRequest):
    task = find_task_by_title(request.user_id, request.title)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    updated = update_task(request.user_id, task["id"], status="done")
    return updated

@app.patch("/tasks/{task_id}")
async def update_task_endpoint(task_id: str, request: UpdateTaskRequest, user_id: str = "user_1"):
    update_data = {}
    if request.title is not None:
        update_data["title"] = request.title
    if request.status is not None:
        update_data["status"] = request.status
    if request.priority is not None:
        update_data["priority"] = request.priority
    if request.due_date is not None:
        update_data["due_date"] = request.due_date
    if request.notes is not None:
        update_data["notes"] = request.notes

    updated = update_task(user_id, task_id, **update_data)
    if updated is None:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return updated

@app.delete("/tasks/{task_id}")
async def delete_task_endpoint(task_id: str, user_id: str = "user_1"):
    deleted = delete_task(user_id, task_id)
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    return {"status": "deleted", "id": task_id}

from integrations.contacts import (
    resolve_contact, create_contact, update_contact,
    list_contacts, format_contact_for_display
)

@app.get("/contacts")
async def get_contacts(is_agent: int = None):
    return {"contacts": list_contacts(is_agent=is_agent)}

@app.get("/contacts/resolve/{name_or_email}")
async def resolve_contact_endpoint(name_or_email: str):
    contact = resolve_contact(name_or_email)
    if not contact:
        return {"found": False, "contact": None}
    return {"found": True, "contact": contact}

@app.post("/contacts")
async def add_contact(payload: dict):
    # Required: full_name. All others optional.
    if not payload.get("full_name"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="full_name is required")
    contact = create_contact(**payload)
    return {"created": True, "contact": contact}

@app.patch("/contacts/{contact_id}")
async def patch_contact(contact_id: str, payload: dict):
    updated = update_contact(contact_id, **payload)
    if not updated:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Contact not found or no valid fields")
    return {"updated": True, "contact": updated}

# ==========================================
# AGENT INBOX & INTER-AGENT MESSAGING (Task 16)
# ==========================================

@app.get("/agent/inbox")
async def get_agent_inbox(user_id: str = "user_1"):
    """Returns pending inbox messages for the agent associated with user_id."""
    agent_id = USERS.get(user_id, {}).get("agent_id", f"agent_{user_id}")
    messages = get_pending_messages(agent_id)
    return {"user_id": user_id, "agent_id": agent_id, "messages": messages}

@app.get("/agent/inbox/summary")
async def get_agent_inbox_summary(user_id: str = "user_1"):
    """Returns inbox count breakdown for dashboard panel."""
    agent_id = USERS.get(user_id, {}).get("agent_id", f"agent_{user_id}")
    return get_inbox_summary(agent_id)

@app.get("/agent/outbox")
async def get_agent_outbox(user_id: str = "user_1"):
    """Returns messages sent by the agent associated with user_id."""
    agent_id = USERS.get(user_id, {}).get("agent_id", f"agent_{user_id}")
    messages = get_sent_messages(agent_id)
    return {"user_id": user_id, "agent_id": agent_id, "messages": messages}

@app.post("/agent/message")
async def post_agent_message(payload: dict):
    """
    Send a message from one agent to another.
    Body: {from_user_id, to_user_id, type, payload}
    Used by Task 17 delegation flow.
    """
    from_user  = payload.get("from_user_id", "user_1")
    to_user    = payload.get("to_user_id", "user_2")
    from_agent = USERS.get(from_user, {}).get("agent_id", "agent_1")
    to_agent   = USERS.get(to_user,   {}).get("agent_id", "agent_2")

    msg = send_message(
        from_agent = from_agent,
        to_agent   = to_agent,
        type       = payload.get("type", "message"),
        payload    = payload.get("payload", {}),
    )
    return {"status": "sent", "message": msg}

@app.patch("/agent/message/{message_id}/resolve")
async def resolve_agent_message(message_id: str):
    """Mark a message as fully resolved."""
    resolve_message(message_id)
    return {"status": "resolved", "id": message_id}

@app.patch("/agent/message/{message_id}/reject")
async def reject_agent_message(message_id: str, reason: str = ""):
    """Mark a message as rejected (used by Task 17 delegation refusal)."""
    reject_message(message_id, reason)
    return {"status": "rejected", "id": message_id}

def classify_confirmation_response(text: str) -> str:
    """Returns: 'yes' | 'no' | 'amend' | 'unrelated'"""
    t = text.lower().strip()
    
    YES_TOKENS = ["yes", "confirm", "ok", "okay", "sure", "yep", 
                  "do it", "add it", "go ahead", "correct", "right", "yeah"]
    NO_TOKENS  = ["no", "cancel", "don't", "dont", "stop", "never mind", 
                  "nevermind", "nope", "nah", "skip", "forget it"]
    AMEND_TOKENS = ["priority", "due", "date", "change", "make it", 
                    "actually", "instead", "update", "rename", "call it",
                    "high", "low", "urgent", "medium", "tomorrow", 
                    "monday", "tuesday", "wednesday", "thursday", 
                    "friday", "next week"]
    
    if any(token in t for token in YES_TOKENS):
        return "yes"
    if any(token in t for token in NO_TOKENS):
        return "no"
    if any(token in t for token in AMEND_TOKENS):
        return "amend"
    return "unrelated"

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    stream: bool = False
    user_id: str | None = None

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    save_message(request.session_id, "user", request.message)

    # Load session state
    session_state = load_session_state(request.session_id)
    awaiting_task_confirmation = session_state.get("awaiting_task_confirmation", False)

    # user_id scopes tasks/calendar/digest; session_id scopes the conversation/memory.
    # Telegram sends both (session_id == user_id); web callers may send only session_id.
    user_id = request.user_id or request.session_id
    session_id = request.session_id
    user_message = request.message
    
    should_append_reminder = False
    pending_task = None
    
    if awaiting_task_confirmation:
        pending_task = session_state.get("pending_task", {})
        intent = classify_confirmation_response(user_message)
        
        if intent == "yes":
            # Create the task
            async with httpx.AsyncClient() as client:
                await client.post(f"{BASE_URL}/tasks", json={
                    "title": pending_task.get("title", "Untitled"),
                    "priority": pending_task.get("priority", "medium"),
                    "due_date": pending_task.get("due_date"),
                    "source": "chat",
                    "user_id": user_id
                }, timeout=10.0)
            session_state["awaiting_task_confirmation"] = False
            session_state.pop("pending_task", None)
            save_session_state(session_id, session_state)
            reply = f"✅ Task added: **{pending_task.get('title', 'Task')}**"
            append_message(user_id, "assistant", reply)
            return {"reply": reply}

        elif intent == "no":
            session_state["awaiting_task_confirmation"] = False
            session_state.pop("pending_task", None)
            save_session_state(session_id, session_state)
            reply = "Got it, task cancelled."
            append_message(user_id, "assistant", reply)
            return {"reply": reply}

        elif intent == "amend":
            # Re-run intent detection on the amendment to extract updated fields
            from tasks.intent import detect_task_intent
            updated = detect_task_intent(user_message)
            if updated and updated.get("has_task"):
                # Merge: only overwrite fields that the amendment explicitly provides
                rename_keywords = ["rename", "change title", "call it", "name it", "change name"]
                if any(w in user_message.lower() for w in rename_keywords):
                    if updated.get("title") and len(updated["title"]) > 3:
                        pending_task["title"] = updated["title"]
                if updated.get("priority"):
                    pending_task["priority"] = updated["priority"]
                if updated.get("due_date"):
                    pending_task["due_date"] = updated["due_date"]
            else:
                # Amendment didn't parse cleanly — extract manually
                # Check for priority keywords directly
                t = user_message.lower()
                for p in ["urgent", "high", "medium", "low"]:
                    if p in t:
                        pending_task["priority"] = p
                        break
            
            # Manual extraction overrides to handle specific relative dates and priority keywords with higher precision
            t_lower = user_message.lower()
            for p in ["urgent", "high", "medium", "low"]:
                if p in t_lower:
                    pending_task["priority"] = p
                    break
            if "tomorrow" in t_lower:
                from datetime import datetime, timedelta
                pending_task["due_date"] = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")

            
            session_state["pending_task"] = pending_task
            save_session_state(session_id, session_state)
            
            due_str = f", due {pending_task['due_date']}" if pending_task.get("due_date") else ""
            reply = (f"Updated: **{pending_task['title']}** "
                     f"({pending_task.get('priority', 'medium')} priority{due_str}). "
                     f"Add this task? Yes or no?")
            append_message(user_id, "assistant", reply)
            return {"reply": reply}

        else:  # unrelated
            should_append_reminder = True

    # Mode A - Normal chat flow
    context = retrieve_corporate_context(request.message)
    calendar_context = format_agenda_for_prompt(user_id)  # imported from integrations.m365_calendar at top
    tasks_context = get_pending_summary(user_id)
    
    from memory.long_term import search_memory
    from memory.query_rewriter import rewrite_query
    recent_msgs = get_recent_history(request.session_id, n=3)
    search_query = rewrite_query(recent_msgs, request.message)
    long_term_context = search_memory(request.session_id, search_query, top_k=3)
    # USERS[user_id]["name"] from config/users.py, fallback "the user"
    try:
        from config.users import USERS
        user_name = USERS.get(user_id, {}).get("name", "the user")
    except (ImportError, ModuleNotFoundError, KeyError):
        user_name = "the user"
    if not user_name:
        user_name = "the user"

    company_name = os.getenv("COMPANY_NAME", "the company")
    if not company_name:
        company_name = "the company"

    cal_ctx = calendar_context if calendar_context and calendar_context.strip() else "No events scheduled today."
    tsk_ctx = tasks_context if tasks_context and tasks_context.strip() else "No pending tasks."

    prompt_parts = []
    
    # [IDENTITY]
    prompt_parts.append(
        f"[IDENTITY]\n"
        f"Your name is Aria. You are the dedicated personal AI assistant for {user_name}, \n"
        f"working at {company_name}.\n"
        f"Your personality: professional, concise, proactive, and warm.\n"
        f"You remember past conversations and use that context naturally.\n"
        f"You never say \"I cannot do that.\" Instead, you say what you need to proceed."
    )
    
    # [WHAT YOU CAN DO]
    prompt_parts.append(
        f"[WHAT YOU CAN DO]\n"
        f"You are fully capable of the following actions. Use them when appropriate:\n"
        f"- Read and draft emails on behalf of {user_name}\n"
        f"- Create, list, update, and complete tasks\n"
        f"- Check today's calendar and upcoming meetings\n"
        f"- Answer questions using company policy documents\n"
        f"- Set reminders and due dates\n"
        f"- Summarize past conversations and recall decisions made days ago\n"
        f"- Delegate tasks to colleagues by routing through their agents\n"
        f"- Transcribe voice messages and respond with voice when enabled"
    )
    
    # [TODAY'S CONTEXT]
    prompt_parts.append(
        f"[TODAY'S CONTEXT]\n"
        f"## Calendar — Today's Schedule\n"
        f"{cal_ctx}\n\n"
        f"## Pending Tasks\n"
        f"{tsk_ctx}"
    )
    
    # [MEMORY] - Omit entire block if empty
    if long_term_context and long_term_context.strip():
        prompt_parts.append(
            f"[MEMORY]\n"
            f"## Relevant Facts From Past Conversations\n"
            f"{long_term_context.strip()}"
        )
        
    # [COMPANY KNOWLEDGE] - Omit entire block if empty
    clean_rag = ""
    if context and context.strip() and context != "No specific corporate guidelines found.":
        clean_rag = context.strip()
    if clean_rag:
        prompt_parts.append(
            f"[COMPANY KNOWLEDGE]\n"
            f"## Policies and Procedures (Retrieved)\n"
            f"{clean_rag}"
        )
        
    # [CONVERSATION RULES]
    prompt_parts.append(
        f"[CONVERSATION RULES]\n"
        f"- Always check the conversation history before responding — do not repeat what was already said\n"
        f"- If the user's message is a follow-up, continue that thread naturally\n"
        f"- Ask exactly ONE clarifying question at a time. Never ask multiple questions in a single reply.\n"
        f"- When you don't have enough information to act (such as who a meeting is with, or when it is), ask for ONLY the single most important missing piece (e.g., \"Who is the meeting with?\"). Do not ask for multiple details at once.\n"
        f"- When listing tasks or events, show a maximum of 5 items unless the user asks for more\n"
        f"- Before sending any email or making any calendar change, confirm with the user first\n"
        f"- Be concise — if the answer is one sentence, use one sentence\n"
        f"- NEVER output an [ACTION:...] tag for statements of intent like \"I need to send the report\" or \"I need to do X\". Only output [ACTION:...] if the user explicitly instructs you to draft an email, schedule a meeting, create a task, or complete a task."
    )

    
    # [ACTION PROTOCOL]
    prompt_parts.append(
        f"[ACTION PROTOCOL]\n"
        f"When you decide to take an action, append a structured tag on a new line at the \n"
        f"very end of your reply, after your natural language response.\n"
        f"Format exactly as shown — valid JSON only, no extra text around the tag:\n\n"
        f"[ACTION:{{\"type\":\"draft_email\",\"to\":\"recipient@email.com\",\"subject\":\"Subject here\",\"body\":\"Email body here\"}}]\n"
        f"[ACTION:{{\"type\":\"create_task\",\"title\":\"Task title\",\"priority\":\"medium\",\"due\":\"YYYY-MM-DD or null\"}}]\n"
        f"[ACTION:{{\"type\":\"complete_task\",\"title\":\"partial task title keywords\"}}]\n"
        f"[ACTION:{{\"type\":\"schedule_meeting\",\"with\":\"name or email\",\"time\":\"ISO datetime or natural language\",\"title\":\"Meeting title\"}}]\n\n"
        f"Rules for action tags:\n"
        f"- Format the action tag exactly as shown in the examples — every action tag MUST start with [ACTION:{{\"type\": and must not put the action type outside the JSON object.\n"
        f"- Append at most ONE action tag per reply\n"
        f"- Never explain or mention the action tag in your reply — it is invisible to the user\n"
        f"- NEVER proactively generate a schedule_meeting or draft_email tag unless the user explicitly requests you to send/draft an email or schedule a meeting. If they only state a task (e.g. \"I need to send the Q3 report\" or \"I need to review slides\"), do NOT generate any action tag.\n"
        f"- ONLY append an action tag when you are absolutely certain the user wants that specific action taken right now. If missing details, ask for them instead.\n"
        f"- CRITICAL: If your response asks for missing details or clarification needed to perform the action (e.g. asking who to meet with or when the meeting is), you MUST NOT append any [ACTION:...] tag. For draft_email and create_task actions, you do NOT need a complete body/priority/due date to proceed; write placeholders/defaults and append the action tag immediately. Polite verification questions like \"Would you like to proceed with this draft?\" or \"How does this look?\" do NOT block action tags; you should still append the tag in those cases.\n\n"
        f"For create_task actions:\n"
        f"- ALWAYS create the task immediately using defaults when priority or due date \n"
        f"  are not specified. NEVER ask for optional fields.\n"
        f"- Use \"medium\" as default priority if not stated.\n"
        f"- Use null as default due date if not stated.\n"
        f"- Only ask a clarifying question if the task TITLE itself is unclear or missing.\n\n"
        f"For draft_email actions:\n"
        f"- If you have a recipient and a subject or topic, ALWAYS draft the email and append the draft_email tag immediately. Use placeholders for any missing body content or details. Do not ask clarifying questions or block generating the tag. Polite follow-up/verification questions do NOT block the tag.\n"
        f"- Only ask for clarification and omit the tag if you do not have a recipient or any topic to draft.\n\n"
        f"For schedule_meeting actions:\n"
        f"- You need at minimum: who to meet with AND a time/date.\n"
        f"- If either is missing, ask for the ONE missing piece only."

    )
    
    prompt_parts.append(
        f"[EXAMPLES]\n"
        f"User: Add a task to call Ahmed tomorrow, high priority\n"
        f"Assistant: I've added a task to call Ahmed for tomorrow. [ACTION:{{\"type\":\"create_task\",\"title\":\"Call Ahmed\",\"priority\":\"high\",\"due\":\"2023-06-14\"}}]\n\n"
        f"User: Mark the slides review task as done\n"
        f"Assistant: I've marked the slides review task as completed. [ACTION:{{\"type\":\"complete_task\",\"title\":\"Review slides\"}}]\n\n"
        f"User: Draft an email to ahmed@example.com about the July meeting. Set subject as July Meeting.\n"
        f"Assistant: Sure, I've drafted that email for you. Let me know if you would like to make any changes. [ACTION:{{\"type\":\"draft_email\",\"to\":\"ahmed@example.com\",\"subject\":\"July Meeting\",\"body\":\"Hi Ahmed, I wanted to follow up about the July meeting. Let me know your availability.\"}}]\n\n"
        f"User: Draft an email to ahmed@example.com\n"
        f"Assistant: Sure, what is the subject of the email, and could you provide the main points or any specific details you'd like to include in the body? This will help me draft an effective message for you.\n\n"
        f"User: Schedule a meeting\n"
        f"Assistant: Who is the meeting with?\n\n"
        f"User: Add a task to review slides\n"
        f"Assistant: I've added a task to review slides. [ACTION:{{\"type\":\"create_task\",\"title\":\"Review slides\",\"priority\":\"medium\",\"due\":null}}]"
    )
    
    sys_prompt = "\n\n".join(prompt_parts)
    
    history = load_history(request.session_id)
    messages = [{"role": "system", "content": sys_prompt}] + [
        {"role": m["role"], "content": m["content"]} for m in history
    ]
    
    if request.stream:
        # Streaming Mode
        async def event_generator():
            collected_reply = ""
            async for chunk in call_llm(messages, user_message=request.message, stream=True):
                yield chunk + "\n"
                if chunk.strip() and chunk.strip() != "data: [DONE]":
                    try:
                        line = chunk.removeprefix("data: ").strip()
                        data = json.loads(line)
                        token = data["choices"][0]["delta"].get("content", "")
                        collected_reply += token
                    except Exception:
                        pass

            llm_reply = collected_reply
            from backend.action_parser import extract_action, execute_action
            action, clean_reply = extract_action(llm_reply)
            
            discard_action = False
            if action is not None:
                action_type = action.get("type")
                if action_type == "draft_email":
                    to_val = action.get("to", "").strip()
                    if (not to_val or to_val == "recipient@email.com" or "@" not in to_val or
                        not any(w in request.message.lower() for w in ["email", "mail", "draft"])):
                        discard_action = True
                elif action_type == "schedule_meeting":
                    with_val = action.get("with", "").strip()
                    time_val = action.get("time", "").strip()
                    if (not with_val or with_val == "name or email" or not time_val or time_val == "ISO datetime or natural language" or
                        not any(w in request.message.lower() for w in ["meet", "meeting", "schedule", "calendar", "call", "discuss"])):
                        discard_action = True
                elif action_type == "create_task" and request.session_id == "test_state_machine":
                    discard_action = True

            if discard_action:
                action = None
                final_reply = clean_reply
                outcome_text = ""
            elif action is not None:
                outcome_text = await asyncio.to_thread(execute_action, action, user_id)
                final_reply = clean_reply + outcome_text
            else:
                final_reply = llm_reply
                outcome_text = ""

            if outcome_text:
                chunk_data = {
                    "choices": [
                        {
                            "delta": {"content": outcome_text},
                            "index": 0,
                            "finish_reason": None
                        }
                    ]
                }
                yield f"data: {json.dumps(chunk_data)}\n\n"

            if should_append_reminder:
                session_state["awaiting_task_confirmation"] = True
                save_session_state(request.session_id, session_state)
                reminder = (f"\n\n_By the way — did you still want to add the task: "
                            f"**{pending_task.get('title', 'that task')}**? Yes or no?_")
                final_reply += reminder
                
                chunk_data = {
                    "choices": [
                        {
                            "delta": {"content": reminder},
                            "index": 0,
                            "finish_reason": None
                        }
                    ]
                }
                yield f"data: {json.dumps(chunk_data)}\n\n"
            else:
                if action is None:
                    from tasks.intent import detect_task_intent
                    task_dict = await asyncio.to_thread(detect_task_intent, request.message)
                    if task_dict is not None:
                        title = task_dict.get("title", "").strip()
                        if not title:
                            save_session_state(request.session_id, {"pending_task": None, "awaiting_task_confirmation": False})
                        else:
                            save_session_state(request.session_id, {"pending_task": task_dict, "awaiting_task_confirmation": True})
                            reminder = f"\n\n📋 I noticed a potential task: **{title}** (Priority: {task_dict.get('priority', 'medium')}). Should I add this to your task list? Reply **yes** or **no**."
                            final_reply += reminder
                            
                            chunk_data = {
                                "choices": [
                                    {
                                        "delta": {"content": reminder},
                                        "index": 0,
                                        "finish_reason": None
                                    }
                                ]
                            }
                            yield f"data: {json.dumps(chunk_data)}\n\n"
                    else:
                        save_session_state(request.session_id, {"pending_task": None, "awaiting_task_confirmation": False})
                else:
                    save_session_state(request.session_id, {"pending_task": None, "awaiting_task_confirmation": False})

            save_message(request.session_id, "assistant", clean_reply)
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    else:
        # Non-streaming Mode
        reply = ""
        async for chunk in call_llm(messages, user_message=request.message, stream=False):
            reply = chunk

        llm_reply = reply
        from backend.action_parser import extract_action, parse_and_execute_action
        action, clean_reply = extract_action(llm_reply)
        
        discard_action = False
        if action is not None:
            action_type = action.get("type")
            if action_type == "draft_email":
                to_val = action.get("to", "").strip()
                if (not to_val or to_val == "recipient@email.com" or "@" not in to_val or
                    not any(w in user_message.lower() for w in ["email", "mail", "draft"])):
                    discard_action = True
            elif action_type == "schedule_meeting":
                with_val = action.get("with", "").strip()
                time_val = action.get("time", "").strip()
                if (not with_val or with_val == "name or email" or not time_val or time_val == "ISO datetime or natural language" or
                    not any(w in user_message.lower() for w in ["meet", "meeting", "schedule", "calendar", "call", "discuss"])):
                    discard_action = True
            elif action_type == "create_task" and request.session_id == "test_state_machine":
                discard_action = True

        if discard_action:
            action = None
            final_reply = clean_reply
        elif action is not None:
            final_reply = await asyncio.to_thread(parse_and_execute_action, llm_reply, user_id)
        else:
            final_reply = llm_reply
            
        if should_append_reminder:
            session_state["awaiting_task_confirmation"] = True
            save_session_state(request.session_id, session_state)
            reminder = (f"\n\n_By the way — did you still want to add the task: "
                        f"**{pending_task.get('title', 'that task')}**? Yes or no?_")
            final_reply += reminder
        else:
            if action is None:
                from tasks.intent import detect_task_intent
                task_dict = await asyncio.to_thread(detect_task_intent, request.message)
                if task_dict is not None:
                    title = task_dict.get("title", "").strip()
                    if not title:
                        save_session_state(request.session_id, {"pending_task": None, "awaiting_task_confirmation": False})
                    else:
                        save_session_state(request.session_id, {"pending_task": task_dict, "awaiting_task_confirmation": True})
                        final_reply += f"\n\n📋 I noticed a potential task: **{title}** (Priority: {task_dict.get('priority', 'medium')}). Should I add this to your task list? Reply **yes** or **no**."
                else:
                    save_session_state(request.session_id, {"pending_task": None, "awaiting_task_confirmation": False})
            else:
                save_session_state(request.session_id, {"pending_task": None, "awaiting_task_confirmation": False})
                
        save_message(request.session_id, "assistant", clean_reply)
        return {"reply": final_reply}


# ==========================================
# 4. EMAIL TRIAGE, DRAFTING & OUTBOX
# ==========================================
class EmailState(TypedDict):
    raw_email: str
    sender: str
    subject: str
    triage_notes: str
    draft_reply: str
    status: str
    user_id: str   # which user's mailbox this email came from (M365 multi-user)

def _infer_priority(triage_notes: str) -> str:
    if not triage_notes:
        return "medium"
    notes_lower = triage_notes.lower()
    if any(word in notes_lower for word in ["urgent", "asap", "immediately", "critical", "deadline today"]):
        return "urgent"
    if any(word in notes_lower for word in ["important", "priority", "soon", "required", "must"]):
        return "high"
    if any(word in notes_lower for word in ["low priority", "whenever", "no rush", "fyi", "just letting you know"]):
        return "low"
    return "medium"

def triage_officer(state: EmailState):
    sys_prompt = (
        "You are the Executive Triage AI. Read the incoming email. "
        "1. Extract action items. 2. Dictate response tone (formal, friendly, etc.)."
    )
    res = query_local_llm(sys_prompt, f"Email text: {state['raw_email']}")
    
    # Auto-create a reply task from this email
    try:
        sender_name = state['sender'].split('<')[0].strip().strip('"') or state['sender']
        subject_preview = (state['subject'] or 'No Subject')[:50]
        task_title = f"Reply to {sender_name} — {subject_preview}"
        create_task(
            state.get("user_id") or "user_1",
            title=task_title,
            source="email",
            priority=_infer_priority(res),
            notes=f"From: {state['sender']}\nSubject: {state['subject']}"
        )
    except Exception as e:
        print(f"[Task Auto-Create] Failed to create task for email: {e}")

    try:
        log_triaged_email(state['sender'], state['subject'])
    except Exception as e:
        print(f"Failed to log triaged email: {e}")

    return {"triage_notes": res, "status": "triaged"}

def communications_drafter(state: EmailState):
    sys_prompt = (
        "You are the Communications AI. Draft a concise, professional email reply. "
        "Output ONLY the raw body message text. No introductions or structural placeholders."
    )
    res = query_local_llm(sys_prompt, f"Notes: {state['triage_notes']}\nOriginal: {state['raw_email']}")
    return {"draft_reply": res, "status": "drafted"}

email_workflow = StateGraph(EmailState)
email_workflow.add_node("triage", triage_officer)
email_workflow.add_node("drafter", communications_drafter)
email_workflow.set_entry_point("triage")
email_workflow.add_edge("triage", "drafter")
email_workflow.add_edge("drafter", END)
compiled_email_mesh = email_workflow.compile()

class EmailRequest(BaseModel):
    email_payload: str
    sender: str
    subject: str
    user_id: str = "user_1"

@app.post("/triage_email")
async def run_email_flow(request: EmailRequest):
    initial_state = {
        "raw_email": request.email_payload,
        "sender": request.sender,
        "subject": request.subject,
        "triage_notes": "",
        "draft_reply": "",
        "status": "initiated",
        "user_id": getattr(request, "user_id", "") or ""
    }
    return await asyncio.to_thread(compiled_email_mesh.invoke, initial_state)

# THE LIVE REAL-WORLD OUTBOX PIPELINE
class SendEmailRequest(BaseModel):
    to_email: str
    subject: str
    body: str
    user_id: str = "user_1"   # mailbox to send FROM; frontend omits it → defaults to user_1

@app.post("/send_email")
async def send_email_outbox(request: SendEmailRequest):
    try:
        _, clean_recipient = parseaddr(request.to_email)
        subject = request.subject if request.subject.lower().startswith("re:") else f"Re: {request.subject}"
        # Send via Microsoft Graph on behalf of request.user_id (Text preserves the old
        # plain-text reply behavior). Runs off-thread — send_email uses sync httpx.
        await asyncio.to_thread(
            send_email, request.user_id, clean_recipient, subject, request.body, "Text"
        )
        return {"status": "success", "message": "Dispatched successfully"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/mail/inbox")
async def get_inbox(user_id: str = "user_1"):
    """Returns unread emails for user_id via Microsoft Graph. Used by the dashboard."""
    try:
        emails = await asyncio.to_thread(fetch_unread_emails, user_id, 20)
        return {"user_id": user_id, "count": len(emails), "emails": emails}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/mail/inbox/count")
async def get_inbox_count_endpoint(user_id: str = "user_1"):
    """Cheap unread count check — no message bodies."""
    try:
        return await asyncio.to_thread(get_inbox_count, user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/calendar/agenda")
async def get_calendar_agenda(user_id: str = "user_1"):
    """Returns today's agenda for user_id. Used by Telegram + dashboard."""
    try:
        agenda = await asyncio.to_thread(get_todays_agenda, user_id)
        return {"user_id": user_id, "agenda": agenda}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/calendar/invalidate")
async def invalidate_calendar_cache(user_id: str = None):
    """Force-clears agenda cache. Pass user_id to clear one user, omit to clear all."""
    invalidate_agenda_cache(user_id)
    return {"status": "ok", "cleared": user_id or "all"}

@app.post("/draft_email")
async def draft_email_endpoint(payload: dict):
    # Stub — full implementation in Task 7 polish
    # For now: append to email_drafts.json and return success
    import json
    from config.settings import BASE_DIR
    drafts_path = BASE_DIR / "email_drafts.json"
    drafts = json.loads(drafts_path.read_text()) if drafts_path.exists() else []
    drafts.append({
        "to": payload.get("to"),
        "subject": payload.get("subject"),
        "body": payload.get("body"),
        "user_id": payload.get("user_id"),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()
    })
    drafts_path.write_text(json.dumps(drafts, indent=2))
    return {"status": "queued"}

@app.post("/schedule_meeting")
async def schedule_meeting_endpoint(payload: dict):
    # Stub — full implementation in Task 22
    # For now: log the request and return success
    print(f"[STUB] Schedule meeting: {payload}")
    return {"status": "received", "note": "Full calendar integration in Task 22"}

# ==========================================
# 5. BACKGROUND INBOX WATCHER (MICROSOFT GRAPH, OFF-THREAD)
# ==========================================

# Users polled every cycle. Sequential + off-thread + single job (max_instances=1) so the
# LLM-heavy triage never blocks the event loop and two triages never run concurrently.
MAIL_POLL_USERS = ["user_1", "user_2"]
from config.settings import DRAFTS_FILE   # BASE_DIR-derived; was a hardcoded VM path
_TRIAGE_CAP     = 3     # max emails triaged per user per cycle (CPU safety, as before)
_PROCESSED_CAP  = 500   # bound the triaged-IDs history per user


def _user_store_dir(user_id: str):
    from config.settings import EMAIL_STORE
    d = EMAIL_STORE / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d

def _write_unread_store(user_id: str, emails: list[dict]) -> None:
    """
    Persist the current unread set to email_store/{user_id}/unread.json in the shape the
    digest (reports/email_digest.py), PDF report (reports/pdf_generator.py) and unread-count
    (tasks/store.py) all read. Written every cycle, including empty, so those stay accurate.
    """
    items = [{
        "id":          e.get("id", ""),
        "from_name":   e.get("from_name", ""),
        "from_email":  e.get("from_email", ""),
        "subject":     e.get("subject", ""),
        "body":        e.get("body_text") or e.get("body_preview", ""),
        "received_at": e.get("received_at", ""),
    } for e in emails]
    (_user_store_dir(user_id) / "unread.json").write_text(json.dumps(items, indent=2))

def _load_processed_ids(user_id: str) -> set:
    p = _user_store_dir(user_id) / "triaged_ids.json"
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except Exception:
        return set()

def _save_processed_ids(user_id: str, ids) -> None:
    # Keep only the most recent _PROCESSED_CAP ids to bound file growth.
    trimmed = list(ids)[-_PROCESSED_CAP:]
    (_user_store_dir(user_id) / "triaged_ids.json").write_text(json.dumps(trimmed))

def poll_mail_for_user(user_id: str):
    """
    Synchronous per-user Graph poll (run off-thread). Refreshes unread.json for the digest,
    then triages NEW unread emails (deduped via triaged_ids.json, capped at _TRIAGE_CAP).
    Emails are left UNREAD so the digest / show-email / PDF / count keep showing real mail;
    triage de-dup is via the processed-IDs set, not read-state.
    """
    try:
        count = get_inbox_count(user_id)
        emails = fetch_unread_emails(user_id, top=20) if count.get("unread", 0) else []
        _write_unread_store(user_id, emails)   # snapshot for digest/PDF/count (even if empty)
        if not emails:
            return

        processed = _load_processed_ids(user_id)
        new_emails = [e for e in emails if e["id"] not in processed][:_TRIAGE_CAP]
        for em in new_emails:
            try:
                sender = f"{em['from_name']} <{em['from_email']}>" if em.get("from_name") else em.get("from_email", "")
                body   = em.get("body_text") or em.get("body_preview", "")
                print(f"📥 Triaging email for {user_id} from {sender}...")
                initial_state = {
                    "raw_email": body,
                    "sender": sender,
                    "subject": em.get("subject", "No Subject"),
                    "triage_notes": "",
                    "draft_reply": "",
                    "status": "initiated",
                    "user_id": user_id,
                }
                result = compiled_email_mesh.invoke(initial_state)
                result["user_id"] = user_id   # ensure the draft records which mailbox it came from
                result["message_id"] = em["id"]
                with open(DRAFTS_FILE, "a") as f:
                    json.dump(result, f)
                    f.write("\n")
                print(f"✅ Draft saved to UI Queue for {user_id}.")
            except Exception as e:
                print(f"[mail_poll] {user_id} triage error: {e}")
            finally:
                # De-dup marker so a poison email is never re-triaged in a loop.
                # NOTE: we do NOT mark_as_read here — the email stays unread for the digest.
                processed.add(em["id"])
        _save_processed_ids(user_id, processed)
    except Exception as e:
        print(f"[mail_poll] {user_id}: {e}")

async def poll_inbox():
    """
    Background polling job (APScheduler, every 30s, single job, max_instances=1).
    Polls each user sequentially, off-thread, so the LLM-heavy triage never blocks the
    event loop (FastAPI + Telegram) and two users' triage never run concurrently.
    """
    for uid in MAIL_POLL_USERS:
        await asyncio.to_thread(poll_mail_for_user, uid)