# Enterprise Agentic OS — Architecture & Execution Plan (canonical spec)

> Single source of truth. Built on the DGX, pushed to GitHub (`MrPapayaOyister/aganetiAi`). Do not re-derive from chat.

**Stack conventions:** FastAPI + async SQLAlchemy 2.0 + `asyncpg`; **LangGraph** orchestration (Postgres checkpointer, real dynamic tool-calling, resumable, approval-interrupt); vLLM local models on the DGX; faster-whisper (CUDA) STT + Kokoro (EN) / XTTS-v2 (AR) TTS; Qdrant vectors (BGE-M3, EN+AR); Redis pub/sub + Arq async workers; Postgres for all relational state. On-prem, single tenant, multi-tenant-**ready** schema.

## Locked decisions (from user)
1. **Postgres now** (migrate off SQLite `tasks/tasks.db`), async SQLAlchemy + asyncpg.
2. **Per-agent permission model**: permissions are `resource.action`, seeded per template, user can grant more. **HARD GATE on top: any outbound/external action always requires explicit user approval via the `approvals` queue, regardless of permission.** Non-negotiable.
3. **Per-user / multi-tenant-ready**: `org_id` on every user-data table from day one; Org → Department → Designation → User; Postgres RLS.
4. **Agent creation: templates + free-form.** Role-based templates (Exec Assistant, Finance Analyst, PM, HR BP, Sales, Legal, Eng Lead) seed a default fleet + permissions + prompts.
5. **Admin sees NO user content** (structural: admin DB role has no SELECT on content tables).
6. **Build focus order (current):** (1) real LangGraph core logic, (2) multi-LLM model inference/routing, (3) voice in/out (EN+AR) — the current LangGraph is a 2-phase Manager→Developer toy and must be replaced with a real dynamic tool-calling mesh.

## SECTION 1 — Executive Summary
On-prem agentic platform: every employee gets a persistent **Primary Agent** (chief of staff seeded from department/designation/JD) that spawns/delegates to role-specific **Specialist Sub-Agents** doing real work (email, meetings, tasks, calendar, docs, cross-dept coordination) behind local vLLM models. Differentiators: **hard approval gate** on every outbound action, **strict admin/content data isolation**, **real LangGraph mesh** with dynamic tool-calling. v1 = org/auth + per-employee primary+specialist agents with permissioned execution behind the gate + real streaming chat with episodic+semantic memory + multi-LLM routing + analytics/governance/knowledge wired to real events. v2+ = real-time meeting transcription, team analytics, learned routing, more integrations.

