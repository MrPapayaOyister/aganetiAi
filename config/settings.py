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

# Database — the SQLite file lives in tasks/ (tasks/store.py owns it). This was
# previously BASE_DIR/"tasks.db" (wrong), which made integrations/contacts.py connect
# to an empty auto-created root DB, so contact lookup silently always failed.
DB_PATH = str(BASE_DIR / "tasks" / "tasks.db")

# Email draft approval queue (Streamlit reads this; mail watcher appends to it).
# Derived from BASE_DIR so the project is portable across hosts (was hardcoded to
# the old cloud-VM path /home/my_vm_google/projects/...).
DRAFTS_FILE = str(BASE_DIR / "frontend" / "email_drafts.json")

# LLM Config
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://100.107.179.44:6333")
LLM_SMART_URL = os.getenv("LLM_SMART_URL", "http://100.107.179.44:6333")
LLM_FAST_URL  = os.getenv("LLM_FAST_URL",  "http://100.107.179.44:8081")

# Vector DB Config
QDRANT_URL = os.getenv("QDRANT_URL", "http://100.107.179.44:6333")

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
TTS_URL       = os.getenv("TTS_URL",        "http://100.107.179.44:5002")
STT_URL       = os.getenv("STT_URL",        "http://100.107.179.44:5003")

# whisper.cpp CUDA server (GPU STT). When reachable, /stt forwards audio here
# for sub-200ms transcription on the GB10; otherwise it falls back to the local
# CPU faster-whisper module. Empty string disables the GPU path.
WHISPER_CPP_URL = os.getenv("WHISPER_CPP_URL", "http://127.0.0.1:8090")

# ──────────────────────────────────────────────────────────────────────────
# Tier-1 additions (DGX Spark): GPU Whisper, RAG ingestion, native tool-calling
# ──────────────────────────────────────────────────────────────────────────

# Voice / Whisper (STT). On the GB10 the CUDA backend is sub-second; "auto" tries
# CUDA first and transparently falls back to CPU if the backend is unavailable.
WHISPER_MODEL   = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE  = os.getenv("WHISPER_DEVICE", "cuda")    # auto | cuda | cpu
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "auto")   # auto | float16 | int8 | int8_float16

# RAG ingestion (data_vault → Qdrant). Chunk sizes are in words.
DATA_VAULT_DIR    = BASE_DIR / "data_vault"
RAG_COLLECTION    = os.getenv("RAG_COLLECTION", "corporate_memory")
RAG_CHUNK_SIZE    = int(os.getenv("RAG_CHUNK_SIZE", "300"))
RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "60"))
RAG_STATE_FILE    = BASE_DIR / "data_vault" / ".ingest_state.json"
EMBED_MODEL_NAME  = os.getenv("EMBED_MODEL_NAME", "BAAI/bge-small-en-v1.5")
EMBED_DIM         = int(os.getenv("EMBED_DIM", "384"))

# Native function-calling via llama.cpp `tools` payload. When true, /chat asks the
# model for structured tool_calls instead of the brittle [ACTION:{json}] tail tag.
# The legacy action-tag parser is retained as a fallback.
NATIVE_TOOLS = os.getenv("NATIVE_TOOLS", "true").lower() == "true"

# Background workers (APScheduler jobs + Telegram bot). Set RUN_BACKGROUND=false to
# serve the HTTP API only — useful for local testing and for a web-dashboard host
# that shouldn't also poll inboxes or drive the bot.
RUN_BACKGROUND = os.getenv("RUN_BACKGROUND", "true").lower() == "true"

# Passive task capture: an EXTRA LLM call per chat turn that guesses whether the user
# stated a to-do, then asks "shall I add this task?". With native tools the model
# already creates tasks on request, so this is off by default — it doubled chat
# latency and produced noisy unsolicited prompts.
PASSIVE_TASK_DETECT = os.getenv("PASSIVE_TASK_DETECT", "false").lower() == "true"

# Pre-load heavy local models (Whisper, TTS) at startup in the background so the FIRST
# voice message doesn't trigger a multi-second download/load that starves the bot.
PREWARM_MODELS = os.getenv("PREWARM_MODELS", "true").lower() == "true"

# Proactive briefings: unified morning brief (08:00) + end-of-day "what slipped" (18:00).
PROACTIVE_BRIEFINGS = os.getenv("PROACTIVE_BRIEFINGS", "true").lower() == "true"
# How often the RAG ingestion job rescans data_vault/ (seconds).
RAG_WATCH_INTERVAL = int(os.getenv("RAG_WATCH_INTERVAL", "300"))

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

