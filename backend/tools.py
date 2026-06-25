"""
Native function-calling for the chat assistant.

Replaces the brittle ``[ACTION:{json}]`` tail-tag protocol with OpenAI-style
``tools`` that llama.cpp (run with ``--jinja``) returns as structured
``tool_calls``. The actual side-effects reuse the already-tested
``backend.action_parser.execute_action`` dispatcher, so there is a single place
that talks to the task / email / meeting endpoints.

Toggled by ``NATIVE_TOOLS`` in config.settings; the legacy action-tag path
remains as a fallback.
"""

from __future__ import annotations

import json
import re

from backend.action_parser import execute_action

# OpenAI tool schema — kept in lock-step with execute_action's action types.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a to-do task for the user. Use whenever the user "
                           "asks to add/create/remember a task or action item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short task title"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"],
                                 "description": "Defaults to medium if unstated"},
                    "due": {"type": "string",
                            "description": "Due date as YYYY-MM-DD, or null if none"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark an existing task as done, matched by partial title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Keywords from the task title"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_email",
            "description": "Draft an email AND save it to the user's approval queue. "
                           "ALWAYS call this when the user asks to draft/write/send an "
                           "email and a recipient (or a clear topic) is known — do NOT "
                           "just write the email text in chat. Fill the body with your "
                           "best draft (use placeholders for unknown details); the user "
                           "reviews and sends it from the queue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": "Schedule a meeting / calendar event. Requires both who to "
                           "meet with and a time. If the user provides an email address "
                           "directly, use it as-is — do NOT call resolve_contact first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "with": {"type": "string", "description": "Attendee name or email"},
                    "time": {"type": "string", "description": "ISO datetime or natural language"},
                    "title": {"type": "string"},
                },
                "required": ["with", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_analytics",
            "description": "Answer a quantitative question about the user's own data "
                           "(task and email stats). Use for 'how many tasks did I finish "
                           "last week', 'what's on my plate', 'who do I email most'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": ["summary", "completed", "created",
                                 "pending_by_priority", "top_contacts", "triage_volume"],
                    },
                    "days": {"type": "integer", "description": "Look-back window (default 7)"},
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_contact",
            "description": "Look up a saved contact by NAME to get their email address. "
                           "Call this ONLY when you have a person's name but NOT their email. "
                           "Do NOT call this when the user has already given an email address. "
                           "Also useful when the user asks 'what is X's email?'.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Person's name or nickname"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Search company policy/handbook documents (the knowledge vault) "
                           "to answer questions about company rules, processes, or facts.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search the user's long-term memory for facts learned in past "
                           "conversations (preferences, people, commitments).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "Save a durable fact to the user's long-term memory when they say "
                           "'remember that ...' or state a lasting preference/commitment.",
            "parameters": {
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a one-time Telegram reminder at a specific future time. "
                           "Use when the user says 'remind me to X at Y' or 'remind me in Z minutes'. "
                           "Do NOT use for recurring schedules — use set_reminder only for one-off alerts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message":   {"type": "string", "description": "What to remind the user about"},
                    "remind_at": {"type": "string",
                                  "description": "When to fire — ISO datetime, or natural language "
                                                 "like '4pm', 'tomorrow at 9am', 'in 30 minutes'"},
                },
                "required": ["message", "remind_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information, news, or facts the model "
                           "may not know. Use for questions about recent events, live data, "
                           "product specs, or any 'what is the latest ...' query. "
                           "Returns top results with titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer",
                                    "description": "Number of results (default 5, max 10)"},
                },
                "required": ["query"],
            },
        },
    },
]

# Tools that produce a user-facing side effect (vs read-only tools whose result the
# model folds into its answer). Used by the chat loop to decide what to surface.
ACTION_TOOLS = {"create_task", "complete_task", "draft_email", "schedule_meeting",
                "remember_fact", "set_reminder"}
READ_TOOLS = {"get_analytics", "resolve_contact", "search_knowledge", "recall_memory",
              "web_search"}


def _lead_in(name: str, args: dict) -> str:
    """A short natural-language confirmation (the model returns empty content
    alongside a tool call, so we synthesize the human-facing sentence)."""
    if name == "create_task":
        title = args.get("title", "task")
        extras = []
        pr = args.get("priority")
        if pr and pr != "medium":
            extras.append(f"{pr} priority")
        due = args.get("due")
        if due and str(due).lower() not in ("null", "none", ""):
            extras.append(f"due {due}")
        suffix = f" ({', '.join(extras)})" if extras else ""
        return f"I've added a task: **{title}**{suffix}."
    if name == "complete_task":
        return f"Marking **{args.get('title', 'that task')}** as done."
    if name == "draft_email":
        to = args.get("to", "")
        subj = args.get("subject")
        return f"I've drafted an email to {to}" + (f" — *{subj}*." if subj else ".")
    if name == "schedule_meeting":
        who = args.get("with", "")
        when = args.get("time", "")
        return f"Setting up a meeting with {who} ({when})."
    if name == "set_reminder":
        msg = args.get("message", "")
        when = args.get("remind_at", "")
        return f"Reminder set: *{msg}* at {when}."
    return ""


def _balanced_json_objects(text: str) -> list:
    """Extract top-level {...} JSON objects from free text (brace-balanced)."""
    objs, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "{":
            depth, j = 0, i
            while j < n:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            objs.append(json.loads(text[i:j + 1]))
                        except Exception:
                            pass
                        i = j
                        break
                j += 1
        i += 1
    return objs