## SECTION 2 — Feature List (tags: v1 build now / v2 after stable / future deferred)
- **Auth & Org**: [v1] Supabase Google SSO + ES256 JWT (built), Org→Dept→Designation→User, role (employee/manager/admin), org_id RLS. [v2] SAML/OIDC/SCIM. [future] multi-org SaaS.
- **Onboarding/Profile**: [v1] /onboarding (profile+JD+dept+designation), primary-agent seeding, edit+reseed. [v2] HRIS import, manager approval. [future] skills graph.
- **Primary Agent**: [v1] one persistent per user, role-seeded prompt, own memory, streaming chat, persona editor, model assignment. [v2] proactive briefs, style learning. [future] autonomous background op.
- **Sub-Agent System**: [v1] spawn specialists (template/free-form), LangGraph delegation primary→specialist, lifecycle. [v2] multi-hop delegation, dept-shared agents, concurrency caps. [future] cross-org sharing.
- **Templates (role-based)**: [v1] curated role templates seeding fleet+permissions+prompts, free-form. [v2] admin templates, versioning. [future] marketplace.
- **Memory**: [v1] episodic (Postgres), semantic (Qdrant per-agent+per-user), memory-as-tools, /memory edit/dump. [v2] consolidation job, dept shared memory. [future] provenance UI.
- **Multi-LLM Routing**: [v1] model registry (32B tool, 7B fast, VL vision), rule-based routing by task/capability/latency, per-agent model+fallback, per-request cost log. [v2] latency-aware dynamic, external providers (gated). [future] learned routing.
- **Tool Registry & Execution**: [v1] central typed registry, per-agent allowlist, LangGraph dynamic tool-calling, arg validation (injection-safe). [v2] MCP client/server, user-defined tools. [future] sandboxed code exec.
- **Approval/Governance**: [v1] hard gate (outbound→approvals→user), queue with payload diff, audit log from events. [v2] user auto-approve policies, manager escalation. [future] OOO delegation, SLAs.
- **Tasks**: [v1] CRUD, agent-created, comments, employee→employee delegation. [v2] subtasks/deps, kanban, recurring. [future] gantt.
- **Schedule/Calendar**: [v1] Google Cal read, event create via approval, free-slot, conflict, NL scheduling. [v2] M365, focus blocks. [future] cross-org.
- **Meetings**: [v1] first-class object, pre-brief (agenda+attendee+docs RAG), post-summary+action-items→tasks (from upload). [v2] real-time transcription+diarization. [future] live assistant, bot-join.
- **Documents/Knowledge**: [v1] upload→OCR→chunk→embed→Qdrant, semantic search dept/role-scoped, agent RAG tool, ACLs. [v2] tagging/collections, versioning. [future] collab editing, DLP.
- **Voice**: [v1] streaming STT (Whisper/CUDA) + TTS (Kokoro EN / XTTS-v2 AR), EN+AR, WS session cross-turn context. [v2] barge-in, per-persona voice, wake word. [future] SIP.
- **Analytics**: [v1] events instrumentation (spine exists), personal analytics + per-agent perf, aggregation→snapshots. [v2] team/dept (content-blind), admin cost dashboards. [future] predictive.
- **Alerts**: [v1] rule-based (overdue task, stale agent, missing brief, failed run), center+email digest. [v2] AI anomalies, custom rules. [future] on-call routing.
- **Admin (isolated)**: [v1] system/GPU/model metrics, provisioning, routing+flags, aggregate cost — no content. [v2] quota enforcement, policy editor. [future] eDiscovery (separate audited path).
- **Integrations**: [v1] Google (Gmail/Cal/Contacts), Telegram. [v2] Slack, M365, GitHub, Jira. [future] webhooks.
- **Notifications**: [v1] in-app WS + email digest. [v2] web push, Telegram. [future] SMS/mobile.

## SECTION 3 — Postgres schema
Convention: every table `id UUID PK default gen_random_uuid()`, `created_at/updated_at TIMESTAMPTZ default now()`, `deleted_at TIMESTAMPTZ` (soft delete); user-data tables `org_id UUID NOT NULL REFERENCES organizations`; partial idx `WHERE deleted_at IS NULL`. Qdrant collections (BGE-M3, payload-filtered by org_id+scope): `profiles`, `agent_memory`, `user_memory`, `documents`, `meeting_transcripts`(v2).

