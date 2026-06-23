# ROADMAP — Additional Features

Proposed *new* capabilities, sized to the DGX Spark hardware. Grouped by ambition so
the user can pick a tier. Each item lists the **DGX hardware angle** — what about the
new host makes the feature worth doing now that wasn't worth doing on the cloud VM.

## Tier 1 — Things you should turn on day one on the DGX

### 1.1 Run a much larger "smart" model
**DGX angle.** GB10 + 121 GB system RAM = the existing 7B-class model is leaving
quality on the table. Reasonable upgrades, all GGUF, all already supported by the
existing llama.cpp container:
- **Qwen 2.5 14B Instruct (Q4_K_M / Q5_K_M)** — clear quality jump for the
  tool-calling + JSON-emission style the project already uses.
- **Llama 3.1 8B Instruct (Q5_K_M / Q6_K)** — solid all-rounder, faster than 14B.
- **Mistral Small 3 (24B, Q4_K_M)** — top of what GB10 + 16 GB VRAM can comfortably
  run if the GPU has enough memory; verify with `nvidia-smi` after first load.

Pair the smart model swap with **B2 in IMPROVEMENTS.md** (native tool calls) for the
biggest reliability lift.

### 1.2 Always-on streaming chat (no Telegram race conditions)
The streaming branch in `/chat` already works; turn it on for Telegram replies via
`edit_message_text` ticks (or send incremental "..." → final message swap). Smoother
UX, and the GPU finishes generation before the Telegram round-trip would have anyway,
so it's free.

### 1.3 GPU-accelerated Whisper
Switch `faster-whisper` to its CUDA backend (`device="cuda", compute_type="float16"`).
On GB10, voice-note transcription drops from CPU seconds to sub-second per minute of
audio. Already in `requirements.txt`; just needs config in
`integrations/whisper_transcriber.py`.

### 1.4 Real RAG ingestion pipeline
Today `backend/ingest.py` is one-shot over `data_vault/company_handbook.txt`. Replace
with a small ingest service that watches `data_vault/` for new files (PDFs, docx,
xlsx — `document_handler.py` already extracts text), chunks them with overlap,
embeds, and upserts to Qdrant `corporate_memory`. **DGX angle:** local embedding at
GPU speed makes daily ingestion of a real document drop folder painless.

## Tier 2 — Meaningful product features

### 2.1 Web dashboard (replaces or augments Streamlit)
A single-page app under `frontend/` with:
- **Today panel**: calendar + due tasks + agent inbox.
- **Inbox panel**: unread mail + the LangGraph triage's draft, with the same
  Accept / Edit / Send buttons the Streamlit page has, but actually wired to user_id.
- **Tasks panel**: full CRUD, drag to reschedule.
- **Schedules panel**: create natural-language schedules, list/delete.
- **Memory panel**: search Qdrant collections, view long-term memory entries,
  delete bad memories.
- **Voice button**: in-browser recording → `/transcribe` → `/chat`.

Suggested stack: Next.js or vanilla Vite + React + shadcn/ui. Auth: a single bearer
token bound to user_id (or M365 SSO if you want to be fancy).

### 2.2 Meeting recorder + post-meeting brief
**DGX angle.** Whisper-large on GPU + a smart model = an actual differentiator.
- User drops an audio file (Telegram voice / browser upload).
- Whisper transcribes (with speaker diarization via `pyannote.audio` if you want it).
- LLM produces: summary, action items, attendee follow-ups, draft reply emails.
- Action items become tasks. Follow-up emails become drafts in the approval queue.

This is the highest-leverage new feature for an exec-style user.

### 2.3 Document drafting on top of the existing report engine
`reports/pdf_generator.py` already renders branded PDFs with per-user style. Extend
into a "draft a memo / proposal / SOP" flow:
- User: "draft a one-pager on our Q3 priorities, include the calendar headlines and
  the open delegations from agent_2".
- System: assembles context (calendar, tasks, agent inbox, optionally a Qdrant
  search), drafts in markdown, renders to branded PDF, delivers to Telegram.

