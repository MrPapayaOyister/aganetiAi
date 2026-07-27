# RAG Retrieval Layer — Frozen Contract (v1)

> The single, frozen use case. Do NOT expand scope until the retrieval eval (Phase 5) passes.

## Frozen use case
For **one logged-in user**, answer a natural-language question grounded in **their email threads + meeting notes + curated docs**, return the answer with inline `[n]` citations to specific chunks (or abstain **"Evidence missing — I cannot answer from available sources"**), and offer to hand the grounded result into **one** next action (`create_task` / `draft_email` / `meeting_brief`) behind the existing outbound-approval gate.

## The one boundary
`ingest.search_corporate(query, owner, source_types=None, since=None)` → ranked chunks **with metadata** → `registry._search_documents` wraps a numbered evidence block → the primary agent answers only from that block citing `[n]` (or abstains) → a confirmed action becomes the gated approval preview. Everything downstream (hybrid, rerank, clarify, MCP) plugs into this one boundary without changing the contract.

## Source model
- `source_type ∈ {file, email, meeting}`.
- **Ownership (hard rule):** `email` and `meeting` chunks are **ALWAYS** owned by the individual `user_id` — never the shared `__org__` sentinel. `file` (doc) chunks default to the uploader's `user_id`; a doc may be `__org__` **only when explicitly curated** as shared org knowledge.
- **ACL:** a search returns the caller's own chunks **plus** the shared `__org__` corpus, never another user's private content (`MatchAny([owner, "__org__"])`). `owner=None` (unauthenticated/legacy) returns **org-only**.
- **Source priority (tie-break for ranking/citation):** curated `file` > `meeting` > `email`.

## Sensitivity
`sensitivity ∈ {public, internal, confidential, restricted}` (ordered). Default `internal`. Reserved for a future policy filter (a user's allowed levels); stored on every chunk now so the filter is a payload match later, not a re-ingest.

## Canonical chunk metadata (Qdrant payload + Postgres lineage)
`{text, user_id (owner), org_id, source_type, source_id, document_id, chunk_index, title, author, timestamp, sensitivity, version, heading_path}`. Postgres (`documents`/`document_chunks` + `email_*`/`meeting_*`) is the canonical lineage/dedup/version store; Qdrant holds the vector + a filterable subset. Link = one deterministic `qdrant_point_id` per chunk (1 chunk = 1 point).

## Status
- ✅ Per-user ACL live (owner-namespaced ids + `MatchAny` filter).
- ✅ Legacy unfiltered path (`retrieve_corporate_context`) closed → routes through `search_corporate`.
- ✅ Alembic adopted (genesis baseline stamped); lineage tables land via migration.
- ⏳ Email/meeting ingestion, rich metadata, hybrid+rerank, citations, retrieval eval — Phases 2–5.