Tables (domain cols only; audit cols implied):
- **organizations**(name, slug UNIQUE, settings JSONB)
- **departments**(org_id, name, parent_id→departments SET NULL, head_user_id)
- **designations**(org_id, title, level INT, scopes JSONB)
- **users**(org_id, supabase_uid UNIQUE, email, full_name, department_id, designation_id, role, manager_id→users, primary_agent_id, locale, status)
- **employee_profiles**(user_id UNIQUE, job_description, responsibilities JSONB, working_hours JSONB, seed_prompt, onboarded_at) — **Qdrant: profiles**
- **user_sessions**(user_id, jwt_id, ip, user_agent, expires_at, revoked_at)
- **agents**(org_id, user_id, kind[primary|specialist], template_key, name, persona, system_prompt, model_key, fallback_models JSONB, status, config JSONB, department_id) — partial unique one primary/user
- **agent_permissions**(agent_id, permission, is_outbound BOOL, granted_by, granted_at) unique(agent_id,permission)
- **agent_tools**(agent_id, tool_key→tool_registry, enabled, config)
- **agent_runs**(org_id, user_id, agent_id, parent_run_id→agent_runs, trigger, goal, status[queued|running|awaiting_approval|done|failed|cancelled], state JSONB[LangGraph checkpoint], model_key, tokens_in/out, cost_micros, error, started_at, finished_at)
- **agent_memory_episodic**(agent_id, session_id, role, content, tool_calls JSONB, tokens, run_id)
- **agent_memory_semantic**(agent_id, qdrant_point_id UNIQUE, collection, text, source, salience) — **Qdrant: agent_memory**
- **user_memory_semantic**(user_id, qdrant_point_id, collection, text, source, salience) — **Qdrant: user_memory**
- **tool_registry**(key UNIQUE, name, description, args_schema JSONB, handler, is_outbound BOOL, required_permission, default_model_hint, enabled)
- **approvals**(org_id, user_id, agent_id, run_id, tool_key, action_type, payload JSONB, preview, status[pending|approved|rejected|expired], decided_by, decided_at, expires_at)
- **tasks**(org_id, user_id, assignee_id→users, title, source, status, priority, due_date TIMESTAMPTZ, notes, agent_id, meeting_id, reminder_sent BOOL)
- **task_comments**(task_id, author_id, agent_id, body)
- **schedules**(org_id, user_id, label, cron_expression, action_type, action_payload JSONB, is_active BOOL, agent_id)
- **meetings**(org_id, owner_id, calendar_event_id, title, starts_at, ends_at, location, status, briefing_id, summary_id)
- **meeting_participants**(meeting_id, user_id, email, role, response)
- **meeting_transcripts**(meeting_id, provider, language, segments JSONB, full_text) — **Qdrant v2**
- **meeting_summaries**(meeting_id, summary, decisions JSONB, model_key)
- **meeting_action_items**(meeting_id, text, owner_id, task_id→tasks SET NULL, status)
- **documents**(org_id, owner_id, department_id, title, filename, mime, size_bytes, storage_path, status, acl JSONB, language, sha256) unique(org_id,sha256)
- **document_chunks**(document_id, chunk_index, text, qdrant_point_id UNIQUE, token_count) — **Qdrant: documents**
- **events**(org_id, user_id, agent_id, run_id, kind, name, success BOOL, duration_ms, cost_micros, meta JSONB, ts) — **partition monthly, BRIN(ts)**
- **analytics_snapshots**(org_id, scope, scope_id, period, period_start DATE, metrics JSONB) unique(scope,scope_id,period,period_start)
- **alert_rules**(org_id, name, type, condition JSONB, severity, channels JSONB, enabled, scope)
- **alerts**(org_id, user_id, rule_id, severity, title, body, entity JSONB, status, acked_at)
- **llm_models**(key UNIQUE, provider, endpoint, capabilities JSONB, cost_in/out, latency_tier, enabled)
- **llm_routing_rules**(org_id, priority, match JSONB, action JSONB, constraint BOOL)
- **voice_sessions**(org_id, user_id, agent_id, session_id, language, turns, stt_ms, tts_ms, status, ended_at)
- **integrations**(org_id, user_id, provider, status, scopes JSONB, external_account) unique(user_id,provider)
- **integration_tokens**(user_id, provider, access_token_enc BYTEA, refresh_token_enc BYTEA, expires_at) — **encrypt at rest (AEAD)**
- **notifications**(user_id, type, title, body, entity JSONB, read_at, channels JSONB)
- **admin_audit_log**(org_id, admin_id, action, target_type, target_id, diff JSONB[config only])
- **feature_flags**(org_id nullable, key, enabled, rollout JSONB, updated_by)

