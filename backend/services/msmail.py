"""
Outlook / Exchange mail over Microsoft Graph (per-user OAuth tokens).

Async twin of `backend/services/gmail.py`. Returns the SAME dict shape the Gmail
service returns, plus the Graph-only extras earlier code depends on
(conversation_id, body_preview/body_text/body_html, importance) — so the digest,
triage and dashboard paths work against either provider without branching.

All calls go through the shared retrying httpx client. Never logs tokens.
"""
from __future__ import annotations

import re
import logging

from fastapi import HTTPException

from backend.services.provider_tokens import get_ms_headers, MS_CONFIGURED
from backend.services.http_client import api_request

log = logging.getLogger("aria.msmail")

GRAPH = "https://graph.microsoft.com/v1.0"

_SELECT = ("id,subject,from,bodyPreview,receivedDateTime,body,isRead,conversationId,"
           "importance,hasAttachments,toRecipients")


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_message(msg: dict) -> dict:
    """Graph message → the shared email shape (Gmail keys + Graph extras)."""
    addr = (msg.get("from") or {}).get("emailAddress") or {}
    body = msg.get("body") or {}
    content = body.get("content", "") or ""
    is_html = (body.get("contentType") or "").lower() == "html"
    preview = msg.get("bodyPreview", "") or ""
    importance = msg.get("importance", "normal")

    labels = []
    if not msg.get("isRead", True):
        labels.append("UNREAD")
    if importance == "high":
        labels.append("IMPORTANT")

    return {
        "id":              msg.get("id", ""),
        "conversation_id": msg.get("conversationId", ""),
        "subject":         msg.get("subject") or "(no subject)",
        "from_name":       addr.get("name", "") or addr.get("address", ""),
        "from_email":      addr.get("address", ""),
        # `preview` is the Gmail key, `body_preview` the Graph one — emit both.
        "preview":         preview[:150],
        "body_preview":    preview,
        "received_at":     msg.get("receivedDateTime", ""),
        "is_read":         bool(msg.get("isRead", False)),
        "is_important":    importance == "high",
        "importance":      importance,
        "has_attachments": bool(msg.get("hasAttachments", False)),
        "labels":          labels,
        "body_html":       content if is_html else "",
        "body_text":       (content if not is_html else _strip_html(content)) or preview,
    }


async def get_inbox(user_id: str, max_results: int = 20) -> list[dict]:
    """Recent INBOX messages, newest first."""
    if not MS_CONFIGURED:
        return []
    headers = await get_ms_headers(user_id)
    r = await api_request("GET", f"{GRAPH}/me/mailFolders/inbox/messages", headers=headers,
                          params={"$top": max_results,
                                  "$orderby": "receivedDateTime desc",
                                  "$select": _SELECT})
    if r.status_code != 200:
        log.warning("graph inbox failed for %s: %s", user_id, r.status_code)
        return []
    return [_parse_message(m) for m in (r.json().get("value") or [])]


async def get_unread(user_id: str, max_results: int = 20) -> list[dict]:
    """Unread INBOX messages, newest first (the triage / digest feed)."""
    if not MS_CONFIGURED:
        return []
    headers = await get_ms_headers(user_id)
    r = await api_request("GET", f"{GRAPH}/me/mailFolders/inbox/messages", headers=headers,
                          params={"$filter": "isRead eq false",
                                  "$top": max_results,
                                  "$orderby": "receivedDateTime desc",
                                  "$select": _SELECT})
    if r.status_code != 200:
        log.warning("graph unread fetch failed for %s: %s", user_id, r.status_code)
        return []
    return [_parse_message(m) for m in (r.json().get("value") or [])]


async def get_unread_count(user_id: str) -> int:
    if not MS_CONFIGURED:
        return 0
    return (await get_inbox_count(user_id)).get("unread", 0)


async def get_inbox_count(user_id: str) -> dict:
    """Unread + total counts for the inbox folder. Cheap — no bodies fetched."""
    if not MS_CONFIGURED:
        return {"unread": 0, "total": 0}
    headers = await get_ms_headers(user_id)
    r = await api_request("GET", f"{GRAPH}/me/mailFolders/inbox", headers=headers)
    if r.status_code != 200:
        return {"unread": 0, "total": 0}
    f = r.json()
    return {"unread": f.get("unreadItemCount", 0), "total": f.get("totalItemCount", 0)}


