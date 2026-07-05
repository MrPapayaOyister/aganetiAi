"""Async LLM client → local vLLM (OpenAI-compatible), with tool-calling.

This is the single chokepoint every agent turn goes through. The multi-LLM
router (Phase D) plugs in here: `chat()` will consult routing rules + per-agent
model config; today it targets the 32B tool model directly. Responses are
normalised to plain OpenAI-format dicts so LangGraph state stays JSON-serialisable.
"""
from __future__ import annotations

import os
from openai import AsyncOpenAI

_BASE = os.getenv("VLLM_TOOL_URL", "http://localhost:9000/v1")
_MODEL = os.getenv("VLLM_TOOL_MODEL", "qwen2.5-32b")
_FAST_BASE = os.getenv("VLLM_FAST_URL", "http://localhost:9002/v1")
_FAST_MODEL = os.getenv("VLLM_FAST_MODEL", "qwen2.5-7b")

_tool_client = AsyncOpenAI(base_url=_BASE, api_key="local")
_fast_client = AsyncOpenAI(base_url=_FAST_BASE, api_key="local")


def _normalise(msg) -> dict:
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        out["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
            for tc in msg.tool_calls
        ]
    return out


async def chat(messages: list[dict], tools: list[dict] | None = None, *,
               model: str | None = None, tier: str = "tool",
               temperature: float = 0.2, max_tokens: int = 1024) -> dict:
    """One assistant turn. Returns a normalised assistant message dict
    (with `tool_calls` when the model wants to call tools)."""
    client = _fast_client if tier == "fast" else _tool_client
    use_model = model or (_FAST_MODEL if tier == "fast" else _MODEL)
    kwargs: dict = dict(model=use_model, messages=messages,
                        temperature=temperature, max_tokens=max_tokens)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    resp = await client.chat.completions.create(**kwargs)
    return _normalise(resp.choices[0].message)
