"""Specialist agent templates + delegation-as-a-tool (the real agent mesh).

The primary agent delegates a focused subtask by calling the `delegate` tool,
which runs a specialist sub-agent (its own persona + tool allowlist) as a NESTED
executor run and returns the result. Primary → specialist, real and dynamic.

These in-code templates are the seed set; Phase B moves them to the DB `agents`
table (per-user, with permission sets) but the executor contract is unchanged.
"""
from __future__ import annotations

from .registry import Tool, register

# Specialist skill-packs (read-only / non-outbound — outbound tools stay on the
# primary where the approval gate applies directly). Each pack is small + focused
# so the sub-agent picks among ~3 tools near-perfectly (the scope-test design).
SPECIALISTS: dict[str, dict] = {
    "calendar_agent": {
        "name": "Calendar Agent",
        "prompt": "You are a calendar specialist. Use get_agenda / next_event / current_time to "
                  "answer scheduling questions. Be concise and factual.",
        "tools": ["get_agenda", "next_event", "current_time"],
    },
    "research_agent": {
        "name": "Research Agent",
        "prompt": "You are a research specialist. Use search_memory (past facts) and "
                  "search_documents (the user's uploaded files) to answer, and get_analytics for "
                  "usage stats. Report what you found; never invent facts.",
        "tools": ["search_memory", "search_documents", "get_analytics"],
    },
    "task_agent": {
        "name": "Task Agent",
        "prompt": "You are a task specialist. Use list_tasks to report workload, create_task to add "
                  "one, and complete_task to close one. Be concise.",
        "tools": ["list_tasks", "create_task", "complete_task"],
    },
    "email_agent": {
        "name": "Email Agent",
        "prompt": "You are an email specialist. Use list_emails to see the inbox, read_email to open "
                  "one, email_digest to summarise, and draft_email to prepare a reply for the user "
                  "to review. You cannot send directly — the primary sends with the user's approval.",
        "tools": ["list_emails", "read_email", "email_digest", "draft_email"],
    },
    "analyst_agent": {
        "name": "Analyst Agent",
        "prompt": "You are a data analyst. Use run_python to compute real answers in a secure sandbox "
                  "(calculations, statistics, parsing/analyzing CSV/JSON or uploaded data) and "
                  "search_documents to pull the data from the user's files. Always compute — never "
                  "guess numbers. Show the key result clearly.",
        "tools": ["run_python", "search_documents"],
    },
    "predictive_agent": {
        "name": "Predictive Agent",
        "prompt": "You are a predictive-insights specialist. Tools: predict_task_slippage (what may "
                  "slip), predict_followups (who to chase), predict_relationship_value (benefit of "
                  "investing in a person), predict_deal_outcome (expected value/profit of a tracked "
                  "deal), create_opportunity (record a deal so it can be forecast), list_opportunities. "
                  "For a deal forecast the deal must exist — if 'no deal on file', offer to create it. "
                  "Every tool computes the numbers from real data and returns an evidence block: narrate "
                  "ONLY those figures, state the confidence, and NEVER invent probabilities or amounts.",
        "tools": ["predict_task_slippage", "predict_followups", "predict_relationship_value",
                  "predict_deal_outcome", "create_opportunity", "list_opportunities"],
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
                 "Specialists: calendar_agent (schedule/agenda), research_agent (memory, documents, "
                 "analytics), task_agent (tasks), email_agent (inbox reading/summaries/drafts), "
                 "predictive_agent (forecasts: what may slip, who to follow up with, the value of "
                 "connecting with a person), analyst_agent (run Python to calculate / analyze data / "
                 "crunch numbers). Delegate any prediction/forecast to predictive_agent, and any "
                 "'calculate / run code / analyze this data' request to analyst_agent. Use when a "
                 "subtask fits a domain."),
    parameters={"type": "object", "properties": {
        "to_agent": {"type": "string", "enum": list(SPECIALISTS.keys())},
        "task": {"type": "string", "description": "the subtask in natural language"}},
        "required": ["to_agent", "task"]},
    handler=_delegate, required_permission="delegate",
))


def specialist_names() -> list[str]:
    return list(SPECIALISTS.keys())


# Register the capability-sprint skills (side-effect import — must come after the
# registry is defined). Imported here because __init__ already imports agents.
from . import skills  # noqa: E402,F401
