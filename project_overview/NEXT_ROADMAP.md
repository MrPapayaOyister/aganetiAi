# NEXT ROADMAP — making Aria better

## STATUS (updated 2026-06-23 — BATCH 3)
**P0 — ALL DONE.** See above.

**P1 — ALL DONE (minus #16 pluggable providers, dropped by user):**
✅ #6 streaming · ✅ #7 proactive briefings · ✅ #8 calendar intelligence (real Graph
`create_calendar_event`, attendee resolution, free-slot finder, `parse_meeting_time` NL
parser — stub fully replaced) · ✅ #9 contact-aware email · ✅ #10 thread summarization
+ inbox prioritization (priority scoring by importance/sender/urgency keywords, thread
detection by conversationId, `summarise_thread` fn) · ✅ #11 writing-style learning
(`backend/writing_style.py` — accepted drafts logged on `/send_email`, style examples
injected into system prompt as few-shot block) · ✅ #12 memory-as-tools ·
✅ #13 multi-step chaining · ✅ #14 meeting recorder (`/meeting/transcribe` multipart +
`/meeting/transcribe_path` for Telegram, Whisper→LLM→tasks+follow-up draft, Telegram
audio handler) · ✅ #17 web-search tool (`ddgs` DDGS, returns top results with sources).

**New tools (11 total):** `set_reminder` (one-shot Telegram alert — APScheduler `date`
trigger, stored as `once:{ISO}` cron, restored on restart), `web_search` (DuckDuckGo,
no API key needed).

**Active lies fixed:** schedule_meeting stub → real Graph event created (verified:
event_id + Teams join_url returned) · "remind me at X" → `set_reminder` actually stores
in DB and fires APScheduler job at exact time.

**Tests:** 41 unit tests pass · eval 95-100% / 22 cases (threshold 85%) — two new golden
cases: `set_reminder`, `web_search`.

**After P1 (user's stated plan):** Web UI design (delegation/tasks/reminders/schedule,
premium-animation Claude connectors) · extreme-low-latency live-chat-over-web architecture.

---


Prioritized backlog grounded in (a) this codebase's gaps, (b) the live issues found on
2026-06-23, and (c) current agentic-AI best practice (proactivity, reliability/evals,
memory-as-tools, guardrails, MCP). Effort: S(≤½day) · M(1-2 days) · L(3+ days).

## Already fixed (2026-06-23)
- Capabilities were under-reported in the chat prompt → now lists documents, analytics,
  memory, RAG, voice.
- Text felt frozen/slow → root cause was the first voice message live-downloading
  spaCy + Kokoro + Whisper (GIL/CPU contention). Added **PREWARM_MODELS** (loads them at
  startup) and turned **passive task-detection off** (removed the 2nd LLM call per turn).

## P0 — Reliability & trust (research: "quality is the #1 barrier to agents in prod")
1. **Eval harness + tests** (M). No tests exist today. Add tool-calling evals (did it call
   the right tool with right args?), golden-set task-completion checks, and an
   LLM-as-judge for reply quality; run in CI. This is the single highest-leverage item.
2. **Structured logging + trace IDs** (S). Replace `print(...)` with the `logging` module,
   rotating file, a request/turn id threaded through chat→tool→endpoint for debuggability.
3. **Guardrail / approval policy** (M). Make explicit what Aria does autonomously vs. what
   needs human approval (send email already gated; add the same for calendar writes and
   any future "execute" actions). Central allow/escalate config.
4. **Embedding consistency** (S). Two embedding backends today: fastembed/384 (corporate
   RAG) vs llama-server/5120 (long-term memory). Unify on fastembed so memory survives
   model swaps and there's one dimension to reason about. (Dim mismatch already hot-fixed.)
5. **Model quality: Q5_K_M/Q6 swap** (S). The 14B Q4 occasionally drifts language / skips
   draft_email. A higher-quality quant is the real fix (GB10 + 121 GB RAM can run it).
