"""Multi-LLM router.

Picks a local vLLM model by capability (tool-calling / vision), tier (interactive
vs fast), and per-agent config (explicit model + fallback chain), then calls it
with a fallback chain on failure/timeout. Every call logs tokens + latency + a
cost estimate to the `events` spine (kind='llm_call') so PageAnalytics can show
real model usage/cost.

Org-level routing *constraint* rules (e.g. PII → local-only) are a Phase-D
refinement layered on `plan()`; the capability/tier/per-agent core is here.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from openai import AsyncOpenAI

try:
    from config.settings import BASE_DIR
    _DB = str(BASE_DIR / "tasks" / "tasks.db")
except Exception:
    _DB = "tasks/tasks.db"

# Local vLLM registry on the DGX. cost_* are micro-USD per 1k tokens (local≈0;
# non-zero once external providers are added as gated integrations).
MODELS: dict[str, dict] = {
    "tool-32b": {"base_url": os.getenv("VLLM_TOOL_URL", "http://localhost:9000/v1"),
                 "model": os.getenv("VLLM_TOOL_MODEL", "qwen2.5-32b"),
                 "caps": {"tool_call": True, "vision": False, "ctx": 65536},
                 "tier": "interactive", "cost_in": 0, "cost_out": 0},
    "fast-7b": {"base_url": os.getenv("VLLM_FAST_URL", "http://localhost:9002/v1"),
                "model": os.getenv("VLLM_FAST_MODEL", "qwen2.5-7b"),
                "caps": {"tool_call": True, "vision": False, "ctx": 32768},
                "tier": "fast", "cost_in": 0, "cost_out": 0},
    "vision-vl": {"base_url": os.getenv("VLLM_VL_URL", "http://localhost:9001/v1"),
                  "model": os.getenv("VLLM_VL_MODEL", "qwen2.5-vl-32b"),
                  "caps": {"tool_call": True, "vision": True, "ctx": 32768},
                  "tier": "interactive", "cost_in": 0, "cost_out": 0},
}
DEFAULT_CHAIN = ["tool-32b", "fast-7b"]

_clients: dict[str, AsyncOpenAI] = {}


def _client(key: str) -> AsyncOpenAI:
    if key not in _clients:
        _clients[key] = AsyncOpenAI(base_url=MODELS[key]["base_url"], api_key="local")
    return _clients[key]


def _capable(key: str, need_tools: bool, need_vision: bool) -> bool:
    caps = MODELS[key]["caps"]
    return (not need_vision or caps["vision"]) and (not need_tools or caps["tool_call"])


def plan(agent: dict | None = None, *, need_tools: bool = False,
         need_vision: bool = False, tier: str | None = None) -> list[str]:
    """Ordered model_keys to try: per-agent explicit + fallbacks, then capability/tier defaults."""
    chain: list[str] = []
    if agent:
        mk = agent.get("model_key")
        if mk in MODELS and _capable(mk, need_tools, need_vision):
            chain.append(mk)
        for fb in (agent.get("fallback_models") or []):
            if fb in MODELS and fb not in chain and _capable(fb, need_tools, need_vision):
                chain.append(fb)
    if need_vision:
        for k in ("vision-vl",):
            if k not in chain:
                chain.append(k)
    elif tier == "fast" and not need_tools:
        for k in ("fast-7b", "tool-32b"):
            if k not in chain:
                chain.append(k)
    for k in DEFAULT_CHAIN:
        if k not in chain and _capable(k, need_tools, need_vision):
            chain.append(k)
    return chain or ["tool-32b"]


def _normalise(msg) -> dict:
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        out["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
            for tc in msg.tool_calls]
    return out


def _log(user_id, agent_id, model_key, ti, to, dt, ok):
    cost = int((ti / 1000.0) * MODELS[model_key]["cost_in"] + (to / 1000.0) * MODELS[model_key]["cost_out"])
    try:
        con = sqlite3.connect(_DB, timeout=5.0)
        con.execute("INSERT INTO events(ts,user_id,kind,name,success,duration_ms,meta) VALUES(?,?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), user_id, "llm_call", model_key,
                     1 if ok else 0, dt,
                     json.dumps({"agent_id": agent_id, "tokens_in": ti, "tokens_out": to, "cost_micros": cost})))
        con.commit()
        con.close()
    except Exception:
        pass


async def complete(messages: list[dict], tools: list[dict] | None = None, *,
                   agent: dict | None = None, need_vision: bool = False, tier: str | None = None,
                   temperature: float = 0.2, max_tokens: int = 1024, ctx: dict | None = None) -> tuple[dict, str]:
    """Route + call with fallback. Returns (assistant_message_dict, model_key_used)."""
    need_tools = bool(tools)
    chain = plan(agent, need_tools=need_tools, need_vision=need_vision, tier=tier)
    ctx = ctx or {}
    last_err: Exception | None = None
    for mk in chain:
        m = MODELS[mk]
        kwargs: dict = dict(model=m["model"], messages=messages, temperature=temperature, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        t0 = time.monotonic()
        try:
            resp = await _client(mk).chat.completions.create(timeout=90, **kwargs)
            dt = int((time.monotonic() - t0) * 1000)
            usage = getattr(resp, "usage", None)
            ti = getattr(usage, "prompt_tokens", 0) or 0
            to = getattr(usage, "completion_tokens", 0) or 0
            await asyncio.to_thread(_log, ctx.get("user_id"), ctx.get("agent_id"), mk, ti, to, dt, True)
            return _normalise(resp.choices[0].message), mk
        except Exception as e:  # noqa: BLE001
            last_err = e
            await asyncio.to_thread(_log, ctx.get("user_id"), ctx.get("agent_id"), mk, 0, 0,
                                    int((time.monotonic() - t0) * 1000), False)
            continue
    raise RuntimeError(f"all models failed (chain={chain}): {last_err}")
