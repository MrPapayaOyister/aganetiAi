"""
Shared async HTTP client + a retrying request helper for Google APIs.

A single httpx.AsyncClient is reused across the process (connection pooling)
rather than created per request. `google_request` retries transient failures
(429 / 5xx) up to `max_retries` times with small backoff.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("aria.google.http")

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    """Return the process-wide shared AsyncClient, creating it on first use."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    return _client


async def aclose_client() -> None:
    """Close the shared client (call on app shutdown if desired)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def google_request(
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
    Perform a Google API request with retry on 429/500/502/503.

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
                log.warning("Google API %s %s → %s, retry %d/%d in %.1fs",
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
            log.warning("Google API transport error after retries: %s", e)
            raise
