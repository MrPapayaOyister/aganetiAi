"""
Internal service authentication (Phase 0 security).

The Telegram bot, the scheduler, and a few in-process self-calls talk to the
FastAPI backend over http://127.0.0.1:8000. Once the global auth middleware is
enforced, those callers must identify themselves — they have no Supabase JWT.

They present a shared secret header `X-Internal-Token` (value = INTERNAL_API_TOKEN
from .env) plus `X-Internal-User` (the internal user_id to act as). The auth
middleware accepts this ONLY when the token matches, and maps it to that user.

Note: after network lockdown the backend is only reachable via loopback (directly
by internal callers, and via the reverse proxy for external traffic), so the
token — not the source IP — is the real trust boundary. Keep it long and secret.
"""
from __future__ import annotations

import os


def internal_token() -> str:
    return os.getenv("INTERNAL_API_TOKEN", "")


def internal_headers(user_id: str) -> dict:
    """Headers for an internal/self call so it passes global auth enforcement.

    `user_id` is REQUIRED. It used to default to "user_1", so any internal caller
    that forgot to say whom it was acting for ran against the seeded admin's
    mailbox, calendar and tasks. The middleware now rejects the call outright if
    this header is empty, and this signature makes the omission a TypeError at the
    call site instead of a silent impersonation at the far end.
    """
    if not user_id:
        raise ValueError("internal_headers requires the user_id to act as")
    return {
        "X-Internal-Token": internal_token(),
        "X-Internal-User": user_id,
    }
