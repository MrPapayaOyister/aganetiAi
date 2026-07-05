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


def preview(name: str, args: dict) -> str:
    """Human-readable one-liner for the approval card."""
    if name == "send_email":
        return f"Send email to {args.get('to')} — subject: {args.get('subject')!r}"
    if name == "create_calendar_event":
        return f"Create calendar event {args.get('title')!r} at {args.get('start')} with {args.get('attendees')}"
    return f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"


async def _internal_post(path: str, body: dict, user_id: str) -> tuple[int, str]:
    import httpx
    headers = {"X-Internal-Token": _INTERNAL_TOKEN, "X-Internal-User": user_id}
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post(f"{_SELF}{path}", json=body, headers=headers)
    return r.status_code, r.text[:200]


# ── read / internal tools ─────────────────────────────────────────────────────

def _query_tasks(user_id: str, status: str | None) -> list[dict]:
    con = sqlite3.connect(_DB, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        sql = "SELECT title, status, priority, due_date FROM tasks WHERE user_id=?"
        args: list[Any] = [user_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += (" ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
                "WHEN 'medium' THEN 2 ELSE 3 END, due_date LIMIT 25")
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


async def _list_tasks(ctx, status: str | None = None) -> str:
    rows = await asyncio.to_thread(_query_tasks, ctx["user_id"], status)
    if not rows:
        return "No tasks found."
    return f"{len(rows)} task(s):\n" + "\n".join(
        f"- [{r['priority']}] {r['title']} ({r['status']}"
        + (f", due {r['due_date']}" if r.get('due_date') else "") + ")" for r in rows)


async def _get_agenda(ctx, days_ahead: int = 2) -> str:
    try:
        from backend.services import gcalendar
        events = await gcalendar.get_google_agenda(ctx["user_id"], days_ahead=days_ahead)
    except Exception as e:
        return f"Calendar unavailable ({e}). The user may need to connect Google in Settings."
    if not events:
        return "No upcoming events."
    return "Upcoming events:\n" + "\n".join(
        f"- {e.get('title', '(untitled)')} at {e.get('start', '')}" for e in events[:8])


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
