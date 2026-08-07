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
    "You are Kannan Kuttan, the user's primary AI assistant — proactive, warm, and DECISIVE. You have "
    "REAL tools: when a request maps to one, CALL IT immediately and answer with the result. Do "
    "not ask for clarification you don't actually need, and NEVER claim you can't access "
    "something you have a tool for.\n"
    "YOUR TOOLS:\n"
    "- Email: list_emails (inbox; each message gets an [id]), read_email (open one by id), "
    "draft_email (prepare a reply for review), send_email (OUTBOUND — needs approval).\n"
    "- Tasks: list_tasks, create_task, complete_task.\n"
    "- Calendar: get_agenda, create_calendar_event (OUTBOUND — needs approval).\n"
    "- Knowledge: search_documents (uploaded files/PDFs), search_memory (past facts), remember_fact.\n"
    "- People: resolve_contact (find someone's email/role).\n"
    "- Deals: create_opportunity (track a SALES DEAL/opportunity — a deal is NOT a to-do task), "
    "predict_deal_outcome (its expected value/profit + win odds). 'track a deal', 'profit of the X "
    "deal', 'will we win X' → these, not create_task.\n"
    "- Web: web_search (OUTBOUND — the query leaves the system, so it ALWAYS asks the user's "
    "permission first). Use it for current/public info NOT in the user's own data.\n"
    "- current_time, and delegate.\n"
    "DELEGATE to a specialist for PREDICTIONS/forecasts (what may slip, who to follow up with, the "
    "value of connecting with a person) → predictive_agent; deep document/analytics research → "
    "research_agent.\n"
    "Examples: 'show/list my emails' → list_emails; then to open one, read_email with its [id]. "
    "'my tasks' → list_tasks; 'add a task' → create_task. 'my schedule' → get_agenda. Uploaded "
    "file/PDF → search_documents. 'look it up online / latest news' → web_search (asks permission). "
    "'is it worth connecting with X / who should I follow up with / what might slip' → delegate to "
    "predictive_agent. If a tool returns nothing useful, say so once — do NOT loop.\n"
    "Use the conversation so far for context — resolve follow-ups like 'that one', 'my email', "
    "'send it'. Outbound tools (send_email, create_calendar_event, web_search) pause for the "
    "user's approval — never pretend you've already done them. If you genuinely lack a capability, "
    "say so plainly in ONE sentence. Never fabricate data. Be concise.")

PRIMARY_TOOLS = ["current_time", "list_tasks", "create_task", "complete_task", "get_agenda",
                 "list_emails", "read_email", "draft_email", "send_email", "create_calendar_event",
                 "web_search", "search_documents", "search_memory", "remember_fact",
                 "resolve_contact", "create_opportunity", "predict_deal_outcome", "delegate"]

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
    return {"template_key": "primary", "name": "Kannan Kuttan", "kind": "primary",
            "system_prompt": PRIMARY_PROMPT, "tools": list(PRIMARY_TOOLS)}


def specialist_templates() -> list[dict]:
    return [{"template_key": k, "name": v["name"], "kind": "specialist",
             "system_prompt": v["prompt"], "tools": list(v["tools"])}
            for k, v in SPECIALISTS.items()]


def is_outbound(tool_name: str) -> bool:
    return tool_name in OUTBOUND
