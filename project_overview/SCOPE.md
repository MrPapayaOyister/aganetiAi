# SCOPE — What Aganeti AI Actually Is

## One sentence

A **self-hosted personal AI office assistant** ("Aria") for a small team (currently
2-user POC) that reads/drafts your email, manages your task list, briefs you on
upcoming meetings, lets agents delegate tasks to each other, and produces PDF reports —
all running locally on a single machine, with a local llama.cpp LLM and Qdrant for
vector memory.

## Who it's for

The user registry (`config/users.py`) is hard-coded to two users (`user_1`, `user_2`),
each tied to:
- a **Telegram chat id** (primary UI),
- a **Microsoft 365 mailbox + calendar** (Graph API, MSAL device-flow auth),
- a **Qdrant collection** for personal memory (`memory_user_1`, `memory_user_2`),
- an **agent id** (`agent_1`, `agent_2`) for inter-user message passing.

This shape is explicit in the code — it is not generic SaaS multi-tenancy.

## What it does today (from the code)

### 1. Chat assistant ("Aria")
- `POST /chat` in [backend/main.py:823](../backend/main.py) is the central endpoint.
- Builds a layered system prompt with sections: `[IDENTITY] [WHAT YOU CAN DO]
  [TODAY'S CONTEXT] [MEMORY] [COMPANY KNOWLEDGE] [CONVERSATION RULES] [ACTION PROTOCOL]
  [EXAMPLES]` (backend/main.py:944–1056).
- Per-turn context injection: today's calendar agenda + pending tasks + RAG hit from
  Qdrant `corporate_memory` collection + long-term memory recall + per-user name +
  company name.
- LLM is asked to emit `[ACTION:{json}]` tags at the end of replies; `extract_action`
  parses them; `execute_action` (backend/action_parser.py:25) dispatches to:
  - `create_task`, `complete_task` → `tasks/store.py` SQLite
  - `draft_email` → `/draft_email` (appends to `email_drafts.json`)
  - `schedule_meeting` → `/schedule_meeting` (currently a logged stub).
- A two-stage **task-confirmation state machine** lives in `/chat`:
  unconfirmed task detected → ask yes/no/amend → on `yes` create task, on `amend`
  re-parse with `tasks/intent.py` (LLM-based intent classifier).

### 2. Email pipeline
- **Inbound**: APScheduler job `poll_inbox` runs every 30s, sequentially for each
  user, off the event-loop thread. Per user it calls
  `integrations/m365_mail.fetch_unread_emails`, writes a snapshot to
  `email_store/{user_id}/unread.json`, and triages up to 3 new unread emails
  per cycle. De-dup is via `triaged_ids.json`, **not** the read flag — emails stay
  unread on Microsoft so the digest still sees them (main.py:1455–1495).
- **LangGraph triage mesh**: `triage_officer → communications_drafter`
  (main.py:1259–1302). The triage step also auto-creates a "Reply to …" task with
  an LLM-inferred priority and logs the email to `logs/email_log.json` (capped at 50
  entries). The drafter writes a reply into the per-user drafts file.
- **Outbound**: `POST /send_email` calls `m365_mail.send_email` via Graph; Streamlit
  approval queue (`frontend/app.py`) is the human-in-the-loop gate.

### 3. Calendar & meeting prep
- `integrations/m365_calendar.py`: Microsoft Graph `calendarView`, with two TTL
  caches (agenda 5 min, upcoming events 2 min).
- APScheduler job `check_upcoming_meetings` every 5 min finds events that start in
  25–35 minutes for each user, builds a brief, enriches it with:
  - the **most recent triaged email** from any attendee (`email_log.json` lookup),
  - any **related pending task** whose title/notes mention an attendee,
  
  and sends it as a Telegram message to that user's chat (main.py:196–264).
- `_briefed_events` dedup set prevents duplicate briefs for the same event.

### 4. Task store
- SQLite at `tasks/tasks.db` (WAL mode), schema in `tasks/store.py`: `tasks` and
  `contacts` tables, per-user-id scoped. Migrations are bolted-on `ALTER TABLE … try`
  blocks. REST surface: `POST/GET/PATCH/DELETE /tasks`, `POST /tasks/complete_by_title`,
  `GET /tasks/summary`.

### 5. Scheduler (natural-language cron)
- `scheduler/schedule_manager.py`: regex-driven parser converts phrases like
  "every weekday at 9am to summarize emails" → cron expression + action_type
  (`email_digest`, `task_summary`, `generate_report`, `custom_reminder`).
- Persisted in the same SQLite (`schedules` table). On FastAPI startup
  (`lifespan`), all active schedules are restored into APScheduler.

### 6. Inter-agent delegation
- `integrations/agent_inbox.py` + `POST /agent/message` + `poll_agent_inbox`
  job (every 30s).
- When `user_1`'s agent sends a `task_delegation` message to `user_2`'s agent,
  the recipient gets a Telegram message with **inline Accept/Reject keyboards**
  (main.py:290–342). Acceptance/rejection routes a confirmation back to the
  original sender's Telegram.

### 7. Reports
- `reports/email_digest.py` builds a Telegram-formatted unread-mail digest.
- `reports/pdf_generator.py` uses Jinja2 + WeasyPrint to produce PDFs with
  sections {calendar, tasks, emails, memory} and **per-user `report_style`**
  (font, primary color, tone, optional logo).
- `POST /report/generate` returns the path; scheduled `generate_report` action
  delivers it as a Telegram document.

### 8. Voice & docs
- `integrations/whisper_transcriber.py`: faster-whisper for incoming voice notes.
- `integrations/tts.py`: Kokoro TTS for outgoing voice (gated by `TTS_ENABLED`,
  needs system `espeak-ng`).
- `integrations/document_handler.py` + `libreoffice_converter.py`: convert
  uploaded docs (pdfplumber, python-docx, openpyxl) to text for downstream use.

### 9. LLM routing
- Two llama.cpp endpoints in `docker-compose.yml`:
  - `llama_server` on **:8080** — "smart" model (path from `LLM_MODEL_PATH`).
  - `llama_fast` on **:8081** — Qwen 2.5 1.5B Instruct (Q4_K_M).
- `integrations/model_router.py` routes by keyword: "smart" trigger words
  (email, schedule, task, summarize, …) → 8080; everything else → 8081.

### 10. RAG / memory
- `qdrant` container, collection `corporate_memory` for company policy.
- Per-user collections (`memory_user_1`, …) for long-term memory recall
  (referenced from `memory.long_term`/`memory.query_rewriter` — **see
  CURRENT_GAPS.md**).
- Embedding model: `BAAI/bge-small-en-v1.5` via `fastembed`.

## What it explicitly is *not*

- Not a generic chatbot. There's no concept of "anonymous user" — every entry point
  resolves a `user_id` from the registry and unauthorized chat ids get bounced.
- Not a Slack/Teams app. Telegram is the only conversational surface.
- Not multi-tenant / cloud SaaS. Single machine, single deployment, single tenant.
- Not a coding agent — the "Manager → Developer" LangGraph mesh exposed at
  `/delegate` is structural-instruction-only; it doesn't compile, test, or write code.
