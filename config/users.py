"""
User registry for multi-user support (2-user POC).

Single source of truth mapping user_id <-> telegram_chat_id <-> M365 identity. Every entry
point (Telegram handlers, scheduler jobs, backend endpoints) resolves a stable user_id
string from here, which then flows through tasks, memory, mail and calendar.
"""

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import BASE_DIR
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

# ── User registry ──────────────────────────────────────────────
USERS: dict[str, dict] = {
    "user_1": {
        "telegram_chat_id":  int(os.getenv("USER_1_TELEGRAM_ID", "0")),
        "name":              os.getenv("USER_1_NAME", "User One"),
        "m365_email":        os.getenv("USER_1_M365_EMAIL", ""),
        "is_agent":          True,
        "agent_id":          "agent_1",
        "qdrant_collection": "memory_user_1",
    },
    "user_2": {
        "telegram_chat_id":  int(os.getenv("USER_2_TELEGRAM_ID", "0")),
        "name":              os.getenv("USER_2_NAME", "User Two"),
        "m365_email":        os.getenv("USER_2_M365_EMAIL", ""),
        "is_agent":          True,
        "agent_id":          "agent_2",
        "qdrant_collection": "memory_user_2",
    },
}

# ── Reverse lookup: telegram_chat_id (int) → user_id string ────
_TELEGRAM_ID_TO_USER: dict[int, str] = {
    v["telegram_chat_id"]: k
    for k, v in USERS.items()
    if v["telegram_chat_id"] != 0
}


def get_user_by_telegram_id(telegram_chat_id: int) -> dict | None:
    """
    Returns full user dict (including a "user_id" key) for a given Telegram chat ID.
    Returns None if unknown — callers MUST handle None (unauthorised user).
    """
    user_id = _TELEGRAM_ID_TO_USER.get(telegram_chat_id)
    if not user_id:
        return None
    return {"user_id": user_id, **USERS[user_id]}


def get_all_user_ids() -> list[str]:
    """Returns list of all registered user_id strings."""
    return list(USERS.keys())


def get_user_name(user_id: str) -> str:
    """Returns display name for user_id, fallback to user_id string."""
    return USERS.get(user_id, {}).get("name", user_id)
