"""
FastMCP server exposing Microsoft 365 (mail, calendar, contacts) to Hermes.

Registered in ~/.hermes/config.yaml under `mcp_servers`:

    mcp_servers:
      microsoft_graph:
        command: /home/matrix/aganetiAi/.venv/bin/python
        args: ["-m", "backend.mcp.m365_server"]
        env:
          PYTHONPATH: /home/matrix/aganetiAi
        enabled: true

Run standalone (for testing): python -m backend.mcp.m365_server

Why this rather than an npm Graph server: the platform ALREADY holds a valid
delegated token for this Azure app — obtained through the web OAuth flow, stored
Fernet-encrypted, and refreshed automatically by `provider_tokens`. A separate
MCP server would mean a second consent, a second token store, and a second thing
that can silently expire. This wraps the services that already work, so Hermes
and the platform share one connection and one refresh path.

Credentials are NOT passed through this file. `provider_tokens` reads
M365_CLIENT_ID / M365_CLIENT_SECRET / M365_TENANT_ID from the aganetiAi `.env`
itself, so no secret is ever written into the Hermes config.

Outbound actions (sending mail, creating events) are DISABLED by default. The
platform's own agent puts those behind a human approval gate; MCP has no such
gate, so an agent that can call `send_mail` can send mail with nobody in the
loop. Set M365_ALLOW_OUTBOUND=1 to register them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Optional

from fastmcp import FastMCP

from backend.services import mscalendar, mscontacts, msmail
from backend.services import provider_tokens as pt
from backend.services.http_client import api_request
from backend.services.provider_tokens import get_ms_headers

log = logging.getLogger("aganeti.mcp.m365")

# Which account this server acts for. The OAuth flow stores tokens under the
# Supabase uid; `provider_tokens` also resolves internal aliases ("user_1"), so
# either form works here.
USER_ID = os.getenv("M365_USER_ID", "d250c11b-9dba-457c-b33b-cc9822064c62")

ALLOW_OUTBOUND = os.getenv("M365_ALLOW_OUTBOUND", "").strip().lower() in {"1", "true", "yes"}

# Hard ceiling on any listing, whatever the caller asks for. The model is served
# by qwen-fast at a 40,960-token context; an unbounded listing can exceed that on
# its own, and the failure mode is an unrecoverable 400 mid-conversation rather
# than a truncated answer.
MAX_LIST = int(os.getenv("M365_MAX_LIST", "25"))

mcp = FastMCP("microsoft-graph")


# Fields that make a LISTING unusable. `_parse_message` returns the full body on
# every message because the platform's own callers want it; handing that to a
# model is a different matter. Measured: 20 messages with bodies is ~69k tokens
# against a 41k context — the listing alone cannot fit, twice over. Listings
# therefore carry headers and a short preview; `get_message_body` fetches a body
# when one is actually wanted.
_HEAVY = ("body_html", "body_text", "body_preview")
PREVIEW_CHARS = 160


def _slim(msg: dict[str, Any]) -> dict[str, Any]:
    """A message as a listing should show it — no bodies, bounded preview."""
    out = {k: v for k, v in msg.items() if k not in _HEAVY}
    preview = msg.get("preview") or msg.get("body_preview") or ""
    out["preview"] = preview[:PREVIEW_CHARS]
    return out


def _err(e: Exception) -> dict[str, Any]:
    """Turn an exception into something an agent can act on.

    HTTPException carries the structured `not_connected` / `token_expired`
    payloads the auth layer raises, including the URL to reconnect at — far more
    useful to the caller than a stack trace it cannot read.
    """
    detail = getattr(e, "detail", None)
    if isinstance(detail, dict):
        return {"error": detail.get("error", "graph_error"),
                "message": detail.get("message", ""),
                "connect_url": detail.get("connect_url", "")}
    return {"error": type(e).__name__, "message": str(e)[:300]}


# ── status ────────────────────────────────────────────────────────────────────

@mcp.tool
async def m365_status() -> dict:
    """Report whether Microsoft 365 is connected for this account.

    Call this first when a mail or calendar tool returns an error — it
    distinguishes "not connected" from "connected but the request failed".
    """
    try:
        provider = await pt.which_provider(USER_ID)
        connected = await pt.connected_providers(USER_ID)
        return {"user_id": USER_ID, "active_provider": provider,
                "connected_providers": connected,
                "microsoft_connected": "microsoft" in connected,
                "outbound_enabled": ALLOW_OUTBOUND}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ── mail (read) ───────────────────────────────────────────────────────────────

@mcp.tool
async def list_inbox(max_results: int = 10) -> list[dict] | dict:
    """List recent inbox messages, newest first: sender, subject, date, preview.

    Bodies are NOT included — call get_message_body(message_id) for one. To find
    mail from a specific person, use search_messages instead of listing and
    filtering, which is both slower and far larger.
    """
    try:
        msgs = await msmail.get_inbox(USER_ID, max_results=min(max_results, MAX_LIST))
        return [_slim(m) for m in msgs]
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def list_unread(max_results: int = 10) -> list[dict] | dict:
    """List unread inbox messages (headers and preview only, no bodies)."""
    try:
        msgs = await msmail.get_unread(USER_ID, max_results=min(max_results, MAX_LIST))
        return [_slim(m) for m in msgs]
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def search_messages(sender: Optional[str] = None, query: Optional[str] = None,
                          unread_only: bool = False, max_results: int = 10
                          ) -> list[dict] | dict:
    """Find messages by sender and/or free text, filtered by Graph server-side.

    `sender` matches the from-address by substring, so "aisha" finds
    aisha@example.com. Use this for "mail from X" — listing the whole inbox and
    filtering client-side wastes context and misses anything past the page size.
    """
    try:
        headers = await get_ms_headers(USER_ID)
        params: dict[str, Any] = {"$top": min(max_results, MAX_LIST),
                                  "$select": msmail._SELECT}
        clauses = []
        if unread_only:
            clauses.append("isRead eq false")
        if sender and "@" in sender:
            # An exact address is a filter; a bare name is not — Graph has no
            # `contains` on from/emailAddress, so names go through $search below.
            clauses.append(f"from/emailAddress/address eq '{sender.replace(chr(39), chr(39)*2)}'")
        if clauses:
            params["$filter"] = " and ".join(clauses)

        terms = [t for t in (query, sender if sender and "@" not in sender else None) if t]
        if terms:
            # $search cannot be combined with $orderby in Graph — it returns by
            # relevance instead, which is the sane ordering for a search anyway.
            params["$search"] = '"' + " ".join(terms).replace('"', "") + '"'
        else:
            params["$orderby"] = "receivedDateTime desc"

        # A bare name goes through $search, which matches subject and BODY too —
        # so "aisha" returns mail that merely mentions her. Over-fetch, then keep
        # only real sender matches, so "mail from X" means from X.
        want = min(max_results, MAX_LIST)
        params["$top"] = min(want * 5, 50) if (sender and "@" not in sender) else want

        r = await api_request("GET", f"{msmail.GRAPH}/me/messages",
                              headers={**headers, "ConsistencyLevel": "eventual"},
                              params=params)
        if r.status_code != 200:
            return {"error": "graph_search_failed", "status": r.status_code,
                    "message": r.text[:200]}

        rows = [msmail._parse_message(m) for m in (r.json().get("value") or [])]
        if sender:
            needle = sender.lower()
            rows = [m for m in rows
                    if needle in (m.get("from_email") or "").lower()
                    or needle in (m.get("from_name") or "").lower()]
        rows.sort(key=lambda m: m.get("received_at") or "", reverse=True)
        return [_slim(m) for m in rows[:want]]
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def unread_count() -> dict:
    """How many unread messages are in the inbox."""
    try:
        return {"unread": await msmail.get_unread_count(USER_ID)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def get_message(message_id: str) -> dict | None:
    """Fetch one message's metadata by id (ids come from list_inbox/list_unread)."""
    try:
        return await msmail.get_message(USER_ID, message_id)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def get_message_body(message_id: str) -> dict:
    """Fetch the full plain-text body of one message."""
    try:
        return {"message_id": message_id, "body": await msmail.get_message_body(USER_ID, message_id)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def get_conversation(conversation_id: str, top: int = 20) -> list[dict] | dict:
    """Fetch every message in one email thread, oldest first."""
    try:
        return await msmail.get_conversation(USER_ID, conversation_id, top=top)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def email_digest() -> dict:
    """A summary of the mailbox: counts plus the most recent/important messages."""
    try:
        return await msmail.get_email_digest(USER_ID)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ── calendar (read) ───────────────────────────────────────────────────────────

@mcp.tool
async def get_agenda(days_ahead: int = 1) -> list[dict] | dict:
    """Calendar events for the next `days_ahead` days."""
    try:
        return await mscalendar.get_agenda(USER_ID, days_ahead=days_ahead)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def todays_agenda() -> dict:
    """Today's agenda as a human-readable summary string."""
    try:
        return {"agenda": await mscalendar.get_todays_agenda(USER_ID)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def next_event() -> dict | None:
    """The next upcoming calendar event, or null if there is none."""
    try:
        return await mscalendar.get_next_event(USER_ID)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def upcoming_events(window_minutes: int = 60) -> list[dict] | dict:
    """Events starting within the next `window_minutes` minutes."""
    try:
        return await mscalendar.get_upcoming_events(USER_ID, window_minutes=window_minutes)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def find_free_slots(date_iso: str, duration_min: int = 60) -> list[dict] | dict:
    """Free slots of at least `duration_min` on a date (YYYY-MM-DD)."""
    try:
        return await mscalendar.find_free_slots(USER_ID, date_iso, duration_min=duration_min)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ── contacts (read) ───────────────────────────────────────────────────────────

@mcp.tool
async def list_contacts(max_results: int = 50) -> list[dict] | dict:
    """List saved contacts and directory people."""
    try:
        return await mscontacts.get_contacts(USER_ID, max_results=max_results)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def search_contacts(query: str) -> list[dict] | dict:
    """Search contacts and the org directory by name or email fragment."""
    try:
        return await mscontacts.search_contacts(USER_ID, query)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool
async def get_contact_by_email(email: str) -> dict | None:
    """Look up one contact by exact email address."""
    try:
        return await mscontacts.get_contact_by_email(USER_ID, email)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ── outbound (opt-in) ─────────────────────────────────────────────────────────
# Registered only when M365_ALLOW_OUTBOUND is set. These SEND on the user's
# behalf with no approval step in between — see the module docstring.

if ALLOW_OUTBOUND:

    @mcp.tool
    async def send_mail(to: str, subject: str, body: str,
                        cc: Optional[str] = None) -> dict:
        """Send an email from the connected mailbox. This delivers immediately."""
        try:
            await msmail.send_message(USER_ID, to=to, subject=subject, body=body)
            return {"sent": True, "to": to, "subject": subject}
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool
    async def mark_as_read(message_id: str) -> dict:
        """Mark one message as read."""
        try:
            await msmail.mark_as_read(USER_ID, message_id)
            return {"marked_read": True, "message_id": message_id}
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool
    async def create_event(title: str, start: str, end: str,
                           attendees: Optional[str] = None) -> dict:
        """Create a calendar event. `start`/`end` are ISO-8601. Invites are sent."""
        try:
            ev = await mscalendar.create_event(USER_ID, title=title, start=start, end=end)
            return {"created": True, "event": ev}
        except Exception as e:  # noqa: BLE001
            return _err(e)


def main() -> None:
    # stderr, never stdout: stdout IS the MCP transport, and a stray log line
    # there corrupts the JSON-RPC stream and the host drops the connection.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    mode = "read+write" if ALLOW_OUTBOUND else "read-only"
    log.warning("m365 mcp: serving for user %s (%s)", USER_ID, mode)
    mcp.run()


if __name__ == "__main__":
    main()
