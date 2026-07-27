"""Tool-calling accuracy eval — LIVE orchestrator stack.

Runs a golden set of prompts through the REAL multi-LLM router (router.complete)
with the REAL primary-agent tool schemas (registry.openai_schemas) and checks
whether the model selects the right tool (or correctly answers with no tool).
This is the metric to watch when swapping models/quants or editing prompts/tool
descriptions. It is side-effect-free: it inspects the model's tool CHOICE without
executing the tool (no tasks created, no emails sent).

Usage:
    python -m evals.tool_calling_eval           # tool-selection accuracy
    python -m evals.tool_calling_eval --judge   # also LLM-judge the no-tool replies

Requires the tool model (router MODELS['tool-...'] @ :9000) up.
Exit code is non-zero if accuracy < THRESHOLD (so it can gate a release).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from backend.orchestrator import agents as _agents  # noqa: F401  registers skills into the registry
from backend.orchestrator import registry, router, templates

THRESHOLD = 0.85  # fail the run below this tool-selection accuracy

# Test with exactly the tools the PRIMARY agent is given in production, so the gate
# reflects real chat behaviour (not the full 27-tool platform superset).
PRIMARY_TOOLS = list(templates.PRIMARY_TOOLS)
SCHEMAS = registry.openai_schemas(PRIMARY_TOOLS)

SYSTEM = (
    "You are Aria, an executive assistant. Today is Tuesday. Respond in English. "
    "Call a tool ONLY when the user explicitly asks you to act or fetch something now; "
    "otherwise just answer. Asking a clarifying question means you must NOT call a tool. "
    "For someone's email/contact details use resolve_contact. For the user's uploaded "
    "files/PDFs use search_documents. For current events/news use web_search (it asks the "
    "user's permission). To hand work to a specialist use delegate."
)

# expect = a tool name, a list of acceptable tool names, or None for "no tool".
GOLDEN = [
    {"msg": "Add a task to call Ahmed tomorrow, high priority.", "expect": "create_task"},
    {"msg": "Mark the slides review task as done.", "expect": "complete_task"},
    {"msg": "What's on my task list right now?", "expect": "list_tasks"},
    {"msg": "Draft an email to sarah@acme.com letting her know the report is ready.", "expect": "draft_email"},
    {"msg": "Send an email to sarah@acme.com right now telling her the report is ready.", "expect": ["send_email", "draft_email"]},
    {"msg": "Schedule a meeting with akshay@meerana.ae tomorrow at 3pm about Q3.", "expect": "create_calendar_event"},
    {"msg": "What's on my calendar this week?", "expect": "get_agenda"},
    {"msg": "Do I have any new emails?", "expect": "list_emails"},
    {"msg": "Open and read the latest email from Sarah.", "expect": ["read_email", "list_emails"]},
    {"msg": "What's Akshay's email address?", "expect": "resolve_contact"},
    {"msg": "What does our uploaded leave-policy PDF say about carryover?", "expect": "search_documents"},
    {"msg": "What do you remember about my meeting preferences?", "expect": "search_memory"},
    {"msg": "Remember that I prefer morning meetings.", "expect": "remember_fact"},
    {"msg": "What's the latest news about AI startups this week?", "expect": "web_search"},
    {"msg": "Start tracking a new deal with Acme worth 50k.", "expect": "create_opportunity"},
    {"msg": "How likely is the Acme deal to close?", "expect": ["predict_deal_outcome", "create_opportunity"]},
    {"msg": "Ask the research agent to look into competitor pricing.", "expect": "delegate"},
    {"msg": "What's today's date?", "expect": "current_time"},
    {"msg": "What is the capital of France?", "expect": None},
    {"msg": "Thanks, that's really helpful!", "expect": None},
    {"msg": "Can you explain what you can do?", "expect": None},
    {"msg": "Schedule a meeting.", "expect": None},  # missing who + when -> ask, don't act
    {"msg": "Good morning!", "expect": None},
]


async def _first_tool(messages: list) -> str | None:
    """The tool the real router would select for this turn (no execution)."""
    msg, _model_key = await router.complete(messages, SCHEMAS, temperature=0.2, max_tokens=256)
    tcs = msg.get("tool_calls") or []
    return tcs[0].get("function", {}).get("name") if tcs else None


async def _reply(messages: list) -> str:
    msg, _ = await router.complete(messages, None, temperature=0.3, max_tokens=200)
    return msg.get("content", "") or ""


async def _judge(question: str, reply: str) -> int:
    prompt = (f"Rate 1-5 how well this reply answers the user (5=great). "
              f"Reply with ONLY the digit.\n\nUser: {question}\nReply: {reply}")
    try:
        msg, _ = await router.complete([{"role": "user", "content": prompt}],
                                       None, tier="fast", temperature=0.0, max_tokens=4)
        txt = (msg.get("content") or "").strip()
        return int(next(c for c in txt if c.isdigit()))
    except Exception:
        return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true", help="also LLM-judge no-tool replies")
    args = ap.parse_args()

    passed = 0
    print(f"Tool-calling eval (LIVE router, {len(PRIMARY_TOOLS)} primary tools) — {len(GOLDEN)} cases\n" + "-" * 66)
    for case in GOLDEN:
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": case["msg"]}]
        got = await _first_tool(messages)
        expected = case["expect"]
        ok = got in expected if isinstance(expected, list) else got == expected
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] expect={str(expected):26} got={str(got):18} | {case['msg'][:38]}")

    acc = passed / len(GOLDEN)
    print("-" * 66)
    print(f"Tool-selection accuracy: {passed}/{len(GOLDEN)} = {acc:.0%}  (threshold {THRESHOLD:.0%})")

    if args.judge:
        print("\nLLM-judge (no-tool replies):")
        scores = []
        for case in (c for c in GOLDEN if c["expect"] is None):
            reply = await _reply([{"role": "system", "content": SYSTEM},
                                  {"role": "user", "content": case["msg"]}])
            s = await _judge(case["msg"], reply)
            scores.append(s)
            print(f"  score={s}/5 | {case['msg'][:45]}")
        if scores:
            print(f"  avg quality: {sum(scores)/len(scores):.1f}/5")

    return 0 if acc >= THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
