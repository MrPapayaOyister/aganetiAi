"""Specialist agent templates + delegation-as-a-tool (the real agent mesh).

The primary agent delegates a focused subtask by calling the `delegate` tool,
which runs a specialist sub-agent (its own persona + tool allowlist) as a NESTED
executor run and returns the result. Primary → specialist, real and dynamic.

These in-code templates are the seed set; Phase B moves them to the DB `agents`
table (per-user, with permission sets) but the executor contract is unchanged.
"""
from __future__ import annotations

from .registry import Tool, register

SPECIALISTS: dict[str, dict] = {
    "calendar_agent": {
        "name": "Calendar Agent",
        "prompt": "You are a calendar specialist. Use get_agenda / current_time to answer "
                  "scheduling questions. Be concise and factual.",
        "tools": ["get_agenda", "current_time"],
    },
    "research_agent": {
        "name": "Research Agent",
        "prompt": "You are a research specialist. Use search_memory to recall what the user "
                  "knows. Report what you found; never invent facts.",
        "tools": ["search_memory"],
    },
    "task_agent": {
        "name": "Task Agent",
        "prompt": "You are a task specialist. Use list_tasks to report on the user's workload "
                  "concisely.",
        "tools": ["list_tasks"],
    },
    "email_agent": {
        "name": "Email Agent",
        "prompt": "You are an email specialist. Draft with draft_email. To actually send, use "
                  "send_email — that is an outbound action requiring the user's approval.",
        "tools": ["draft_email", "send_email"],
    },
}


async def _delegate(ctx, to_agent: str, task: str) -> str:
    spec = SPECIALISTS.get(to_agent)
    if not spec:
        return f"error: no specialist '{to_agent}'. Available: {list(SPECIALISTS)}"
    from . import graph  # late import — avoids agents↔graph cycle
    sub = {"id": to_agent, "tools": spec["tools"]}
    res = await graph.run_turn(user_id=ctx["user_id"], agent=sub, user_message=task,
                               system_prompt=spec["prompt"], session_id=f"deleg:{to_agent}")
    if res["status"] == "awaiting_approval":
        return (f"[{spec['name']} prepared an action needing your approval: "
                f"{res['approval']['preview']}]")
    return f"[{spec['name']}] {res['final']}"


register(Tool(
    name="delegate",
    description=("Delegate a focused subtask to a specialist sub-agent and get its result. "
                 "Specialists: calendar_agent (schedule), research_agent (memory/recall), "
                 "task_agent (tasks), email_agent (email). Use when a subtask fits a domain."),
    parameters={"type": "object", "properties": {
        "to_agent": {"type": "string", "enum": list(SPECIALISTS.keys())},
        "task": {"type": "string", "description": "the subtask in natural language"}},
        "required": ["to_agent", "task"]},
    handler=_delegate, required_permission="delegate",
))


def specialist_names() -> list[str]:
    return list(SPECIALISTS.keys())
