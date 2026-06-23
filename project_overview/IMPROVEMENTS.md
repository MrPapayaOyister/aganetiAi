# IMPROVEMENTS — Cleanup & Hardening

Concrete changes against the **existing** product. Roadmap (new features) is in
`ROADMAP.md`. Severity / sequencing maps onto `CURRENT_GAPS.md`.

## A. Tier 1 — fix before deployment

### A1. Make `requirements.txt` actually install everything

Replace with:

```
# Core API
fastapi
uvicorn[standard]
pydantic

# LLM / RAG
openai
langgraph
qdrant-client
fastembed

# Microsoft 365
msal
httpx

# Telegram
aiogram

# Scheduling
apscheduler

# Voice
faster-whisper
kokoro>=0.9.4
soundfile

# Documents / reports
weasyprint
jinja2
pdfplumber
python-docx
openpyxl
pandas

# Frontend
streamlit
requests

# Misc
python-dotenv
```

Pin versions once the DGX environment is verified. System pkgs (espeak-ng, ffmpeg,
libreoffice) belong in a `INSTALL.md` or a `Dockerfile`, not requirements.txt.

### A2. systemd unit (optional but very recommended on DGX)

`/etc/systemd/system/aganeti-api.service`:

```ini
[Unit]
Description=Aganeti AI FastAPI
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=matrix
WorkingDirectory=/home/matrix/aganetiAi
EnvironmentFile=/home/matrix/aganetiAi/.env
ExecStart=/home/matrix/aganetiAi/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now aganeti-api`.

### A3. Move hardcoded paths into `config/settings.py`

```python
# config/settings.py
DRAFTS_FILE = BASE_DIR / "frontend" / "email_drafts.json"
```

Then `backend/main.py` and `frontend/app.py` import it.

### A4. Fix the `memory/` gitignore footgun

`.gitignore`:

```diff
-memory/
+memory/qdrant_data/
+memory/*.bin
```

Then `git add memory/store.py memory/long_term.py memory/query_rewriter.py` (and
whatever else lives there) and commit. The current state is a landmine for the next
person who clones.

## B. Tier 2 — refactor + ergonomics

### B1. Split `backend/main.py`

1503 lines → routers + jobs + prompt:

```
backend/
├── main.py                # 50-100 lines: app, lifespan, includes
├── deps.py                # shared clients (openai, qdrant, embed_model)
├── routers/
│   ├── chat.py
│   ├── tasks.py
│   ├── contacts.py
│   ├── mail.py
│   ├── calendar.py
│   ├── agents.py
│   ├── reports.py
│   └── schedules.py
├── jobs/
│   ├── poll_inbox.py
│   ├── agent_inbox.py
│   ├── meeting_prep.py
│   ├── digests.py
│   └── user_schedules.py
├── prompts/
│   └── aria.py            # the [IDENTITY]...[EXAMPLES] builder
└── meshes/
    ├── delegation.py      # Manager -> Developer
    └── email.py           # Triage -> Drafter
```

Same `app = FastAPI(...)`; just `app.include_router(...)` per router.

### B2. Replace `[ACTION:{json}]` with native function-calling

llama.cpp's server now supports an `tools`/`functions` array on `/v1/chat/completions`
that produces a structured `tool_calls` response. This kills:
- the regex parse,
- the "discard if recipient is the placeholder" heuristics in `/chat` (which are
  papering over LLM-format mistakes),
- the `[ACTION:...]` placement rules in the system prompt (≈200 lines of brittle
  text instruction).

Tools to expose: `create_task`, `complete_task`, `draft_email`, `schedule_meeting`,
plus eventually `update_task`, `list_tasks`, `search_memory`, `agenda_today`.

### B3. Iterate user_ids, don't hardcode

```python
# in backend/main.py (or jobs/meeting_prep.py post-refactor)
for uid in get_all_user_ids():
    ...
```

…replacing `["user_1", "user_2"]` in `MAIL_POLL_USERS` and the meeting-prep loop.
Then adding `user_3` is a one-line `.env` change.

