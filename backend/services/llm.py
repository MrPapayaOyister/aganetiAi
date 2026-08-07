"""
The single inference gateway. Every LLM call in the app goes through here.

    app ──► LiteLLM (http://localhost:4000/v1) ──► qwen-fast ──► vLLM (:9002)

Nothing talks to vLLM, llama.cpp, or any model port directly any more. That
matters because the gateway is where the API key, the model name, retries,
fallbacks and cost accounting live — bypassing it means bypassing all of them.

Model names are NEVER hardcoded at call sites: pass nothing and you get
`LLM_MODEL`. The old llama.cpp habit of sending `"model": "local-model"` (a name
the server ignored) is gone — vLLM 404s on an unknown model, so a stray literal
is a hard failure, not a silent one.

Reasoning tokens: Qwen3 emits `<think>…</think>` by default. We disable it at
source with `chat_template_kwargs={"enable_thinking": false}` (verified to pass
through LiteLLM's drop_params), and still strip defensively on the way out so a
model or gateway that ignores the flag can't leak reasoning into the UI.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator, Iterator, Optional

import httpx

from config.settings import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, LLM_TIMEOUT

log = logging.getLogger("aganeti.llm")

CHAT_URL = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"


# ── Typed failures ────────────────────────────────────────────────────────────
# Before these existed, every transport and HTTP error collapsed into `return ""`.
# That is survivable for chat (an empty answer degrades gracefully) and actively
# misleading for extraction: a gateway timeout arrived at the knowledge-graph
# extractor as an empty string and was reported as "unparseable JSON (0 chars)" —
# a parser error for something that never reached the parser. Debugging that cost
# real time, so the distinction is now carried in the type.

class LLMError(RuntimeError):
    """Base for every gateway failure. Catch this to treat all of them alike."""


class LLMTimeoutError(LLMError):
    """The request exceeded a deadline — ours (httpx) or the gateway's.

    LiteLLM enforces its own per-route timeout and answers HTTP 408 when it
    fires, so a timeout can arrive as either an httpx exception or a status code.
    Both map here, because the caller's decision (retry, or back off) is the same.
    """


class LLMGatewayError(LLMError):
    """The gateway answered, but not with a completion — 4xx/5xx, bad JSON body,
    or a malformed envelope. Retrying an identical request usually will not help."""


class LLMEmptyResponseError(LLMError):
    """A well-formed 200 whose content is empty. The model genuinely returned
    nothing — distinct from never having been reached."""


# HTTP statuses that mean "deadline exceeded" rather than "bad request".
_TIMEOUT_STATUSES = {408, 504}

# Qwen3 thinking control. LiteLLM forwards this to vLLM's chat template.
NO_THINK = {"enable_thinking": False}

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.S | re.I)
_THINK_OPEN = re.compile(r"<think>", re.I)
_THINK_CLOSE = re.compile(r"</think>", re.I)


def headers() -> dict:
    """Auth + content-type for the gateway. LiteLLM 401s without the key."""
    h = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        h["Authorization"] = f"Bearer {LLM_API_KEY}"
    return h


def strip_think(text: str) -> str:
    """Remove complete <think>…</think> blocks from a non-streamed answer.

    Also handles the truncated case: if a block was opened and max_tokens cut the
    response before the close tag, everything from the open tag on is reasoning."""
    if not text or "<think" not in text.lower():
        return text
    out = _THINK_BLOCK.sub("", text)
    m = _THINK_OPEN.search(out)
    if m and not _THINK_CLOSE.search(out[m.end():]):
        out = out[:m.start()]
    return out.strip()


class ThinkFilter:
    """Streaming counterpart of `strip_think`.

    Tokens arrive split arbitrarily ("<th", "ink>"), so a regex per chunk cannot
    work. This buffers just enough to recognise a tag, emits everything outside
    the block, and swallows everything inside it.
    """

    _MAX_HOLD = 16          # longest tag we must be able to reassemble: "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> str:
        """Return the portion of `chunk` that is safe to emit."""
        if not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []
        while self._buf:
            if self._in_think:
                m = _THINK_CLOSE.search(self._buf)
                if not m:
                    # Keep a tail that might be a partial "</think>".
                    self._buf = self._buf[-self._MAX_HOLD:]
                    return "".join(out)
                self._buf = self._buf[m.end():]
                self._in_think = False
                continue
            m = _THINK_OPEN.search(self._buf)
            if not m:
                # Emit all but a possible partial "<think>" at the tail.
                if len(self._buf) > self._MAX_HOLD:
                    out.append(self._buf[:-self._MAX_HOLD])
                    self._buf = self._buf[-self._MAX_HOLD:]
                if "<" not in self._buf:
                    out.append(self._buf)
                    self._buf = ""
                return "".join(out)
            out.append(self._buf[:m.start()])
            self._buf = self._buf[m.end():]
            self._in_think = True
        return "".join(out)

    def flush(self) -> str:
        """Emit whatever is still held once the stream ends."""
        if self._in_think:
            self._buf = ""
            return ""
        tail, self._buf = self._buf, ""
        return tail


def build_payload(messages: list, *, model: Optional[str] = None,
                  stream: bool = False, think: bool = False,
                  **kwargs: Any) -> dict:
    """Assemble a chat-completions body with the model, and thinking off by default."""
    payload: dict = {
        "model": model or LLM_MODEL,
        "messages": messages,
        **kwargs,
    }
    if stream:
        payload["stream"] = True
    if not think:
        payload["chat_template_kwargs"] = NO_THINK
    return payload


# ── Sync ──────────────────────────────────────────────────────────────────────

def _classify(e: Exception) -> LLMError:
    """Map a transport/HTTP failure onto the typed hierarchy."""
    if isinstance(e, (httpx.TimeoutException, httpx.ReadTimeout, httpx.ConnectTimeout)):
        return LLMTimeoutError(f"request exceeded the client deadline: {e}")
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status in _TIMEOUT_STATUSES:
            # LiteLLM's own per-route `timeout:` fired. Reported as a status, but
            # it is a deadline, and the caller should treat it as one.
            return LLMTimeoutError(f"gateway deadline exceeded (HTTP {status})")
        return LLMGatewayError(f"gateway returned HTTP {status}: {e.response.text[:200]}")
    if isinstance(e, httpx.HTTPError):
        return LLMGatewayError(f"transport failure: {e}")
    return LLMGatewayError(f"{type(e).__name__}: {e}")


def complete(messages: list, *, model: Optional[str] = None, timeout: float | None = None,
             think: bool = False, raise_on_error: bool = False, **kwargs: Any) -> str:
    """Blocking single-shot completion → the assistant text (reasoning stripped).

    `raise_on_error` selects the failure contract:

      * **False (default)** — return "" on any failure, exactly as before. Every
        existing caller degrades on an empty answer, and flipping that default
        would turn a soft "feature unavailable" into a 500 on the chat path.
      * **True** — raise LLMTimeoutError / LLMGatewayError / LLMEmptyResponseError.
        Use this wherever an empty string would be indistinguishable from real
        output, which is precisely the knowledge-graph extraction case.

    The failure is logged with its classification either way, so even the
    degrading path no longer reports a timeout as an unexplained blank.
    """
    try:
        r = httpx.post(CHAT_URL, headers=headers(),
                       json=build_payload(messages, model=model, think=think, **kwargs),
                       timeout=timeout or LLM_TIMEOUT)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"].get("content") or ""
    except Exception as e:  # noqa: BLE001
        err = _classify(e)
        log.warning("llm complete failed [%s]: %s", type(err).__name__, err)
        if raise_on_error:
            raise err from e
        return ""
    if raise_on_error and not content.strip():
        raise LLMEmptyResponseError("gateway returned a 200 with empty content")
    return content if think else strip_think(content)


def complete_raw(messages: list, *, model: Optional[str] = None,
                 timeout: float | None = None, think: bool = False,
                 **kwargs: Any) -> dict:
    """Blocking completion returning the full JSON body — for callers that need
    tool_calls or usage. Raises on HTTP error so they can handle it themselves."""
    r = httpx.post(CHAT_URL, headers=headers(),
                   json=build_payload(messages, model=model, think=think, **kwargs),
                   timeout=timeout or LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ── Async ─────────────────────────────────────────────────────────────────────

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    """Process-wide async client. Keep-alive matters: a cold TCP+TLS handshake per
    token-stream is a visible chunk of first-token latency."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=LLM_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=300.0),
        )
    return _client