6. **Centralized error envelope** (S) + don't crash chat on any single context source
   (calendar already hardened; do the same audit everywhere).

## P1 — Make existing features better
7. **True token streaming for text** (M). The native-tools path returns one chunk; stream
   the model's answer token-by-token (parse streamed tool_calls) so replies appear live.
8. **Proactive briefings** (M). Research's biggest theme: don't just react. A morning brief
   (calendar + top tasks + flagged email), pre-meeting nudges (already partly there),
   end-of-day "what slipped". Push without being asked.
9. **Calendar intelligence** (L). Real `schedule_meeting` (it's a stub) — find free slots,
   create the Graph event, send invites, detect/resolve conflicts, protect focus blocks.
10. **Contact-aware email** (M). Before draft_email, resolve names→addresses via the
    contacts store ("email Akshay" should fill akshay@meerana.ae automatically).
11. **Email thread summarization + prioritization** (M). Summarize long threads; rank
    unread by importance in the digest, not just list them.
12. **Writing-style learning** (L). Learn the user's tone from accepted vs edited drafts
    (the data the local fine-tuning loop would use); start with few-shot style exemplars.
13. **Memory-as-tools** (M). Expose remember/forget/recall as native tools (MemGPT style)
    so the model curates memory in-conversation, plus the /memory endpoints already built.
14. **Multi-step tool loops** (M). Today /chat does one tool round. Allow the model to
    chain (e.g. resolve contact → draft email) by feeding tool results back for a 2nd pass.

## P1 — New capabilities (your stated plan)
15. **Meeting recorder + post-meeting brief** (L). NEXT. Audio → Whisper (+ diarization)
    → summary, action items (→ tasks), follow-up emails (→ drafts). Highest-leverage new
    feature; the pieces (Whisper, tasks, drafts) already exist.
16. **Web dashboard** (L, deferred until base solid). Today/Inbox/Tasks/Schedules/Memory
    panels wired to the real endpoints; run the API host with RUN_BACKGROUND=false.
17. **Pluggable providers** (L). MailProvider / CalendarProvider / ChatChannel interfaces
    so Gmail, Slack, and a web channel can be added without betting everything on M365.
18. **Web search / browse tool** (M). Let Aria answer current-info questions and research
    topics, with citations — a native tool calling out to a search API.
19. **Document Q&A over the vault** (M). "What does our leave policy say?" — RAG answer
    with the source chunk cited (ingestion already built; add a retrieval-cite tool).

## P2 — Platform & scale
20. **Install systemd service** (S, needs sudo). `deploy/aganeti-api.service` exists; make
    it managed so it survives reboots and auto-restarts.
21. **De-hardcode users** (S). Iterate `get_all_user_ids()` in poll_inbox + meeting prep;
    adding user_3 should be one `.env` change.
22. **Packaging + real migrations** (M). `pyproject.toml`/src layout (drop sys.path hacks);
    alembic or versioned SQL instead of try/except ALTER TABLE.
23. **MCP server** (M). Expose Aria's tools over MCP so other agents/clients can drive it,
    and let Aria consume external MCP servers (the 2026 interop standard).
24. **Local fine-tuning loop** (L). LoRA on accepted/rejected drafts once enough data —
    turns the GB10 into a compounding advantage.
25. **Security pass** (M). The API binds 0.0.0.0 (LAN-exposed, unauthenticated). Add an API
    key / bind loopback + reverse proxy before it leaves a trusted LAN.

## Sequencing suggestion
1. P0 #1 (evals) + #2 (logging) + #5 (Q5/Q6) — make it trustworthy & debuggable.
2. #7 (streaming) + #8 (proactive briefings) + #10 (contact-aware email) — quick UX wins.
3. #15 (meeting recorder) — the marquee new feature.
4. #9 (calendar intelligence) + #16 (web dashboard).