def extract_text_tool_calls(content: str) -> list:
    """
    Fallback: pull tool calls out of RAW CONTENT when the model emits them as text
    instead of structured tool_calls. With the full production prompt this build often
    emits `<tool_call>{...}</tool_call>` or a bare `{"name":..,"arguments":{..}}` in the
    content, which would otherwise be shown verbatim and the action silently skipped.
    """
    if not content:
        return []
    candidates = []
    fenced = re.findall(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", content, re.DOTALL)
    for frag in fenced:
        try:
            candidates.append(json.loads(frag))
        except Exception:
            pass
    if not candidates:
        candidates = _balanced_json_objects(content)
    calls = []
    for obj in candidates:
        if isinstance(obj, dict) and obj.get("name") and obj["name"] in (ACTION_TOOLS | READ_TOOLS):
            args = obj.get("arguments", {})
            if isinstance(args, dict):
                args = json.dumps(args)
            calls.append({"id": f"text_{len(calls)}",
                          "function": {"name": obj["name"], "arguments": args}})
    return calls


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def dispatch_tool_call(name: str, raw_args, user_id: str) -> str:
    """Execute one tool call and return a user-facing confirmation/answer string."""
    args = _parse_args(raw_args)

    # Guardrail: deny unknown/hallucinated actions outright. (comms actions are still
    # safe-by-design — draft_email queues a draft, it never sends.)
    from backend.guardrails import decide
    if decide(name) == "deny":
        return f"⚠️ I'm not able to perform that action ('{name}')."

    # Read-only analytics: answer directly, don't go through execute_action.
    if name == "get_analytics":
        from backend.analytics import run_metric
        try:
            days = int(args.get("days", 7))
        except (TypeError, ValueError):
            days = 7
        result = run_metric(args.get("metric", "summary"), user_id, days)
        return result.get("human") or str(result)

    if name == "resolve_contact":
        from integrations.contacts import resolve_contact as _rc
        c = _rc(args.get("name", ""))
        if not c:
            return f"No saved contact found for '{args.get('name','')}'."
        return (f"Contact: {c.get('full_name','')} | email: {c.get('email') or 'none on file'}"
                f" | role: {c.get('role') or '-'} | company: {c.get('company') or '-'}")

    if name == "search_knowledge":
        from backend.ingest import search_corporate
        hits = search_corporate(args.get("query", ""), top_k=3)
        if not hits:
            return "No relevant company documents found."
        return "\n\n".join(f"[{h['source']}] {h['text'][:600]}" for h in hits)

    if name == "recall_memory":
        from memory.long_term import search_memory
        mem = search_memory(user_id, args.get("query", ""), top_k=5)
        return mem.strip() if mem and mem.strip() else "No relevant memory found."

    if name == "remember_fact":
        from memory.long_term import upsert_facts
        from datetime import datetime, timezone
        fact = (args.get("fact") or "").strip()
        if not fact:
            return "Nothing to remember."
        try:
            upsert_facts([fact], user_id, datetime.now(timezone.utc).isoformat())
            return f"🧠 Noted: {fact}"
        except Exception as e:
            return f"⚠️ Could not save that to memory ({e})."

    if name == "set_reminder":
        import httpx as _httpx
        message  = (args.get("message") or "").strip()
        remind_at = (args.get("remind_at") or "").strip()
        if not message or not remind_at:
            return "⚠️ Need both a message and a time to set a reminder."
        try:
            r = _httpx.post(
                "http://127.0.0.1:8000/set_reminder",
                json={"user_id": user_id, "message": message, "remind_at": remind_at},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return f"⏰ Reminder set for *{data.get('remind_at', remind_at)}*: {message}"
        except Exception as e:
            return f"⚠️ Could not set reminder: {e}"

    if name == "web_search":
        query = (args.get("query") or "").strip()
        if not query:
            return "⚠️ No query provided."
        max_r = min(int(args.get("max_results") or 5), 10)
        try:
            from ddgs import DDGS
            results = DDGS().text(query, max_results=max_r)
            if not results:
                return f"No web results found for: {query}"
            lines = [f"**{r['title']}**\n{r.get('body', r.get('snippet', ''))[:300]}\n🔗 {r['href']}"
                     for r in results]
            return f"🌐 Web results for *{query}*:\n\n" + "\n\n".join(lines)
        except Exception as e:
            return f"⚠️ Web search failed: {e}"

    action = {"type": name, **args}
    outcome = execute_action(action, user_id).strip()  # "✅ Task created.", etc.
    lead = _lead_in(name, args)
    if lead and outcome:
        return f"{lead}\n{outcome}"
    return lead or outcome


def execute_single_tool(name: str, raw_args, user_id: str):
    """Run one tool; return (result_string, is_action). Read tools' results are fed
    back to the model; action tools' results are surfaced to the user."""
    result = dispatch_tool_call(name, raw_args, user_id)
    return result, (name in ACTION_TOOLS)


def run_tool_calls(tool_calls: list, user_id: str) -> str:
    """Execute every tool call from an assistant message; join confirmations."""
    parts = []
    for tc in tool_calls:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        parts.append(dispatch_tool_call(name, fn.get("arguments"), user_id))
    return "\n\n".join(p for p in parts if p)