async def aclose_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def acomplete(messages: list, *, model: Optional[str] = None,
                    think: bool = False, **kwargs: Any) -> str:
    """Async single-shot completion → assistant text (reasoning stripped)."""
    try:
        r = await get_client().post(
            CHAT_URL, headers=headers(),
            json=build_payload(messages, model=model, think=think, **kwargs))
        r.raise_for_status()
        content = r.json()["choices"][0]["message"].get("content") or ""
    except Exception as e:  # noqa: BLE001
        log.warning("llm acomplete failed: %s", e)
        return ""
    return content if think else strip_think(content)


async def acomplete_raw(messages: list, *, model: Optional[str] = None,
                        think: bool = False, **kwargs: Any) -> dict:
    """Async completion returning the full JSON body (tool_calls, usage, …)."""
    r = await get_client().post(
        CHAT_URL, headers=headers(),
        json=build_payload(messages, model=model, think=think, **kwargs))
    r.raise_for_status()
    return r.json()


async def astream(messages: list, *, model: Optional[str] = None,
                  think: bool = False, **kwargs: Any) -> AsyncIterator[str]:
    """Yield assistant content tokens, with <think> blocks filtered out."""
    payload = build_payload(messages, model=model, stream=True, think=think, **kwargs)
    filt = None if think else ThinkFilter()
    async with get_client().stream("POST", CHAT_URL, headers=headers(), json=payload) as resp:
        if resp.status_code != 200:
            body = (await resp.aread())[:200]
            log.warning("llm stream failed: %s %s", resp.status_code, body)
            return
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                tok = json.loads(data)["choices"][0].get("delta", {}).get("content", "")
            except Exception:
                continue
            if not tok:
                continue
            out = filt.feed(tok) if filt else tok
            if out:
                yield out
    if filt:
        tail = filt.flush()
        if tail:
            yield tail