## SECTION 4 — Backend to make dummy pages real
Infra: FastAPI api + WS gateway (`/ws`, multiplexed channels: approvals/activity/dashboard/documents/agent/voice), Arq workers (doc-proc, analytics, alerts, meeting, memory-consolidate), APScheduler cron. SSE for chat tokens; WS for approvals/activity/voice.
- **PageAssistant**: `POST /chat` SSE (routes to primary agent, LangGraph, episodic+semantic+RAG context); `GET /agents/{id}/sessions/{sid}/history`; `GET /agents/{id}/status`; `WS /ws/voice`.
- **PageAgentMatrix**: `GET/POST/PATCH/DELETE /agents`; `GET /agent-templates`; `POST/DELETE /agents/{id}/permissions`; `POST /agents/{id}/delegate`. WS agent.updated.
- **PageGovernance/approvals**: `GET /approvals?status=pending`; `POST /approvals/{id}/approve|reject` (approve executes stored payload + resumes LangGraph from awaiting_approval); `GET /audit`. WS approvals channel; row is source of truth.
- **PageExecutive/RightPanel**: `GET /dashboard/summary`, `GET /activity`, `GET /me/context`. WS dashboard/activity.
- **PageAnalytics**: events(partitioned)→Arq rollups→analytics_snapshots→`GET /analytics/summary|tools|tasks|active_hours|agents`.
- **PageKnowledgeHub/documents**: `POST /documents`→Arq doc-proc (extract/OCR[unstructured/pypdf/Tesseract/Qwen-VL]→chunk→BGE-M3→Qdrant), `POST /knowledge/search` (ACL-filtered). WS documents.
- **PageSettings/onboarding**: `GET/PATCH /me/profile|settings|agents/{id}`; `GET/DELETE /integrations`; `POST /onboarding`. Replace in-memory SettingsContext with server state+React Query.
- **Voice pipeline**: WS `/ws/voice`; 16kHz Opus/PCM16; Silero VAD endpointing; STT faster-whisper large-v3 CUDA fp16 (partials+final, ≤400ms); agent run w/ voice session context; TTS Kokoro(EN)/XTTS-v2(AR) streamed sentence-by-sentence (first audio ≤1.2s). Msg types: session.start/audio.chunk/stt.partial/stt.final/agent.thinking/agent.token/tts.chunk/turn.end/approval.required/error/session.end. Failure→text fallback, resume-by-id 60s.
- **Multi-LLM router**: registry(32B@9000 tool, 7B@9002 fast, VL@9001 vision); decision: org constraint rules first → agent explicit model if capable+healthy → task_type→capability→cheapest healthy in latency tier; fallback chain on unhealthy/deep-queue/timeout; cost logged per run; org constraint rules override agent prefs.

## SECTION 5 — Phased roadmap (A–F)
- **A Postgres foundation + org schema + auth (L)**: async SQLAlchemy models + Alembic; org/dept/designation/user/profile; migrate SQLite data; JWT→users row w/ org_id/role; /onboarding. Gate: all endpoints on Postgres, onboarding seeds primary agent, row counts match, no sync DB in request path. Risk: cutover→additive+checkpoint+rollback.
- **B Agent system + LangGraph executor (XL)**: DB agents replace KNOWN_AGENTS; primary auto-seed; CRUD+templates+permissions; LangGraph executor (plan→tool→observe→loop→delegate→await_approval, Postgres checkpointer); approvals on is_outbound; /chat→primary. Gate: template create, delegation, send_email creates approval & doesn't send till approved. Risk: runaway→step/token/depth/budget caps.
- **C Chat + memory (L)**: episodic+semantic live; context builder; memory tools + /memory. Gate: recall across sessions, forget works, no cross-user bleed. Risk: memory poisoning→injection guard+permissioned writes.
- **D Multi-LLM routing + tool registry (M)**: router across local models, per-agent model+fallback, cost/latency, org constraints. Gate: vision→VL, 32B outage→7B, PII refuses external, cost per run. Risk: GPU OOM→fixed partitions+concurrency caps+queue.
- **E Meetings/docs/knowledge/voice (XL)**: doc pipeline; RAG tool; meeting pre-brief+post-summary→tasks; voice EN/AR. Parallel tracks. Gate: PDF searchable, brief cites doc, EN+AR turn <2s, action items→tasks. Risk: voice/GPU contention→dedicated instances/priority queue+text fallback.
- **F Analytics/alerts/admin/governance realtime (L)**: events→rollups; alert engine; data-isolated admin; WS governance. Gate: overdue→alert+digest, admin can't load content (audited), charts real. Risk: admin leak→DB role no SELECT on content tables.

