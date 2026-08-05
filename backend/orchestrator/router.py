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

# Local model registry. Every entry addresses the LiteLLM gateway — never a vLLM
# port — so the gateway owns keys, routing and its own fallbacks. The names here
# are LiteLLM `model_list` entries, not served-model names.
# cost_* are micro-USD per 1k tokens (local≈0).
from config.settings import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_VISION_MODEL

MODELS: dict[str, dict] = {
    "gateway": {"base_url": LLM_BASE_URL, "api_key": LLM_API_KEY,
                "model": LLM_MODEL,
                "caps": {"tool_call": True, "vision": False, "ctx": 32768},
                "tier": "interactive", "cost_in": 0, "cost_out": 0},
    "gateway-fast": {"base_url": LLM_BASE_URL, "api_key": LLM_API_KEY,
                     "model": os.getenv("LLM_FAST_MODEL", LLM_MODEL),
                     "caps": {"tool_call": True, "vision": False, "ctx": 32768},
                     "tier": "fast", "cost_in": 0, "cost_out": 0},
    "vision-vl": {"base_url": LLM_BASE_URL, "api_key": LLM_API_KEY,
                  "model": LLM_VISION_MODEL,
                  "caps": {"tool_call": True, "vision": True, "ctx": 32768},
                  "tier": "interactive", "cost_in": 0, "cost_out": 0},
}

DEFAULT_CHAIN = ["gateway", "gateway-fast"]

# ── External provider: Azure OpenAI (dashboard builder only) ──────────────────
# Enabled only when AZURE_OPENAI_API_KEY is set. The dashboard agents select it
# when DASHBOARD_LLM=azure (or openai); plan() auto-appends the local vLLM chain
# as fallback, so a bad key / outage silently degrades to local.
_AZURE_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
if _AZURE_KEY:
    MODELS["azure"] = {
        "provider": "azure",
        "azure_endpoint": os.getenv("AZURE_OPENAI_ENDPOINT", "https://nazo-openai.openai.azure.com"),
        "api_version": os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
        "model": os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1"),  # Azure DEPLOYMENT name
        "api_key": _AZURE_KEY,
        "caps": {"tool_call": True, "vision": True, "ctx": 128000},
        "tier": "interactive",
        "cost_in": 2000, "cost_out": 8000,  # micro-USD per 1k (~GPT-4.1 $2/$8 per 1M)
    }


def dashboard_model() -> "str | None":
    """model_key for the dashboard agents; None → local default chain.
    DASHBOARD_LLM=azure|openai → Azure GPT-4.1 (local vLLM auto-appended fallback)."""
    if os.getenv("DASHBOARD_LLM", "").lower() in ("azure", "openai") and "azure" in MODELS:
        return "azure"
    return None

_clients: dict[str, AsyncOpenAI] = {}


def _client(key: str) -> AsyncOpenAI:
    if key not in _clients:
        m = MODELS[key]
        if m.get("provider") == "azure":
            from openai import AsyncAzureOpenAI
            _clients[key] = AsyncAzureOpenAI(
                azure_endpoint=m["azure_endpoint"],
                api_key=m["api_key"],
                api_version=m.get("api_version", "2025-01-01-preview"),
            )
        else:
            _clients[key] = AsyncOpenAI(base_url=m["base_url"], api_key=m.get("api_key") or "unset")
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
        for k in ("gateway-fast", "gateway"):
            if k not in chain:
                chain.append(k)
    for k in DEFAULT_CHAIN:
        if k not in chain and _capable(k, need_tools, need_vision):
            chain.append(k)
    return chain or ["gateway"]


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
    # Route through the dual-backend event logger so per-agent LLM metrics land in
    # Postgres (kind='llm_call'; agent/tokens/cost in meta) for /analytics/agents.
    try:
        from backend import events
        events.log_event("llm_call", user_id=user_id, name=model_key, success=bool(ok), duration_ms=dt,
                         meta={"agent_id": agent_id, "tokens_in": ti, "tokens_out": to, "cost_micros": cost})
    except Exception:
        pass


async def complete(messages: list[dict], tools: list[dict] | None = None, *,
                   agent: dict | None = None, need_vision: bool = False, tier: str | None = None,
                   temperature: float = 0.2, max_tokens: int = 1024, tool_choice: str = "auto", ctx: dict | None = None) -> tuple[dict, str]:
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
            kwargs["tool_choice"] = tool_choice
        if m.get("provider") != "azure":
            # Qwen3 reasoning off at source (see backend/services/llm.py).
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
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
