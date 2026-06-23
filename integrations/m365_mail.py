"""
Microsoft 365 Mail Operations (Microsoft Graph)

All Graph mail operations for both users go through this module. Every function takes a
user_id and obtains a Bearer token via integrations.m365_auth.get_access_token(user_id),
so callers never deal with auth.

Used by: backend/main.py (inbox poll, /send_email, /mail endpoints),
reports/email_digest.py (digest fetch). Tokens/scopes are set up in M365-A.
"""

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from integrations.m365_auth import get_access_token

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT    = 30  # seconds — Graph API is fast but give headroom


def _headers(user_id: str) -> dict:
    return {
        "Authorization": f"Bearer {get_access_token(user_id)}",
        "Content-Type":  "application/json"
    }


def fetch_unread_emails(user_id: str, top: int = 20) -> list[dict]:
    """
    Fetch unread emails from inbox via Microsoft Graph.
    Returns list of dicts with keys:
      id, subject, from_name, from_email, body_preview,
      received_at, body_html, body_text
    """
    url    = f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
    params = {
        "$filter":  "isRead eq false",
        "$top":     top,
        "$orderby": "receivedDateTime desc",
        "$select":  "id,subject,from,bodyPreview,receivedDateTime,body,isRead"
    }
    resp = httpx.get(url, headers=_headers(user_id), params=params, timeout=TIMEOUT)
    resp.raise_for_status()

    emails = []
    for msg in resp.json().get("value", []):
        emails.append({
            "id":           msg["id"],
            "subject":      msg.get("subject", "(no subject)"),
            "from_name":    msg.get("from", {}).get("emailAddress", {}).get("name", ""),
            "from_email":   msg.get("from", {}).get("emailAddress", {}).get("address", ""),
            "body_preview": msg.get("bodyPreview", ""),
            "received_at":  msg.get("receivedDateTime", ""),
            "body_html":    msg.get("body", {}).get("content", "") if msg.get("body", {}).get("contentType") == "html" else "",
            "body_text":    msg.get("body", {}).get("content", "") if msg.get("body", {}).get("contentType") == "text" else msg.get("bodyPreview", ""),
        })
    return emails


def send_email(
    user_id:   str,
    to_email:  str,
    subject:   str,
    body:      str,
    body_type: str = "HTML"   # "HTML" or "Text"
) -> None:
    """
    Send email on behalf of user_id via Microsoft Graph.
    Saves to Sent Items automatically (saveToSentItems: true).
    """
    url     = f"{GRAPH_BASE}/me/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": body_type,
                "content":     body
            },
            "toRecipients": [
                {"emailAddress": {"address": to_email}}
            ]
        },
        "saveToSentItems": True
    }
    resp = httpx.post(url, headers=_headers(user_id), json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    # Graph returns 202 Accepted for sendMail — no body


def mark_as_read(user_id: str, message_id: str) -> None:
    """Mark a specific message as read after triage processing."""
    url  = f"{GRAPH_BASE}/me/messages/{message_id}"
    resp = httpx.patch(
        url,
        headers=_headers(user_id),
        json={"isRead": True},
        timeout=15
    )
    resp.raise_for_status()


def get_email_by_id(user_id: str, message_id: str) -> dict | None:
    """
    Fetch a full email by ID. Returns None if the message does not exist (404) or the ID is
    malformed (Graph returns 400 for an invalid/nonexistent message-id format).
    """
    url  = f"{GRAPH_BASE}/me/messages/{message_id}"
    resp = httpx.get(url, headers=_headers(user_id), timeout=TIMEOUT)
    if resp.status_code in (400, 404):
        return None
    resp.raise_for_status()
    msg = resp.json()
    return {
        "id":           msg["id"],
        "subject":      msg.get("subject", ""),
        "from_name":    msg.get("from", {}).get("emailAddress", {}).get("name", ""),
        "from_email":   msg.get("from", {}).get("emailAddress", {}).get("address", ""),
        "body_preview": msg.get("bodyPreview", ""),
        "received_at":  msg.get("receivedDateTime", ""),
        "body_html":    msg.get("body", {}).get("content", ""),
    }


def get_inbox_count(user_id: str) -> dict:
    """Returns unread and total count for inbox. Cheap — no message bodies fetched."""
    url    = f"{GRAPH_BASE}/me/mailFolders/inbox"
    resp   = httpx.get(url, headers=_headers(user_id), timeout=15)
    resp.raise_for_status()
    folder = resp.json()
    return {
        "unread": folder.get("unreadItemCount", 0),
        "total":  folder.get("totalItemCount", 0)
    }
