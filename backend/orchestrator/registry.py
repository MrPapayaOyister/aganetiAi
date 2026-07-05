"""Tool registry — typed, per-agent-permissioned, injection-aware.

Every tool declares a JSON-schema for its args, the permission it requires, and
whether it is `is_outbound` (touches an external system). The executor uses
`is_outbound` as the single chokepoint for the hard approval gate: an outbound
tool never executes directly — it raises ApprovalRequired, which the graph turns
into an `approvals` row + interrupt (added in the next increment).

v1 tools here are real + read-only/internal (safe to run in the loop). Outbound
tools (draft/send email, calendar write, delegate-with-side-effects) are added
alongside the approval interrupt.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

try:
    from config.settings import BASE_DIR
    _DB = str(BASE_DIR / "tasks" / "tasks.db")
except Exception:  # standalone / test
    _DB = "tasks/tasks.db"


class ApprovalRequired(Exception):
    """Raised by an outbound tool. Carries the exact action to approve+execute."""
    def __init__(self, tool_key: str, action_type: str, payload: dict, preview: str):
        self.tool_key, self.action_type, self.payload, self.preview = tool_key, action_type, payload, preview
        super().__init__(f"approval required: {action_type}")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                         # JSON schema for args
    handler: Callable[..., Awaitable[str]]    # async (ctx, **args) -> str
    required_permission: str = ""
    is_outbound: bool = False


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def openai_schemas(allowed: list[str] | None = None) -> list[dict]:
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in _REGISTRY.values()
        if allowed is None or t.name in allowed
    ]


def all_names() -> list[str]:
    return list(_REGISTRY.keys())


# ── real v1 tools ─────────────────────────────────────────────────────────────

def _query_tasks(user_id: str, status: str | None) -> list[dict]:
    con = sqlite3.connect(_DB, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        sql = "SELECT title, status, priority, due_date FROM tasks WHERE user_id=?"
        args: list[Any] = [user_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, due_date LIMIT 25"
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


async def _list_tasks(ctx: dict, status: str | None = None) -> str:
    rows = await asyncio.to_thread(_query_tasks, ctx["user_id"], status)
    if not rows:
        return "No tasks found."
    lines = [f"- [{r['priority']}] {r['title']} ({r['status']}"
             + (f", due {r['due_date']}" if r.get('due_date') else "") + ")" for r in rows]
    return f"{len(rows)} task(s):\n" + "\n".join(lines)


async def _calc(ctx: dict, expr: str) -> str:
    import ast, operator as op
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
           ast.Pow: op.pow, ast.Mod: op.mod, ast.USub: op.neg}

    def ev(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp):
            return ops[type(node.op)](ev(node.operand))
        raise ValueError("unsupported expression")
    try:
        return str(ev(ast.parse(expr, mode="eval").body))
    except Exception as e:
        return f"error: {e}"


async def _now(ctx: dict) -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%A, %B %d, %Y at %I:%M %p %Z")


register(Tool(
    name="list_tasks",
    description="List the current user's tasks, optionally filtered by status (pending, done). Use this whenever the user asks about their tasks, to-dos, or workload.",
    parameters={"type": "object", "properties": {
        "status": {"type": "string", "enum": ["pending", "done"], "description": "optional status filter"}}},
    handler=_list_tasks, required_permission="tasks.read",
))
register(Tool(
    name="calc",
    description="Evaluate an arithmetic expression (e.g. '17*24', '(3+4)/2').",
    parameters={"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]},
    handler=_calc, required_permission="",
))
register(Tool(
    name="current_time",
    description="Get the current date and time.",
    parameters={"type": "object", "properties": {}},
    handler=_now, required_permission="",
))
