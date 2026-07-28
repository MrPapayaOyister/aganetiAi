"""Proactive email intelligence.

Given a NEW email (from the inbox poller), the fast local model produces a one-line
summary and detects whether the email requires an action. It then raises a dashboard
notification, and — if there's an action — a 'task_proposal' the user can APPROVE to
create a real task (approval-gated: nothing is created without the user's click).

Sync + best-effort (runs inside the off-thread mail poll); never raises.
"""
from __future__ import annotations

import json
import os
import re

from backend import notifications


def _json_llm(prompt: str, max_tokens: int = 320) -> str:
    """Call the 32B tool model in JSON mode — the 7B is unreliable at strict JSON;
    the 32B + response_format=json_object returns well-formed output. Background job,
    so the extra latency is fine."""
    import httpx
    url = os.getenv("VLLM_TOOL_URL", "http://localhost:9000/v1").rstrip("/")
    model = os.getenv("VLLM_TOOL_MODEL", "qwen2.5-32b")
    r = httpx.post(f"{url}/chat/completions", timeout=90.0, json={
        "model": model, "temperature": 0.1, "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


_PROMPT = (
    "You are an executive assistant triaging a new email for your principal.\n"
    "From: {sender}\nSubject: {subject}\nBody:\n{body}\n\n"
    "Reply with ONLY a JSON object, no prose:\n"
    '{{"summary": "<one concise sentence>", '
    '"task": "<a short actionable task title IF the email asks the principal to do '
    'something or has a deadline, else empty string>", '
    '"priority": "High|Medium|Low"}}'
)


def _analyze(sender: str, subject: str, body: str) -> dict:
    try:
        raw = _json_llm(_PROMPT.format(sender=sender, subject=subject, body=body[:2500]))
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def proactive_alert(user_id: str, email: dict) -> None:
    """Summarize a new email → notify; if actionable → also raise a task_proposal."""
    try:
        subject = email.get("subject") or "(no subject)"
        sender = email.get("from_name") or email.get("from_email") or "Unknown sender"
        msg_id = email.get("id") or ""
        body = email.get("body_text") or email.get("body_preview") or ""

        data = _analyze(sender, subject, body)
        summary = (data.get("summary") or "").strip() or f"New email: {subject}"

        notifications.notify(
            user_id, title=f"\U0001F4E7 {sender}: {subject}", body=summary, kind="mail",
            entity={"message_id": msg_id, "sender": sender, "subject": subject},
            dedup_key=f"mail:{msg_id}" if msg_id else None)

        task = (data.get("task") or "").strip()
        if task:
            pri = (data.get("priority") or "Medium").capitalize()
            notifications.notify(
                user_id, title=f"✅ Suggested task: {task}",
                body=f"From “{subject}” ({sender}). Approve to add it to your tasks.",
                kind="task_proposal",
                entity={"title": task, "priority": pri, "source": "email", "subject": subject},
                dedup_key=f"task:{msg_id}" if msg_id else None)
    except Exception as e:  # noqa: BLE001
        print(f"[mail_intel] proactive_alert failed for {user_id}: {e}")
