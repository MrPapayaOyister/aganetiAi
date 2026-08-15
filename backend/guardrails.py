"""
Action guardrails — the single place that decides what Aria may do autonomously,
what must produce a draft/approval step, and what is outright denied.

Research consensus (2026): an agent's reliability comes from explicit tool contracts
and approval policy, not the model alone. This centralizes that policy so no tool can
silently bypass it, and so the autonomy posture is one env var.

AUTONOMY_LEVEL:
  - "assist"      : even task creation is gated (approval) — most conservative
  - "standard"    : tasks/reads are autonomous; comms (email/meeting) are approval-gated  (default)
  - "autonomous"  : everything the agent can do runs without sign-off
                    (note: there is deliberately NO direct "send email" tool — comms
                     always become a draft for the human to send)
"""

from __future__ import annotations

import os

AUTONOMY_LEVEL = os.getenv("AUTONOMY_LEVEL", "standard").lower()

# Emergency kill switch. Comma-separated tool names that are refused outright,
# ahead of every other rule and regardless of grants or autonomy level. This is the
# operational lever for "turn that tool off NOW" without a deploy — the P0 audit
# found there was no way to disable a tool short of editing an agent's permissions
# one row at a time.
DENIED_TOOLS = {t.strip() for t in os.getenv("AGANETI_DENIED_TOOLS", "").split(",") if t.strip()}

# Every dispatchable action MUST be listed here; anything else is denied by default.
ACTION_CATEGORY = {
    "create_task": "task",
    "complete_task": "task",
    "get_analytics": "read",
    "resolve_contact": "read",
    "search_knowledge": "read",
    "recall_memory": "read",
    "get_emails": "read",         # read-only inbox listing
    "read_email": "read",         # read-only: one message's body, no mutation
    "get_agenda": "read",         # read-only Google Calendar agenda
    "get_contacts": "read",       # read-only Google Contacts lookup
    "remember_fact": "task",      # benign memory write
    "set_reminder": "task",       # one-shot Telegram alert, no external side-effect
    "draft_email": "comms",       # produces a draft in the approval queue, never sends
    "schedule_meeting": "comms",
    "web_search": "read",
    "play_youtube_video": "read",      # renders a player; no state is mutated
    "get_youtube_video_info": "read",  # oEmbed metadata lookup
    "search_youtube": "read",          # keyless yt-dlp lookup, no state change
    "get_weather": "read",             # keyless Open-Meteo lookup, no state change
    "get_news": "read",                # public RSS fetch, no state change
    "watch_live_tv": "read",           # opens a player; no state change
    "add_tv_channel": "task",          # writes to the user's channel library
    "search_tv_channels": "read",      # directory lookup, no state change
    "add_tv_channels_bulk": "task",    # the picker is the confirmation
    # Pure CPU: encodes a string into an image. No network, no state, nothing
    # leaves the process — the strongest "read" in this table.
    "generate_qr_code": "read",
    # Open a player against a public directory; no state is mutated.
    "uae_radio": "read",
    "arabic_radio": "read",
    "quran_radio": "read",
    "radio_by_genre": "read",
    "search_radio": "read",
    "search_radio_stations": "read",   # directory browse, nothing saved
    "my_radio": "read",                # plays the user's own list
    "add_radio_stations_bulk": "task", # the picker is the confirmation
    "remove_radio_station": "task",
}