async def get_message(user_id: str, message_id: str) -> dict | None:
    """Full message by id. None when it doesn't exist (404) or the id is malformed (400)."""
    if not MS_CONFIGURED:
        return None
    headers = await get_ms_headers(user_id)
    r = await api_request("GET", f"{GRAPH}/me/messages/{message_id}", headers=headers)
    if r.status_code in (400, 404):
        return None
    if r.status_code != 200:
        return None
    return _parse_message(r.json())


async def get_message_body(user_id: str, message_id: str) -> str:
    """Plain-text body of a message (HTML stripped if needed)."""
    if not MS_CONFIGURED:
        return "Connect Microsoft 365 to read this message."
    msg = await get_message(user_id, message_id)
    if not msg:
        return ""
    return (msg.get("body_text") or "").strip()


async def get_conversation(user_id: str, conversation_id: str, top: int = 20) -> list[dict]:
    """All messages in a thread, oldest-first (for summarization)."""
    if not MS_CONFIGURED:
        return []
    headers = await get_ms_headers(user_id)
    r = await api_request("GET", f"{GRAPH}/me/messages", headers=headers, params={
        "$filter": f"conversationId eq '{conversation_id}'",
        "$orderby": "receivedDateTime asc",
        "$top": top,
        "$select": "id,subject,from,bodyPreview,receivedDateTime,body",
    })
    if r.status_code != 200:
        return []
    out = []
    for msg in r.json().get("value", []):
        m = _parse_message(msg)
        out.append({
            "from": f"{m['from_name']} <{m['from_email']}>" if m["from_name"] else m["from_email"],
            "subject": m["subject"],
            "date": m["received_at"][:10],
            "body": (m["body_text"] or "")[:800],
        })
    return out


async def send_message(user_id: str, to: str, subject: str, body: str,
                       reply_to_id: str | None = None,
                       body_type: str = "Text") -> dict:
    """
    Send mail as the user. When `reply_to_id` is given, Graph's /reply keeps the
    message in-thread (the equivalent of Gmail's threadId). Saves to Sent Items.
    """
    if not MS_CONFIGURED:
        raise HTTPException(status_code=503, detail={
            "error": "microsoft_not_configured",
            "message": "Microsoft OAuth is not configured on the server "
                       "(M365_CLIENT_ID/M365_CLIENT_SECRET).",
        })
    headers = await get_ms_headers(user_id)

    if reply_to_id:
        r = await api_request("POST", f"{GRAPH}/me/messages/{reply_to_id}/reply",
                              headers=headers,
                              json={"message": {"toRecipients": [
                                        {"emailAddress": {"address": to}}]},
                                    "comment": body})
        if r.status_code not in (200, 201, 202):
            log.warning("graph reply failed for %s: %s", user_id, r.status_code)
            raise RuntimeError(f"Graph reply failed ({r.status_code})")
        return {"id": reply_to_id, "thread_id": reply_to_id}

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": body_type, "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": True,
    }
    r = await api_request("POST", f"{GRAPH}/me/sendMail", headers=headers, json=payload)
    if r.status_code not in (200, 201, 202):   # sendMail returns 202 with no body
        log.warning("graph send failed for %s: %s", user_id, r.status_code)
        raise RuntimeError(f"Graph send failed ({r.status_code})")
    return {"id": "", "thread_id": ""}


async def mark_as_read(user_id: str, message_id: str) -> None:
    if not MS_CONFIGURED:
        return
    headers = await get_ms_headers(user_id)
    await api_request("PATCH", f"{GRAPH}/me/messages/{message_id}",
                      headers=headers, json={"isRead": True})


async def get_email_digest(user_id: str) -> dict:
    """Summarize the latest unread messages for the dashboard digest panel.
    Mirrors gmail.get_gmail_email_digest's return shape."""
    if not MS_CONFIGURED:
        return {"unread_count": 0, "important_count": 0, "senders": [], "subjects": [],
                "summary": "Connect Microsoft 365 in Settings → Account to see your email digest."}
    emails = await get_unread(user_id, max_results=10)
    senders, subjects = [], []
    for m in emails:
        name = m.get("from_name") or m.get("from_email")
        if name and name not in senders:
            senders.append(name)
        if m.get("subject"):
            subjects.append(m["subject"])
    important = sum(1 for m in emails if m.get("is_important"))
    unread = (await get_inbox_count(user_id)).get("unread", 0)
    top = senders[:5]
    summary = (f"You have {unread} unread email(s)"
               + (f" from {', '.join(top)}." if top else ".")) if unread else "No unread email."
    return {"unread_count": unread, "important_count": important,
            "senders": top, "subjects": subjects[:5], "summary": summary}
