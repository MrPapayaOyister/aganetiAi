"""Tool registry — typed, per-agent-permissioned, injection-aware.

Every tool declares a JSON-schema for its args, the permission it requires, and
whether it is `is_outbound` (touches an external system). `is_outbound` is the
SINGLE chokepoint for the hard approval gate: the executor never calls an outbound
tool's handler directly — it pauses the run and emits an approval. Only after the
user approves does the handler run (executing the real external action).

Read/internal tools run inline in the loop. Outbound tool handlers are written to
perform the REAL action (Gmail/Graph via the internal API) and are invoked solely
on approval.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

try:
    from config.settings import BASE_DIR
    _DB = str(BASE_DIR / "tasks" / "tasks.db")
except Exception:
    _DB = "tasks/tasks.db"

_INTERNAL_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
_SELF = "http://127.0.0.1:8000"


class ApprovalRequired(Exception):
    def __init__(self, action_type: str, payload: dict, preview: str):
        self.action_type, self.payload, self.preview = action_type, payload, preview
        super().__init__(f"approval required: {action_type}")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Awaitable[str]]
    required_permission: str = ""
    is_outbound: bool = False


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def openai_schemas(allowed: list[str] | None = None) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in _REGISTRY.values() if allowed is None or t.name in allowed]


def all_names() -> list[str]:
    return list(_REGISTRY.keys())


def all_permissions() -> set[str]:
    """Every distinct `required_permission` declared by a registered tool.

    This is the vocabulary an operator may grant instead of enumerating tool names.
    It exists because the field used to be decorative: nothing read it, and the
    permissions route dropped any grant that was not a tool name, so "email.read"
    could not even be stored. See backend/orchestrator/authz.grant_matches.
    """
    return {t.required_permission for t in _REGISTRY.values() if t.required_permission}


def tools_for_permission(permission: str) -> list[str]:
    """Tool names covered by one permission — for showing an operator what a grant
    actually confers before they save it."""
    if not permission:
        return []
    return sorted(t.name for t in _REGISTRY.values() if t.required_permission == permission)


def preview(name: str, args: dict) -> str:
    """Human-readable one-liner for the approval card."""
    if name == "send_email":
        return f"Send email to {args.get('to')} — subject: {args.get('subject')!r}"
    if name == "create_calendar_event":
        return f"Create calendar event {args.get('title')!r} at {args.get('start')} with {args.get('attendees')}"
    if name == "web_search":
        return f"Search the web for: {args.get('query')!r} (top {args.get('max_results', 5)} results)"
    return f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"


async def _internal_post(path: str, body: dict, user_id: str) -> tuple[int, str]:
    import httpx
    headers = {"X-Internal-Token": _INTERNAL_TOKEN, "X-Internal-User": user_id}
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post(f"{_SELF}{path}", json=body, headers=headers)
    return r.status_code, r.text[:200]


# ── read / internal tools ─────────────────────────────────────────────────────

def _query_tasks(user_id: str, status: str | None) -> list[dict]:
    # Route through the dual-backend task store (Postgres when cut over) so the
    # list_tasks tool never reads a stale SQLite copy after the tasks cutover.
    from tasks.store import get_all_tasks
    rows = get_all_tasks(user_id, status)
    return [{"title": r.get("title"), "status": r.get("status"),
             "priority": r.get("priority"), "due_date": r.get("due_date")} for r in rows[:25]]


async def _list_tasks(ctx, status: str | None = None) -> str:
    rows = await asyncio.to_thread(_query_tasks, ctx["user_id"], status)
    if not rows:
        return "No tasks found."
    return f"{len(rows)} task(s):\n" + "\n".join(
        f"- [{r['priority']}] {r['title']} ({r['status']}"
        + (f", due {r['due_date']}" if r.get('due_date') else "") + ")" for r in rows)


async def _get_agenda(ctx, days_ahead: int = 2) -> str:
    try:
        from backend.services import mailbox
        events = await mailbox.agenda(ctx["user_id"], days_ahead=days_ahead)
    except Exception as e:
        return f"Calendar unavailable ({e}). The user may need to connect Google in Settings."
    if not events:
        return "No upcoming events."
    return "Upcoming events:\n" + "\n".join(
        f"- {e.get('title', '(untitled)')} at {e.get('start', '')}" for e in events[:8])


async def _list_emails(ctx, max_results: int = 10) -> str:
    try:
        from backend.services.mailbox import inbox as read_inbox
        emails = await read_inbox(ctx["user_id"], max_results=max_results)
    except Exception as e:
        return f"Email unavailable ({e}). The user may need to connect Google in Settings."
    if not emails:
        return "No recent emails found (the inbox is empty or Google isn't connected — check Settings)."
    lines = []
    for e in emails[:max_results]:
        mark = "•" if not e.get("is_read") else " "
        star = "★" if e.get("is_important") else ""
        subj = e.get("subject") or "(no subject)"
        frm = e.get("from_name") or e.get("from_email") or "?"
        # include the id so read_email can be called on a specific message
        lines.append(f"{mark}{star} [{e.get('id')}] {subj} — {frm}")
    return f"{len(emails)} recent email(s) (use the [id] with read_email):\n" + "\n".join(lines)


async def _search_documents(ctx, query: str) -> str:
    try:
        from backend.ingest import search_corporate
        # ACL: scope to the caller's own docs + the shared org corpus (never other users').
        hits = await asyncio.to_thread(search_corporate, query, 6, ctx.get("user_id"))
    except Exception as e:
        return f"Document search unavailable ({e})."
    lines = []
    for h in (hits or []):
        src = h.get("source") or "?"
        txt = " ".join((h.get("text") or "").split())
        if txt:
            lines.append(f"[{src}] {txt[:500]}")
    if not lines:
        return "No matching document content found — the user may need to upload the file first."
    return "Relevant excerpts from the user's documents:\n" + "\n\n".join(lines)


async def _search_memory(ctx, query: str) -> str:
    try:
        from memory.long_term import search_memory
        mem = await asyncio.to_thread(search_memory, ctx["user_id"], query, 5)
    except Exception as e:
        return f"Memory search unavailable ({e})."
    return (mem or "").strip() or "No relevant long-term memory found."


async def _draft_email(ctx, to: str, subject: str, body: str) -> str:
    # Non-outbound: produces a draft for the user to review. Nothing is sent.
    return f"Draft ready (not sent):\nTo: {to}\nSubject: {subject}\n\n{body}"


async def _now(ctx) -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%A, %B %d, %Y at %I:%M %p %Z")


async def _calc(ctx, expr: str) -> str:
    import ast, operator as op
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
           ast.Pow: op.pow, ast.Mod: op.mod, ast.USub: op.neg}

    def ev(n):
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.BinOp):
            return ops[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp):
            return ops[type(n.op)](ev(n.operand))
        raise ValueError("bad expr")
    try:
        return str(ev(ast.parse(expr, mode="eval").body))
    except Exception as e:
        return f"error: {e}"


async def _set_reminder(ctx, message: str, remind_at: str) -> str:
    from backend import reminders
    res = await asyncio.to_thread(reminders.add, ctx["user_id"], message, remind_at)
    if res.get("ok"):
        try:
            when = datetime.fromisoformat(res["fire_at"]).astimezone().strftime("%A %I:%M %p")
        except Exception:
            when = res.get("fire_at", "")
        return f'Reminder set: "{message}" for {when}. It will appear in your dashboard notifications.'
    return "I could not understand the time. Try 'in 30 minutes', 'at 5pm', or 'tomorrow 9am'."


# ── outbound tools (handler runs ONLY after approval) ─────────────────────────

async def _send_email(ctx, to: str, subject: str, body: str) -> str:
    code, txt = await _internal_post("/send_email",
                                     {"to_email": to, "subject": subject, "body": body,
                                      "user_id": ctx["user_id"]}, ctx["user_id"])
    return "Email sent." if code < 300 else f"Send failed ({code}): {txt}"


async def _create_calendar_event(ctx, title: str, start: str, attendees: str = "") -> str:
    code, txt = await _internal_post("/schedule_meeting",
                                     {"title": title, "time": start, "with": attendees,
                                      "user_id": ctx["user_id"]}, ctx["user_id"])
    return "Calendar event created." if code < 300 else f"Create failed ({code}): {txt}"


# ── registration ──────────────────────────────────────────────────────────────
register(Tool("list_tasks",
              "List the current user's tasks, optionally filtered by status (pending, done).",
              {"type": "object", "properties": {"status": {"type": "string", "enum": ["pending", "done"]}}},
              _list_tasks, "tasks.read"))
register(Tool("get_agenda",
              "Get the user's upcoming calendar events for the next N days.",
              {"type": "object", "properties": {"days_ahead": {"type": "integer"}}},
              _get_agenda, "calendar.read"))
register(Tool("list_emails",
              "List the user's recent inbox emails (subject + sender, with unread/important marks). "
              "Use for 'show/list/fetch my emails', 'what's in my inbox', 'any new mail', 'recent emails'.",
              {"type": "object", "properties": {"max_results": {"type": "integer"}}},
              _list_emails, "email.read"))
register(Tool("search_documents",
              "Search the user's uploaded documents / knowledge base and use the excerpts to answer "
              "questions about or SUMMARIZE an uploaded file (e.g. a PDF the user shared in chat). "
              "Use whenever the user refers to a document, file, PDF, or 'the attachment'.",
              {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
              _search_documents, "documents.read"))
register(Tool("search_memory",
              "Search the user's long-term memory for relevant facts/preferences.",
              {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
              _search_memory, "memory.read"))
register(Tool("draft_email",
              "Draft an email for the user to review (does NOT send).",
              {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"},
                                                 "body": {"type": "string"}}, "required": ["to", "subject", "body"]},
              _draft_email, "email.read"))
register(Tool("current_time", "Get the current date and time.",
              {"type": "object", "properties": {}}, _now, ""))
register(Tool("calc", "Evaluate an arithmetic expression.",
              {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]},
              _calc, ""))
register(Tool("set_reminder",
              "Set a reminder that appears in the user's dashboard notifications at a future time. "
              "Use when the user says 'remind me to X at/in Y'. remind_at accepts an ISO 8601 datetime "
              "OR a phrase like 'in 30 minutes', 'in 2 hours', 'at 5pm', 'tomorrow 9am'.",
              {"type": "object", "properties": {"message": {"type": "string"}, "remind_at": {"type": "string"}},
               "required": ["message", "remind_at"]},
              _set_reminder, ""))
# outbound
register(Tool("send_email",
              "Send an email on the user's behalf. OUTBOUND — requires user approval.",
              {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"},
                                                 "body": {"type": "string"}}, "required": ["to", "subject", "body"]},
              _send_email, "email.send", is_outbound=True))
register(Tool("create_calendar_event",
              "Create a calendar event / schedule a meeting. OUTBOUND — requires user approval.",
              {"type": "object", "properties": {"title": {"type": "string"}, "start": {"type": "string"},
                                                 "attendees": {"type": "string"}}, "required": ["title", "start"]},
              _create_calendar_event, "calendar.write", is_outbound=True))
