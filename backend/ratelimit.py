"""Basic in-memory rate limiting (Phase 0, Item 6) — pure ASGI.

Implemented as pure ASGI (not BaseHTTPMiddleware) so it never buffers the
response body: BaseHTTPMiddleware stacked around a StreamingResponse/SSE breaks
with "No response returned", which killed the /agent/chat token stream.

Single-instance sliding window keyed by the real client IP (X-Forwarded-For behind
the VM/Tailscale proxy, else the socket peer). Loopback self-calls exempt. Tighter
bucket for expensive LLM/voice/ingest routes. Tunable via env:
RATE_LIMIT_WINDOW / RATE_LIMIT_MAX / RATE_LIMIT_CHAT_MAX.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque

_WINDOW = float(os.getenv("RATE_LIMIT_WINDOW", "60"))
_DEFAULT_MAX = int(os.getenv("RATE_LIMIT_MAX", "120"))
_CHAT_MAX = int(os.getenv("RATE_LIMIT_CHAT_MAX", "20"))

_CHAT_PREFIXES = ("/chat", "/agent/chat", "/stt", "/tts", "/transcribe",
                  "/meeting/transcribe", "/report/generate", "/ingest")
_EXEMPT_PREFIXES = ("/health", "/favicon.ico", "/docs", "/redoc", "/openapi.json")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

_buckets: dict[str, deque] = defaultdict(deque)


def _limit_for(path: str) -> int:
    return _CHAT_MAX if any(path.startswith(p) for p in _CHAT_PREFIXES) else _DEFAULT_MAX


class RateLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if method == "OPTIONS" or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await self.app(scope, receive, send)

        client = scope.get("client")
        peer = client[0] if client else ""
        if peer in _LOOPBACK:  # internal self-calls — never throttle
            return await self.app(scope, receive, send)

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        xff = headers.get("x-forwarded-for", "")
        ip = xff.split(",")[0].strip() if xff else (peer or "unknown")

        limit = _limit_for(path)
        klass = "chat" if limit == _CHAT_MAX else "gen"
        bkey = f"{ip}:{klass}"

        now = time.monotonic()
        dq = _buckets[bkey]
        while dq and now - dq[0] > _WINDOW:
            dq.popleft()

        if len(dq) >= limit:
            retry = int(_WINDOW - (now - dq[0])) + 1
            body = json.dumps({"detail": "Too many requests. Please slow down."}).encode()
            await send({"type": "http.response.start", "status": 429, "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(retry).encode()),
                (b"x-ratelimit-limit", str(limit).encode()),
                (b"x-ratelimit-window", str(int(_WINDOW)).encode()),
            ]})
            await send({"type": "http.response.body", "body": body})
            return

        dq.append(now)
        return await self.app(scope, receive, send)
