"""Canonical agent templates — the single source of truth for the seed set.

Onboarding seeds DB Agent rows from these; the executor route falls back to the
primary template only when a user has no DB agent yet (pre-seed). Permissions are
stored as TOOL NAMES (matching the executor's allowlist, which gates by
registry Tool.name), with an explicit outbound set that is approval-gated.

Editing an agent in the DB overrides these defaults for that user; template_key
ties a DB row back to its origin template so re-seeding stays idempotent.
"""
from __future__ import annotations

PRIMARY_PROMPT = (
    "You are the user's Primary AI assistant — their chief of staff. Use tools to answer "
    "with real data; delegate domain subtasks to specialist agents via the delegate tool. "
    "Sending email and creating calendar events are OUTBOUND actions requiring the user's "
    "explicit approval: call the tool and the system pauses for their approval — do not "
    "pretend you have sent anything. Never fabricate data. Be concise and professional.")

PRIMARY_TOOLS = ["list_tasks", "get_agenda", "search_memory", "draft_email", "send_email",
                 "create_calendar_event", "delegate", "current_time", "calc"]

# Tools that touch an external system → ALWAYS approval-gated (the non-negotiable gate).
# The executor's authority is registry Tool.is_outbound; this set mirrors it for seeding
# AgentPermission.is_outbound and is the floor (per-agent perms may add, never remove).
OUTBOUND = {"send_email", "create_calendar_event"}

# Tools every agent may use without an explicit grant (no external effect, no data access).
ALWAYS_ALLOWED = ["current_time", "calc"]

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


def primary_template() -> dict:
    return {"template_key": "primary", "name": "Aria", "kind": "primary",
            "system_prompt": PRIMARY_PROMPT, "tools": list(PRIMARY_TOOLS)}


def specialist_templates() -> list[dict]:
    return [{"template_key": k, "name": v["name"], "kind": "specialist",
             "system_prompt": v["prompt"], "tools": list(v["tools"])}
            for k, v in SPECIALISTS.items()]


def is_outbound(tool_name: str) -> bool:
    return tool_name in OUTBOUND
