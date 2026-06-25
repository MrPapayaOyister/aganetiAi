"""
Builds the identity-aware agent context for each chat request.
The LLM only sees tools the user is actually authorized to use.

Usage:
    ctx = await build_agent_context(user)
    system_prompt = ctx.system_prompt
    tools = ctx.tool_definitions   # list of OpenAI-format tool dicts
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from backend.auth.context import UserContext


# ── Tool definitions (OpenAI function-calling format) ──────────

_ALL_TOOLS: dict[str, dict[str, Any]] = {

    "tasks_read": {
        "type": "function",
        "function": {
            "name": "get_tasks",
            "description": "List the user's tasks, optionally filtered by status or priority.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status":   {"type": "string", "enum": ["pending","in_progress","done","all"]},
                    "priority": {"type": "string", "enum": ["urgent","high","medium","low"]},
                },
            },
        },
    },

    "tasks_write": {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a new task for the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":    {"type": "string"},
                    "priority": {"type": "string", "enum": ["urgent","high","medium","low"]},
                    "due_date": {"type": "string", "description": "ISO date, e.g. 2025-07-01"},
                    "notes":    {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },

    "files_search": {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search the user's uploaded documents using semantic search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },

    "gmail_read": {
        "type": "function",
        "function": {
            "name": "get_emails",
            "description": "Fetch recent Gmail messages for the user. Returns subject, sender, snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 10},
                    "unread_only": {"type": "boolean", "default": True},
                },
            },
        },
    },

    "gmail_send": {
        "type": "function",
        "function": {
            "name": "send_email_gmail",
            "description": "Draft and send an email via the user's Gmail account. Always confirm with the user before sending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to":      {"type": "string"},
                    "subject": {"type": "string"},
                    "body":    {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },

    "gcal_read": {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": "Fetch upcoming Google Calendar events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {"type": "integer", "default": 7},
                    "max_results": {"type": "integer", "default": 10},
                },
            },
        },
    },

    "gcal_write": {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a Google Calendar event. Confirm with user before creating.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":        {"type": "string"},
                    "start":        {"type": "string", "description": "ISO 8601 datetime"},
                    "end":          {"type": "string", "description": "ISO 8601 datetime"},
                    "attendees":    {"type": "array", "items": {"type": "string"}},
                    "description":  {"type": "string"},
                },
                "required": ["title", "start", "end"],
            },
        },
    },

    "m365_mail_read": {
        "type": "function",
        "function": {
            "name": "get_emails_m365",
            "description": "Fetch recent Outlook / Microsoft 365 emails.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 10},
                    "unread_only": {"type": "boolean", "default": True},
                },
            },
        },
    },

    "m365_mail_send": {
        "type": "function",
        "function": {
            "name": "send_email_m365",
            "description": "Send email via Outlook / Microsoft 365. Always confirm before sending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to":      {"type": "string"},
                    "subject": {"type": "string"},
                    "body":    {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },

    "m365_cal_read": {
        "type": "function",
        "function": {
            "name": "get_calendar_events_m365",
            "description": "Fetch upcoming Microsoft 365 calendar events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead":  {"type": "integer", "default": 7},
                    "max_results": {"type": "integer", "default": 10},
                },
            },
        },
    },

    "m365_contacts_read": {
        "type": "function",
        "function": {
            "name": "search_contacts_m365",
            "description": "Search Microsoft 365 contacts by name or email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },

    "analytics": {
        "type": "function",
        "function": {
            "name": "get_analytics_summary",
            "description": "Get the user's productivity analytics: email volume, task completion rate, meeting load.",
            "parameters": {"type": "object", "properties": {}},
        },
    },

    "agent_messaging": {
        "type": "function",
        "function": {
            "name": "send_agent_message",
            "description": "Dispatch a task to another agent in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent":    {"type": "string"},
                    "message_type": {"type": "string"},
                    "payload":     {"type": "object"},
                },
                "required": ["to_agent", "message_type", "payload"],
            },
        },
    },

    "schedule_reminder": {
        "type": "function",
        "function": {
            "name": "create_reminder",
            "description": "Schedule a reminder for the user at a specific time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label":    {"type": "string"},
                    "datetime": {"type": "string", "description": "ISO 8601 datetime"},
                    "message":  {"type": "string"},
                },
                "required": ["label", "datetime"],
            },
        },
    },
}


@dataclass
class AgentContext:
    system_prompt: str
    tool_definitions: list[dict[str, Any]]
    available_tool_names: list[str]


async def build_agent_context(user: UserContext) -> AgentContext:
    """
    Build the LLM-ready agent context for this user.
    Only tools the user is authorised to use are included.
    """
    available = user.available_tools()

    # Always include schedule_reminder if they have tasks
    if "tasks_write" in available:
        available.append("schedule_reminder")

    tool_defs = [
        _ALL_TOOLS[t] for t in available if t in _ALL_TOOLS
    ]

    system_prompt = f"""{user.system_prompt_context()}

You are Aria — a proactive, context-aware AI assistant.
You handle tasks, scheduling, email, calendar, documents, and research.

RULES:
1. Only call tools from your available list — never fabricate tool calls.
2. Before sending any email or creating any calendar event, summarize what you're about to do and ask the user to confirm.
3. If a user asks for something that requires a tool you don't have, explain what they need to connect or enable — and offer the link to Settings > Connected Apps.
4. When referencing emails or events, include the key details (sender, subject, time) — do not make up data.
5. Keep responses concise. Use markdown only when it adds clarity.
6. You know who the user is. Use their name naturally when appropriate.
"""

    return AgentContext(
        system_prompt=system_prompt,
        tool_definitions=tool_defs,
        available_tool_names=available,
    )