## SECTION 6 — Architecture decisions (Decision→Rationale→Risk→Mitigation)
- **Isolation → row-level org_id + Postgres RLS**. Clean multi-tenant later, one pool. Risk: missing filter leak → RLS policies + repo always injects org_id + isolation tests.
- **Executor → LangGraph + Postgres checkpointer**. Needs resumable tool-calling w/ approval-interrupt. Risk: complexity → thin typed nodes, per-run budgets, checkpoints survive restart.
- **Memory → Postgres episodic + Qdrant semantic + window recency**. Right store per shape. Risk: drift → single memory service owns writes, pointer tables, nightly reconcile.
- **Voice → turn-based streaming + server VAD (v1)**. Near-real-time at fraction of full-duplex cost; barge-in v2. STT≤400ms, first TTS≤1.2s. Fallback→text, resume-by-id.
- **Doc pipeline → async Arq/Redis, GPU OCR only when needed**. Off request path. Risk: backlog → bounded concurrency, per-org limits, WS status.
- **Meeting transcription → post-meeting upload v1; real-time v2**. 80% value now, avoids hard real-time. Whisper large-v3 + pyannote(v2).
- **Routing → rule-based + org constraints (not learned)**. Auditable; avoids cold-start/cost-explosion. Risk: sprawl → constraint rules cap external/enforce local-only, per-agent+org budgets.
- **Approval queue → WS push + persisted rows**. Instant UX + guaranteed delivery. Risk: socket miss → row is source of truth, reconnect resync, email backstop.
- **Admin isolation → separate DB role, metrics-only surface**. Structural not conventional. Admin role: no SELECT on agent_memory_*/documents/meeting_transcripts.
- **DGX serving → vLLM + fixed GPU partitions**. Batching throughput for concurrency. Risk: OOM → pin gpu-memory-utilization, cap max-num-seqs, reserved slices STT/TTS/embed, priority queue.

## SECTION 7 — Frontend changes
Per component (current→changes→realtime): Login(static→OAuth+onboarding gate); Assistant(fake→SSE chat+WS voice+status+memory); AgentMatrix(FE-only→real CRUD/templates/permissions/delegate,WS agent.updated); Executive(hardcoded→dashboard/summary+activity,WS); Analytics(static→/analytics/* from snapshots); Governance(none→approvals+audit,WS approvals→promote to /approvals); KnowledgeHub(none→/documents+search,WS documents); Settings(in-mem→server persist+React Query); RightPanel(static→/me/context,WS); SettingsContext(in-mem→server-backed React Query).
Net-new pages: /onboarding, /meetings, /documents, /admin(isolated), /alerts, /approvals, /voice. Cross-cutting: real API client (src/lib/api typed fetch+React Query), WS client (src/lib/ws multiplexed+reconnect), feature-flag hook.

## SECTION 8 — NOT in v1
Real-time meeting transcription/diarization; learned routing; team/dept analytics; auto-approve policies; external LLM providers; Slack/M365/GitHub; multi-hop delegation; SAML/OIDC/SCIM; doc versioning/collab edit; mobile/push/SMS; barge-in/voice-clone; sandboxed code exec; compliance/eDiscovery; true multi-org SaaS.

## SECTION 9 — Top 10 risks
1. Approval-gate bypass → outbound is registry property enforced at one executor chokepoint + CI test. 2. Cross-org/user leak → RLS+repo org_id+Qdrant payload filter+tests. 3. GPU OOM/contention → fixed partitions, caps, reserved slices, priority queue, health fallback. 4. Executor runaway → step/token/depth/budget caps, timeouts, circuit breaker. 5. Postgres cutover loss → additive dual-write, reconcile, SQLite retained, rollback, checkpoint. 6. Admin isolation leak → DB role no SELECT on content. 7. Prompt injection → guard(content=data)+outbound-gate+arg validation+permissioned memory. 8. events growth → monthly partition+BRIN+rollups+retention. 9. WS scale → Redis fanout, multiplexed socket, durable state, reconnect resync, heartbeat. 10. OAuth token compromise → encrypt at rest, least-scope, rotation, never to FE.
