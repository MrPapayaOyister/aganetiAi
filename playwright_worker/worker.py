"""The worker: `execute(BrowserCommand) -> BrowserObservation` (§6.2).

One entry point, one closed action enum, no code channel. Everything the worker is
authoritative for — the ref map, timeouts, staleness, budgets, submit-like refusal —
is decided here or in the modules this one owns. Everything it is *not* authoritative
for — authorization — it does not attempt (§6.2): it trusts that its caller
already authorized, and asserts only session ownership, which is a property of a
resource it created and can therefore check honestly.

`execute` never raises. Every failure becomes a `BrowserObservation` with `ok=False`
and a typed code, because a worker that raises at its RPC boundary makes every error
untyped at exactly the layer §9.1 exists to serve.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional
from urllib.parse import urlparse

from .config import Budgets, Limits
from .errors import ErrorCode, WorkerError
from .inspector import Inspector
from .protocol import (
    NAVIGATING, SESSIONLESS, Action, BrowserCommand, BrowserObservation,
)
from .screenshots import ScreenshotStore, capture
from .session import BrowserSession, SessionManager

log = logging.getLogger("playwright_worker.worker")


class BrowserWorker:
    """Bounded pool of browser sessions behind one narrow operation."""

    def __init__(self, *, limits: Optional[Limits] = None,
                 budgets: Optional[Budgets] = None,
                 sessions: Optional[SessionManager] = None) -> None:
        self.limits = limits or Limits.from_env()
        self.budgets = budgets or Budgets.from_env()
        self.sessions = sessions or SessionManager(limits=self.limits, budgets=self.budgets)
        self.inspector = Inspector(max_elements=self.budgets.max_inspect_elements)
        self.screenshots = ScreenshotStore(limits=self.limits)

    async def start(self) -> None:
        await self.sessions.start()

    async def stop(self) -> None:
        await self.sessions.stop()

    # ── the one operation ─────────────────────────────────────────────────────
    async def execute(self, payload: dict | BrowserCommand) -> BrowserObservation:
        t0 = time.monotonic()
        action_name = ""
        sess: BrowserSession | None = None
        try:
            cmd = payload if isinstance(payload, BrowserCommand) else BrowserCommand.parse(payload)
            action_name = cmd.action.value

            if cmd.action in SESSIONLESS:
                return await self._open(cmd, t0)

            sess = self.sessions.resolve(cmd.browser_session_id,
                                         tenant_id=cmd.tenant_id, user_id=cmd.user_id)

            # §6.3: actions within one session are serialized. Two concurrent
            # actions on one page is a correctness hazard, not a throughput one.
            async with sess.lock:
                # Re-resolve under the lock. A close could have won the race between
                # the resolve above and acquiring the lock, and acting on a
                # closed context throws an untyped Playwright error.
                sess = self.sessions.resolve(cmd.browser_session_id,
                                             tenant_id=cmd.tenant_id, user_id=cmd.user_id)
                if cmd.action is Action.CLOSE:
                    return await self._close(cmd, sess, t0)

                # Budgets are charged BEFORE the work (see TaskBudget.charge_action).
                sess.budget.charge_action()
                if cmd.action in NAVIGATING:
                    sess.budget.charge_navigation()

                obs = await self._dispatch(cmd, sess)
                sess.touch()
                obs.duration_ms = int((time.monotonic() - t0) * 1000)
                obs.browser_session_id = sess.id
                obs.budgets = sess.budget.remaining()
                return obs

        except WorkerError as e:
            obs = BrowserObservation.failure(
                action_name or "unknown", e,
                duration_ms=int((time.monotonic() - t0) * 1000),
                browser_session_id=(sess.id if sess else ""),
                url=(sess.page.url if sess and sess.page else ""))
            if sess is not None:
                obs.budgets = sess.budget.remaining()
            return obs
        except Exception as e:  # noqa: BLE001
            # Anything unclassified is a worker bug. It still leaves as a typed
            # observation — an untyped escape would break the caller's error
            # handling at the worst possible moment.
            log.exception("unhandled worker error on %s", action_name)
            return BrowserObservation.failure(
                action_name or "unknown",
                WorkerError(ErrorCode.TIMEOUT, f"worker error: {type(e).__name__}"),
                duration_ms=int((time.monotonic() - t0) * 1000),
                browser_session_id=(sess.id if sess else ""))

    # ── dispatch ──────────────────────────────────────────────────────────────
    async def _dispatch(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        a = cmd.action
        if a is Action.NAVIGATE:   return await self._navigate(cmd, sess)
        if a is Action.BACK:       return await self._back(cmd, sess)
        if a is Action.INSPECT:    return await self._inspect(cmd, sess)
        if a is Action.SCREENSHOT: return await self._screenshot(cmd, sess)
        if a is Action.EXTRACT:    return await self._extract(cmd, sess)
        if a is Action.WAIT:       return await self._wait(cmd, sess)
        if a is Action.CLICK:      return await self._click(cmd, sess)
        if a is Action.FILL:       return await self._fill(cmd, sess)
        if a is Action.SELECT:     return await self._select(cmd, sess)
        if a is Action.CHECK:      return await self._check(cmd, sess)
        if a is Action.UPLOAD:     return await self._upload(cmd, sess)
        if a is Action.SUBMIT:     return await self._submit(cmd, sess)
        raise WorkerError(ErrorCode.BAD_REQUEST, f"unhandled action {a.value}")

    # ── session actions ───────────────────────────────────────────────────────
    async def _open(self, cmd: BrowserCommand, t0: float) -> BrowserObservation:
        args = cmd.arguments
        domains = args.get("allowed_domains") or []
        if not isinstance(domains, (list, tuple)) or not all(isinstance(d, str) for d in domains):
            raise WorkerError(ErrorCode.BAD_REQUEST, "allowed_domains must be a list of strings")
        sess = await self.sessions.open(
            tenant_id=cmd.tenant_id, user_id=cmd.user_id, agent_id=cmd.agent_id,
            session_id=cmd.session_id, allowed_domains=tuple(domains),
            viewport=(int(args.get("viewport_width", 1280)),
                      int(args.get("viewport_height", 800))),
        )
        return BrowserObservation(
            ok=True, action=cmd.action.value, url=sess.page.url,
            browser_session_id=sess.id,
            duration_ms=int((time.monotonic() - t0) * 1000),
            budgets=sess.budget.remaining(),
        )

    async def _close(self, cmd: BrowserCommand, sess: BrowserSession,
                     t0: float) -> BrowserObservation:
        sid = sess.id
        self.screenshots.drop_session(sid)
        await self.sessions.close(sid, reason="explicit")
        return BrowserObservation(ok=True, action=cmd.action.value, browser_session_id=sid,
                                  duration_ms=int((time.monotonic() - t0) * 1000))

    # ── navigation ────────────────────────────────────────────────────────────
    def _check_domain(self, sess: BrowserSession, url: str) -> None:
        """§11.2 lives in the authorization boundary, not here (§4.2). This is a
        worker-side backstop only: it enforces the allowlist the session was OPENED
        with, which the worker knows because it recorded it. It is not a substitute
        for the authorization check and does not pretend to be."""
        if not sess.allowed_domains:
            return
        host = (urlparse(url).hostname or "").lower()
        for allowed in sess.allowed_domains:
            a = allowed.lower()
            if host == a or host.endswith("." + a):
                return
        raise WorkerError(ErrorCode.DOMAIN_DENIED,
                          f"{host or url!r} is not in this session's allowed domains",
                          detail={"host": host, "allowed": list(sess.allowed_domains)})

    async def _navigate(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        url = cmd.arguments.get("url")
        if not isinstance(url, str) or not url.strip():
            raise WorkerError(ErrorCode.BAD_REQUEST, "navigate requires a url")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            # file:, data:, javascript: are all exfiltration or code channels.
            raise WorkerError(ErrorCode.DOMAIN_DENIED,
                              f"scheme {parsed.scheme!r} is not permitted; http(s) only")
        self._check_domain(sess, url)
        try:
            await sess.page.goto(url, timeout=self.budgets.action_timeout_ms,
                                 wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            raise WorkerError(ErrorCode.NAVIGATION_FAILED,
                              f"could not load {url}: {type(e).__name__}",
                              detail={"url": url}) from e
        self.inspector.invalidate(sess, "navigate")
        return await self._page_observation(cmd, sess)

    async def _back(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        try:
            await sess.page.go_back(timeout=self.budgets.action_timeout_ms,
                                    wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            raise WorkerError(ErrorCode.NAVIGATION_FAILED,
                              f"back failed: {type(e).__name__}") from e
        self.inspector.invalidate(sess, "back")
        return await self._page_observation(cmd, sess)

    # ── read actions ──────────────────────────────────────────────────────────
    async def _inspect(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        limit = cmd.arguments.get("max_elements")
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise WorkerError(ErrorCode.BAD_REQUEST, "max_elements must be a positive integer")
        views, total, truncated = await self.inspector.inspect(sess, limit=limit)
        obs = await self._page_observation(cmd, sess)
        obs.elements = views
        obs.element_total = total
        obs.element_truncated = truncated
        return obs

    async def _screenshot(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        png, masked = await capture(
            sess.page, refs=sess.refs, full_page=bool(cmd.arguments.get("full_page")),
            timeout_ms=self.budgets.action_timeout_ms)
        shot = self.screenshots.put(
            png=png, browser_session_id=sess.id, tenant_id=cmd.tenant_id,
            user_id=cmd.user_id, url=sess.page.url, masked_count=masked)
        obs = await self._page_observation(cmd, sess)
        obs.screenshot_ref = shot.ref
        obs.extracted = shot.meta()
        return obs

    async def _extract(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        """Structured text/attribute extraction. **No JS evaluation** (§3.2)."""
        args = cmd.arguments
        ref = args.get("element_ref")
        attribute = args.get("attribute")
        if attribute is not None and not isinstance(attribute, str):
            raise WorkerError(ErrorCode.BAD_REQUEST, "attribute must be a string")

        obs = await self._page_observation(cmd, sess)
        if ref:
            loc = await self.inspector.locator(sess, ref)
            try:
                if attribute:
                    obs.extracted = await loc.get_attribute(attribute)
                else:
                    obs.extracted = (await loc.inner_text())[:4000]
            except Exception as e:  # noqa: BLE001
                raise WorkerError(ErrorCode.TIMEOUT,
                                  f"extract failed: {type(e).__name__}") from e
            return obs

        scope = args.get("scope", "text")
        if scope not in ("text", "errors", "title"):
            raise WorkerError(ErrorCode.BAD_REQUEST,
                              "scope must be one of: text, errors, title")
        try:
            if scope == "title":
                obs.extracted = await sess.page.title()
            elif scope == "errors":
                # Validation messages the page rendered. §9.1 wants the agent to
                # extract and act on these, so they are a first-class scope rather
                # than something to be found by scraping full text.
                nodes = sess.page.locator("[data-testid=errors] li, .errors li, [role=alert]")
                obs.extracted = [t.strip() for t in await nodes.all_inner_texts()][:20]
            else:
                obs.extracted = (await sess.page.locator("body").inner_text())[:8000]
        except Exception as e:  # noqa: BLE001
            raise WorkerError(ErrorCode.TIMEOUT, f"extract failed: {type(e).__name__}") from e
        return obs

    async def _wait(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        args = cmd.arguments
        condition = args.get("condition", "load")
        timeout = min(int(args.get("timeout_ms", self.budgets.action_timeout_ms)),
                      self.budgets.action_timeout_ms)
        if condition not in ("load", "element_visible", "element_enabled", "element_appears"):
            raise WorkerError(
                ErrorCode.BAD_REQUEST,
                "condition must be one of: load, element_visible, element_enabled, "
                "element_appears")
        try:
            if condition == "load":
                await sess.page.wait_for_load_state("networkidle", timeout=timeout)
            elif condition == "element_appears":
                # The recovery for a DELAYED element (§10.3): something the page has
                # not rendered yet, which by definition has no ref. `element_visible`
                # cannot express it — it needs a ref for an element that does not
                # exist. Waiting on the interactive-element COUNT is semantic, needs
                # no addressing, and therefore opens no selector channel.
                # Measured LIVE, never from len(sess.refs): the ref map is capped at
                # max_inspect_elements, so on a page with more interactive elements
                # than the cap the map size is always below the true count and the
                # comparison below would fire immediately — a wait that never waits.
                baseline = await self.inspector.count_interactive(sess)
                deadline = time.monotonic() + timeout / 1000
                while time.monotonic() < deadline:
                    if await self.inspector.count_interactive(sess) > baseline:
                        break
                    await asyncio.sleep(0.15)
                else:
                    raise WorkerError(
                        ErrorCode.TIMEOUT,
                        f"no new interactive element appeared within {timeout}ms",
                        detail={"baseline_elements": baseline})
                # Whatever arrived has no ref yet; the caller must re-inspect. Bump
                # the generation so any ref it still holds is honestly stale.
                self.inspector.invalidate(sess, "element_appears")
            else:
                ref = args.get("element_ref")
                if not ref:
                    raise WorkerError(ErrorCode.BAD_REQUEST,
                                      f"{condition} requires element_ref")
                loc = await self.inspector.locator(sess, ref)
                state = "visible" if condition == "element_visible" else "attached"
                await loc.wait_for(state=state, timeout=timeout)
                if condition == "element_enabled":
                    deadline = time.monotonic() + timeout / 1000
                    while time.monotonic() < deadline:
                        if await loc.is_enabled():
                            break
                        await asyncio.sleep(0.1)
                    else:
                        raise WorkerError(ErrorCode.TIMEOUT,
                                          f"{ref} did not become enabled within {timeout}ms",
                                          detail={"element_ref": ref})
        except WorkerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise WorkerError(ErrorCode.TIMEOUT,
                              f"wait({condition}) timed out after {timeout}ms") from e
        return await self._page_observation(cmd, sess)

    # ── write actions ─────────────────────────────────────────────────────────
    async def _click(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        """§3.4: refuses submit-like targets, enforced here so it is not bypassable.

        The check reads the classification the INSPECTOR made while walking the DOM.
        There is no path to a click that skips inspection — `locator()` raises
        ELEMENT_NOT_FOUND for a ref the session never issued — so a caller cannot
        reach a submit button by declining to inspect first.
        """
        ref = cmd.arguments["element_ref"]
        entry = self.inspector.describe(sess, ref)
        if self.inspector.is_submit_like(entry):
            # WRONG_TOOL_FOR_SUBMIT, not AUTHZ_DENIED. This is a routing correction
            # — the agent picked the wrong tool and the right one is named — whereas
            # AUTHZ_DENIED means "you may not, and retrying is an escalation
            # attempt". Sharing a code made those indistinguishable in a §11.6 audit
            # row and told the agent to give up on a task it could finish.
            raise WorkerError(
                ErrorCode.WRONG_TOOL_FOR_SUBMIT,
                f"{ref} ({entry.get('tag')}/{entry.get('type')} "
                f"{entry.get('accessible_name','')!r}) is a submit control. "
                f"browser_click does not submit forms — use browser_submit, which is "
                f"approval-gated.",
                detail={"element_ref": ref, "use_instead": Action.SUBMIT.value,
                        "accessible_name": entry.get("accessible_name", "")})
        return await self._activate(cmd, sess, ref, "click")

    async def _submit(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        """The only tool that submits (§3.4). Refuses a NON-submit target, so the
        two tools stay a real partition rather than one being a superset."""
        ref = cmd.arguments["element_ref"]
        entry = self.inspector.describe(sess, ref)
        if not self.inspector.is_submit_like(entry):
            raise WorkerError(
                ErrorCode.BAD_REQUEST,
                f"{ref} is not a submit control; use browser_click for it",
                detail={"element_ref": ref, "use_instead": Action.CLICK.value})
        return await self._activate(cmd, sess, ref, "submit")

    async def _activate(self, cmd: BrowserCommand, sess: BrowserSession,
                        ref: str, what: str) -> BrowserObservation:
        loc = await self.inspector.locator(sess, ref)
        await self._precondition(loc, ref)
        before_url = sess.page.url
        try:
            await loc.click(timeout=self.budgets.action_timeout_ms)
        except Exception as e:  # noqa: BLE001
            raise self._classify(e, ref, f"{what} failed") from e
        # A click may navigate. Settle, then invalidate refs unconditionally: even
        # without navigation the click may have mutated the DOM, and a stale ref that
        # still resolves is the wrong-element hazard §3.3 exists to prevent.
        try:
            await sess.page.wait_for_load_state("domcontentloaded",
                                                timeout=self.budgets.action_timeout_ms)
        except Exception:  # noqa: BLE001 — no navigation is a normal outcome
            pass
        self.inspector.invalidate(sess, what)
        obs = await self._page_observation(cmd, sess)
        obs.extracted = {"navigated": sess.page.url != before_url,
                         "from": before_url, "to": sess.page.url}
        return obs

    async def _fill(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        ref = cmd.arguments["element_ref"]
        value = cmd.arguments.get("value", "")
        if not isinstance(value, str):
            raise WorkerError(ErrorCode.BAD_REQUEST, "value must be a string")
        entry = self.inspector.describe(sess, ref)
        sensitive = bool(cmd.arguments.get("sensitive")) or self.inspector.is_sensitive(entry)
        loc = await self.inspector.locator(sess, ref)
        await self._precondition(loc, ref)
        try:
            await loc.fill(value, timeout=self.budgets.action_timeout_ms)
        except Exception as e:  # noqa: BLE001
            raise self._classify(e, ref, "fill failed") from e
        obs = await self._page_observation(cmd, sess)
        # §7.2: record THAT a value was set, never the value. Applies to fake lab
        # credentials too — the habit is the control.
        obs.extracted = {"filled": ref, "sensitive": sensitive,
                         "length": len(value) if not sensitive else None,
                         "value": None if sensitive else value[:200]}
        return obs

    async def _select(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        ref = cmd.arguments["element_ref"]
        value = cmd.arguments.get("value")
        if not isinstance(value, str):
            raise WorkerError(ErrorCode.BAD_REQUEST, "select requires a string value")
        loc = await self.inspector.locator(sess, ref)
        await self._precondition(loc, ref)
        try:
            await loc.select_option(value, timeout=self.budgets.action_timeout_ms)
        except Exception as e:  # noqa: BLE001
            raise self._classify(e, ref, f"could not select {value!r}") from e
        obs = await self._page_observation(cmd, sess)
        obs.extracted = {"selected": value}
        return obs

    async def _check(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        ref = cmd.arguments["element_ref"]
        checked = cmd.arguments.get("checked", True)
        if not isinstance(checked, bool):
            raise WorkerError(ErrorCode.BAD_REQUEST, "checked must be a boolean")
        loc = await self.inspector.locator(sess, ref)
        await self._precondition(loc, ref)
        try:
            if checked:
                await loc.check(timeout=self.budgets.action_timeout_ms)
            else:
                await loc.uncheck(timeout=self.budgets.action_timeout_ms)
        except Exception as e:  # noqa: BLE001
            raise self._classify(e, ref, "check failed") from e
        obs = await self._page_observation(cmd, sess)
        obs.extracted = {"checked": checked}
        return obs

    async def _upload(self, cmd: BrowserCommand, sess: BrowserSession) -> BrowserObservation:
        """Artifact ID only, never a filesystem path (§11.4).

        The artifact resolver is a worker-side registry. v1 is a dict — the same
        credential-broker shape §10.4 uses — because the interface being right
        matters more than the implementation being sophisticated. A caller cannot
        name a path: `path` is in FORBIDDEN_ARGS.
        """
        ref = cmd.arguments["element_ref"]
        artifact_id = cmd.arguments.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise WorkerError(ErrorCode.BAD_REQUEST, "upload requires artifact_id")
        artifact = ARTIFACTS.get(artifact_id)
        if artifact is None:
            raise WorkerError(ErrorCode.ELEMENT_NOT_FOUND,
                              f"unknown artifact {artifact_id!r}",
                              detail={"artifact_id": artifact_id})
        loc = await self.inspector.locator(sess, ref)
        try:
            await loc.set_input_files({
                "name": artifact["filename"],
                "mimeType": artifact["content_type"],
                "buffer": artifact["content"],
            }, timeout=self.budgets.action_timeout_ms)
        except Exception as e:  # noqa: BLE001
            raise self._classify(e, ref, "upload failed") from e
        obs = await self._page_observation(cmd, sess)
        obs.extracted = {"artifact_id": artifact_id, "filename": artifact["filename"],
                         "size": len(artifact["content"]), "sha256": artifact["sha256"]}
        return obs

    # ── helpers ───────────────────────────────────────────────────────────────
    async def _precondition(self, loc: Any, ref: str) -> None:
        """Visibility and enabled-ness, checked before acting so the failure is the
        §9.1 code with the right recovery rather than a generic TIMEOUT."""
        try:
            if not await loc.is_visible():
                raise WorkerError(ErrorCode.ELEMENT_NOT_VISIBLE,
                                  f"{ref} is not visible",
                                  detail={"element_ref": ref})
            if not await loc.is_enabled():
                raise WorkerError(ErrorCode.ELEMENT_DISABLED,
                                  f"{ref} is disabled — a precondition is unmet",
                                  detail={"element_ref": ref})
        except WorkerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise WorkerError(ErrorCode.STALE_REF,
                              f"{ref} could not be examined: {type(e).__name__}",
                              detail={"element_ref": ref}) from e

    @staticmethod
    def _classify(exc: Exception, ref: str, message: str) -> WorkerError:
        """Map a Playwright exception onto §9.1. Ordering matters: Playwright's
        timeout error is what surfaces for a detached node too, so the detach and
        visibility cases are matched on message before falling back to TIMEOUT."""
        name = type(exc).__name__
        text = str(exc).lower()
        if "not attached" in text or "element is not attached" in text:
            return WorkerError(ErrorCode.STALE_REF, f"{message}: element detached",
                               detail={"element_ref": ref})
        if "disabled" in text:
            return WorkerError(ErrorCode.ELEMENT_DISABLED, f"{message}: element disabled",
                               detail={"element_ref": ref})
        if "not visible" in text or "hidden" in text:
            return WorkerError(ErrorCode.ELEMENT_NOT_VISIBLE, f"{message}: not visible",
                               detail={"element_ref": ref})
        if "timeout" in text or name == "TimeoutError":
            return WorkerError(ErrorCode.TIMEOUT, f"{message}: timed out",
                               detail={"element_ref": ref})
        return WorkerError(ErrorCode.ELEMENT_NOT_FOUND, f"{message}: {name}",
                           detail={"element_ref": ref})

    async def _page_observation(self, cmd: BrowserCommand,
                                sess: BrowserSession) -> BrowserObservation:
        """URL + title + validation signal, on every successful action.

        Validation errors are surfaced as a `VALIDATION_ERROR` observation that is
        still `ok=True`: §9.1 is explicit that this is an expected signal, not a
        failure, and marking it `ok=False` would make every caller's error branch
        treat a form that needs correcting as something that went wrong.
        """
        try:
            title = await sess.page.title()
        except Exception:  # noqa: BLE001
            title = ""
        obs = BrowserObservation(ok=True, action=cmd.action.value,
                                 url=sess.page.url, title=title)
        try:
            errs = sess.page.locator("[data-testid=errors] li, .errors li, [role=alert]")
            messages = [t.strip() for t in await errs.all_inner_texts() if t.strip()]
        except Exception:  # noqa: BLE001
            messages = []
        if messages:
            obs.error = ErrorCode.VALIDATION_ERROR
            obs.error_message = "; ".join(messages[:10])
            obs.recovery = "not a failure; correct the field and continue"
            obs.error_detail = {"messages": messages[:10]}
        return obs


#: The v1 artifact registry (§11.4). A dict, deliberately — the interface being
#: right matters more than the implementation being sophisticated. Populated by
#: tests and, later, by the tool layer resolving a real artifact id.
ARTIFACTS: dict[str, dict] = {}


def register_artifact(artifact_id: str, *, filename: str, content: bytes,
                      content_type: str = "application/octet-stream") -> dict:
    """Register uploadable bytes under an opaque id. No path ever enters the worker."""
    import hashlib
    rec = {"filename": filename, "content": content, "content_type": content_type,
           "sha256": hashlib.sha256(content).hexdigest()}
    ARTIFACTS[artifact_id] = rec
    return rec
