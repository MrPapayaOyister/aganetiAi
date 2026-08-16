"""The routing bench: ask the live model what tool it would call, faithfully.

WHY THIS EXISTS, AND WHY IT IS NOT A HAND-ROLLED REQUEST
--------------------------------------------------------
Three separate investigations reached confident, wrong conclusions because the
bench sending the request did not match what the server sends. Each divergence
cost a full cycle:

  1. system prompt — the bench sent ~3.2k chars, the server sends ~9.8k. Tools
     like get_emails routed correctly under one and not the other.
  2. temperature — the bench used 0, the server uses 0.2.
  3. `chat_template_kwargs: {"enable_thinking": False}` — the server disables
     thinking; the bench never sent the flag, so it ran WITH thinking. On one
     identical payload that single flag was the whole result:
         thinking ON   8/8 tool re-called
         thinking OFF  0/8 tool re-called
     Every conclusion drawn on the old bench was measuring a different system.

So this module does NOT assemble a request body. It calls
`backend.services.llm.build_payload`, the same function the server calls, so a
future change there cannot silently desynchronise the bench again. The only
things stated locally are the sampling parameters the /chat handler passes, and
they are named in one place below.

TRIALS, NEVER SINGLE SAMPLES
----------------------------
The gateway is non-deterministic even at temperature 0 — two runs of the same
A/B have disagreed. Anything asserted from one sample here is noise. `trials()`
takes N and returns counts; callers assert on a majority, not an instance.
"""

from __future__ import annotations

import collections
import json
import pathlib
import urllib.request

import pytest

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "system_prompt.txt"

# What backend/main.py's call_llm_tools passes to acomplete_raw. Kept beside the
# import of build_payload so the two are read together; if these drift from the
# handler the bench is lying again.
TEMPERATURE = 0.2
MAX_TOKENS = 256


def system_prompt() -> str:
    """A real captured /chat system prompt, redacted.

    Redacted rather than rebuilt: the live one is assembled inline across ~180
    lines of the request handler and cannot be called from a test. Its SHAPE and
    SCALE are what mattered — a short prompt gives different routing answers — so
    the capture is kept at full length with the real names and dates scrubbed.

    When the prompt assembly is extracted into a function, this should call it
    instead and the fixture can go.
    """
    return FIXTURE.read_text(encoding="utf-8")


def gateway_up() -> bool:
    from config.settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    try:
        body = {"model": LLM_MODEL, "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1}
        req = urllib.request.Request(
            LLM_BASE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {LLM_API_KEY}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


needs_model = pytest.mark.skipif(
    not gateway_up(), reason="LLM gateway not reachable — routing bench skipped")


def ask(messages: list[dict], tools=None) -> str | None:
    """One call. Returns the first tool name, or None if the model called none.

    The body comes from build_payload, so `chat_template_kwargs` and anything
    else the server sends is included by construction rather than by memory.
    """
    from config.settings import LLM_API_KEY, LLM_BASE_URL
    from backend.services import llm as _llm
    import backend.tools as _tools

    payload = _llm.build_payload(
        messages,
        tools=tools if tools is not None else _tools.tools_for([]),
        tool_choice="auto", temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
    req = urllib.request.Request(
        LLM_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LLM_API_KEY}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.load(r)
    calls = (data["choices"][0]["message"] or {}).get("tool_calls") or []
    return calls[0]["function"]["name"] if calls else None


def chain(messages: list[dict], tools=None) -> list[str]:
    """EVERY tool name in the response, not just the first.

    Recording only the first scored correct chaining as a failure: asked to
    "draft an email to <name>", the model calls resolve_contact first and
    draft_email after, which is right. A bench that stops at index 0 reports
    that as a miss.
    """
    from config.settings import LLM_API_KEY, LLM_BASE_URL
    from backend.services import llm as _llm
    import backend.tools as _tools

    payload = _llm.build_payload(
        messages,
        tools=tools if tools is not None else _tools.tools_for([]),
        tool_choice="auto", temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
    req = urllib.request.Request(
        LLM_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LLM_API_KEY}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.load(r)
    calls = (data["choices"][0]["message"] or {}).get("tool_calls") or []
    return [c["function"]["name"] for c in calls]


def turn(user_message: str, history: list[dict] | None = None) -> list[dict]:
    """The message list a /chat turn sends: system prompt, history, user."""
    return ([{"role": "system", "content": system_prompt()}]
            + list(history or [])
            + [{"role": "user", "content": user_message}])


def trials(messages: list[dict], n: int = 5) -> collections.Counter:
    """N calls, counted. `None` (no tool call) is counted under the key None."""
    return collections.Counter(ask(messages) for _ in range(n))


def routes_to(user_message: str, want: str, n: int = 5,
              history: list[dict] | None = None) -> tuple[int, dict]:
    """(hits, full distribution) — a tool anywhere in the chain counts as a hit."""
    msgs = turn(user_message, history)
    hits, dist = 0, collections.Counter()
    for _ in range(n):
        names = chain(msgs)
        dist[tuple(names) or ("(none)",)] += 1
        hits += want in names
    return hits, dict(dist)
