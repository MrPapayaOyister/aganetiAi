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
from backend.service_auth import internal_headers  # Phase 0: auth for internal self-calls

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
    {
        "type": "function",
        "function": {
            "name": "get_emails",
            "description": "Read the user's recent Gmail inbox messages (subject, sender, "
                           "preview, read/unread). Use when the user asks about their email, "
                           "unread messages, or what's in their inbox.",
            "parameters": {
                "type": "object",
                "properties": {"max_results": {"type": "integer", "description": "Default 10"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agenda",
            "description": "Read the user's upcoming Google Calendar events (title, time, "
                           "attendees). Use for 'what's on my calendar', 'my agenda', "
                           "'next meeting'.",
            "parameters": {
                "type": "object",
                "properties": {"days_ahead": {"type": "integer", "description": "Default 1"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_contacts",
            "description": "Read or search the user's real Google contacts (name, email, "
                           "phone, company). Pass `query` to search; omit to list.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Optional search text"}},
            },
        },
    },
]

# Tools that produce a user-facing side effect (vs read-only tools whose result the
# model folds into its answer). Used by the chat loop to decide what to surface.
ACTION_TOOLS = {"create_task", "complete_task", "draft_email", "schedule_meeting",
                "remember_fact", "set_reminder"}
READ_TOOLS = {"get_analytics", "resolve_contact", "search_knowledge", "recall_memory",
              "web_search", "get_emails", "get_agenda", "get_contacts"}


# ── Sync→async bridge for the Google services (dispatch runs in a worker thread) ──
import asyncio as _asyncio

# The primary (uvicorn) event loop, registered at app startup. The shared httpx
# AsyncClient in services/http_client.py is bound to whichever loop first uses
# it — i.e. this one. Tool dispatch runs inside asyncio.to_thread worker
# threads, so we must marshal Google coroutines back ONTO this loop rather than
# spinning up a throwaway loop (a fresh loop can't reuse the bound httpx client,
# which is exactly why the agent's get_emails failed while the REST route worked).
_MAIN_LOOP: "_asyncio.AbstractEventLoop | None" = None


def set_main_loop(loop) -> None:
    """Register the primary event loop (call once from the FastAPI lifespan)."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def _run_async(coro):
    """Run an async coroutine from the sync tool dispatcher.

    Preferred path: schedule the coroutine on the registered main loop (which
    owns the shared httpx client) and block this worker thread for the result.
    Falls back to a private loop only if no main loop is available.
    """
    import asyncio
    loop = _MAIN_LOOP
    if loop is not None and loop.is_running():
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not loop:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=90)
    # No usable main loop (e.g. a standalone script / test) — own loop is fine.
    return asyncio.run(coro)


_GOOGLE_CTA = ("To use email, calendar, or contacts features, connect your Google "
               "account in Settings → Connected Apps.")


def _google_call(coro):
    """Run a Google-service coroutine, mapping a not-connected error to a friendly
    string. Returns (result, error_message)."""
    from fastapi import HTTPException
    try:
        return _run_async(coro), None
    except HTTPException as he:
        detail = he.detail if isinstance(he.detail, dict) else {}
        if str(detail.get("error", "")).startswith("google_"):
            return None, _GOOGLE_CTA
        return None, "That action isn't available right now."
    except Exception:
        return None, "That action failed — please try again."


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

    Safety rules applied to minimise false positives:
    - <tool_call>…</tool_call> tagged content is preferred and processed first.
    - Bare JSON is only matched as a fallback when NO tagged calls were found.
    - In both paths, the JSON object must have BOTH a "name" key (in our known tool
      set) AND an "arguments" key that is a dict/object — bare `{"name":"…"}` echoes
      from the model's summary text are rejected.
    - Callers (call_llm_tools) further gate this with allow_text_recovery=False in
      follow-up rounds after an action tool has already executed.
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
        if not (isinstance(obj, dict) and obj.get("name") and
                obj["name"] in (ACTION_TOOLS | READ_TOOLS)):
            continue
        # Require "arguments" to be a dict — bare {"name": "…"} echoes are rejected.
        args = obj.get("arguments")
        if not isinstance(args, dict):
            continue
        calls.append({"id": f"text_{len(calls)}",
                      "function": {"name": obj["name"], "arguments": json.dumps(args)}})
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
        from backend.services import gcontacts
        q = args.get("name", "")
        res, err = _google_call(gcontacts.search_contacts(user_id, q))
        if err:
            return err
        if not res:
            return f"No contact found for '{q}'."
        c = res[0]
        return (f"Contact: {c.get('name','')} | email: {c.get('email') or 'none on file'}"
                f" | phone: {c.get('phone') or '-'} | company: {c.get('company') or '-'}")

    # ── Real Google read tools ──
    if name == "get_emails":
        from backend.services import gmail
        try:
            n = int(args.get("max_results", 10))
        except (TypeError, ValueError):
            n = 10
        res, err = _google_call(gmail.get_gmail_inbox(user_id, n))
        if err:
            return err
        if not res:
            return "Your inbox is empty."
        lines = [f"{'•' if not m['is_read'] else ' '} {m['subject']} — {m['from_name']}"
                 for m in res[:n]]
        unread = sum(1 for m in res if not m["is_read"])
        return f"{unread} unread of {len(res)} recent emails:\n" + "\n".join(lines)

    if name == "get_agenda":
        from backend.services import gcalendar
        try:
            days = int(args.get("days_ahead", 1))
        except (TypeError, ValueError):
            days = 1
        res, err = _google_call(gcalendar.get_google_agenda(user_id, days))
        if err:
            return err
        if not res:
            return "No upcoming events."
        return "Upcoming events:\n" + "\n".join(
            f"- {e['title']} at {e['start']}" + (f" ({e['location']})" if e.get('location') else "")
            for e in res)

    if name == "get_contacts":
        from backend.services import gcontacts
        q = (args.get("query") or "").strip()
        coro = gcontacts.search_contacts(user_id, q) if q else gcontacts.get_google_contacts(user_id)
        res, err = _google_call(coro)
        if err:
            return err
        if not res:
            return "No contacts found."
        return "Contacts:\n" + "\n".join(
            f"- {c['name']}" + (f" <{c['email']}>" if c.get('email') else "") for c in res[:15])

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
                headers=internal_headers(user_id),
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

    # Schedule a real Google Calendar event (replaces the M365 path).
    if name == "schedule_meeting":
        who = (args.get("with") or "").strip()
        when = (args.get("time") or "").strip()
        title = (args.get("title") or (f"Meeting with {who}" if who else "Meeting")).strip()
        if not when:
            return "When should I schedule it?"
        try:
            from integrations.m365_calendar import parse_meeting_time
            start, end = parse_meeting_time(when)
        except Exception:
            return "I couldn't understand that time — try e.g. 'tomorrow at 3pm'."
        attendees = None
        if who:
            if "@" in who:
                attendees = [who]
            else:
                from backend.services import gcontacts
                res, _ = _google_call(gcontacts.search_contacts(user_id, who))
                if res and res[0].get("email"):
                    attendees = [res[0]["email"]]
        from backend.services import gcalendar
        created, err = _google_call(gcalendar.create_google_event(
            user_id, title, start, end, attendees=attendees))
        if err:
            return err
        link = created.get("htmlLink") if isinstance(created, dict) else None
        return f"Scheduled **{title}** for {start}." + (f"\n{link}" if link else "")

    action = {"type": name, **args}
    outcome = execute_action(action, user_id).strip()  # "✅ Task created.", etc.
    lead = _lead_in(name, args)
    if lead and outcome:
        return f"{lead}\n{outcome}"
    return lead or outcome


def execute_single_tool(name: str, raw_args, user_id: str):
    """Run one tool; return (result_string, is_action). Read tools' results are fed
    back to the model; action tools' results are surfaced to the user.

    Every call is timed + logged to the events table for analytics (P5)."""
    import time as _t
    _t0 = _t.monotonic()
    ok = True
    try:
        result = dispatch_tool_call(name, raw_args, user_id)
        # Heuristic success: dispatch returns a ⚠️-prefixed string on failure.
        ok = not (isinstance(result, str) and result.lstrip().startswith("⚠️"))
        return result, (name in ACTION_TOOLS)
    except Exception:
        ok = False
        raise
    finally:
        try:
            from backend import events
            events.log_event("tool_called", user_id=user_id, name=name, success=ok,
                             duration_ms=int((_t.monotonic() - _t0) * 1000))
        except Exception:
            pass


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
