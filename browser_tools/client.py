"""The ONE way this package reaches the worker (requirement 6).

Every tool goes through `WorkerGateway.call()`. Nothing else in `browser_tools`
constructs a `BrowserWorker`, opens an HTTP session, or touches the worker in any
other way — asserted by a test that walks the package's source.

Why it matters beyond tidiness: **Phase E inserts authorization here.** If a tool
could reach the worker by a second route, E's anti-bypass test would be unprovable —
you cannot demonstrate that every call passes a gate when there is more than one
door. The gateway therefore exists before there is anything to gate, and the
`_authorize` hook below is a deliberate no-op placeholder rather than something
Phase E has to retrofit.

Two transports, one interface (§6.1's `[DECIDE]`: "keep the worker interface
identical either way, so the swap is one adapter"):

  * `InProcessTransport` — a `BrowserWorker` in this process. Level 2 tests use it.
  * `HttpTransport` — the containerised worker. What Phase D deploys against.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional, Protocol

log = logging.getLogger("browser_tools.client")

WORKER_URL = os.getenv("BROWSER_WORKER_URL", "http://playwright-worker:8090")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")


class Transport(Protocol):
    """How bytes reach the worker. Deliberately two methods, not one: fetching a
    screenshot is not an `execute` and pretending otherwise would put image bytes
    on the same path as observations."""

    async def execute(self, payload: dict) -> dict: ...

    async def fetch_screenshot(self, ref: str, *, tenant_id: str, user_id: str) -> bytes: ...

    async def session_facts(self, browser_session_id: str, *, tenant_id: str,
                            user_id: str) -> dict | None: ...


class InProcessTransport:
    """Wraps a `BrowserWorker` object directly.

    §6.1 warns that in-process handles must not leak into the tool layer. They do
    not: this class is the only thing holding the worker, and it exposes exactly the
    same two methods the HTTP transport does. A tool cannot tell which it has.
    """

    def __init__(self, worker: Any):
        self._w = worker

    async def execute(self, payload: dict) -> dict:
        obs = await self._w.execute(payload)
        return obs.as_dict()

    async def fetch_screenshot(self, ref: str, *, tenant_id: str, user_id: str) -> bytes:
        shot = self._w.screenshots.get(ref, tenant_id=tenant_id, user_id=user_id)
        return shot.png

    async def session_facts(self, browser_session_id: str, *, tenant_id: str,
                            user_id: str) -> dict | None:
        return self._w.session_facts(browser_session_id, tenant_id=tenant_id,
                                     user_id=user_id)


class HttpTransport:
    """The containerised worker over its narrow RPC surface."""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None,
                 timeout_s: float = 90.0):
        self.base_url = (base_url or WORKER_URL).rstrip("/")
        self.token = token if token is not None else WORKER_TOKEN
        self.timeout_s = timeout_s

    def _headers(self, **extra: str) -> dict[str, str]:
        h = {"Content-Type": "application/json", **extra}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def execute(self, payload: dict) -> dict:
        import httpx
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.post(f"{self.base_url}/execute", json=payload,
                             headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def fetch_screenshot(self, ref: str, *, tenant_id: str, user_id: str) -> bytes:
        import httpx
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.get(f"{self.base_url}/screenshot/{ref}",
                            headers=self._headers(**{"X-Tenant-Id": tenant_id,
                                                     "X-User-Id": user_id}))
            r.raise_for_status()
            return r.content

    async def session_facts(self, browser_session_id: str, *, tenant_id: str,
                            user_id: str) -> dict | None:
        import httpx
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.get(f"{self.base_url}/session/{browser_session_id}",
                            headers=self._headers(**{"X-Tenant-Id": tenant_id,
                                                     "X-User-Id": user_id}))
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()


class AuthorizationDenied(Exception):
    """Raised by `_authorize` to stop a call before the transport.

    An exception rather than a return value on purpose: a caller that forgets to
    check a returned verdict still does not reach the worker. The only way past
    this is not to call `_authorize` at all, and there is one call site.
    """

    def __init__(self, code: str, reason: str, rule: str = "", terminal: bool = True):
        self.code = code
        self.reason = reason
        self.rule = rule
        self.terminal = terminal
        super().__init__(f"{code}: {reason}")


class WorkerGateway:
    """The chokepoint. One method in, one observation out.

    `authorizer` is how Phase E's policy is injected. `browser_tools` still knows
    nothing about the boundary, `TenantContext`, or the registry — it knows only
    that something may refuse a call before the transport.
    """

    def __init__(self, transport: Transport, authorizer: Any = None):
        self._t = transport
        self._authorizer = authorizer

    async def call(self, *, action: str, tenant_id: str, user_id: str, agent_id: str,
                   session_id: str, browser_session_id: str = "",
                   arguments: Optional[dict] = None) -> dict:
        """Build the `BrowserCommand` payload and execute it.

        The payload is assembled HERE, not by the tools. A tool passes validated
        arguments; the ownership envelope is this function's responsibility, so a
        tool cannot omit a field or invent one.
        """
        payload = {
            "action": action,
            "browser_session_id": browser_session_id,
            "tenant_id": tenant_id, "user_id": user_id,
            "agent_id": agent_id, "session_id": session_id,
            "arguments": dict(arguments or {}),
        }
        await self._authorize(payload)
        return await self._t.execute(payload)

    async def fetch_screenshot(self, ref: str, *, tenant_id: str, user_id: str) -> bytes:
        """Read captured bytes back, so the tool layer can persist them durably.

        Separate from `call` on purpose: this is the only path on which image bytes
        exist in this process at all, and it is short — fetch, persist, discard. The
        bytes never reach a tool result (§7.2).
        """
        return await self._t.fetch_screenshot(ref, tenant_id=tenant_id, user_id=user_id)

    async def session_facts(self, browser_session_id: str, *, tenant_id: str,
                            user_id: str) -> dict | None:
        """Owner + live page host, for the authorization resolver.

        Not routed through `call()`: it is not an action, charges no budget, and
        must be answerable BEFORE `_authorize` runs — routing it through the very
        function it feeds would be circular.
        """
        return await self._t.session_facts(browser_session_id, tenant_id=tenant_id,
                                           user_id=user_id)

    async def _authorize(self, payload: dict) -> None:
        """The single authorization point. Raises `AuthorizationDenied` to refuse.

        Called from `call()` BEFORE `self._t.execute(...)`, so a denial means the
        transport is never touched and Playwright is never reached. That ordering
        is the anti-bypass property, and it is what
        `test_a_denial_never_reaches_the_worker` asserts — by counting transport
        invocations, not by inspecting the returned error.

        With no authorizer injected this is a no-op, which is what keeps
        `browser_tools` independently testable.
        """
        if self._authorizer is None:
            return None
        await self._authorizer(payload)
        return None


_default_gateway: Optional[WorkerGateway] = None


def get_gateway() -> WorkerGateway:
    """The process-wide gateway. Defaults to HTTP — the deployed shape."""
    global _default_gateway
    if _default_gateway is None:
        _default_gateway = WorkerGateway(HttpTransport())
    return _default_gateway


def set_gateway(gateway: WorkerGateway) -> None:
    """Injection point for tests and for Phase D's wiring."""
    global _default_gateway
    _default_gateway = gateway


def in_process_gateway(worker: Any) -> WorkerGateway:
    """Convenience for Level 2 tests: drive a real `BrowserWorker` with no HTTP."""
    return WorkerGateway(InProcessTransport(worker))
