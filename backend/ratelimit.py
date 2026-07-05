"""Basic in-memory rate limiting (Phase 0, Item 6).

Single-instance backend, so a process-local sliding window is sufficient and
needs no extra dependency (slowapi/redis are not installed). Keyed by the caller:
the real client IP (via X-Forwarded-For behind the VM/Tailscale proxy, else the
socket peer). Loopback self-calls (the bot / action_parser / scheduler hitting
127.0.0.1) are exempt so internal orchestration is never throttled. Expensive
LLM/voice/ingest routes get a tighter bucket than ordinary CRUD.

Tunable via env: RATE_LIMIT_WINDOW (s), RATE_LIMIT_MAX, RATE_LIMIT_CHAT_MAX.
"""
import os
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_WINDOW = float(os.getenv("RATE_LIMIT_WINDOW", "60"))
_DEFAULT_MAX = int(os.getenv("RATE_LIMIT_MAX", "120"))       # general routes / window / key
_CHAT_MAX = int(os.getenv("RATE_LIMIT_CHAT_MAX", "20"))      # expensive routes / window / key

_CHAT_PREFIXES = ("/chat", "/stt", "/tts", "/transcribe", "/meeting/transcribe",
                  "/report/generate", "/ingest")
_EXEMPT_PREFIXES = ("/health", "/favicon.ico", "/docs", "/redoc", "/openapi.json")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

_buckets: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _limit_for(path: str) -> int:
    return _CHAT_MAX if any(path.startswith(p) for p in _CHAT_PREFIXES) else _DEFAULT_MAX


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        peer = request.client.host if request.client else ""
        if peer in _LOOPBACK:  # internal self-calls — never throttle
            return await call_next(request)

        limit = _limit_for(path)
        klass = "chat" if limit == _CHAT_MAX else "gen"
        bkey = f"{_client_ip(request)}:{klass}"

        now = time.monotonic()
        dq = _buckets[bkey]
        while dq and now - dq[0] > _WINDOW:
            dq.popleft()

        if len(dq) >= limit:
            retry = int(_WINDOW - (now - dq[0])) + 1
            return JSONResponse(
                {"detail": "Too many requests. Please slow down."},
                status_code=429,
                headers={"Retry-After": str(retry),
                         "X-RateLimit-Limit": str(limit),
                         "X-RateLimit-Window": str(int(_WINDOW))},
            )

        dq.append(now)
        return await call_next(request)
