# TIER 2 — Status (implemented on the DGX Spark)

Subset of Tier-2 the user prioritized. Web dashboard is deferred until the base is
solid; meeting recorder is the next planned item (not yet built).

| Item | Status | Entry points |
|---|---|---|
| Document drafting (memo/proposal/SOP/one-pager/letter/brief) | ✅ done | `POST /document/draft`; Telegram "draft a memo about …" |
| Analytics over own data | ✅ done | `GET /analytics?metric=…`; `GET /analytics/metrics`; chat tool `get_analytics` |
| Memory hygiene | ✅ done | `GET /memory/dump`, `POST /memory/forget`, `PATCH /memory/edit` |
| Meeting recorder + brief | ⛔ not started | next up |
| Pluggable providers | ⛔ not started | later |
| Web dashboard | ⏸ deferred | after base hardening |

## Document drafting (`backend/documents.py`)

- `draft_document(user_id, doc_type, topic, includes, title)` → branded PDF via the
  existing WeasyPrint engine + a new `reports/templates/document_base.html`.
- doc types: memo, proposal, sop, one-pager, letter, brief (aliased loosely).
- `includes` pulls live context into the draft: `calendar`, `tasks`, `agents`
  (delegations), `memory`. Verified it pulls real tasks into the output.
- Self-contained markdown→HTML converter (the `markdown` lib isn't installed; avoids a
  new dependency).
- Telegram: "draft a memo/proposal/sop/one-pager/letter about X (include my tasks)"
  generates and sends the PDF. Checked before the report intent so it isn't swallowed.

## Analytics (`backend/analytics.py`)

- Narrow **allowlist** of parameterized metrics (NOT free-form text-to-SQL):
  `summary`, `completed`, `created`, `pending_by_priority`, `top_contacts`,
  `triage_volume`. Unknown metrics are rejected.
- Over `tasks/tasks.db` + `logs/email_log.json`. Each result carries a `human` string.
- Exposed to chat as the `get_analytics` tool — "how many tasks did I finish last week?"
  is answered directly.

## Memory hygiene (`backend/memory_admin.py`)

- `dump` (scroll all facts), `forget` (by point_id or best semantic match for a query),
  `edit` (replace fact text + re-embed). All handle a missing collection gracefully.

## Bug fixed: long-term memory dimension mismatch

`memory/long_term.py` hardcoded `VECTOR_SIZE = 3584` (a 7B). The 14B embeds at **5120**,
so every memory upsert silently failed after the model swap. Now the dimension is
**probed from the live endpoint and cached** (`get_vector_size()`), so collections
always match the model. Verified: seed → dump → edit → forget round-trips correctly.

## Note for the future web dashboard

`RUN_BACKGROUND=false` runs the API HTTP-only (no bot, no inbox polling) — the right
mode for a dashboard host that shouldn't double-drive the bot.
