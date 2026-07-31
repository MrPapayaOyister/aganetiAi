"""
Call async provider services from synchronous code.

Several long-lived callers are synchronous by design and already run off the
event loop via `asyncio.to_thread` (the 30s inbox poll, the meeting-brief
sweep, the PDF/digest builders, Telegram handlers). The provider services are
async. `run_sync` bridges the two:

  * from a worker thread while the app loop is alive → hand the coroutine to
    that loop (`run_coroutine_threadsafe`) so the shared httpx client and its
    connection pool are reused;
  * from a loop-less process (CLI scripts, one-off verification) → spin a
    private loop with `asyncio.run` and close its client afterwards.

Calling it from *inside* the event loop thread is a bug (it would deadlock), so
that raises loudly instead.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Optional

log = logging.getLogger("aria.async_bridge")

_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Record the application's event loop. Called once from the FastAPI lifespan."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop or asyncio.get_running_loop()


def run_sync(coro: Coroutine, timeout: float = 60.0) -> Any:
    """Run `coro` to completion from synchronous code and return its result."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass                     # good — we are not on an event loop
    else:
        coro.close()
        raise RuntimeError(
            "run_sync() called from the event loop thread — await the coroutine instead."
        )

    loop = _MAIN_LOOP
    if loop is not None and loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    # No app loop (CLI / tests): private loop, and close its httpx client so the
    # per-loop pool in http_client does not leak a client bound to a dead loop.
    async def _wrapped():
        from backend.services import http_client
        try:
            return await coro
        finally:
            await http_client.aclose_client()

    return asyncio.run(_wrapped())
