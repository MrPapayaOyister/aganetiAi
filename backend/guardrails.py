"""
Action guardrails — the single place that decides what Aria may do autonomously,
what must produce a draft/approval step, and what is outright denied.

Research consensus (2026): an agent's reliability comes from explicit tool contracts
and approval policy, not the model alone. This centralizes that policy so no tool can
silently bypass it, and so the autonomy posture is one env var.

AUTONOMY_LEVEL:
  - "assist"      : even task creation is gated (approval) — most conservative
  - "standard"    : tasks/reads are autonomous; comms (email/meeting) are approval-gated  (default)
  - "autonomous"  : everything the agent can do runs without sign-off
                    (note: there is deliberately NO direct "send email" tool — comms
                     always become a draft for the human to send)
"""

from __future__ import annotations

import os

AUTONOMY_LEVEL = os.getenv("AUTONOMY_LEVEL", "standard").lower()

# Every dispatchable action MUST be listed here; anything else is denied by default.
ACTION_CATEGORY = {
    "create_task": "task",
    "complete_task": "task",
    "get_analytics": "read",
    "resolve_contact": "read",
    "search_knowledge": "read",
    "recall_memory": "read",
    "get_emails": "read",         # read-only inbox listing
    "read_email": "read",         # read-only: one message's body, no mutation
    "get_agenda": "read",         # read-only Google Calendar agenda
    "get_contacts": "read",       # read-only Google Contacts lookup
    "remember_fact": "task",      # benign memory write
    "set_reminder": "task",       # one-shot Telegram alert, no external side-effect
    "draft_email": "comms",       # produces a draft in the approval queue, never sends
    "schedule_meeting": "comms",
    "web_search": "read",
}

# verdicts: "auto" (do it), "approval" (do the draft/queue step), "deny" (refuse)
def decide(action_type: str) -> str:
    cat = ACTION_CATEGORY.get(action_type)
    if cat is None:
        return "deny"  # unknown / hallucinated action
    if cat == "read":
        return "auto"
    if cat == "task":
        return "auto" if AUTONOMY_LEVEL in ("standard", "autonomous") else "approval"
    if cat == "comms":
        return "auto" if AUTONOMY_LEVEL == "autonomous" else "approval"
    return "approval"


def policy_snapshot() -> dict:
    return {
        "autonomy_level": AUTONOMY_LEVEL,
        "actions": {a: {"category": c, "verdict": decide(a)}
                    for a, c in ACTION_CATEGORY.items()},
        "note": "comms actions always create a draft for the user to send; "
                "there is no autonomous send-email capability.",
    }
