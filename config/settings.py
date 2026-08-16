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

# ── Inference: ONE gateway, one model ─────────────────────────────────────────
# Everything goes through LiteLLM, which owns model routing, keys and fallbacks:
#     app -> LiteLLM (:4000) -> qwen-fast -> vLLM (:9002)
# The retired split (LLM_SMART_URL :8080 / LLM_FAST_URL :8081, llama.cpp) is gone;
# so is the "local-model" placeholder, which vLLM rejects with a 404.
# Call sites must use backend/services/llm.py rather than these values directly.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:4000/v1")
LLM_MODEL    = os.getenv("LLM_MODEL", "qwen-fast")
LLM_API_KEY  = os.getenv("LLM_API_KEY", "")
LLM_TIMEOUT  = float(os.getenv("LLM_TIMEOUT", "120"))
# Vision (scanned-PDF OCR) — same gateway, a model_list entry that can see images.
LLM_VISION_MODEL = os.getenv("LLM_VISION_MODEL", "qwen-vl")

# Vector DB Config
QDRANT_URL = os.getenv("QDRANT_URL", "http://100.107.179.44:6333")

# ── Graph DB (Neo4j) ──────────────────────────────────────────────────────────
# Infrastructure only, alongside Postgres and Qdrant. Nothing in the request path
# reads from it yet; see backend/graph/. Credentials come from .env — never inline.
# NEO4J_ENABLED=false disables the driver entirely (no connect attempt at startup).
NEO4J_URI      = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
NEO4J_ENABLED  = os.getenv("NEO4J_ENABLED", "true").lower() == "true"
# Seconds to wait for the startup connectivity probe before giving up. The probe
# NEVER blocks boot — a dead graph logs a warning and the app serves as before.
NEO4J_CONNECT_TIMEOUT = float(os.getenv("NEO4J_CONNECT_TIMEOUT", "5"))

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

# TTS Configuration
TTS_ENABLED = os.getenv("TTS_ENABLED", "false").lower() == "true"
TTS_URL       = os.getenv("TTS_URL",        "http://100.107.179.44:5002")
STT_URL       = os.getenv("STT_URL",        "http://100.107.179.44:5003")

# whisper.cpp CUDA server (GPU STT). When reachable, /stt forwards audio here
# for sub-200ms transcription on the GB10; otherwise it falls back to the local
# CPU faster-whisper module. Empty string disables the GPU path.
#
# Default is EMPTY on purpose. It used to be http://127.0.0.1:8090, but no
# whisper.cpp server is deployed and port 8090 now belongs to the SeaweedFS
# volume server — `curl :8090/status` returns its DiskStatuses JSON. Every /stt
# request was therefore POSTing audio at an object store, taking a non-200 back,
# and falling through to CPU with only an info-level log to show for it. An
# empty default makes the CPU path an explicit choice instead of the silent
# result of a misrouted request. Set this only when a real whisper.cpp server
# exists, and check the port is not already taken.
WHISPER_CPP_URL = os.getenv("WHISPER_CPP_URL", "")

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

# Load the sentence-embedding model at import time. Measured cost: ~0.23 GB RSS.
# On a unified-memory host (CPU and GPU share one pool) that headroom is scarce,
# and a dev instance running beside production never touches RAG — so dev turns
# this off. Defaults to on so production behaviour is unchanged.
LOAD_EMBED_MODEL = os.getenv("LOAD_EMBED_MODEL", "true").lower() == "true"

# How long a rendered widget (chat_artifacts kind="embed") is kept before the
# nightly prune soft-deletes it. Only embeds: charts/tables/PDFs are work
# product and are never pruned by this. 0 disables the job entirely.
EMBED_RETENTION_DAYS = int(os.getenv("EMBED_RETENTION_DAYS", "90"))

