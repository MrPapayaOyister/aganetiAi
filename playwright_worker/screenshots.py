"""Screenshot capture, masking, and reference storage.

Two rules from §7.2 and the task's step 5, both enforced here:

**Masked before encoding.** Password-type and sensitive-classified fields are covered
by Playwright's own `mask=` parameter, which paints over them *during* capture. This
matters more than it sounds: masking after encoding means the unmasked pixels existed
in a buffer, and a crash dump or a log of that buffer leaks them. Playwright never
produces the unmasked bytes at all.

**Stored by reference, never returned inline.** The observation carries a
`screenshot_ref`; the bytes stay in this store behind a TTL. §7.2 forbids
unredacted screenshots in state, and returning even a redacted one inline would put
a multi-megabyte base64 blob on the path that eventually reaches an LLM context.

The store is in-memory and per-worker-process. That is correct for a PoC and wrong
for a multi-replica deployment — recorded as a §6 contradiction rather than papered
over, because a caller that gets a ref from replica A and fetches it from replica B
would get a 404 that looks like expiry.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .config import Limits
from .errors import ErrorCode, WorkerError

log = logging.getLogger("playwright_worker.screenshots")


@dataclass
class StoredShot:
    ref: str
    png: bytes
    browser_session_id: str
    tenant_id: str
    user_id: str
    url: str
    masked_count: int
    created_at: float

    def meta(self) -> dict:
        """Everything about the shot except the bytes."""
        return {"screenshot_ref": self.ref, "url": self.url,
                "masked_fields": self.masked_count, "bytes": len(self.png),
                "created_at": self.created_at}


class ScreenshotStore:
    """TTL'd, capped, ownership-checked."""

    def __init__(self, limits: Limits | None = None):
        self.limits = limits or Limits.from_env()
        self._shots: dict[str, StoredShot] = {}

    def put(self, *, png: bytes, browser_session_id: str, tenant_id: str,
            user_id: str, url: str, masked_count: int) -> StoredShot:
        self._evict()
        ref = "shot_" + secrets.token_urlsafe(16)
        shot = StoredShot(ref=ref, png=png, browser_session_id=browser_session_id,
                          tenant_id=tenant_id, user_id=user_id, url=url,
                          masked_count=masked_count, created_at=time.monotonic())
        self._shots[ref] = shot
        return shot

    def get(self, ref: str, *, tenant_id: str, user_id: str) -> StoredShot:
        """Fetch by reference. Ownership is asserted here too — a screenshot is as
        sensitive as the session that produced it, and an expired-vs-someone-else's
        distinction would leak the same thing §5.1 refuses to leak."""
        self._evict()
        shot = self._shots.get(ref)
        if shot is None or shot.tenant_id != tenant_id or shot.user_id != user_id:
            raise WorkerError(ErrorCode.SESSION_NOT_FOUND,
                              "no such screenshot reference")
        return shot

    def _evict(self) -> None:
        now = time.monotonic()
        for ref, shot in list(self._shots.items()):
            if now - shot.created_at >= self.limits.screenshot_ttl_s:
                self._shots.pop(ref, None)
        # Oldest-first overflow eviction, so a long-running worker cannot grow
        # without bound between TTL sweeps.
        while len(self._shots) > self.limits.max_screenshots:
            oldest = min(self._shots.values(), key=lambda s: s.created_at)
            self._shots.pop(oldest.ref, None)

    def drop_session(self, browser_session_id: str) -> int:
        """Close of a session drops its shots. A screenshot outliving the context it
        came from is a leak with a longer half-life than the context itself."""
        doomed = [r for r, s in self._shots.items()
                  if s.browser_session_id == browser_session_id]
        for r in doomed:
            self._shots.pop(r, None)
        return len(doomed)

    @property
    def count(self) -> int:
        return len(self._shots)


async def capture(page: Any, *, refs: dict[str, dict], full_page: bool,
                  timeout_ms: int) -> tuple[bytes, int]:
    """Capture with sensitive fields masked. Returns (png_bytes, masked_count).

    The mask list is built from the inspector's `sensitive` classification, which
    already covers `type=password` plus name/id/autocomplete heuristics. Anything the
    inspector did not see cannot be masked — so a page that was never inspected is
    captured with a blanket mask over every password input as a floor, rather than
    unmasked.
    """
    masks = []
    for ref, entry in refs.items():
        if entry.get("sensitive"):
            marker = f"{entry['nonce']}:{ref}"
            masks.append(page.locator(f"[data-pw-ref='{marker}']"))

    # Floor: mask every password input regardless of what the ref map knows. If the
    # page was never inspected, `refs` is empty and this is the only protection.
    masks.append(page.locator("input[type=password]"))

    png = await page.screenshot(
        full_page=bool(full_page), timeout=timeout_ms, mask=masks,
        mask_color="#000000", type="png",
    )
    return png, len(masks)
