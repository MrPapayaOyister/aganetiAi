"""
Shared async HTTP client + a retrying request helper for provider APIs
(Google and Microsoft Graph).

Clients are pooled PER EVENT LOOP. The server normally runs one loop, so this
behaves like a single shared client; but legacy sync callers bridge into their
own loop via `async_bridge.run_sync`, and an httpx.AsyncClient bound to a dead
loop raises on reuse. Keying by loop keeps those paths safe.

`api_request` retries transient failures (429 / 5xx) up to `max_retries` times
with small backoff. Never logs tokens.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("aria.provider.http")

_clients: dict[int, httpx.AsyncClient] = {}


def _loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:      # no running loop (sync context) — process-wide slot
        return 0


def get_client() -> httpx.AsyncClient:
    """Return the AsyncClient bound to the *running* loop, creating it on first use."""
    key = _loop_key()
    c = _clients.get(key)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        _clients[key] = c
    return c


async def aclose_client() -> None:
    """Close the client bound to the running loop (call on app shutdown if desired)."""
    c = _clients.pop(_loop_key(), None)
    if c is not None and not c.is_closed:
        await c.aclose()


async def api_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    json: Optional[Any] = None,
    data: Optional[Any] = None,
    max_retries: int = 2,
) -> httpx.Response:
    """
    Perform a provider API request with retry on 429/500/502/503.

    Returns the httpx.Response (caller checks status / parses). Never logs tokens.
    Raises httpx.HTTPError only after exhausting retries on a transport error.
    """
    client = get_client()
    attempt = 0
    while True:
        try:
            resp = await client.request(method, url, headers=headers, params=params,
                                        json=json, data=data)
            if resp.status_code in (429, 500, 502, 503) and attempt < max_retries:
                wait = 0.6 * (attempt + 1)
                log.warning("provider API %s %s → %s, retry %d/%d in %.1fs",
                            method, url.split("?")[0], resp.status_code, attempt + 1, max_retries, wait)
                await asyncio.sleep(wait)
                attempt += 1
                continue
            return resp
        except (httpx.TransportError, httpx.TimeoutException) as e:
            if attempt < max_retries:
                await asyncio.sleep(0.6 * (attempt + 1))
                attempt += 1
                continue
            log.warning("provider API transport error after retries: %s", e)
            raise


# Back-compat alias — the Google services were written against this name.
google_request = api_request
