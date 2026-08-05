"""
Gmail integration (per-user, via Google OAuth tokens).

Mirrors the response shapes the dashboard/routes already expect so existing
endpoints keep working. All calls are async over the shared httpx client.
Never logs tokens. Falls back to mock data when Google creds are unconfigured.
"""
from __future__ import annotations

import re
from html import unescape as _unescape
import base64
import asyncio
import logging
from email.utils import parseaddr, parsedate_to_datetime

from fastapi import HTTPException

from backend.services.provider_tokens import get_google_headers, GOOGLE_CONFIGURED
from backend.services.http_client import google_request

log = logging.getLogger("aria.gmail")

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

_MOCK_INBOX: list[dict] = []  # no fake data — routes return connected:False when Google is unconfigured


def _b64url_decode(data: str) -> bytes:
    data = data.replace("-", "+").replace("_", "/")
    data += "=" * (-len(data) % 4)
    try:
        return base64.b64decode(data)
    except Exception:
        return b""


def _strip_html(html: str) -> str:
    """HTML mail body → readable plain text.

    Entities are unescaped LAST, after tags are gone: doing it first would turn an
    escaped "&lt;div&gt;" in the message text into a real tag and delete it. Without
    this step the model reads back literal "&lt;name@host&gt;" and quotes it to the
    user."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _has_attachments(payload: dict) -> bool:
    for part in payload.get("parts", []) or []:
        if part.get("filename"):
            return True
        if not (part.get("mimeType", "").startswith("text/")):
            if part.get("body", {}).get("attachmentId"):
                return True
    return False


async def get_gmail_inbox(user_id: str, max_results: int = 20) -> list[dict]:
    """Return recent INBOX messages in the dashboard's email shape."""
    if not GOOGLE_CONFIGURED:
        return _MOCK_INBOX
    headers = await get_google_headers(user_id)
    listing = await google_request("GET", f"{GMAIL}/messages", headers=headers,
                                   params={"labelIds": "INBOX", "maxResults": max_results})
    if listing.status_code != 200:
        log.warning("gmail list failed for %s: %s", user_id, listing.status_code)
        return []
    ids = [m["id"] for m in (listing.json().get("messages") or [])]

    async def _one(mid: str) -> dict | None:
        r = await google_request("GET", f"{GMAIL}/messages/{mid}", headers=headers, params={
            "format": "metadata",
            "metadataHeaders": ["Subject", "From", "Date"],
        })
        if r.status_code != 200:
            return None
        m = r.json()
        hs = m.get("payload", {}).get("headers", [])
        labels = m.get("labelIds", []) or []
        from_raw = _header(hs, "From")
        from_name, from_email = parseaddr(from_raw)
        date_raw = _header(hs, "Date")
        try:
            received = parsedate_to_datetime(date_raw).isoformat() if date_raw else ""
        except Exception:
            received = ""
        return {
            "id": m.get("id"),
            "subject": _header(hs, "Subject") or "(no subject)",
            "from_name": from_name or from_email,
            "from_email": from_email,
            "preview": (m.get("snippet") or "")[:150],
            "received_at": received,
            "is_read": "UNREAD" not in labels,
            "is_important": "IMPORTANT" in labels,
            "has_attachments": _has_attachments(m.get("payload", {})),
            "labels": labels,
        }

    results = await asyncio.gather(*[_one(i) for i in ids], return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def get_gmail_unread_count(user_id: str) -> int:
    """Return the INBOX unread count (cheap; reads the label resource)."""
    if not GOOGLE_CONFIGURED:
        return len([m for m in _MOCK_INBOX if not m["is_read"]])
    headers = await get_google_headers(user_id)
    r = await google_request("GET", f"{GMAIL}/labels/INBOX", headers=headers)
    if r.status_code != 200:
        return 0
    return int(r.json().get("messagesUnread", 0) or 0)


async def get_gmail_message_body(user_id: str, message_id: str) -> str:
    """Return the plain-text body of a message (HTML stripped if needed)."""
    if not GOOGLE_CONFIGURED:
        return "Connect Google to read this message."
    headers = await get_google_headers(user_id)
    r = await google_request("GET", f"{GMAIL}/messages/{message_id}",
                             headers=headers, params={"format": "full"})
    if r.status_code != 200:
        return ""
    payload = r.json().get("payload", {})

    def _walk(p: dict) -> tuple[str, str]:
        mime = p.get("mimeType", "")
        body = p.get("body", {})
        if mime == "text/plain" and body.get("data"):
            return _b64url_decode(body["data"]).decode("utf-8", "ignore"), ""
        if mime == "text/html" and body.get("data"):
            return "", _b64url_decode(body["data"]).decode("utf-8", "ignore")
        plain, html = "", ""
        for part in p.get("parts", []) or []:
            pp, hh = _walk(part)
            plain = plain or pp
            html = html or hh
        return plain, html

    plain, html = _walk(payload)
    return plain.strip() or _strip_html(html)


async def send_gmail_message(user_id: str, to: str, subject: str, body: str,
                             reply_to_id: str | None = None) -> dict:
    """Send an email as the user. Replies in-thread when reply_to_id is given."""
    if not GOOGLE_CONFIGURED:
        # Don't silently pretend success — surface the real state so the
        # frontend can prompt the user to connect Google.
        raise HTTPException(status_code=503, detail={
            "error": "google_not_configured",
            "message": "Google OAuth is not configured on the server (GOOGLE_CLIENT_ID/SECRET).",
        })
    headers = await get_google_headers(user_id)
    raw_mime = f"To: {to}\r\nSubject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}"
    raw = base64.urlsafe_b64encode(raw_mime.encode("utf-8")).decode()
    payload: dict = {"raw": raw}
    if reply_to_id:
        thr = await google_request("GET", f"{GMAIL}/messages/{reply_to_id}",
                                   headers=headers, params={"format": "metadata"})
        if thr.status_code == 200:
            payload["threadId"] = thr.json().get("threadId")
    r = await google_request("POST", f"{GMAIL}/messages/send", headers=headers, json=payload)
    if r.status_code not in (200, 201):
        log.warning("gmail send failed for %s: %s", user_id, r.status_code)
        raise RuntimeError(f"Gmail send failed ({r.status_code})")
    d = r.json()
    return {"id": d.get("id"), "thread_id": d.get("threadId")}


async def get_gmail_email_digest(user_id: str) -> dict:
    """Summarize the latest unread messages for the dashboard digest panel."""
    if not GOOGLE_CONFIGURED:
        return {"unread_count": 0, "important_count": 0, "senders": [], "subjects": [],
                "summary": "Connect Google in Settings → Account to see your email digest."}
    headers = await get_google_headers(user_id)
    listing = await google_request("GET", f"{GMAIL}/messages", headers=headers,
                                   params={"q": "is:unread in:inbox", "maxResults": 10})
    ids = [m["id"] for m in (listing.json().get("messages") or [])] if listing.status_code == 200 else []

    senders, subjects, important = [], [], 0

    async def _one(mid: str):
        r = await google_request("GET", f"{GMAIL}/messages/{mid}", headers=headers, params={
            "format": "metadata", "metadataHeaders": ["Subject", "From"]})
        return r.json() if r.status_code == 200 else None

    for m in await asyncio.gather(*[_one(i) for i in ids], return_exceptions=True):
        if not isinstance(m, dict):
            continue
        hs = m.get("payload", {}).get("headers", [])
        name, _ = parseaddr(_header(hs, "From"))
        if name and name not in senders:
            senders.append(name)
        subj = _header(hs, "Subject")
        if subj:
            subjects.append(subj)
        if "IMPORTANT" in (m.get("labelIds") or []):
            important += 1

    unread = await get_gmail_unread_count(user_id)
    top = senders[:5]
    summary = (f"You have {unread} unread email(s)"
               + (f" from {', '.join(top)}." if top else ".")) if unread else "No unread email."
    return {"unread_count": unread, "important_count": important,
            "senders": top, "subjects": subjects[:5], "summary": summary}
