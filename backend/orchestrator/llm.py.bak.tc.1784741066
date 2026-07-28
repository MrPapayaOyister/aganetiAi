"""Async LLM entrypoint for agents — delegates to the multi-LLM router.

Stable seam: the executor calls `llm.chat(...)`; the router (router.py) owns model
selection (capability/tier/per-agent + fallback chain) and per-call cost logging.
"""
from __future__ import annotations

from . import router


async def chat(messages: list[dict], tools: list[dict] | None = None, *,
               tier: str = "tool", temperature: float = 0.2, max_tokens: int = 1024,
               agent: dict | None = None, ctx: dict | None = None,
               need_vision: bool = False) -> dict:
    """One assistant turn, routed. `agent` may carry {model_key, fallback_models};
    `ctx` carries {user_id, agent_id} for cost accounting."""
    _tier = "fast" if tier == "fast" else None
    msg, _model_key = await router.complete(
        messages, tools, agent=agent, tier=_tier, need_vision=need_vision,
        temperature=temperature, max_tokens=max_tokens, ctx=ctx)
    return msg
