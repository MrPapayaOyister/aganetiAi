"""
Web search via the self-hosted SearXNG instance.

Why SearXNG rather than a scraper library: only SearXNG egresses to the public
internet, so a user's query never reaches a third-party search API directly —
the on-prem posture the orchestrator's `_web_search` skill already assumes. It
also aggregates many engines behind one call, so a single engine being blocked
degrades the result set instead of failing the search. The JSON response reports
that explicitly in `unresponsive_engines`; the caller can surface it.

Deliberately NOT a scraper fallback. A fallback that silently swaps the search
backend hides an outage: results would keep arriving, quietly ranked by
something else, and nobody would learn the instance was down. SearXNG is on this
box — if it stops, that is worth saying out loud. See SearchUnavailable.

Sync on purpose. The tool dispatcher already runs in a worker thread, so an
async client plus the loop bridge would add machinery and no concurrency.
"""

from __future__ import annotations

import logging

from config.settings import SEARXNG_TIMEOUT, SEARXNG_URL

log = logging.getLogger("aria.websearch")


class SearchUnavailable(RuntimeError):
    """The SearXNG instance could not be reached or refused the request."""


def search(query: str, max_results: int = 5) -> list[dict]:
    """Return up to `max_results` hits as ``[{title, url, content, engine}, ...]``.

    Raises SearchUnavailable when the instance is unreachable, returns a non-200,
    or answers with something that isn't the expected JSON — never a bare
    transport exception, so callers can produce a clean message.
    """
    import httpx

    q = (query or "").strip()
    if not q:
        return []
    n = max(1, min(int(max_results or 5), 10))

    try:
        r = httpx.get(
            f"{SEARXNG_URL}/search",
            params={"q": q, "format": "json"},
            timeout=SEARXNG_TIMEOUT,
        )
    except Exception as exc:
        raise SearchUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if r.status_code != 200:
        # A 403 here almost always means `json` is missing from `search.formats`
        # in settings.yml — the instance is up but refusing this format.
        raise SearchUnavailable(f"HTTP {r.status_code} from {SEARXNG_URL}")

    try:
        payload = r.json()
    except Exception as exc:
        raise SearchUnavailable(f"non-JSON response from {SEARXNG_URL}") from exc

    hits = payload.get("results")
    if not isinstance(hits, list):
        raise SearchUnavailable(f"unexpected JSON shape from {SEARXNG_URL}")

    dead = payload.get("unresponsive_engines") or []
    if dead:
        # Not an error: SearXNG serves whatever the remaining engines returned.
        log.info("searxng: %d engine(s) unresponsive for %r: %s", len(dead), q, dead)

    out: list[dict] = []
    for h in hits:
        if not isinstance(h, dict) or not h.get("url"):
            continue
        out.append({
            "title": (h.get("title") or "").strip() or h["url"],
            "url": h["url"],
            "content": (h.get("content") or "").strip(),
            "engine": h.get("engine") or "",
        })
        if len(out) >= n:
            break
    return out
