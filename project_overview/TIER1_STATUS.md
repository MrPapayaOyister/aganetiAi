# TIER 1 — Status (implemented on the DGX Spark)

Snapshot of the Tier-1 roadmap items. See ROADMAP.md for the original proposals.

| Item | Status | Notes |
|---|---|---|
| 1.1 Larger smart model (14B) | ✅ done | Qwen 2.5 14B Instruct Q4_K_M, full GPU offload, flash-attention (`-fa on`). |
| 1.2 Always-on streaming chat | ✅ already on | Telegram text + voice handlers POST `/chat` with `stream=True` and do `edit_text` ticks. |
| 1.3 GPU Whisper | ⚠️ code ready, runs on CPU | `whisper_transcriber.py` auto-tries CUDA → falls back to CPU. The pip `ctranslate2` wheel on **aarch64 is CPU-only**, so GPU needs a CUDA build (see below). |
| 1.4 Real RAG ingestion | ✅ done | `backend/ingest.py` rewritten: multi-format, overlap chunking, incremental (hash cache), prune-on-delete, `--watch`. Wired as a 5-min APScheduler job. |
| B2 Native tool-calling | ✅ done | llama.cpp `tools` + `--jinja`. Replaces the `[ACTION:{json}]` tag flow. Legacy path retained as fallback. |

## Native tool-calling (B2)

- Enabled by `NATIVE_TOOLS=true` (default). `/chat` makes one tool-aware call to the
  14B; structured `tool_calls` are dispatched through the existing
  `action_parser.execute_action`.
- Tools: `create_task`, `complete_task`, `draft_email`, `schedule_meeting`
  (`backend/tools.py`).
- Requires the smart container to run with `--jinja` (added to `docker-compose.yml`)
  — without it llama.cpp pollutes `content` and ignores the Qwen tool template.
- The system prompt injects **today's date** so relative dates ("tomorrow") resolve
  correctly, and drops the ~200 lines of brittle action-tag instructions in tool mode.
- Verified: create/complete/schedule fire reliably; plain chat takes no action;
  dates resolve correctly. `draft_email` fires on most phrasings but the 14B **Q4**
  occasionally drifts language / answers inline on certain inputs — moving to
  **Q5_K_M / Q6** (ROADMAP 1.1) is the recommended fix.

## Bug fixed along the way

`/draft_email` used to write a JSON *array* to the project-root `email_drafts.json`,
which the Streamlit approval queue (`frontend/email_drafts.json`, JSONL) never reads —
so chat/tool-created drafts silently vanished. It now appends JSONL to the same queue
file with a UI-compatible schema (`sender`/`subject`/`triage_notes`/`draft_reply`).

## GPU Whisper — how to actually get the GPU

`faster-whisper` uses `ctranslate2`; the PyPI wheel for aarch64 has no CUDA. Options:
1. Build `ctranslate2` from source with `-DWITH_CUDA=ON` for CUDA 13 / sbsa, then
   `pip install` the local wheel. `WHISPER_DEVICE=auto` will then pick CUDA.
2. Or run STT inside an NVIDIA CUDA container that ships a CUDA ctranslate2.

Until then it transcribes on CPU (still correct, just slower).

## New env vars (see `.env.example`)

`NATIVE_TOOLS`, `RUN_BACKGROUND`, `RAG_WATCH_INTERVAL`, `RAG_CHUNK_SIZE`,
`RAG_CHUNK_OVERLAP`, `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE`.

`RUN_BACKGROUND=false` serves the HTTP API only (no bot, no inbox polling) — useful
for local testing and a future web-dashboard host.

## RAG ingestion usage

```bash
python -m backend.ingest            # incremental (skips unchanged via hash cache)
python -m backend.ingest --force    # re-ingest everything
python -m backend.ingest --reset    # drop & recreate the collection
python -m backend.ingest --watch 30 # poll data_vault/ every 30s
```
Drop PDFs/docx/xlsx/csv/txt/md into `data_vault/`; the running app also rescans every
`RAG_WATCH_INTERVAL` seconds.
