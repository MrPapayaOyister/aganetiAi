"""Browser session manager (§5).

One Playwright `BrowserContext` per `browser_session_id`, one shared `Browser`
process behind them (§5.3's PoC decision). This module owns the whole lifecycle and
is the only place a context is created or closed.

Four properties carry the weight:

**Opaque ids.** `token_urlsafe(32)` — 256 bits. §5.1 requires unguessable, and a
sequential id would let a caller enumerate other tenants' sessions even without
being able to use them.

**Ownership, asserted from day one (§5.2).** A mismatch returns `SESSION_NOT_FOUND`,
not a distinguishable "denied", because a distinguishable denial confirms the
session exists and therefore leaks the existence of other tenants' work. This is
the one place the worker's error naming is deliberately *less* precise than it
could be.

**Serialization (§6.3).** Every action on a session takes that session's lock. Two
concurrent actions on one page is a correctness hazard — a click racing a
navigation reads a half-torn DOM — so this is not a throughput knob.

**Reaping that actually reaps (§5.4).** A leaked context is a leaked authenticated
browser. The reaper is a supervised background task that (a) survives an exception
in any single close, (b) removes the session from the registry *before* awaiting the
close so a slow close cannot be handed out again, and (c) is idempotent, so an
explicit `browser_close` racing the reaper cannot double-close.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Budgets, Limits
from .errors import ErrorCode, WorkerError

log = logging.getLogger("playwright_worker.session")


@dataclass
class TaskBudget:
    """Live budget counters for one browser session (§9.2).

    Counters live on the session rather than on the caller, which is what makes
    §9.2's "enforced server-side, never by prompt instruction" true: a caller that
    forgets to count, or lies about its count, changes nothing.
    """

    budgets: Budgets
    started_at: float = field(default_factory=time.monotonic)
    actions: int = 0
    navigations: int = 0
    element_retries: dict[str, int] = field(default_factory=dict)

    def remaining(self) -> dict[str, Any]:
        return {
            "actions": max(0, self.budgets.max_actions_per_task - self.actions),
            "navigations": max(0, self.budgets.max_navigations_per_task - self.navigations),
            "wall_clock_ms": max(0, self.budgets.task_wall_clock_ms - self.elapsed_ms()),
        }

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def check_wall_clock(self) -> None:
        if self.elapsed_ms() >= self.budgets.task_wall_clock_ms:
            raise WorkerError(
                ErrorCode.BUDGET_EXCEEDED,
                f"task wall clock exhausted ({self.budgets.task_wall_clock_ms}ms)",
                detail={"budget": "task_wall_clock_ms", "elapsed_ms": self.elapsed_ms(),
                        "actions_taken": self.actions})

    def charge_action(self) -> None:
        """Charged BEFORE the action runs.

        Charging after would let the last action run and only then report
        exhaustion, which means a budget of N permits N+1 actions — and for the one
        budget where that matters most (`browser_submit`) the extra action is the
        irreversible one.
        """
        self.check_wall_clock()
        if self.actions >= self.budgets.max_actions_per_task:
            raise WorkerError(
                ErrorCode.BUDGET_EXCEEDED,
                f"action budget exhausted ({self.budgets.max_actions_per_task} actions)",
                detail={"budget": "max_actions_per_task", "actions_taken": self.actions})
        self.actions += 1

    def charge_navigation(self) -> None:
        if self.navigations >= self.budgets.max_navigations_per_task:
            raise WorkerError(
                ErrorCode.BUDGET_EXCEEDED,
                f"navigation budget exhausted ({self.budgets.max_navigations_per_task})",
                detail={"budget": "max_navigations_per_task",
                        "navigations": self.navigations})
        self.navigations += 1

    def charge_element_retry(self, ref: str) -> None:
        n = self.element_retries.get(ref, 0)
        if n >= self.budgets.max_retries_per_element:
            raise WorkerError(
                ErrorCode.BUDGET_EXCEEDED,
                f"retry budget exhausted for {ref} "
                f"({self.budgets.max_retries_per_element} retries)",
                detail={"budget": "max_retries_per_element", "element_ref": ref})
        self.element_retries[ref] = n + 1


@dataclass
class BrowserSession:
    """§5.1's owned resource, plus the live handles the worker needs."""

    id: str
    tenant_id: str
    user_id: str
    agent_id: str
    session_id: str
    created_at: float
    last_used_at: float
    allowed_domains: tuple[str, ...]
    budget: TaskBudget
    context: Any = None                  # playwright BrowserContext
    page: Any = None                     # playwright Page
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    #: ref -> locator descriptor, session-scoped (§3.3). Populated by the inspector.
    refs: dict[str, dict] = field(default_factory=dict)
    #: Bumped on navigation / DOM mutation. A ref issued under an older generation
    #: is STALE_REF (§3.3) — see `inspector.resolve`.
    ref_generation: int = 0
    last_inspect_url: str = ""

    def owned_by(self, tenant_id: str, user_id: str) -> bool:
        return self.tenant_id == tenant_id and self.user_id == user_id

    def touch(self) -> None:
        self.last_used_at = time.monotonic()

    def describe(self) -> dict:
        return {"browser_session_id": self.id, "tenant_id": self.tenant_id,
                "user_id": self.user_id, "agent_id": self.agent_id,
                "session_id": self.session_id,
                "allowed_domains": list(self.allowed_domains),
                "ref_generation": self.ref_generation,
                "budgets_remaining": self.budget.remaining()}


