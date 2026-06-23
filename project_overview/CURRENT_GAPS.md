# CURRENT GAPS — Audit before deploying anywhere

Findings from reading the repo as it lives on GitHub today. Severity uses standard
**P0** (blocks anyone bringing it up) → **P3** (nice-to-have hygiene).

## P0 — The repo on GitHub does not run on its own

### 1. The `memory/` Python package is gitignored
**Evidence.** `.gitignore` line 3 is `memory/`. But the code imports from it:
- `backend/main.py:36` → `from memory.store import load_history, save_message, load_session_state, save_session_state, append_message`
- `backend/main.py:436` → `from memory.long_term import extract_and_store`
- `backend/main.py:921` → `from memory.long_term import search_memory`
- `backend/main.py:922` → `from memory.query_rewriter import rewrite_query`

So `python -m uvicorn backend.main:app` on a fresh clone of GitHub will fail at import
time. **The cloud VM must hold the real `memory/` package**, and either (a) the
gitignore is wrong and should only ignore the *Qdrant storage* subdir, not the Python
modules, or (b) those modules need to be untracked and ported manually.

**Action.** When migrating to DGX, **rsync from the cloud VM directly**, do not rely
on `git clone` alone. After migration, fix `.gitignore` so the Qdrant data dir
(e.g. `memory/qdrant_data/`) is ignored but the Python package is not.

### 2. Several runtime deps are missing from `requirements.txt`
**Evidence.** `requirements.txt` lists 14 packages. The code imports at minimum:
- `fastapi`, `uvicorn` — backend entry point
- `langgraph` — used at main.py:4 and the two meshes
- `openai` — `OpenAI`, `AsyncOpenAI` clients
- `qdrant-client`, `fastembed` — vector store + embeddings
- `streamlit`, `requests` — frontend app
- `jinja2` — used by `reports/pdf_generator.py`
- `pydantic` (transitive but should be pinned)

None of these are in `requirements.txt`. A fresh `pip install -r requirements.txt`
followed by `uvicorn backend.main:app` will fail immediately.

### 3. Hardcoded VM paths
**Evidence.**
- `backend/main.py:1409`: `DRAFTS_FILE = "/home/my_vm_google/projects/frontend/email_drafts.json"`
- `frontend/app.py:20`: same hardcoded path.

This will break the moment the project is anywhere other than that exact VM filesystem
layout. Should resolve from `BASE_DIR` like every other path in `config/settings.py`.

### 4. Docker images on `docker-compose.yml` are CPU-only llama.cpp
**Evidence.** `image: ghcr.io/ggml-org/llama.cpp:server` (no `-cuda` suffix), no
`deploy.resources.reservations.devices` block, and the command uses `-t 8` / `--threads
4` — pure CPU. On the DGX Spark with a GB10 GPU sitting at 11W idle, this leaves
the headline hardware unused. The CUDA-enabled image is
`ghcr.io/ggml-org/llama.cpp:server-cuda` and needs `nvidia` runtime + `--gpus all`
(or the compose `devices` spec) — see DGX_MIGRATION.md.

### 5. `matrix` user on DGX is not in the `docker` group
**Evidence.** `groups` over SSH returns `matrix adm sudo audio dip plugdev users
lpadmin`. Docker is installed but the socket is root-owned.

**Action.** `sudo usermod -aG docker matrix && newgrp docker` once during setup.

## P1 — Significant rough edges

### 6. `backend/main.py` is 1503 lines
Concerns: helpers, endpoints, LangGraph meshes, three different APScheduler job
factories, the meeting-prep enrichment logic, the email triage state machine, and
the whole `/chat` prompt template all live in one file. Refactor candidates:
- `backend/routers/{chat,tasks,mail,calendar,agents,reports,schedules}.py`
- `backend/jobs/{poll_inbox, meeting_prep, agent_inbox, digests}.py`
- `backend/prompts/aria.py` for the system-prompt builder.

### 7. `tasks/intent.py` blocks the chat path on a second LLM call
Every chat turn that's not in a confirmation state calls `detect_task_intent`, which
is a synchronous OpenAI call against the smart model at `LLM_BASE_URL` (not
`LLM_SMART_URL`/`LLM_FAST_URL` from `route_model`). Two LLM round-trips per turn —
visible latency, and it bypasses the smart/fast router.