# The browser-facing origin this app is served from. Embeds run in a srcdoc
# iframe with an OPAQUE origin and no base URL, so any asset they load (the
# vendored hls.js) must be referenced absolutely — a relative path cannot
# resolve there. FRONTEND_URL is what scripts/dev_backend.sh already exports.
APP_PUBLIC_ORIGIN = (os.getenv("APP_PUBLIC_ORIGIN")
                     or os.getenv("FRONTEND_URL", "http://localhost:3000")).rstrip("/")

# ── Web search: self-hosted SearXNG ──────────────────────────────────────────
# Only SearXNG touches the public internet, so a user's query never goes to a
# third-party search API — the same on-prem posture the orchestrator's
# _web_search skill already assumes. Needs `json` in `search.formats` in the
# instance's settings.yml (it is on by default in this deployment).
SEARXNG_URL     = os.getenv("SEARXNG_URL", "http://localhost:5555").rstrip("/")
SEARXNG_TIMEOUT = float(os.getenv("SEARXNG_TIMEOUT", "15"))

# Background workers (APScheduler jobs + Telegram bot). Set RUN_BACKGROUND=false to
# serve the HTTP API only — useful for local testing and for a web-dashboard host
# that shouldn't also poll inboxes or drive the bot.
RUN_BACKGROUND = os.getenv("RUN_BACKGROUND", "true").lower() == "true"

# Passive task capture: an EXTRA LLM call per chat turn that guesses whether the user
# stated a to-do, then asks "shall I add this task?". With native tools the model
# already creates tasks on request, so this is off by default — it doubled chat
# latency and produced noisy unsolicited prompts.
PASSIVE_TASK_DETECT = os.getenv("PASSIVE_TASK_DETECT", "false").lower() == "true"

# ── YouTube tools (backend/services/youtube.py) ──────────────────────────────
# These replace the Valves the tool carried as an Open WebUI plugin; the tool is
# a normal module here, so its settings are env vars like everything else.
#
# YOUTUBE_EMBED_PLAYER off (default) → a thumbnail card linking out to YouTube in
#                            a new tab. This is the default because inline
#                            playback does NOT work: the player iframe is nested
#                            inside our sandboxed embed and inherits its sandbox,
#                            so youtube.com's script fails with "writeEmbed is not
#                            defined" and renders black. Verified in-browser; no
#                            CSP change fixes it, and the fix that would
#                            (allow-same-origin) is unacceptable for srcdoc — it
#                            is same-origin with the app. Real inline playback
#                            needs the embed served from a separate origin.
#                      on  → the real youtube.com player. Only useful once that
#                            separate-origin work exists.
# YOUTUBE_CLICK_TO_PLAY on → thumbnail facade first; the player loads only after
#                            the user clicks, so no connection to YouTube until
#                            they opt in. Only applies when EMBED_PLAYER is on.
YOUTUBE_EMBED_PLAYER    = os.getenv("YOUTUBE_EMBED_PLAYER", "false").lower() == "true"
YOUTUBE_CLICK_TO_PLAY   = os.getenv("YOUTUBE_CLICK_TO_PLAY", "true").lower() == "true"
# Privacy-enhanced mode is a separate, stricter host that returns "Video
# unavailable" for some content that plays fine from youtube.com. Off by default.
YOUTUBE_NOCOOKIE        = os.getenv("YOUTUBE_NOCOOKIE", "false").lower() == "true"
# Optional public origin (e.g. https://aria.example.ae) passed to the player as
# origin= so it validates the embed directly instead of inferring from the
# referrer. Useful behind a proxy; blank omits the parameter.
YOUTUBE_SITE_ORIGIN     = os.getenv("YOUTUBE_SITE_ORIGIN", "")
YOUTUBE_REQUEST_TIMEOUT = int(os.getenv("YOUTUBE_REQUEST_TIMEOUT", "10"))

