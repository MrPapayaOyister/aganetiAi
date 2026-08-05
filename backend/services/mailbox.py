"""
Provider-agnostic mail / calendar / contacts facade.

Every caller in the app goes through here instead of importing gmail.py or
msmail.py directly. Which backend answers is decided per user by whichever
provider they connected in Settings (`provider_tokens.which_provider`);
Microsoft wins if somebody has connected both.

Reads degrade quietly when no provider is connected (empty list / zero /
empty string) so a disconnected user never 500s a dashboard panel. Writes
raise HTTPException(403) with a `connect_url` so the UI can prompt.

Sync mirrors (`*_sync`) exist for the callers that are synchronous by design
and already run off the event loop — the 30s inbox poll, the digest and PDF
builders, Telegram handlers. They bridge through `async_bridge.run_sync`.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from backend.services import provider_tokens as _pt
from backend.services.async_bridge import run_sync

log = logging.getLogger("aria.mailbox")

NO_PROVIDER = {
    "error": "no_provider_connected",
    "message": "Connect Microsoft 365 or Google in Settings → Connected Apps.",
    "connect_url": "/auth/microsoft/connect",
}


async def provider_for(user_id: str) -> str | None:
    """The provider this user's mail/calendar should be read from, or None."""
    try:
        return await _pt.which_provider(user_id)
    except Exception as e:  # noqa: BLE001 — never let routing break a read
        log.warning("provider lookup failed for %s: %s", user_id, e)
        return None


def _require(provider: str | None) -> str:
    if not provider:
        raise HTTPException(status_code=403, detail=NO_PROVIDER)
    return provider


# ── Mail: reads ───────────────────────────────────────────────────────────────

async def inbox(user_id: str, max_results: int = 20) -> list[dict]:
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import msmail
        return await msmail.get_inbox(user_id, max_results)
    if p == "google":
        from backend.services import gmail
        return await gmail.get_gmail_inbox(user_id, max_results)
    return []


async def unread(user_id: str, max_results: int = 20) -> list[dict]:
    """Unread inbox messages — the triage/digest feed."""
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import msmail
        return await msmail.get_unread(user_id, max_results)
    if p == "google":
        from backend.services import gmail
        # Gmail has no unread-only fetch; filter the recent window.
        msgs = await gmail.get_gmail_inbox(user_id, max_results)
        return [m for m in msgs if not m.get("is_read")]
    return []


async def unread_count(user_id: str) -> int:
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import msmail
        return await msmail.get_unread_count(user_id)
    if p == "google":
        from backend.services import gmail
        return await gmail.get_gmail_unread_count(user_id)
    return 0


async def inbox_count(user_id: str) -> dict:
    """{"unread": n, "total": n} — cheap, no bodies fetched."""
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import msmail
        return await msmail.get_inbox_count(user_id)
    if p == "google":
        from backend.services import gmail
        n = await gmail.get_gmail_unread_count(user_id)
        return {"unread": n, "total": n}     # Gmail exposes no cheap total
    return {"unread": 0, "total": 0}


async def message_body(user_id: str, message_id: str) -> str:
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import msmail
        return await msmail.get_message_body(user_id, message_id)
    if p == "google":
        from backend.services import gmail
        return await gmail.get_gmail_message_body(user_id, message_id)
    return ""


async def conversation(user_id: str, conversation_id: str, top: int = 20) -> list[dict]:
    """Thread messages, oldest-first. Gmail has no equivalent cheap call — returns []."""
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import msmail
        return await msmail.get_conversation(user_id, conversation_id, top)
    return []


async def email_digest(user_id: str) -> dict:
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import msmail
        return await msmail.get_email_digest(user_id)
    if p == "google":
        from backend.services import gmail
        return await gmail.get_gmail_email_digest(user_id)
    return {"unread_count": 0, "important_count": 0, "senders": [], "subjects": [],
            "summary": "Connect Microsoft 365 or Google in Settings to see your email digest."}


# ── Mail: writes ──────────────────────────────────────────────────────────────

async def send(user_id: str, to: str, subject: str, body: str,
               reply_to_id: str | None = None) -> dict:
    p = _require(await provider_for(user_id))
    if p == "microsoft":
        from backend.services import msmail
        return await msmail.send_message(user_id, to, subject, body, reply_to_id)
    from backend.services import gmail
    return await gmail.send_gmail_message(user_id, to, subject, body, reply_to_id)


async def mark_read(user_id: str, message_id: str) -> None:
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import msmail
        await msmail.mark_as_read(user_id, message_id)


# ── Calendar ──────────────────────────────────────────────────────────────────

async def agenda(user_id: str, days_ahead: int = 1) -> list[dict]:
    """Structured events from now through `days_ahead` days."""
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import mscalendar
        return await mscalendar.get_agenda(user_id, days_ahead)
    if p == "google":
        from backend.services import gcalendar
        return await gcalendar.get_google_agenda(user_id, days_ahead)
    return []


async def agenda_text(user_id: str) -> str:
    """Today's agenda as a text block for LLM prompts / Telegram / PDF."""
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import mscalendar
        return await mscalendar.get_todays_agenda(user_id)
    if p == "google":
        return _format_agenda_text(await agenda(user_id, days_ahead=1))
    return ""