# ── Runtime B (backend/orchestrator) tool policy ──────────────────────────────
# ACTION_CATEGORY above is Runtime A's catalogue (backend/tools.py). The LangGraph
# runtime has a DIFFERENT, almost disjoint catalogue, and until now guardrails knew
# nothing about it — which is half of why the audit found `decide()` decorative.
#
# This table classifies every tool in backend/orchestrator/registry.py so the SAME
# policy function decides for both runtimes. It is intentionally a separate map:
# merging them would silently widen Runtime A's allowlist (its dispatcher denies
# anything ACTION_CATEGORY does not name, and that is load-bearing).
#
# Categories are additive to the three above:
#   code — executes caller-supplied code. Gated like `task`: auto at standard,
#          approval at assist. It is sandboxed (network-none Docker), which is why
#          it is not `comms`.
#   data — reads the customer's production database. Auto, but nameable here so a
#          tenant can be tightened without a code change.
TOOL_CATEGORY = {
    # reads
    "current_time": "read", "calc": "read", "list_tasks": "read", "get_agenda": "read",
    "next_event": "read", "list_emails": "read", "read_email": "read",
    "email_digest": "read", "search_documents": "read", "search_memory": "read",
    # knowledge_search federates the same corpus as search_documents plus the
    # knowledge graph, read-only and with no egress — so it carries the same
    # classification. (POC-1)
    "knowledge_search": "read",
    "resolve_contact": "read", "get_analytics": "read", "list_opportunities": "read",
    "predict_task_slippage": "read", "predict_followups": "read",
    "predict_relationship_value": "read", "predict_deal_outcome": "read",
    "list_charts": "read",
    # writes to the user's own records
    "create_task": "task", "complete_task": "task", "remember_fact": "task",
    "set_reminder": "task", "create_opportunity": "task",
    "save_chart": "task", "delete_chart": "task",
    # a draft is the approval step — nothing is sent
    "draft_email": "read",
    # sub-agent execution: the nested run re-enters this same boundary per tool,
    # so gating the delegation itself would double-gate.
    "delegate": "read",
    # code execution (sandboxed)
    "run_python": "code",
    # customer production data (read-only SQL, SELECT-only + PII blocklist)
    "get_database_schema": "data", "query_data": "data",
    "forecast_metric": "data", "compare_periods": "data",
    # curated metric contract (backend/dashboard/analytics_tools.py) — registered
    # at dashboard-router import time, so only present in some processes.
    "list_metrics": "data", "run_metric": "data",
    # outbound — these ALSO carry registry is_outbound=True, which is the
    # independent authority. Listed for completeness of the snapshot.
    "send_email": "comms", "create_calendar_event": "comms", "web_search": "comms",

    # ── consumer capabilities migrated from Runtime A (see consumer_tools.py) ──
    # Category `egress`, not `comms`, and the distinction is deliberate rather than
    # convenient. `web_search` is approval-gated because it sends ARBITRARY USER
    # TEXT to a general-purpose search engine — that is the on-prem posture the
    # gate exists for. These send a NARROW, TOOL-SPECIFIC parameter (a video id, a
    # station name, a city) to a FIXED, KNOWN endpoint, and one of them
    # (generate_qr_code) reaches no network at all. Treating them as `comms` would
    # put an approval click in front of "play some music", which nobody would keep
    # switched on for long — and a gate that gets switched off protects nothing.
    #
    # `egress` is auto at standard/autonomous and approval at assist, so a tenant
    # that does want every outbound byte reviewed sets AUTONOMY_LEVEL=assist and
    # gets it, without this table having to lie about what these tools are.
    "play_youtube_video": "egress", "get_youtube_video_info": "egress",
    "search_youtube": "egress",
    "watch_live_tv": "egress", "search_tv_channels": "egress",
    "add_tv_channels": "task",
    "play_radio": "egress", "search_radio_stations": "egress", "my_radio": "egress",
    "manage_radio_stations": "task",
    "get_weather": "egress", "get_news": "egress",
    "generate_qr_code": "read",          # pure CPU, no network at all

    # Knowledge graph. Read-only, on-prem Neo4j, tenant-filtered in Cypher — no
    # egress at all, so `read` rather than `egress`. See orchestrator/graph_tools.py.
    "graph_search": "read",

    # ── browser automation (browser_tools/, Phase D) ──────────────────────────
    # NONE of these is outbound, and that is a decision with a mechanism behind it
    # rather than a convenience.
    #
    # WHY NOT OUTBOUND. `risk_of()` (authz.py:131-141) lets `is_outbound` override
    # the category outright, and the `outbound` rule is evaluated BEFORE
    # `guardrail_approval` in the rule order (authz.py:207-249). The `outbound`
    # rule yields APPROVAL_REQUIRED. So marking browser tools outbound would put an
    # approval click in front of `browser_inspect` — the tool the agent uses to see
    # the page at all — and in front of all 13 others, not just `browser_submit`.
    # §3.2's Trap 2 flags this as a `[DECIDE]`; this is the decision.
    #
    # It is also factually true today: the worker reaches only
    # http://browser-lab:8080, an internal compose service. Nothing leaves the
    # deployment. `web_search` is `comms` because a user-authored query egresses to
    # a third-party engine; a browser session against an internal lab does not.
    #
    # THIS STOPS BEING TRUE AT PHASE K, when the allowlist gains an external host.
    # The tripwire is `test_browser_domain_allowlist_is_internal_only` in
    # tests/test_browser_registration.py: it fails the moment a non-internal host is
    # added, and its failure message says to revisit this block. Do not silence it.
    #
    # `browser_submit` is the only one gated, and NOT from its category — approval
    # comes from `guardrail_approval` via the `comms` mapping below. Writing a
    # scarier value on the others would gate nothing (§3.2).
    "browser_open": "read", "browser_close": "read",
    "browser_navigate": "read", "browser_inspect": "read",
    "browser_screenshot": "read", "browser_extract": "read",
    "browser_wait": "read", "browser_back": "read",
    # Writes to the page, not to our records — but a form field is state the user
    # will be held to, so `task` rather than `read`.
    "browser_click": "task", "browser_fill": "task", "browser_select": "task",
    "browser_check": "task", "browser_upload": "task",
    # The one approval-gated tool. `comms` -> approval at standard autonomy, which
    # is what makes the §19 governance demo real.
    "browser_submit": "comms",
}