### 8. Outdated docs in `context/`
`context/current_app.txt` still talks about IMAP polling, Google Calendar, and a
Streamlit chat that "does nothing yet". The current code uses Microsoft Graph
end-to-end and `/chat` is fully wired. `context/missing.txt` complains there's no
`/chat` endpoint — which there is. These files should be removed or rewritten;
`project_overview/` supersedes them.

### 9. README.md is two words long
Literally: `# aganetiAi`. No setup instructions, no env-var matrix, no deploy notes.

### 10. No `.env.example`
`config/settings.py` reads ~20 env vars but there's no template. Every onboarding
will rediscover them by grep.

### 11. Hardcoded user list in poll_inbox + meeting prep
`MAIL_POLL_USERS = ["user_1", "user_2"]` (main.py:1408) and the meeting-prep loop
(main.py:238) both hardcode the user ids. Should iterate `get_all_user_ids()` like
the digest/reminder loops do. Adding a 3rd user today requires changes in 2 places
plus the schema additions.

### 12. SQLite `ALTER TABLE … try/except` in place of real migrations
`tasks/store.py:58` and similar in `schedule_manager.py`. Works for a 2-user POC,
not for anything else. Should adopt `alembic` or even hand-rolled versioned
migrations.

### 13. No tests
`scripts/verify_task16.py` and `integrations/verify_m365_*.py` are ad-hoc verification
scripts. There's no `tests/` dir, no `pytest`, no CI.

### 14. Streamlit frontend is half-deprecated
- It still reads/writes the old hardcoded drafts path.
- The chat input goes to `/chat` but doesn't pass `user_id`, so it always falls back
  to `session_id` (a fresh uuid) — meaning Streamlit users get no calendar/task
  context and write to a phantom session's history.
- Telegram is the real UI; either rewrite Streamlit into a useful dashboard or remove it.

### 15. Action-tag JSON in the system prompt is brittle
The prompt teaches the LLM to emit `[ACTION:{json}]` and the regex
(`r'\[ACTION:(\{.*?\})\]\s*$'`) requires the tag to be the last thing in the reply.
Small LLMs frequently violate either the JSON or the tail position. Better:
function-calling via llama.cpp's `tools` payload, or constrained JSON via a
grammar/JSON-schema.

## P2 — Hygiene

- Multiple `sys.path.append(…)` hacks in `config/users.py`, `integrations/telegram_bot.py`,
  `integrations/m365_*.py`. Package the project (`pyproject.toml`, `src/` layout)
  and drop them.
- `email_drafts.json` is appended-as-JSON-Lines but parsed by `frontend/app.py` as
  `f.readlines()` and per-line `json.loads`. Works, but contradicts the `/draft_email`
  stub which writes a JSON array (`drafts_path.write_text(json.dumps(drafts, …))`).
  Two writers, two formats — pick one.
- `tasks/intent.py` strips ```` ```json ```` fences manually. Use the OpenAI client
  `response_format={"type": "json_object"}` (supported by llama.cpp server with
  recent builds) instead.
- `_briefed_events` and `_TELEGRAM_ID_TO_USER` are in-memory only — survive a restart
  by persisting briefed event ids.
- The `[EXAMPLES]` block in the chat system prompt has a hardcoded date
  `2023-06-14` (main.py:1045). Use a real "today" reference.

## P3 — Possibly intentional, flag for review

- `query_local_llm` (the sync helper in main.py:495) talks to `LLM_BASE_URL`, not the
  router. Used by the LangGraph meshes — they always hit one fixed endpoint,
  bypassing smart/fast selection. May be intentional (LangGraph nodes always = smart),
  but the router would let policy decide.
- `data_vault/company_handbook.txt` is the only seeded RAG content. The corporate
  RAG pipeline (`backend/ingest.py`) needs to be run once to populate the
  `corporate_memory` collection before `/chat` answers usefully.
- Inbox mark-as-read is **deliberately disabled** so digests keep working — comment
  at main.py:1490. Confirm this is the intended product behavior.