### B4. Real migrations

Adopt `alembic` (auto-generates from SQLAlchemy models) — or, lighter, a hand-rolled
`schema_version` table + numbered SQL files in `migrations/`. The current
`try: ALTER TABLE … except: pass` approach silently swallows real errors.

### B5. Persist `_briefed_events` / `mail-poll triaged_ids` lookup

`_briefed_events` is fine in memory because a restart "loses" briefs that already
went out — no duplicate harm. But move it to `email_store/{user}/briefed_events.json`
so over a restart you don't *re-send* a brief if APScheduler happens to fire inside
the 25–35 min window again.

### B6. Replace JSON-fence stripping with `response_format`

In `tasks/intent.py` and any other place that asks the LLM for JSON, use
`response_format={"type": "json_object"}` on the OpenAI client. llama.cpp server
supports it as of mid-2024 builds. Removes the markdown-fence babysitting code.

### B7. Async-ify `tasks/intent.py`

Use `AsyncOpenAI` and `await` it from `/chat`. Currently it's wrapped in
`asyncio.to_thread` which works but introduces a thread-pool hop on every chat turn.

## C. Tier 3 — code quality / dev experience

### C1. `pyproject.toml` + `src/aganeti/` layout

Kill the `sys.path.append(...)` calls in `config/users.py`, `integrations/telegram_bot.py`,
`integrations/m365_*.py`. Standard packaging means `pip install -e .` and clean
imports.

### C2. Tests

A small `tests/` with:
- `test_action_parser.py` — happy path + malformed JSON + missing trailing tag.
- `test_schedule_parser.py` — every cron pattern in `schedule_manager.py`.
- `test_intent_classifier.py` — golden file of inputs → expected dicts, with the
  LLM mocked.
- `test_users.py` — `get_user_by_telegram_id` for known/unknown ids.
- `test_endpoints.py` — `httpx.AsyncClient(app=app)` smoke for /health, /tasks,
  /contacts, /chat with the LLM and Qdrant mocked.

Wire to GitHub Actions: `pytest -q` on every PR.

### C3. Strip the outdated `context/`

`context/current_app.txt` and `context/missing.txt` are historical scratch notes that
no longer match reality (CURRENT_GAPS.md §8). Delete them — `project_overview/` is the
maintained replacement.

### C4. Logging

Replace the dozens of `print(...)`s in `backend/main.py` with a configured `logging`
setup: per-module loggers, rotating file handler under `logs/aganeti.log`, INFO to
stdout. The bot output is currently un-searchable.

### C5. Centralised error envelope

Every endpoint that catches an exception returns its own shape (`{status: error}` vs
`{"error": ...}` vs raising `HTTPException`). Pick one (probably `HTTPException`
+ a single `add_exception_handler` for unexpected exceptions) and unify.

### C6. Frontend cleanup

Either:
- **Promote** Streamlit into a real per-user dashboard (today's calendar, pending
  tasks, agent inbox, schedules), wired to the real endpoints with `user_id` in the
  query string, or
- **Delete** `frontend/` and let Telegram be the single UI.

Half-finished Streamlit is the worst of both worlds.

## D. Tier 4 — security

- **Secrets.** `.env` next to the code is fine for a single-host POC, but rotate the
  bot token + M365 client id during the cloud→DGX move; the old VM stays alive for
  rollback, so two hosts hold the same secret today.
- **Telegram identity gate.** Currently `resolve_user_id` returns `None` for unknown
  chat ids and handlers send `UNAUTHORIZED_MSG`. Good. But add rate limiting on the
  start handler so an attacker can't probe registered chat ids cheaply.
- **Action execution.** `execute_action` calls back into local endpoints with no
  auth (loopback only, so OK today). If the API ever binds to a non-loopback address
  outside the LAN, add an API key header.
- **PDF generation.** WeasyPrint will follow `file://` and `http://` references in
  the rendered HTML. The current templates are static — keep it that way and reject
  user-supplied HTML.
