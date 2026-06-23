"""
Configuration Settings Module

This module loads environment variables from a `.env` file at the project root.
By centralizing all configuration settings here, other modules (such as backend API,
mail watcher, etc.) can import settings cleanly. This enables easy modularization
if these services are ever split into separate packages/containers.
"""

from pathlib import Path
import os
from dotenv import load_dotenv

# BASE_DIR is the project root — always absolute, regardless of where uvicorn is launched from
BASE_DIR = Path(__file__).resolve().parent.parent
# e.g. if settings.py is at ~/projects/config/settings.py → BASE_DIR = ~/projects/

load_dotenv(BASE_DIR / ".env")

# Memory
MEMORY_DIR  = BASE_DIR / "memory"
TOKENS_DIR  = BASE_DIR / "tokens"
LOGS_DIR    = BASE_DIR / "logs"
TEMP_DIR    = BASE_DIR / "temp"
EMAIL_STORE = BASE_DIR / "email_store"

# Database
DB_PATH = str(BASE_DIR / "tasks.db")

# LLM Config
LLM_BASE_URL = os.getenv("LLM_BASE_URL")
LLM_SMART_URL = os.getenv("LLM_SMART_URL", "http://localhost:8080")
LLM_FAST_URL  = os.getenv("LLM_FAST_URL",  "http://localhost:8081")

# Vector DB Config
QDRANT_URL = os.getenv("QDRANT_URL")

# Email Credentials
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
APP_PASSWORD = os.getenv("APP_PASSWORD")

# SMTP Outbox Config
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT")

# IMAP Inbox Config
IMAP_SERVER = os.getenv("IMAP_SERVER")

# Telegram Bot Configuration
# Used for routing Telegram chat events and enforcing authorized user access
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Google Calendar Config
CALENDAR_TOKEN_PATH = str(TOKENS_DIR / "calendar_token.json")

# TTS Configuration
TTS_ENABLED = os.getenv("TTS_ENABLED", "false").lower() == "true"

# Microsoft 365
M365_CLIENT_ID  = os.getenv("M365_CLIENT_ID", "")
M365_TENANT_ID  = os.getenv("M365_TENANT_ID", "common")
M365_SCOPES     = os.getenv(
    "M365_SCOPES",
    "Mail.ReadWrite Mail.Send Calendars.ReadWrite offline_access User.Read"
).split()
M365_AUTHORITY  = f"https://login.microsoftonline.com/{M365_TENANT_ID}"

# Per-user M365 email addresses (used in Graph API /me calls scoped to user)
USER_1_M365_EMAIL = os.getenv("USER_1_M365_EMAIL", "")
USER_2_M365_EMAIL = os.getenv("USER_2_M365_EMAIL", "")

# Bootstrap directories
for _dir in [MEMORY_DIR, TOKENS_DIR, LOGS_DIR, TEMP_DIR, EMAIL_STORE]:
    _dir.mkdir(parents=True, exist_ok=True)

