import httpx
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from integrations.m365_mail import fetch_unread_emails

# Keywords that raise an email's importance score
_URGENT_WORDS = re.compile(
    r"\b(urgent|asap|action required|deadline|critical|immediately|today|overdue|"
    r"important|please respond|response needed|time.sensitive)\b",
    re.IGNORECASE,
)


def _priority_score(email: dict, known_emails: set[str]) -> int:
    """
    Score an email 0-10 for prioritised display. Higher = show first.
    Factors: sender is a known contact, Graph importance flag, urgent keywords,
    recency (received in last 2 h), is part of a multi-message thread.
    """
    score = 0
    sender = email.get("from_email", "").lower()
    if sender and sender in known_emails:
        score += 3
    importance = email.get("importance", "normal")
    if importance == "high":
        score += 3
    subject = email.get("subject", "")
    body    = email.get("body_preview", "") or email.get("body", "")
    if _URGENT_WORDS.search(subject) or _URGENT_WORDS.search(body[:300]):
        score += 2
    recv = email.get("received_at", "")
    if recv:
        try:
            dt = datetime.fromisoformat(recv.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if age_h < 2:
                score += 1
        except Exception:
            pass
    if email.get("thread_count", 1) > 1:
        score += 1
    return score


def _known_sender_emails() -> set[str]:
    """Quickly fetch all contact email addresses from the DB for scoring."""
    try:
        import sqlite3
        from config.settings import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT lower(email) FROM contacts WHERE email IS NOT NULL").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def summarise_email(subject: str, body: str) -> str:
    """Summarise the email body in one sentence (≤15 words) via the fast local LLM."""
    payload = {
        "model": "local-model",
        "messages": [{
            "role": "user",
            "content": (
                "Summarise this email in ONE sentence. Maximum 15 words. "
                "No preamble. Output only the summary sentence.\n\n"
                f"Subject: {subject}\nBody (first 300 chars): {body[:300]}"
            ),
        }],
        "max_tokens": 40,
        "temperature": 0.1,
    }
    try:
        r = httpx.post("http://localhost:8081/v1/chat/completions", json=payload, timeout=12.0)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        pass
    return subject


def summarise_thread(user_id: str, conversation_id: str) -> str:
    """
    Fetch all messages in a conversation thread and return a concise LLM summary.
    Returns a plain-text paragraph the agent can relay to the user.
    """
    from integrations.m365_mail import fetch_conversation
    try:
        msgs = fetch_conversation(user_id, conversation_id)
    except Exception as e:
        return f"Could not fetch thread: {e}"
    if not msgs:
        return "No messages found in this thread."

    thread_text = ""
    for m in msgs:
        thread_text += f"\n---\nFrom: {m['from']}\nDate: {m['date']}\n{m['body']}\n"

    payload = {
        "model": "local-model",
        "messages": [{
            "role": "user",
            "content": (
                "Summarise this email thread in 3-5 bullet points. "
                "Focus on decisions made, action items, and open questions. "
                "Be concise.\n\n" + thread_text[:3000]
            ),
        }],
        "max_tokens": 200,
        "temperature": 0.2,
    }
    try:
        r = httpx.post("http://localhost:8080/v1/chat/completions", json=payload, timeout=30.0)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Summary failed: {e}"
    return "Could not summarise thread."


def build_digest(user_id: str, emails: list[dict]) -> str:
    """
    Builds the digest Markdown string from a list of email dicts,
    sorted by priority score (importance, known sender, urgency keywords, recency).
    """
    total_count = len(emails)
    if total_count == 0:
        return "📬 No unread emails right now. Your inbox is clear! ✅"

    # Group by conversation to detect threads and add thread_count
    conv_counts: dict[str, int] = {}
    for e in emails:
        cid = e.get("conversation_id", e.get("id", ""))
        conv_counts[cid] = conv_counts.get(cid, 0) + 1
    for e in emails:
        cid = e.get("conversation_id", e.get("id", ""))
        e["thread_count"] = conv_counts.get(cid, 1)

    known = _known_sender_emails()
    ranked = sorted(emails, key=lambda e: _priority_score(e, known), reverse=True)
    top_emails = ranked[:3]

    now = datetime.now()
    today_str = f"{now.strftime('%A, %B')} {int(now.strftime('%d'))}"
    lines = [f"📬 *Email Digest* — {today_str}", ""]

    plural = "s" if total_count != 1 else ""
    lines.append(f"You have *{total_count} unread email{plural}*.")
    lines.append("")
    lines.append(f"Here are the top {min(3, total_count)} by priority:")
    lines.append("")

    number_emojis = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣"}

    for idx, e in enumerate(top_emails, 1):
        sender = e.get("from_name") or e.get("from_email", "")
        subject = e.get("subject", "")
        body = e.get("body") or e.get("body_preview", "")
        summary = summarise_email(subject, body)
        thread_n = e.get("thread_count", 1)
        thread_tag = f" _(thread: {thread_n} msgs)_" if thread_n > 1 else ""
        importance_tag = " 🔴" if e.get("importance") == "high" else ""

        emoji = number_emojis.get(idx, f"{idx}️⃣")
        lines.append(f"{emoji} *From:* {sender}{importance_tag}{thread_tag}")
        lines.append(f"   *Subject:* {subject}")
        lines.append(f"   _{summary}_")
        lines.append("")

    lines.append(
        'Reply with *"show email 1"* to see the full email, '
        '*"summarize email 1 thread"* for the full thread summary, '
        'or *"reply to email 1"* to draft a response.'
    )
    return "\n".join(lines)


def get_top_emails_for_digest(user_id: str, top: int = 3) -> list[dict]:
    """Returns the top N unread emails in a compact shape for dashboards."""
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
    Generates a formatted digest of unread emails, ranked by priority.
    Live from Microsoft Graph; falls back to last snapshot in email_store/{user_id}/unread.json.
    """
    if not bypass_disable_check and Path(f"email_store/{user_id}/digest_disabled").exists():
        return "📬 Scheduled email digest is currently disabled. ✅"

    try:
        emails = fetch_unread_emails(user_id, top=20)
        return build_digest(user_id, _emails_for_build_digest(emails))
    except Exception:
        pass

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