# ── Weather ──────────────────────────────────────────────────────────────────
# Fallback city when the user asks for "weather" without naming a place. UNSET by
# default, and deliberately not derived from anything: APP_TIMEZONE would return
# the wrong city for a travelling or non-local user, and inferring from system
# context or IP is exactly what the tool's own parameter description forbids.
# When set, the card says so visibly, so the assumption is correctable rather
# than silent. When unset, the assistant asks which city.
WEATHER_DEFAULT_LOCATION = os.getenv("WEATHER_DEFAULT_LOCATION", "").strip()

# Pre-load heavy local models (Whisper, TTS) at startup in the background so the FIRST
# voice message doesn't trigger a multi-second download/load that starves the bot.
PREWARM_MODELS = os.getenv("PREWARM_MODELS", "true").lower() == "true"

# Proactive briefings: unified morning brief (08:00) + end-of-day "what slipped" (18:00).
PROACTIVE_BRIEFINGS = os.getenv("PROACTIVE_BRIEFINGS", "true").lower() == "true"
# How often the RAG ingestion job rescans data_vault/ (seconds).
RAG_WATCH_INTERVAL = int(os.getenv("RAG_WATCH_INTERVAL", "300"))

# Microsoft 365 — confidential-client web OAuth (backend/routes/provider_auth.py).
# The scope list lives in backend/services/provider_tokens.MS_SCOPES so the connect,
# refresh and capability paths can never drift apart.
M365_CLIENT_ID     = os.getenv("M365_CLIENT_ID", "")
M365_CLIENT_SECRET = os.getenv("M365_CLIENT_SECRET", "")
M365_TENANT_ID     = os.getenv("M365_TENANT_ID", "common")

# The connected mailbox address now comes from provider_connections.provider_email,
# not from env — USER_*_M365_EMAIL is gone with the device flow.

# Bootstrap directories
for _dir in [MEMORY_DIR, LOGS_DIR, TEMP_DIR, EMAIL_STORE]:
    _dir.mkdir(parents=True, exist_ok=True)



# ── Knowledge graph: extraction model + traversal depth ──────────────────────
# KG_EXTRACT_MODEL routes extraction to a LiteLLM model with a LONGER deadline
# than chat. Extraction over a ~650-word document measures 21-61s; the chat
# route's 30s timeout turns that into an HTTP 408. Separate route = separate
# timeout, with no effect on chat or any other consumer.
KG_EXTRACT_MODEL = os.getenv("KG_EXTRACT_MODEL", "qwen-extract")
KG_EXTRACT_TIMEOUT = float(os.getenv("KG_EXTRACT_TIMEOUT", "300"))

# Graph traversal depth for the context provider. DEFAULT 1 — unchanged. Raising
# it widens the frontier substantially on a dense graph (max degree 120 today),
# so measure graph_ms before moving it in production. Configuration only: no
# retrieval algorithm reads this beyond passing it to the existing API.
GRAPH_RETRIEVAL_DEPTH = max(1, min(3, int(os.getenv("GRAPH_RETRIEVAL_DEPTH", "1"))))


# ──────────────────────────────────────────────────────────────────────────
# Object storage (SeaweedFS) and shared cache (Redis)
#
# Declared HERE, not read with a bare os.getenv at the call site. Modules that
# call os.getenv directly only see these when config.settings has already been
# imported, which is why the observability panel reported SeaweedFS as "not
# deployed" while a healthy four-container cluster was running — the same class
# of bug already fixed in services/provider_tokens.py and auth/supabase_client.py.
#
# Addresses are host-published loopback ports on purpose. The backend runs as a
# HOST process (deploy/aganeti-api.service), not a container, so Docker's
# embedded DNS does not resolve — `seaweed-master:9333` or `redis:6379` would
# fail. Verified: `getent hosts redis` returns nothing from this runtime.
# ──────────────────────────────────────────────────────────────────────────
SEAWEEDFS_ENABLED     = os.getenv("SEAWEEDFS_ENABLED", "true").lower() == "true"
SEAWEEDFS_MASTER_URL  = os.getenv("SEAWEEDFS_MASTER_URL", "http://127.0.0.1:9333")
SEAWEEDFS_FILER_URL   = os.getenv("SEAWEEDFS_FILER_URL", "http://127.0.0.1:8888")
SEAWEEDFS_S3_URL      = os.getenv("SEAWEEDFS_S3_URL", "http://127.0.0.1:8333")
SEAWEEDFS_BUCKET_NAME = os.getenv("SEAWEEDFS_BUCKET_NAME", "agentic-ai")
SEAWEEDFS_ACCESS_KEY  = os.getenv("SEAWEEDFS_ACCESS_KEY", "")
SEAWEEDFS_SECRET_KEY  = os.getenv("SEAWEEDFS_SECRET_KEY", "")
SEAWEEDFS_TIMEOUT     = float(os.getenv("SEAWEEDFS_TIMEOUT", "5"))