class SessionManager:
    """Owns every context. Nothing else may create or close one."""

    def __init__(self, *, limits: Optional[Limits] = None,
                 budgets: Optional[Budgets] = None) -> None:
        self.limits = limits or Limits.from_env()
        self.budgets = budgets or Budgets.from_env()
        self._sessions: dict[str, BrowserSession] = {}
        self._playwright = None
        self._browser = None
        self._reaper: asyncio.Task | None = None
        # Guards the registry itself (create/lookup/remove), NOT page actions —
        # those take the per-session lock. Holding a global lock across a page
        # action would serialize every session against every other.
        self._registry_lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Launch the shared browser and the reaper. Idempotent."""
        if self._browser is not None:
            return
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())
        log.info("worker started: chromium up, reaper running")

    async def stop(self) -> None:
        """Close everything. Safe to call twice, and safe to call after a partial
        start — a worker that cannot shut down cleanly leaks the exact thing §5.4
        says must not leak."""
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for sid in list(self._sessions):
            await self.close(sid, reason="shutdown")
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    # ── create ────────────────────────────────────────────────────────────────
    async def open(self, *, tenant_id: str, user_id: str, agent_id: str,
                   session_id: str, allowed_domains: tuple[str, ...],
                   viewport: tuple[int, int] = (1280, 800)) -> BrowserSession:
        if not tenant_id or not user_id:
            # The worker does not authorize (§6.2) but it cannot own a resource on
            # behalf of nobody: an unowned session is one no ownership assertion can
            # ever fail against.
            raise WorkerError(ErrorCode.AUTHZ_DENIED,
                              "browser_open requires tenant_id and user_id")
        await self.start()
        async with self._registry_lock:
            self._enforce_caps(tenant_id, user_id)
            now = time.monotonic()
            sid = "bs_" + secrets.token_urlsafe(32)
            context = await self._browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]},
                accept_downloads=False,       # §5.3 downloads disabled
                permissions=[],               # §5.3 geolocation/camera/mic/notifications denied
                storage_state=None,           # §5.3 no storageState reuse
                ignore_https_errors=False,
            )
            # Belt and braces: `permissions=[]` grants none, but an explicit clear
            # also revokes anything a future default might grant.
            with contextlib.suppress(Exception):
                await context.clear_permissions()
            context.set_default_timeout(self.budgets.action_timeout_ms)
            page = await context.new_page()
            sess = BrowserSession(
                id=sid, tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                session_id=session_id, created_at=now, last_used_at=now,
                allowed_domains=allowed_domains,
                budget=TaskBudget(budgets=self.budgets),
                context=context, page=page,
            )
            self._sessions[sid] = sess
            log.info("session opened %s tenant=%s user=%s", sid[:12], tenant_id, user_id)
            return sess

    def _enforce_caps(self, tenant_id: str, user_id: str) -> None:
        """§5.4 caps. A real DoS vector — Chromium contexts are expensive."""
        live = [s for s in self._sessions.values() if not s.closed]
        if len(live) >= self.limits.max_total_sessions:
            raise WorkerError(ErrorCode.BUDGET_EXCEEDED,
                              f"worker at capacity ({self.limits.max_total_sessions} sessions)",
                              detail={"cap": "max_total_sessions"})
        per_tenant = sum(1 for s in live if s.tenant_id == tenant_id)
        if per_tenant >= self.limits.max_sessions_per_tenant:
            raise WorkerError(ErrorCode.BUDGET_EXCEEDED,
                              f"tenant session cap reached "
                              f"({self.limits.max_sessions_per_tenant})",
                              detail={"cap": "max_sessions_per_tenant"})
        per_user = sum(1 for s in live if s.tenant_id == tenant_id and s.user_id == user_id)
        if per_user >= self.limits.max_sessions_per_user:
            raise WorkerError(ErrorCode.BUDGET_EXCEEDED,
                              f"user session cap reached "
                              f"({self.limits.max_sessions_per_user})",
                              detail={"cap": "max_sessions_per_user"})

    # ── resolve ───────────────────────────────────────────────────────────────
    def resolve(self, browser_session_id: str, *, tenant_id: str,
                user_id: str) -> BrowserSession:
        """Look up a session AND assert ownership (§5.1).

        A session that exists but belongs to someone else returns the same error as
        one that never existed. That is deliberate and is the reason this is not
        two separate methods: a caller cannot accidentally use a lookup that skips
        the assertion.
        """
        sess = self._sessions.get(browser_session_id)
        if sess is None or sess.closed:
            raise WorkerError(ErrorCode.SESSION_NOT_FOUND,
                              "no such browser session")
        if not sess.owned_by(tenant_id, user_id):
            log.warning("ownership mismatch on %s (owner=%s/%s presented=%s/%s)",
                        browser_session_id[:12], sess.tenant_id, sess.user_id,
                        tenant_id, user_id)
            raise WorkerError(ErrorCode.SESSION_NOT_FOUND,
                              "no such browser session")
        return sess

    # ── close ─────────────────────────────────────────────────────────────────
    async def close(self, browser_session_id: str, *, reason: str = "explicit") -> bool:
        """Close and deregister. Idempotent; returns True if this call did the work.

        The session is removed from the registry BEFORE the context close is
        awaited. If it were removed after, a slow close would leave it resolvable —
        and an action could be admitted against a context that is already tearing
        down.
        """
        async with self._registry_lock:
            sess = self._sessions.pop(browser_session_id, None)
            if sess is None or sess.closed:
                return False
            sess.closed = True
        sess.refs.clear()
        for handle, what in ((sess.page, "page"), (sess.context, "context")):
            if handle is None:
                continue
            try:
                await handle.close()
            except Exception:  # noqa: BLE001 — a failed close must not abort the rest
                log.exception("closing %s for %s failed", what, browser_session_id[:12])
        sess.page = sess.context = None
        log.info("session closed %s (%s)", browser_session_id[:12], reason)
        return True

    # ── reaping (§5.4) ────────────────────────────────────────────────────────
    def expired(self, now: float | None = None) -> list[tuple[str, str]]:
        """(session_id, reason) for everything past a TTL. Pure — the tests drive
        this directly rather than sleeping for fifteen minutes."""
        now = time.monotonic() if now is None else now
        out: list[tuple[str, str]] = []
        for sid, s in self._sessions.items():
            if s.closed:
                continue
            if now - s.last_used_at >= self.limits.idle_ttl_s:
                out.append((sid, "idle_ttl"))
            elif now - s.created_at >= self.limits.absolute_ttl_s:
                out.append((sid, "absolute_ttl"))
        return out

    async def reap(self) -> int:
        """One reaping pass. Returns how many were closed."""
        n = 0
        for sid, reason in self.expired():
            try:
                if await self.close(sid, reason=reason):
                    n += 1
            except Exception:  # noqa: BLE001 — one bad session must not stop the sweep
                log.exception("reaping %s failed", sid[:12])
        return n

    async def _reap_loop(self) -> None:
        """Supervised forever-loop. Any exception is logged and the loop CONTINUES —
        a reaper that dies silently is indistinguishable from one that is working,
        right up until the worker runs out of memory."""
        while True:
            try:
                await asyncio.sleep(self.limits.reap_interval_s)
                closed = await self.reap()
                if closed:
                    log.info("reaper closed %d expired session(s)", closed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("reaper pass failed; continuing")

    # ── introspection (tests + health) ────────────────────────────────────────
    @property
    def live_count(self) -> int:
        return sum(1 for s in self._sessions.values() if not s.closed)

    def stats(self) -> dict:
        live = [s for s in self._sessions.values() if not s.closed]
        return {"live_sessions": len(live),
                "reaper_running": bool(self._reaper and not self._reaper.done()),
                "browser_up": self._browser is not None,
                "limits": vars(self.limits), "budgets": vars(self.budgets)}