def _verdict(category: str | None, level: str | None = None) -> str:
    """category -> "auto" | "approval" | "deny". The ONE place a verdict is decided.

    Both `decide` (Runtime A) and `decide_tool` (Runtime B) go through here, so the
    two runtimes cannot drift apart on what a category means — which is exactly how
    `web_search` ended up approval-gated on one path and auto on the other.
    """
    lvl = (level or AUTONOMY_LEVEL or "standard").lower()
    if category is None:
        return "deny"  # unknown / hallucinated action
    if category in ("read", "data"):
        return "auto"
    if category in ("task", "code", "egress"):
        return "auto" if lvl in ("standard", "autonomous") else "approval"
    if category == "comms":
        return "auto" if lvl == "autonomous" else "approval"
    return "approval"


# verdicts: "auto" (do it), "approval" (do the draft/queue step), "deny" (refuse)
def decide(action_type: str) -> str:
    """Runtime A verdict. Behaviour unchanged: unknown -> deny, same category rules."""
    if action_type in DENIED_TOOLS:
        return "deny"
    return _verdict(ACTION_CATEGORY.get(action_type))


def decide_tool(tool_name: str, *, is_outbound: bool = False) -> str:
    """Runtime B verdict for a registry tool.

    Differs from `decide` in exactly two ways, both deliberate:

      * `is_outbound` (the registry's own flag) forces "approval" regardless of the
        category table. The registry is the authority on what leaves the system;
        this table must never be able to downgrade that.
      * an UNLISTED tool is NOT auto-denied. Runtime B's grant model is a per-agent
        allowlist that the caller has already checked, so an unclassified tool is a
        gap in this table rather than a hallucination. It is treated as `task`
        (auto at standard, approval at assist) and logged by the caller.
    """
    if tool_name in DENIED_TOOLS:
        return "deny"
    if is_outbound:
        return "approval"
    return _verdict(TOOL_CATEGORY.get(tool_name, "task"))


def policy_snapshot() -> dict:
    return {
        "autonomy_level": AUTONOMY_LEVEL,
        "denied_tools": sorted(DENIED_TOOLS),
        "actions": {a: {"category": c, "verdict": decide(a)}
                    for a, c in ACTION_CATEGORY.items()},
        "tools": {t: {"category": c, "verdict": decide_tool(t)}
                  for t, c in TOOL_CATEGORY.items()},
        "note": "comms actions always create a draft for the user to send; "
                "there is no autonomous send-email capability.",
    }