# The volume server is published on host 8090 (container 8080). Recorded because
# 8090 was previously the WHISPER_CPP_URL default and the collision sent STT
# audio to the object store; see the note on WHISPER_CPP_URL above.
SEAWEEDFS_VOLUME_URL  = os.getenv("SEAWEEDFS_VOLUME_URL", "http://127.0.0.1:8090")

# Redis. DB 1, NOT DB 0 — db0 on this host is the Video Indexer's Celery broker
# (its _kombu.binding.* keys are live there). db1-db15 were verified empty.
# Note this instance has no requirepass and runs maxmemory-policy=allkeys-lru
# with a 2GB ceiling shared with that other application: a separate DB index
# isolates the KEYSPACE but NOT eviction, so either side can evict the other.
REDIS_ENABLED         = os.getenv("REDIS_ENABLED", "false").lower() == "true"
REDIS_URL             = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/1")
REDIS_CONNECT_TIMEOUT = float(os.getenv("REDIS_CONNECT_TIMEOUT", "2.0"))
REDIS_DEFAULT_TTL     = int(os.getenv("REDIS_DEFAULT_TTL", "300"))
REDIS_KEY_PREFIX      = os.getenv("REDIS_KEY_PREFIX", "aganeti")

# ── P0 remediation: authorization boundary + runtime migration ────────────────
# These are read at their point of use (backend/orchestrator/authz.py,
# backend/runtime_flag.py, backend/guardrails.py) so a value can be changed and the
# process restarted without touching this module. They are mirrored here as the one
# place an operator can see the whole switchboard.
#
# AUTHZ_STRICT_TENANT — a tool call carrying no tenant_id is DENIED. Default true.
#   Setting false reinstates the pre-P0 behaviour (tool calls with no isolation
#   boundary) and is an emergency rollback only.
AUTHZ_STRICT_TENANT   = os.getenv("AUTHZ_STRICT_TENANT", "true").lower() not in ("0", "false", "no")
# AGANETI_DENIED_TOOLS — comma-separated kill switch, refused ahead of any grant.
AGANETI_DENIED_TOOLS  = [t.strip() for t in os.getenv("AGANETI_DENIED_TOOLS", "").split(",") if t.strip()]
# Runtime migration (see docs/runtime-migration.md). All default to "no traffic on
# Runtime B", so an unset environment is byte-identical to pre-P0 behaviour.
RUNTIME_B_ENABLED     = os.getenv("RUNTIME_B_ENABLED", "false").lower() in ("1", "true", "yes", "on")
RUNTIME_B_PERCENT     = int(os.getenv("RUNTIME_B_PERCENT", "0") or 0)
RUNTIME_B_USERS       = [u.strip() for u in os.getenv("RUNTIME_B_USERS", "").split(",") if u.strip()]
RUNTIME_B_SESSIONS    = [s.strip() for s in os.getenv("RUNTIME_B_SESSIONS", "").split(",") if s.strip()]
RUNTIME_B_DENY_USERS  = [u.strip() for u in os.getenv("RUNTIME_B_DENY_USERS", "").split(",") if u.strip()]