### 2.4 SQL-grade analytics over the agent's own data
A "command palette" endpoint that lets the LLM answer questions like:
- "How many tasks did I complete last week?"
- "Which contacts have I emailed most often this month?"
- "What's the avg time from triage to send for my replies?"

Implementation: a `db_query` tool exposed to the LLM that wraps a small allowlist of
parameterized queries over `tasks.db` and `logs/email_log.json`. Not a free-form
text-to-SQL — keep the surface narrow.

### 2.5 Per-user memory hygiene
Add `/memory/forget`, `/memory/edit`, `/memory/dump` endpoints. Long-term memory is
written automatically every 6 h (`memory_extract_{uid}` job); users need a way to
correct it. Today a hallucinated "you live in Berlin" fact would silently inject into
every chat turn.

### 2.6 Pluggable provider layer
A `providers/` package with a clean interface:
- `MailProvider` — `fetch_unread`, `send`, `mark_read`. Implementations: `M365Mail`
  (today), `GmailMail` (later), `IMAPSMTPMail` (legacy fallback).
- `CalendarProvider` — same shape, Graph and Google.
- `ChatChannel` — `send_text`, `send_doc`, `receive`. Implementations: Telegram
  (today), Slack, Web.

Then the same single-tenant app works for users on different mail stacks, and the
codebase stops betting on Graph being the only path.

## Tier 3 — Ambitious / research-y

### 3.1 Local fine-tuning loop
**DGX angle.** Spark can comfortably LoRA-tune 7B-13B models. Once you have a few
months of:
- accepted vs rejected email drafts,
- task titles vs the LLM's suggested titles,
- meeting briefs the user actually used,

you have a real preference dataset. Nightly LoRA over the smart model, swap in the
adapter, A/B against the base. Bake this into a `train/` directory with `peft` +
`trl`.

### 3.2 Multi-agent autonomy on real tasks
Today the Manager→Developer mesh at `/delegate` produces text. Wire it to actually
execute:
- Manager decomposes user goal into a task tree (writes to `tasks` table).
- Developer agent (subprocess) picks up tasks marked `assigned_to: agent_X`, executes
  via a tool registry (file ops, web fetch, code edit, shell — all sandboxed).
- Each step posts progress back via the agent inbox (which already handles inline
  Accept/Reject).

The plumbing for delegation, inboxes, and approval gates is **already there**. What's
missing is the executor + tool registry + sandbox. This is where the project could
become a real "company OS for AI agents" — the original framing in the FastAPI
title (`title="Collaborative AI Enterprise OS"`).

### 3.3 Local speech in + out, full duplex
Whisper-streaming + Kokoro-streaming → a "call your assistant" mode over a SIP
trunk (or Telegram voice notes ping-ponging). DGX has enough CPU + GPU headroom
for end-to-end sub-second turns.

### 3.4 Self-hosted vector + relational unification
Today: Qdrant for vectors, SQLite for relational. Once data grows, move to a single
**PostgreSQL with pgvector** instance — keeps the same SQL surface for tasks /
schedules / agent_inbox, gains transactional consistency between "task created" and
"memory updated", and removes a moving part.

### 3.5 Mobile app
Telegram is *good enough* but it's not yours. A thin React Native app talking to the
same FastAPI surface, with on-device microphone, gives you push notifications and a
real "assistant on lock screen" experience without the platform middleman.

## Sequencing suggestion

If forced to pick:
1. **Migrate cleanly + GPU compose + bigger smart model.** Tiers 1.1, 1.3 + every
   P0 from IMPROVEMENTS.md.
2. **Native tool calls (IMPROVEMENTS B2)**, then **Meeting recorder (2.2)** — biggest
   product-quality jumps.
3. **Web dashboard (2.1)** — the moment two users exist, Telegram-only stops scaling.
4. **Local fine-tuning loop (3.1)** — by then you have data; turn the GPU into a
   compounding advantage.
