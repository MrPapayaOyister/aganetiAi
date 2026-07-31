# ARCHITECTURE

Single-host system. One Python process (FastAPI + APScheduler + aiogram on the same
event loop), three Docker containers (two LLM workers + Qdrant), one Streamlit page
served separately. Everything talks over loopback.

## Process topology

```
┌──────────────────────────────── Single host ─────────────────────────────────┐
│                                                                              │
│   ┌────────────────────────────┐                                             │
│   │  FastAPI app (uvicorn)     │           ┌─────────────────────────────┐   │
│   │  backend/main.py           │           │  llama_server (Docker)      │   │
│   │                            │──HTTP─────│  llama.cpp /v1/chat (smart) │   │
│   │  ┌──────────────────────┐  │           │  port 8080                  │   │
│   │  │ APScheduler          │  │           └─────────────────────────────┘   │
│   │  │  poll_inbox 30s      │  │           ┌─────────────────────────────┐   │
│   │  │  poll_agent_inbox 30s│  │──HTTP─────│  llama_fast (Docker)        │   │
│   │  │  meeting_prep 5m     │  │           │  Qwen 2.5 1.5B (fast)       │   │
│   │  │  digest 08:00 daily  │  │           │  port 8081                  │   │
│   │  │  due_reminders daily │  │           └─────────────────────────────┘   │
│   │  │  memory_extract 6h   │  │           ┌─────────────────────────────┐   │
│   │  │  user cron schedules │  │──HTTP─────│  qdrant (Docker)            │   │
│   │  └──────────────────────┘  │           │  ports 6333/6334            │   │
│   │                            │           └─────────────────────────────┘   │
│   │  ┌──────────────────────┐  │           ┌─────────────────────────────┐   │
│   │  │ aiogram bot          │  │──HTTPS────│  Telegram Bot API           │   │
│   │  │ (long polling)       │  │           └─────────────────────────────┘   │
│   │  └──────────────────────┘  │           ┌─────────────────────────────┐   │
│   │                            │──HTTPS────│  Microsoft Graph / Google   │   │
│   │  SQLite tasks.db (WAL)     │           │  mail + calendar + contacts │   │
│   │  data_vault/               │           └─────────────────────────────┘   │
│   │    provider_connections    │                                             │
│   │    .json (encrypted)       │                                             │
│   │  email_store/{user}/*.json │                                             │
│   │  logs/email_log.json       │                                             │
│   └────────────────────────────┘                                             │
│                                                                              │
│   ┌──────────────────────────┐                                               │
│   │  Streamlit frontend/app  │  (separate process, polls drafts file)        │
│   └──────────────────────────┘                                               │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Module map

| Layer | Module | Responsibility |
|---|---|---|
| Config | `config/settings.py` | `.env` loader, paths, all URLs/keys/scopes. Source of truth. |
| Config | `config/users.py` | 2-user registry; `telegram_chat_id ↔ user_id ↔ supabase_uid ↔ agent_id`. |
| API | `backend/main.py` | FastAPI app, all REST endpoints, LangGraph meshes, APScheduler lifespan, every background job. **1503 lines — biggest hot-spot.** |
| API | `backend/action_parser.py` | Parses `[ACTION:{…}]` tags from LLM output, dispatches via httpx loopback. |
| API | `backend/ingest.py` | Loads `data_vault/company_handbook.txt` into Qdrant `corporate_memory`. |
| Chat | `integrations/telegram_bot.py` | aiogram v3 router: `/start /tasks /done /digest /report …` + free-text → `/chat`. **940 lines.** |
| Chat | `tasks/intent.py` | LLM-based task-intent classifier (returns `{has_task, title, priority, due_date}`). |
| LLM | `integrations/model_router.py` | Keyword-routed smart/fast selection. |
| Tasks | `tasks/store.py` | SQLite tasks + contacts. WAL, per-user scoping, on-import migrations. |
| Tasks | `scheduler/schedule_manager.py` | Natural-language → cron, SQLite-backed schedules, hand-off to APScheduler. |
| Auth | `backend/routes/provider_auth.py` | OAuth connect/callback/status/disconnect for **both** Google and Microsoft — one shared authorization-code flow. |
| Auth | `backend/services/provider_tokens.py` | Per-user token load + transparent refresh for either provider; Fernet-encrypted at rest; `which_provider()` decides routing. |
| Mail/Cal | `backend/services/mailbox.py` | **The facade every caller uses.** Dispatches inbox/agenda/send/contacts to Graph or Google per user; `*_sync` mirrors for off-loop callers. |
| Mail | `backend/services/msmail.py` / `gmail.py` | Graph `/me/messages` and Gmail v1 — same return shape. |
| Calendar | `backend/services/mscalendar.py` / `gcalendar.py` | Graph `calendarView` (5-min agenda cache, 2-min events cache) and Google Calendar v3. |
| Contacts | `backend/services/mscontacts.py` / `gcontacts.py` | Graph contacts + tenant directory; Google People. |
| Util | `backend/services/timeparse.py` | Natural-language → ISO meeting times (provider-agnostic). |
| Util | `backend/services/async_bridge.py` | Runs async provider calls from sync, off-loop code. |
| Inter-agent | `integrations/agent_inbox.py` | SQLite-backed message queue between `agent_1` ↔ `agent_2`. |
| Reports | `reports/email_digest.py` | Telegram-Markdown digest from `email_store/{u}/unread.json`. |
| Reports | `reports/pdf_generator.py` | Jinja2 + WeasyPrint, per-user style overrides. |
| Voice | `integrations/whisper_transcriber.py` | faster-whisper STT. |
| Voice | `integrations/tts.py` | Kokoro TTS (needs `espeak-ng` system pkg). |
| Docs | `integrations/document_handler.py` | pdfplumber / python-docx / openpyxl text extraction. |
| Contacts | `integrations/contacts.py` | CRUD over `contacts` table, name/email resolution. |
| Frontend | `frontend/app.py` | Streamlit email-approval queue + chat input that POSTs `/chat`. |

## Endpoint inventory (backend/main.py)

| Method | Path | Purpose |
|---|---|---|
| GET  | `/health` | liveness |
| POST | `/chat` | central chat endpoint (streaming + non-streaming) |
| POST | `/delegate` | Manager→Developer LangGraph mesh |
| POST | `/triage_email` | Triage→Drafter mesh (used by `poll_inbox`) |
| POST | `/send_email` | Outbound via Graph |
| POST | `/draft_email` | Stub: append to drafts file |
| POST | `/schedule_meeting` | **Stub** — logs and returns OK (Task 22 placeholder) |
| GET  | `/mail/inbox` | Unread emails for user |
| GET  | `/mail/inbox/count` | Unread count only |
| GET  | `/calendar/agenda` | Today's agenda for user |
| POST | `/calendar/invalidate` | Force-clear agenda cache |
| POST | `/tasks` | Create task |
| GET  | `/tasks` | List (filter by status, user) |
| PATCH| `/tasks/{id}` | Update |
| DELETE | `/tasks/{id}` | Delete |
| POST | `/tasks/complete_by_title` | Complete by fuzzy title |
| GET  | `/tasks/summary` | Pending summary text |
| GET  | `/contacts` | List |
| GET  | `/contacts/resolve/{x}` | Resolve name-or-email |
| POST | `/contacts` | Create |
| PATCH| `/contacts/{id}` | Update |
| GET  | `/agent/inbox` | Pending inter-agent messages |
| GET  | `/agent/inbox/summary` | Inbox counts |
| GET  | `/agent/outbox` | Sent inter-agent messages |
| POST | `/agent/message` | Send agent→agent (e.g. delegation) |
| PATCH| `/agent/message/{id}/resolve` | Mark resolved |
| PATCH| `/agent/message/{id}/reject` | Mark rejected |
| GET  | `/digest/email/{user_id}` | On-demand mail digest |
| POST | `/report/generate` | Build & return PDF report |
| POST | `/schedule/create` | NL → cron + persist + register |
| GET  | `/schedule/list/{user_id}` | List active schedules |
| DELETE | `/schedule/{user_id}/{id}` | Soft-delete + unregister |

## Background jobs (APScheduler)

| Job id | Cadence | What it does |
|---|---|---|
| `poll_inbox` (no id) | 30 s | Per-user Graph poll → snapshot unread → triage ≤3 new |
| `agent_inbox_poll` | 30 s | Dispatch pending agent messages to Telegram |
| `check_upcoming_meetings` (no id) | 5 min | Meeting briefs for events 25–35 min out |
| `digest_{uid}` | 08:00 daily | Email digest to Telegram |
| `due_tasks_{uid}` | 08:00 daily | Today's due-task reminders |
| `memory_extract_{uid}` | every 6 h | Extracts memory from history (in `memory.long_term`) |
| `user_schedule_{id}` | user-defined | Custom user schedules (digest / task_summary / report / reminder) |

## External dependencies

- **Microsoft 365** — Graph API for mail + calendar + contacts. Confidential-client
  web OAuth (`/auth/microsoft/connect`); needs `M365_CLIENT_ID`, `M365_CLIENT_SECRET`,
  `M365_TENANT_ID`. The MSAL device flow was removed — tokens now live encrypted in
  `provider_connections` alongside Google's.
- **Google** — Gmail / Calendar / People, same OAuth flow (`/auth/google/connect`).
- **Telegram Bot API** — single bot, two authorized chat ids.
- **Qdrant** — vector DB, embeddings via `fastembed` (`BAAI/bge-small-en-v1.5`).
- **llama.cpp** — OpenAI-compatible `/v1/chat/completions` and `/v1/embeddings`.
- **espeak-ng** (system pkg) — required by Kokoro TTS.
- **LibreOffice** — used by `libreoffice_converter.py`.

## On-disk layout (run-time)

```
project_root/
├── .env                      # all secrets + URLs
├── data_vault/provider_connections.json  # per-user OAuth tokens, Fernet-encrypted
├── memory/                   # gitignored — both the Qdrant volume AND the Python modules (see CURRENT_GAPS)
├── email_store/{user}/       # unread.json, triaged_ids.json, digest_disabled marker
├── logs/email_log.json       # capped 50, used for meeting-brief context
├── tasks/tasks.db            # SQLite (tasks, contacts, schedules)
├── temp/                     # transient outputs
└── frontend/email_drafts.json  # Streamlit approval queue (also hardcoded at /home/my_vm_google/projects/...)
```
