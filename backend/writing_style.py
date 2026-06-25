"""
Writing-style learning for email drafts.

When a draft is sent (accepted) from the approval queue, the final body is logged
here as a style example. On future draft_email calls, get_style_context() returns
the 2 most recent examples so the model can match the user's tone and length.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from config.settings import MEMORY_DIR


def _style_path(user_id: str) -> Path:
    p = MEMORY_DIR / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p / "style_examples.jsonl"


def record_sent_draft(user_id: str, subject: str, body: str, to_email: str = "") -> None:
    """
    Save an accepted/sent email body as a style reference.
    Called by the /send_email endpoint after a successful send.
    Keeps the last 20 examples (older ones are dropped from the end).
    """
    body_clean = re.sub(r"\s+", " ", body).strip()
    if len(body_clean) < 20:
        return  # ignore trivial / empty drafts

    entry = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "subject": subject[:120],
        "to":      to_email,
        "body":    body_clean[:800],
    }
    path = _style_path(user_id)
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(0, json.dumps(entry))     # newest first
    lines = lines[:20]                     # keep last 20
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_style_context(user_id: str, n: int = 2) -> str:
    """
    Return a compact block (injected into the system prompt) with the user's
    recent sent emails as style exemplars. Empty string if none on file yet.
    """
    path = _style_path(user_id)
    if not path.exists():
        return ""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return ""

    examples = []
    for line in lines[:n]:
        try:
            e = json.loads(line)
            examples.append(
                f'Subject: {e["subject"]}\n{e["body"][:300]}'
            )
        except Exception:
            pass

    if not examples:
        return ""

    block = "\n\n---\n".join(examples)
    return (
        f"[WRITING STYLE]\n"
        f"Match the user's email tone and length. "
        f"Here are {len(examples)} recent emails they sent (use as style reference):\n\n"
        f"{block}\n"
        f"[/WRITING STYLE]\n"
    )
