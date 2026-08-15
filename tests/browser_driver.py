"""Test-side helper for driving the worker. Not part of the worker.

Level 1 tests drive `execute()` directly, which means every test would otherwise
repeat the same command-dict construction and the same "find the ref whose
accessible name is X" loop. That repetition hides what each test is actually
asserting.

This helper deliberately does **not** wrap error handling: a test must see the raw
`BrowserObservation`, because the observation IS the thing under test. It only
removes the boilerplate of building commands and locating refs by what a human
would call the field — which is also a small proof that the inspector's
`accessible_name` is good enough to navigate by, including on the lab's deliberately
awkward fields (§10.3).
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from playwright_worker import Action, BrowserObservation, BrowserWorker

TENANT = "tenant-alpha"
USER = "user-alice"


class Driver:
    """One identity driving one worker."""

    def __init__(self, worker: BrowserWorker, lab_url: str, *,
                 tenant_id: str = TENANT, user_id: str = USER,
                 agent_id: str = "browser_agent", session_id: str = "task-1"):
        self.w = worker
        self.lab = lab_url.rstrip("/")
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.browser_session_id = ""
        self.last: BrowserObservation | None = None

    # ── command construction ──────────────────────────────────────────────────
    def cmd(self, action: Action, **arguments) -> dict:
        return {
            "action": action.value,
            "browser_session_id": self.browser_session_id,
            "tenant_id": self.tenant_id, "user_id": self.user_id,
            "agent_id": self.agent_id, "session_id": self.session_id,
            "arguments": arguments,
        }

    async def run(self, action: Action, **arguments) -> BrowserObservation:
        self.last = await self.w.execute(self.cmd(action, **arguments))
        return self.last

    # ── convenience ───────────────────────────────────────────────────────────
    async def open(self, allowed_domains: list[str] | None = None) -> BrowserObservation:
        obs = await self.run(Action.OPEN,
                             allowed_domains=allowed_domains
                             if allowed_domains is not None else ["127.0.0.1", "localhost"])
        if obs.ok:
            self.browser_session_id = obs.browser_session_id
        return obs

    async def goto(self, path: str) -> BrowserObservation:
        return await self.run(Action.NAVIGATE, url=f"{self.lab}{path}")

    async def inspect(self, **kw) -> BrowserObservation:
        return await self.run(Action.INSPECT, **kw)

    async def ref(self, name: str, *, role: str | None = None,
                  exact: bool = False) -> str | None:
        """Re-inspect and return the ref whose accessible name matches.

        Always re-inspects, because after any action the previous refs are stale by
        construction (§3.3) — a helper that cached them would make the tests pass
        for the wrong reason.
        """
        obs = await self.inspect()
        # Surface an inspect failure as itself. Returning None here would make a
        # budget exhaustion or a dead session look like "the element is not on the
        # page", which is how a test comes to assert the wrong thing.
        assert obs.ok or obs.error and obs.error.value == "VALIDATION_ERROR", (
            f"inspect failed: {obs.error} {obs.error_message}")
        for e in obs.elements:
            if role and e.role != role:
                continue
            if exact:
                if e.accessible_name.strip().lower() == name.strip().lower():
                    return e.ref
            elif name.strip().lower() in e.accessible_name.strip().lower():
                return e.ref
        return None

    async def fill_by_name(self, name: str, value: str, *, exact: bool = False,
                           **kw) -> BrowserObservation:
        ref = await self.ref(name, exact=exact)
        assert ref, f"no element with accessible name matching {name!r}"
        return await self.run(Action.FILL, element_ref=ref, value=value, **kw)

    async def click_by_name(self, name: str, **kw) -> BrowserObservation:
        ref = await self.ref(name, **kw)
        assert ref, f"no element with accessible name containing {name!r}"
        return await self.run(Action.CLICK, element_ref=ref)

    async def submit_by_name(self, name: str) -> BrowserObservation:
        ref = await self.ref(name)
        assert ref, f"no element with accessible name containing {name!r}"
        return await self.run(Action.SUBMIT, element_ref=ref)

    async def login(self, email: str = "demo@browser-lab.invalid",
                    password: str = "not-a-real-password") -> BrowserObservation:
        await self.goto("/login")
        await self.fill_by_name("Email address", email)
        await self.fill_by_name("Pass phrase", password, sensitive=True)
        return await self.submit_by_name("Continue")

    async def through_profile(self, display: str = "Demo Person") -> BrowserObservation:
        # /profile labels this field "Name", not "Full name" — and labels the
        # display-name field "Name" as well (§10.3's duplicated-label case). The
        # first match in DOM order is `full_name`, which is the one the flow needs.
        await self.fill_by_name("Name", display, exact=True)
        return await self.submit_by_name("Save and continue")


# ── lab control surface ───────────────────────────────────────────────────────
def lab_post(url: str, path: str, body: dict | None = None) -> Any:
    req = urllib.request.Request(
        f"{url.rstrip('/')}{path}", method="POST",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def lab_get(url: str, path: str) -> Any:
    with urllib.request.urlopen(f"{url.rstrip('/')}{path}", timeout=10) as r:
        return json.loads(r.read())


def reset_lab(url: str) -> None:
    lab_post(url, "/_test/reset")


def submissions(url: str) -> list[dict]:
    return lab_get(url, "/_test/submissions")["submissions"]


def set_flags(url: str, **flags: bool) -> Any:
    return lab_post(url, "/_test/flags", flags)