def _format_agenda_text(events: list[dict]) -> str:
    """Render structured events in the same layout the Graph text builder uses."""
    if not events:
        return "No events scheduled for today."
    lines = ["📅 Today's Calendar:\n"]
    for e in events:
        start = (e.get("start") or "")[11:16] or "All day"
        loc = f" | 📍 {e['location']}" if e.get("location") else ""
        link = f" | 🔗 {e['meet_link']}" if e.get("meet_link") else ""
        n = len(e.get("attendees") or [])
        att = f" | 👥 {n}" if n > 1 else ""
        lines.append(f"• {start} — {e.get('title', '(no subject)')}{loc}{att}{link}")
    return "\n".join(lines)


async def upcoming_events(user_id: str, window_minutes: int = 60) -> list[dict]:
    """Structured events starting inside the window (meeting-prep job)."""
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import mscalendar
        return await mscalendar.get_upcoming_events(user_id, window_minutes)
    if p == "google":
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) + timedelta(minutes=window_minutes)
        out = []
        for e in await agenda(user_id, days_ahead=1):
            try:
                start = datetime.fromisoformat((e.get("start") or "").replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if start <= cutoff:
                out.append(e)
        return out
    return []


async def next_event(user_id: str) -> dict | None:
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import mscalendar
        return await mscalendar.get_next_event(user_id)
    if p == "google":
        from backend.services import gcalendar
        return await gcalendar.get_next_event(user_id)
    return None


async def create_event(user_id: str, title: str, start: str, end: str,
                       description: str | None = None,
                       attendees: list[str] | None = None,
                       location: str | None = None) -> dict:
    p = _require(await provider_for(user_id))
    if p == "microsoft":
        from backend.services import mscalendar
        return await mscalendar.create_event(user_id, title, start, end,
                                             description, attendees, location)
    from backend.services import gcalendar
    return await gcalendar.create_google_event(user_id, title, start, end,
                                               description, attendees, location)


async def free_slots(user_id: str, date_iso: str, duration_min: int = 60) -> list[str]:
    """Free start times on a date. Graph-only for now; Google returns []."""
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import mscalendar
        return await mscalendar.find_free_slots(user_id, date_iso, duration_min)
    return []


def format_meeting_brief(event: dict) -> str:
    """One structured event → a Telegram-ready meeting brief. Pure, provider-agnostic."""
    att_lines = [f"  • {a.get('name') or a.get('email', '')}"
                 for a in (event.get("attendees") or [])[:5]]
    loc_str = f"\n📍 {event['location']}" if event.get("location") else ""
    meet_url = f"\n🔗 Join: {event['meet_link']}" if event.get("meet_link") else ""
    att_str = ("\n👥 Attendees:\n" + "\n".join(att_lines)) if att_lines else ""
    preview = (event.get("description") or "")[:200]
    pre_str = f"\n📝 {preview}" if preview else ""
    return (f"⏰ Meeting in ~30 min:\n"
            f"*{event.get('title', '(no subject)')}*\n"
            f"🕐 {(event.get('start') or '')[11:16]}"
            f"{loc_str}{meet_url}{att_str}{pre_str}")


def invalidate_agenda_cache(user_id: str | None = None) -> None:
    """Clear cached agendas (Graph keeps a 5-min cache; Google is uncached)."""
    from backend.services import mscalendar
    mscalendar.invalidate_agenda_cache(user_id)


# ── Contacts ──────────────────────────────────────────────────────────────────

async def search_contacts(user_id: str, query: str) -> list[dict]:
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import mscontacts
        return await mscontacts.search_contacts(user_id, query)
    if p == "google":
        from backend.services import gcontacts
        return await gcontacts.search_contacts(user_id, query)
    return []


async def list_contacts(user_id: str, max_results: int = 50) -> list[dict]:
    p = await provider_for(user_id)
    if p == "microsoft":
        from backend.services import mscontacts
        return await mscontacts.get_contacts(user_id, max_results)
    if p == "google":
        from backend.services import gcontacts
        return await gcontacts.get_google_contacts(user_id, max_results)
    return []


# ── Sync mirrors for off-loop callers ─────────────────────────────────────────

def inbox_sync(user_id: str, max_results: int = 20) -> list[dict]:
    return run_sync(inbox(user_id, max_results))


def unread_sync(user_id: str, max_results: int = 20) -> list[dict]:
    return run_sync(unread(user_id, max_results))


def inbox_count_sync(user_id: str) -> dict:
    return run_sync(inbox_count(user_id))


def conversation_sync(user_id: str, conversation_id: str, top: int = 20) -> list[dict]:
    return run_sync(conversation(user_id, conversation_id, top))


def agenda_text_sync(user_id: str) -> str:
    return run_sync(agenda_text(user_id))


def send_sync(user_id: str, to: str, subject: str, body: str,
              reply_to_id: str | None = None) -> dict:
    return run_sync(send(user_id, to, subject, body, reply_to_id))
