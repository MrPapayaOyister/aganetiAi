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


def internal_headers(user_id: str = "user_1") -> dict:
    """Headers for an internal/self call so it passes global auth enforcement."""
    return {
        "X-Internal-Token": internal_token(),
        "X-Internal-User": user_id or "user_1",
    }
