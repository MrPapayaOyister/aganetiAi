import httpx
import json
from datetime import datetime, timezone
from pathlib import Path

from integrations.m365_mail import fetch_unread_emails

def summarise_email(subject: str, body: str) -> str:
    """
    Summarises the email body in one sentence (max 15 words) using the local LLM.
    Returns the subject if any failure occurs.
    """
    payload = {
        "model": "local-model",
        "messages": [
            {
                "role": "user",
                "content": f"Summarise this email in ONE sentence. Maximum 15 words. \nNo preamble. Output only the summary sentence.\n\nSubject: {subject}\nBody (first 300 chars): {body[:300]}"
            }
        ],
        "max_tokens": 40,
        "temperature": 0.1
    }
    try:
        response = httpx.post("http://localhost:8080/v1/chat/completions", json=payload, timeout=15.0)
        if response.status_code == 200:
            content = response.json()["choices"][0]["message"]["content"]
            return content.strip()
    except Exception:
        pass
    return subject

def build_digest(user_id: str, emails: list[dict]) -> str:
    """
    Builds the digest Markdown formatted string from a list of email dictionaries.
    """
    total_count = len(emails)
    if total_count == 0:
        return "📬 No unread emails right now. Your inbox is clear! ✅"

    # Sort by received_at descending (newest first)
    emails = sorted(emails, key=lambda x: x.get("received_at", ""), reverse=True)
    top_emails = emails[:3]

    # Platform-independent strftime without leading zero for day
    now = datetime.now()
    day = int(now.strftime("%d"))
    today_str = f"{now.strftime('%A, %B')} {day}"

    lines = []
    lines.append(f"📬 *Email Digest* — {today_str}")
    lines.append("")

    plural_suffix = "s" if total_count != 1 else ""
    lines.append(f"You have *{total_count} unread email{plural_suffix}*.")
    lines.append("")
    lines.append(f"Here are the top {min(3, total_count)}:")
    lines.append("")

    number_emojis = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣"}

    for idx, email_item in enumerate(top_emails, 1):
        from_name = email_item.get("from_name", "")
        from_email = email_item.get("from_email", "")
        sender = from_name if from_name else from_email
        subject = email_item.get("subject", "")
        body = email_item.get("body", "")

        summary = summarise_email(subject, body)

        emoji = number_emojis.get(idx, f"{idx}️⃣")
        lines.append(f"{emoji} *From:* {sender}")
        lines.append(f"   *Subject:* {subject}")
        lines.append(f"   _{summary}_")
        lines.append("")

    lines.append('Reply with *"show email 1"* to see the full email, or *"reply to email 1"* to draft a response.')
    return "\n".join(lines)

def get_top_emails_for_digest(user_id: str, top: int = 3) -> list[dict]:
    """
    Returns the top N unread emails (live from Microsoft Graph) in a compact shape for
    dashboards / quick previews. Distinct from build_digest's input shape.
    """
    emails = fetch_unread_emails(user_id, top=top)
    return [
        {
            "from":    f"{e['from_name']} <{e['from_email']}>" if e.get("from_name") else e.get("from_email", ""),
            "subject": e.get("subject", ""),
            "preview": e.get("body_preview", "")[:200],
            "id":      e.get("id", ""),
        }
        for e in emails
    ]


def _emails_for_build_digest(emails: list[dict]) -> list[dict]:
    """Map Graph fetch dicts (body_text/body_preview) to the keys build_digest expects."""
    for e in emails:
        if "body" not in e:
            e["body"] = e.get("body_text") or e.get("body_preview", "")
    return emails


def get_digest_for_user(user_id: str, bypass_disable_check: bool = False) -> str:
    """
    Generates a formatted digest of unread emails for user_id.
    Fetches live from Microsoft Graph; on any failure, falls back to the last snapshot in
    email_store/{user_id}/unread.json (written by the inbox poll), then to "inbox clear".
    """
    # Check if disabled via flag file
    if not bypass_disable_check and Path(f"email_store/{user_id}/digest_disabled").exists():
        return "📬 Scheduled email digest is currently disabled. ✅"

    # Primary path — live Microsoft Graph fetch
    try:
        emails = fetch_unread_emails(user_id, top=20)
        return build_digest(user_id, _emails_for_build_digest(emails))
    except Exception:
        pass

    # Fallback — last snapshot written by the inbox poll
    p = Path(f"email_store/{user_id}/unread.json")
    if not p.exists():
        return "📬 No unread emails right now. Your inbox is clear! ✅"
    try:
        emails = json.loads(p.read_text())
        if not isinstance(emails, list):
            return "📬 No unread emails right now. Your inbox is clear! ✅"
        return build_digest(user_id, emails)
    except Exception:
        return "📬 No unread emails right now. Your inbox is clear! ✅"
