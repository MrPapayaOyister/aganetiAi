"""Capability-sprint skills — additional tools registered into the shared registry.

Wraps EXISTING backend functions (Gmail, Calendar, Contacts, tasks, memory,
analytics) as executor tools, plus web search (OUTBOUND — approval-gated so it
asks every time) and read-only PREDICTIVE tools.

Predictions follow the honesty contract: the handler computes the numbers in
Python from REAL stored rows and returns an EVIDENCE BLOCK; the model only
narrates it (told to cite only the figures present). Numbers are never invented
by the LLM. Read/predict tools run inline; anything that egresses data is
is_outbound=True and routes through the approval gate.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from .registry import Tool, register


# ── Email ─────────────────────────────────────────────────────────────────────
async def _read_email(ctx, message_id: str) -> str:
    try:
        from backend.services.gmail import get_gmail_message_body
        body = await get_gmail_message_body(ctx["user_id"], message_id)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't read that email ({e})."
    body = (body or "").strip()
    return body[:4000] if body else "The email body is empty or unavailable."


async def _email_digest(ctx) -> str:
    try:
        from backend.services.gmail import get_gmail_email_digest
        d = await get_gmail_email_digest(ctx["user_id"])
    except Exception as e:  # noqa: BLE001
        return f"Email digest unavailable ({e}). The user may need to connect Google in Settings."
    if isinstance(d, dict):
        summary = d.get("digest") or d.get("summary") or ""
        items = d.get("items") or d.get("emails") or []
        lines = [summary] if summary else []
        for it in items[:8]:
            if isinstance(it, dict):
                lines.append(f"- {it.get('subject', '(no subject)')} — {it.get('from_name') or it.get('from_email') or '?'}")
        return "\n".join([x for x in lines if x]) or "Inbox digest is empty."
    return str(d)


# ── Tasks (kept on SQLite, consistent with list_tasks; moves to PG in Phase D) ─
async def _create_task(ctx, title: str, priority: str = "medium", due_date: str = "") -> str:
    def _c():
        from tasks.store import create_task
        return create_task(ctx["user_id"], title=title, source="assistant",
                           priority=priority, due_date=(due_date or None))
    try:
        await asyncio.to_thread(_c)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't create the task ({e})."
    return f"Task created: '{title}'" + (f", due {due_date}" if due_date else "") + f" [priority: {priority}]."


async def _complete_task(ctx, title: str) -> str:
    def _c():
        from tasks.store import find_task_by_title, update_task
        t = find_task_by_title(ctx["user_id"], title)
        if not t:
            return None
        update_task(ctx["user_id"], t["id"], status="done")
        return t
    try:
        t = await asyncio.to_thread(_c)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't complete the task ({e})."
    return f"Marked done: '{t['title']}'." if t else f"No matching open task found for '{title}'."


# ── Memory ────────────────────────────────────────────────────────────────────
async def _remember_fact(ctx, fact: str) -> str:
    def _r():
        from memory.long_term import upsert_facts
        upsert_facts([fact], ctx["user_id"], datetime.now(timezone.utc).isoformat())
    try:
        await asyncio.to_thread(_r)
    except Exception as e:  # noqa: BLE001
        return f"Couldn't save that to memory ({e})."
    return f"Noted — I'll remember: {fact}"


# ── Contacts / calendar ───────────────────────────────────────────────────────
async def _resolve_contact(ctx, name_or_email: str) -> str:
    try:
        from backend.services.gcontacts import search_contacts
        hits = await search_contacts(ctx["user_id"], name_or_email)
    except Exception as e:  # noqa: BLE001
        return f"Contact lookup unavailable ({e})."
    if not hits:
        return f"No contact found matching '{name_or_email}'."
    lines = []
    for h in hits[:5]:
        extra = h.get("role") or h.get("title") or h.get("organization") or h.get("company") or ""
        lines.append(f"- {h.get('name', '?')} — {h.get('email', 'no email on file')}" + (f" ({extra})" if extra else ""))
    return f"Contacts matching '{name_or_email}':\n" + "\n".join(lines)


async def _next_event(ctx) -> str:
    try:
        from backend.services.gcalendar import get_next_event
        e = await get_next_event(ctx["user_id"])
    except Exception as ex:  # noqa: BLE001
        return f"Calendar unavailable ({ex})."
    if not e:
        return "No upcoming events on the calendar."
    return (f"Next up: {e.get('title', '(untitled)')} at {e.get('start', '')}"
            + (f" — {e['location']}" if e.get("location") else ""))


# ── Analytics ─────────────────────────────────────────────────────────────────
async def _get_analytics(ctx, metric: str = "summary", days: int = 7) -> str:
    def _m():
        from backend.analytics import run_metric
        return run_metric(metric, ctx["user_id"], days)
    try:
        d = await asyncio.to_thread(_m)
    except Exception as e:  # noqa: BLE001
        return f"Analytics unavailable ({e})."
    return f"Analytics — {metric} (last {days}d): {d}"


# ── Web search (OUTBOUND — the query egresses; approval-gated by is_outbound) ──
async def _web_search(ctx, query: str, max_results: int = 5) -> str:
    def _run():
        from ddgs import DDGS
        return DDGS().text(query, max_results=min(int(max_results or 5), 10))
    try:
        results = await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        return f"Web search failed ({e})."
    if not results:
        return f"No web results found for: {query}"
    lines = [f"**{r.get('title', '')}**\n{(r.get('body') or r.get('snippet') or '')[:300]}\n🔗 {r.get('href', '')}"
             for r in results]
    return f"Web results for '{query}':\n\n" + "\n\n".join(lines)


# ── Predictive intelligence (read-only; numbers computed in Python) ───────────
def _conf(n_signals: int) -> str:
    return "LOW" if n_signals < 3 else ("MEDIUM" if n_signals < 8 else "HIGH")


async def _predict_task_slippage(ctx) -> str:
    def _tasks():
        from tasks.store import get_all_tasks
        return get_all_tasks(ctx["user_id"], status="pending")
    try:
        tasks = await asyncio.to_thread(_tasks)
    except Exception:  # noqa: BLE001
        tasks = []
    today = date.today()
    horizon = (today + timedelta(days=7)).isoformat()
    tstr = today.isoformat()

    def _due(t):
        return (t.get("due_date") or "")[:10]
    overdue = [t for t in tasks if _due(t) and _due(t) < tstr]
    due_soon = [t for t in tasks if _due(t) and tstr <= _due(t) <= horizon]
    urgent = [t for t in tasks if (t.get("priority") or "").lower() in ("urgent", "high")]
    try:
        from backend.services import gcalendar
        events = await gcalendar.get_google_agenda(ctx["user_id"], days_ahead=7)
    except Exception:  # noqa: BLE001
        events = []
    n_meet = len(events or [])
    score = min(100, len(overdue) * 25 + len(due_soon) * 8 + len(urgent) * 6 + n_meet * 2)
    conf = _conf(len(tasks))
    return "\n".join([
        "TASK-SLIPPAGE RISK — computed from the user's real pending tasks + calendar:",
        f"- pending tasks: {len(tasks)}",
        f"- OVERDUE (due date already passed): {len(overdue)}"
        + (": " + ", ".join(t.get("title", "?") for t in overdue[:5]) if overdue else ""),
        f"- due within 7 days: {len(due_soon)}"
        + (": " + ", ".join(t.get("title", "?") for t in due_soon[:5]) if due_soon else ""),
        f"- high/urgent priority: {len(urgent)}",
        f"- meetings competing for time (next 7d): {n_meet}",
        f"- RISK SCORE: {score}/100   CONFIDENCE: {conf}",
        "INSTRUCTION: Name the tasks most AT RISK and why, citing only the figures above. "
        "Say 'at risk', never a certainty. If confidence is LOW, say the data is thin.",
    ])


async def _predict_followups(ctx) -> str:
    try:
        from backend.services.gmail import get_gmail_inbox
        inbox = await get_gmail_inbox(ctx["user_id"], max_results=30)
    except Exception as e:  # noqa: BLE001
        return f"Follow-up detection unavailable ({e})."
    open_loops = [m for m in (inbox or []) if not m.get("is_read")]
    if not open_loops:
        return "No obvious open loops — no unread inbound messages appear to be awaiting your reply."
    lines = ["OPEN LOOPS — unread inbound messages likely awaiting your response (from the real inbox):"]
    for m in open_loops[:8]:
        who = m.get("from_name") or m.get("from_email") or "?"
        when = (m.get("received_at") or "")[:10]
        lines.append(f"- {who}: \"{m.get('subject', '(no subject)')}\"" + (f" ({when})" if when else ""))
    lines.append("INSTRUCTION: Recommend who to follow up with FIRST and why, citing only these. "
                 "Offer to draft replies with draft_email.")
    return "\n".join(lines)


async def _predict_relationship_value(ctx, person: str) -> str:
    uid = ctx["user_id"]
    contact = None
    try:
        from backend.services.gcontacts import search_contacts
        hits = await search_contacts(uid, person)
        contact = hits[0] if hits else None
    except Exception:  # noqa: BLE001
        contact = None
    inter, last = 0, None
    try:
        from backend.services.gmail import get_gmail_inbox
        inbox = await get_gmail_inbox(uid, max_results=50)
        email = ((contact or {}).get("email") or "").lower()
        needle = person.lower()
        for m in (inbox or []):
            frm = f"{m.get('from_email', '')} {m.get('from_name', '')}".lower()
            if (email and email in frm) or (needle and needle in frm):
                inter += 1
                last = last or m.get("received_at")
    except Exception:  # noqa: BLE001
        pass
    mem = ""
    try:
        from memory.long_term import search_memory
        mem = await asyncio.to_thread(search_memory, uid, person, 3)
    except Exception:  # noqa: BLE001
        mem = ""
    role = (contact or {}).get("role") or (contact or {}).get("title") or ""
    company = (contact or {}).get("organization") or (contact or {}).get("company") or ""
    signals = inter + (1 if (role or company) else 0) + (1 if mem else 0)
    score = min(100, inter * 12 + (20 if (role or company) else 0) + (15 if mem else 0))
    return "\n".join([
        f"RELATIONSHIP-VALUE SIGNAL for '{person}' — from real contact, email and memory data:",
        f"- on file: {contact.get('name') if contact else 'not found in contacts'}"
        + (f" — {role}{(' at ' + company) if company else ''}" if (role or company) else ""),
        f"- recent email interactions (last 50 msgs scanned): {inter}",
        f"- last contact: {last or 'n/a'}",
        f"- known facts from memory: {(mem or 'none recorded').strip()[:300]}",
        f"- CONNECTION-VALUE SCORE: {score}/100   CONFIDENCE: {_conf(signals)}",
        "INSTRUCTION: Explain the POSSIBLE BENEFITS of investing in this relationship, grounded ONLY "
        "in the above. If confidence is LOW or interactions are few, say the data is thin and what "
        "would sharpen the estimate. Never invent numbers, titles, or history.",
    ])


# ── Registration ──────────────────────────────────────────────────────────────
_obj = lambda props, req=None: {"type": "object", "properties": props, **({"required": req} if req else {})}  # noqa: E731

register(Tool("read_email", "Read the full body of a specific email by its id (get the id from list_emails first).",
              _obj({"message_id": {"type": "string"}}, ["message_id"]), _read_email, "email.read"))
register(Tool("email_digest", "Summarize the user's inbox — a digest of recent/important emails.",
              _obj({}), _email_digest, "email.read"))
register(Tool("create_task", "Create a to-do/task for the user.",
              _obj({"title": {"type": "string"}, "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD, optional"}}, ["title"]),
              _create_task, "tasks.write"))
register(Tool("complete_task", "Mark an existing task as done, matched by keywords from its title.",
              _obj({"title": {"type": "string"}}, ["title"]), _complete_task, "tasks.write"))
register(Tool("remember_fact", "Save a durable fact/preference about the user to long-term memory.",
              _obj({"fact": {"type": "string"}}, ["fact"]), _remember_fact, "memory.write"))
register(Tool("resolve_contact", "Look up a person in the user's contacts by name or email (get their email/role).",
              _obj({"name_or_email": {"type": "string"}}, ["name_or_email"]), _resolve_contact, "contacts.read"))
register(Tool("next_event", "Get the single next upcoming calendar event.", _obj({}), _next_event, "calendar.read"))
register(Tool("get_analytics", "Get the user's usage/productivity analytics for a metric over N days.",
              _obj({"metric": {"type": "string"}, "days": {"type": "integer"}}), _get_analytics, "analytics.read"))
register(Tool("web_search",
              "Search the public web for current information. OUTBOUND — the query leaves the system, so it "
              "requires the user's approval each time. Use for news, facts, or anything not in the user's data.",
              _obj({"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"]),
              _web_search, "web.search", is_outbound=True))
register(Tool("predict_task_slippage",
              "Predict which of the user's tasks are at risk of slipping, from their real tasks + calendar load.",
              _obj({}), _predict_task_slippage, "predictions.read"))
register(Tool("predict_followups",
              "Predict who the user should follow up with — open loops (unanswered inbound) in their inbox.",
              _obj({}), _predict_followups, "predictions.read"))
register(Tool("predict_relationship_value",
              "Estimate the POSSIBLE BENEFIT/value of investing in a relationship with a person, from real "
              "contact + email-interaction + memory signals. Use for 'is it worth connecting with X', 'value of X'.",
              _obj({"person": {"type": "string"}}, ["person"]), _predict_relationship_value, "predictions.read"))
