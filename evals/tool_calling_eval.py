"""
Tool-calling accuracy eval (+ optional LLM-as-judge for plain replies).

Runs a golden set of prompts against the live smart model with the production tool
schema and checks whether the model picks the right tool (or correctly answers with no
tool). This is the metric to watch when swapping models (e.g. Q4 -> Q5/Q6) or editing
prompts/tool descriptions.

Usage:
    python -m evals.tool_calling_eval            # tool-selection accuracy
    python -m evals.tool_calling_eval --judge    # also LLM-judge the no-tool replies

Requires the smart llama.cpp server (LLM_SMART_URL) running with --jinja.
Exit code is non-zero if accuracy < THRESHOLD (so it can gate a release).
"""

from __future__ import annotations

import argparse
import sys

import httpx

from config.settings import LLM_SMART_URL, LLM_FAST_URL
from backend.tools import TOOL_SCHEMAS

THRESHOLD = 0.85  # fail the run below this tool-selection accuracy

SYSTEM = (
    "You are Aria, an assistant. Today is 2026-06-23 (Tuesday). Respond in English. "
    "Use a tool ONLY when the user explicitly asks you to act now; otherwise just answer. "
    "Asking a clarifying question means you must NOT call a tool. "
    "When a user asks for someone's email address or contact details, call resolve_contact. "
    "For reminders with a specific time (e.g. 'at 4pm', 'in 30 min'), use set_reminder. "
    "For current events or recent news, use web_search."
)

# expect = tool name, or None for "should not call any tool"
GOLDEN = [
    {"msg": "Add a task to call Ahmed tomorrow, high priority.", "expect": "create_task"},
    {"msg": "Remind me to submit the report by Friday.", "expect": ["create_task", "set_reminder"]},
    {"msg": "Mark the slides review task as done.", "expect": "complete_task"},
    {"msg": "I finished the budget task, close it out.", "expect": "complete_task"},
    {"msg": "Draft an email to sarah@acme.com letting her know the report is ready.", "expect": "draft_email"},
    {"msg": "Write an email to ahmed@example.com about the July meeting.", "expect": "draft_email"},
    # use an address so no contact-resolution step is needed (keeps this case deterministic;
    # with a bare name the model correctly chains resolve_contact first)
    {"msg": "Schedule a meeting with akshay@meerana.ae tomorrow at 3pm about Q3.", "expect": "schedule_meeting"},
    {"msg": "Set up a 30 min sync with the team on Thursday at 10am.", "expect": "schedule_meeting"},
    {"msg": "How many tasks did I finish last week?", "expect": "get_analytics"},
    {"msg": "What's on my plate right now?", "expect": "get_analytics"},
    {"msg": "Who do I email the most?", "expect": "get_analytics"},
    {"msg": "What's Akshay's email address?", "expect": "resolve_contact"},
    {"msg": "What does our company handbook say about leave?", "expect": "search_knowledge"},
    {"msg": "Remember that I prefer morning meetings.", "expect": "remember_fact"},
    {"msg": "What do you remember about my meeting preferences?", "expect": "recall_memory"},
    {"msg": "Remind me to call the team in 30 minutes.", "expect": "set_reminder"},
    {"msg": "What is the latest news about AI startups?", "expect": "web_search"},
    {"msg": "What is the capital of France?", "expect": None},
    {"msg": "Thanks, that's really helpful!", "expect": None},
    {"msg": "Can you explain what you can do?", "expect": None},
    {"msg": "Schedule a meeting.", "expect": None},          # missing who+when -> ask, don't act
    {"msg": "Good morning!", "expect": None},
]


def _first_tool(messages: list) -> str | None:
    payload = {"messages": messages, "tools": TOOL_SCHEMAS, "tool_choice": "auto",
               "temperature": 0.2, "max_tokens": 256, "stream": False}
    r = httpx.post(f"{LLM_SMART_URL}/v1/chat/completions", json=payload, timeout=120.0)
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    tcs = msg.get("tool_calls") or []
    if tcs:
        return tcs[0].get("function", {}).get("name")
    return None


def _judge(question: str, reply: str) -> int:
    prompt = (f"Rate 1-5 how well this reply answers the user (5=great). "
              f"Reply with ONLY the digit.\n\nUser: {question}\nReply: {reply}")
    payload = {"messages": [{"role": "user", "content": prompt}],
               "temperature": 0.0, "max_tokens": 4}
    try:
        r = httpx.post(f"{LLM_FAST_URL}/v1/chat/completions", json=payload, timeout=60.0)
        txt = r.json()["choices"][0]["message"]["content"].strip()
        return int(next(c for c in txt if c.isdigit()))
    except Exception:
        return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true", help="also LLM-judge no-tool replies")
    args = ap.parse_args()

    passed = 0
    print(f"Tool-calling eval — {len(GOLDEN)} cases\n" + "-" * 60)
    for case in GOLDEN:
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": case["msg"]}]
        got = _first_tool(messages)
        expected = case["expect"]
        if isinstance(expected, list):
            ok = got in expected
        else:
            ok = got == expected
        passed += ok
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] expect={str(expected):22} got={str(got):16} | {case['msg'][:40]}")

    acc = passed / len(GOLDEN)
    print("-" * 60)
    print(f"Tool-selection accuracy: {passed}/{len(GOLDEN)} = {acc:.0%}  (threshold {THRESHOLD:.0%})")

    if args.judge:
        print("\nLLM-judge (no-tool replies):")
        scores = []
        for case in (c for c in GOLDEN if c["expect"] is None):
            payload = {"messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": case["msg"]}],
                       "temperature": 0.3, "max_tokens": 200}
            reply = httpx.post(f"{LLM_SMART_URL}/v1/chat/completions", json=payload,
                               timeout=120.0).json()["choices"][0]["message"]["content"]
            s = _judge(case["msg"], reply)
            scores.append(s)
            print(f"  score={s}/5 | {case['msg'][:45]}")
        if scores:
            print(f"  avg quality: {sum(scores)/len(scores):.1f}/5")

    sys.exit(0 if acc >= THRESHOLD else 1)


if __name__ == "__main__":
    main()
