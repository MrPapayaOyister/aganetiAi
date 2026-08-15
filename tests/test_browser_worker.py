"""Level 1 — the worker driven directly from pytest. No LLM, no agent, no registry.

Everything here talks to `BrowserWorker.execute()` and the real browser-lab. The
point of Level 1 (§16 Phase B: "Level 1 tests pass end to end, no LLM involved") is
that the worker is correct *before* anything reasons about it, so a later agent
failure is unambiguously an agent failure.

Two classes of test live here and they are marked differently:

  * `@pytest.mark.browser` + `@pytest.mark.lab` — needs Chromium and a running lab.
  * unmarked — protocol-level, pure Python, runs anywhere. The selector-rejection
    tests are deliberately in this class: the boundary must be provable without a
    browser, because that is the property most likely to be regressed by someone
    who cannot run the browser tests locally.

Every lab test resets the lab first, so nothing depends on execution order.
"""
from __future__ import annotations

import asyncio
import hashlib
import time

import pytest
import pytest_asyncio

from playwright_worker import (
    Action, BrowserCommand, BrowserWorker, Budgets, ErrorCode, Limits, SessionManager,
    register_artifact, validate_element_ref,
)
from playwright_worker.errors import TERMINAL, WorkerError
from playwright_worker.protocol import ALLOWED_ARGS, FORBIDDEN_ARGS

from tests.browser_driver import (
    Driver, lab_get, reset_lab, set_flags, submissions,
)

pytestmark_browser = [pytest.mark.browser, pytest.mark.lab]


# ══════════════════════════════════════════════════════════════════════════════
# Protocol boundary — no browser required
# ══════════════════════════════════════════════════════════════════════════════
class TestSelectorRejection:
    """§3.3 / §11 S3. The worker must not accept a selector from its caller."""

    @pytest.mark.parametrize("hostile", [
        "#app-submit",
        ".btn.primary",
        "button[type=submit]",
        "div > button",
        "input:nth-child(2)",
        "//button[@type='submit']",
        "(//input)[1]",
        "../button",
        "css=button",
        "xpath=//button",
        "text=Submit application",
        "data-testid=app-submit",
        "role=button[name='Submit']",
        "id=app-submit",
        "form button, form input",
        "*",
        "a[href^='http']",
        "e1 e2",
        "e1'",
    ])
    def test_selector_shaped_element_ref_is_refused(self, hostile):
        with pytest.raises(WorkerError) as ei:
            validate_element_ref(hostile)
        assert ei.value.code is ErrorCode.SELECTOR_REJECTED, hostile

    @pytest.mark.parametrize("ref", ["e1", "e17", "e999999"])
    def test_real_refs_are_accepted(self, ref):
        assert validate_element_ref(ref) == ref

    @pytest.mark.parametrize("bad", ["", "E1", "elem1", "1", "e", "e-1", "e1.5", "ref17"])
    def test_non_ref_strings_are_refused(self, bad):
        with pytest.raises(WorkerError) as ei:
            validate_element_ref(bad)
        assert ei.value.code is ErrorCode.SELECTOR_REJECTED

    def test_non_string_element_ref_is_bad_request(self):
        for bad in (17, None, ["e1"], {"ref": "e1"}):
            with pytest.raises(WorkerError) as ei:
                validate_element_ref(bad)
            assert ei.value.code is ErrorCode.BAD_REQUEST

    @pytest.mark.parametrize("channel", sorted(FORBIDDEN_ARGS))
    def test_no_action_accepts_a_code_or_selector_channel(self, channel):
        """§11 S2/S3 as a property of the type, not of a validation pass."""
        for action, allowed in ALLOWED_ARGS.items():
            assert channel not in allowed, f"{action.value} accepts {channel!r}"

    def test_a_forbidden_argument_is_refused_by_name(self):
        cmd = {"action": "browser_click", "browser_session_id": "bs_x",
               "tenant_id": "t", "user_id": "u",
               "arguments": {"element_ref": "e1", "selector": "#x"}}
        with pytest.raises(WorkerError) as ei:
            BrowserCommand.parse(cmd)
        assert ei.value.code is ErrorCode.SELECTOR_REJECTED
        assert "selector" in str(ei.value)

    def test_script_channel_is_refused(self):
        for key in ("script", "code", "js", "evaluate", "expression"):
            with pytest.raises(WorkerError) as ei:
                BrowserCommand.parse({"action": "browser_extract",
                                      "arguments": {key: "return 1"}})
            assert ei.value.code is ErrorCode.SELECTOR_REJECTED, key


class TestUploadContract:
    def test_upload_has_no_path_argument(self):
        """A filesystem path must be unreachable by construction (§11.4 / S6)."""
        assert "path" not in ALLOWED_ARGS[Action.UPLOAD]
        assert "path" in FORBIDDEN_ARGS


class TestClosedActionSet:
    def test_action_enum_is_exactly_the_fourteen(self):
        assert len(list(Action)) == 14
        assert {a.value for a in Action} == {
            "browser_open", "browser_close", "browser_navigate", "browser_inspect",
            "browser_screenshot", "browser_extract", "browser_wait", "browser_back",
            "browser_click", "browser_fill", "browser_select", "browser_check",
            "browser_upload", "browser_submit"}

    @pytest.mark.parametrize("bogus", ["browser_evaluate", "evaluate", "page.goto",
                                       "browser_open ", "", None, 17])
    def test_unknown_action_is_bad_request(self, bogus):
        with pytest.raises(WorkerError) as ei:
            BrowserCommand.parse({"action": bogus})
        assert ei.value.code is ErrorCode.BAD_REQUEST

    def test_unknown_argument_is_refused_not_ignored(self):
        """A silently-dropped argument is how a caller comes to believe it
        constrained something it did not."""
        with pytest.raises(WorkerError) as ei:
            BrowserCommand.parse({"action": "browser_inspect",
                                  "arguments": {"max_elements": 5, "depth": 3}})
        assert ei.value.code is ErrorCode.BAD_REQUEST
        assert "depth" in str(ei.value)

    def test_element_actions_require_a_ref(self):
        for a in ("browser_click", "browser_fill", "browser_select",
                  "browser_check", "browser_upload", "browser_submit"):
            with pytest.raises(WorkerError) as ei:
                BrowserCommand.parse({"action": a, "arguments": {}})
            assert ei.value.code is ErrorCode.BAD_REQUEST


class TestErrorTaxonomy:
    def test_origin_records_which_layer_refused(self):
        """The CODE is the reason, `origin` is the source. An observation always
        comes from the worker; a tool-layer refusal never builds one."""
        from playwright_worker.errors import Origin
        from playwright_worker.protocol import BrowserObservation
        assert {o.value for o in Origin} == {"tool", "worker"}
        obs = BrowserObservation(ok=True, action="browser_inspect")
        assert obs.origin is Origin.WORKER
        assert obs.as_dict()["origin"] == "worker"

    def test_a_worker_refusal_is_stamped_worker(self):
        from playwright_worker.errors import ErrorCode as EC
        from playwright_worker.errors import Origin, WorkerError
        from playwright_worker.protocol import BrowserObservation
        obs = BrowserObservation.failure(
            "browser_click", WorkerError(EC.BAD_REQUEST, "malformed"))
        assert obs.origin is Origin.WORKER

    def test_every_ninepointone_code_exists(self):
        for name in ("ELEMENT_NOT_FOUND", "STALE_REF", "ELEMENT_NOT_VISIBLE",
                     "ELEMENT_DISABLED", "TIMEOUT", "NAVIGATION_FAILED",
                     "VALIDATION_ERROR", "UNEXPECTED_MODAL", "DOMAIN_DENIED",
                     "AUTHZ_DENIED"):
            assert hasattr(ErrorCode, name)

    def test_terminal_codes_are_never_retryable(self):
        assert ErrorCode.DOMAIN_DENIED in TERMINAL
        assert ErrorCode.AUTHZ_DENIED in TERMINAL
        # A routing correction names the tool that works, so it is not terminal.
        # Sharing AUTHZ_DENIED's code made this untrue and told the agent to stop.
        assert ErrorCode.WRONG_TOOL_FOR_SUBMIT not in TERMINAL
        assert ErrorCode.NAVIGATION_FAILED in TERMINAL
        # STALE_REF explicitly is NOT terminal — §9.1 says remap and retry.
        assert ErrorCode.STALE_REF not in TERMINAL
        assert ErrorCode.VALIDATION_ERROR not in TERMINAL


class TestBudgetAccounting:
    """Budgets are pure counters; test them without a browser."""

    def test_action_budget_charges_before_the_action(self):
        from playwright_worker.session import TaskBudget
        b = TaskBudget(budgets=Budgets(max_actions_per_task=2))
        b.charge_action(); b.charge_action()
        with pytest.raises(WorkerError) as ei:
            b.charge_action()
        assert ei.value.code is ErrorCode.BUDGET_EXCEEDED
        # Exactly 2 were permitted — not 3. Charging after the fact would allow N+1,
        # and for browser_submit the extra one is the irreversible action.
        assert b.actions == 2

    def test_navigation_budget_is_separate_from_actions(self):
        from playwright_worker.session import TaskBudget
        b = TaskBudget(budgets=Budgets(max_actions_per_task=100, max_navigations_per_task=1))
        b.charge_navigation()
        with pytest.raises(WorkerError):
            b.charge_navigation()

    def test_per_element_retry_budget_is_per_element(self):
        from playwright_worker.session import TaskBudget
        b = TaskBudget(budgets=Budgets(max_retries_per_element=2))
        b.charge_element_retry("e1"); b.charge_element_retry("e1")
        with pytest.raises(WorkerError):
            b.charge_element_retry("e1")
        b.charge_element_retry("e2")   # a different element has its own allowance

    def test_wall_clock_is_enforced(self):
        from playwright_worker.session import TaskBudget
        b = TaskBudget(budgets=Budgets(task_wall_clock_ms=0))
        with pytest.raises(WorkerError) as ei:
            b.charge_action()
        assert ei.value.detail["budget"] == "task_wall_clock_ms"


# ══════════════════════════════════════════════════════════════════════════════
# Session manager — needs Chromium, not the lab
# ══════════════════════════════════════════════════════════════════════════════
@pytest_asyncio.fixture
async def worker(require_chromium):
    w = BrowserWorker()
    await w.start()
    try:
        yield w
    finally:
        await w.stop()


@pytest_asyncio.fixture
async def driver(worker, lab_url):
    reset_lab(lab_url)
    d = Driver(worker, lab_url)
    obs = await d.open()
    assert obs.ok, obs.error_message
    return d


@pytest.mark.browser
@pytest.mark.asyncio
class TestSessionManager:
    async def test_ids_are_opaque_and_unguessable(self, worker):
        ids = []
        for i in range(3):
            obs = await worker.execute({
                "action": "browser_open", "tenant_id": "t", "user_id": f"u{i}",
                "arguments": {}})
            assert obs.ok
            ids.append(obs.browser_session_id)
        assert all(i.startswith("bs_") for i in ids)
        assert all(len(i) > 30 for i in ids), "id too short to be unguessable"
        assert len(set(ids)) == 3
        # Not sequential: no id is a small edit of another.
        assert not any(a[:-1] == b[:-1] for a in ids for b in ids if a != b)

    async def test_open_without_an_owner_is_refused(self, worker):
        obs = await worker.execute({"action": "browser_open", "arguments": {}})
        assert not obs.ok and obs.error is ErrorCode.AUTHZ_DENIED

    async def test_per_user_cap(self, require_chromium):
        w = BrowserWorker(limits=Limits(max_sessions_per_user=2))
        await w.start()
        try:
            for _ in range(2):
                assert (await w.execute({"action": "browser_open", "tenant_id": "t",
                                         "user_id": "u", "arguments": {}})).ok
            obs = await w.execute({"action": "browser_open", "tenant_id": "t",
                                   "user_id": "u", "arguments": {}})
            assert not obs.ok and obs.error is ErrorCode.BUDGET_EXCEEDED
            assert obs.error_detail["cap"] == "max_sessions_per_user"
            # A different user in the same tenant is unaffected.
            assert (await w.execute({"action": "browser_open", "tenant_id": "t",
                                     "user_id": "other", "arguments": {}})).ok
        finally:
            await w.stop()

    async def test_per_tenant_cap(self, require_chromium):
        w = BrowserWorker(limits=Limits(max_sessions_per_user=5, max_sessions_per_tenant=2))
        await w.start()
        try:
            for i in range(2):
                assert (await w.execute({"action": "browser_open", "tenant_id": "t",
                                         "user_id": f"u{i}", "arguments": {}})).ok
            obs = await w.execute({"action": "browser_open", "tenant_id": "t",
                                   "user_id": "u9", "arguments": {}})
            assert obs.error_detail["cap"] == "max_sessions_per_tenant"
            assert (await w.execute({"action": "browser_open", "tenant_id": "other",
                                     "user_id": "u", "arguments": {}})).ok
        finally:
            await w.stop()

    async def test_idle_ttl_is_reaped(self, require_chromium):
        w = BrowserWorker(limits=Limits(idle_ttl_s=0, reap_interval_s=3600))
        await w.start()
        try:
            obs = await w.execute({"action": "browser_open", "tenant_id": "t",
                                   "user_id": "u", "arguments": {}})
            assert w.sessions.live_count == 1
            assert await w.sessions.reap() == 1
            assert w.sessions.live_count == 0
            # The context is gone, so the session no longer resolves.
            after = await w.execute({"action": "browser_inspect",
                                     "browser_session_id": obs.browser_session_id,
                                     "tenant_id": "t", "user_id": "u", "arguments": {}})
            assert after.error is ErrorCode.SESSION_NOT_FOUND
        finally:
            await w.stop()

    async def test_absolute_ttl_is_reaped_despite_activity(self, require_chromium):
        w = BrowserWorker(limits=Limits(idle_ttl_s=3600, absolute_ttl_s=0,
                                        reap_interval_s=3600))
        await w.start()
        try:
            await w.execute({"action": "browser_open", "tenant_id": "t",
                             "user_id": "u", "arguments": {}})
            assert [r for _, r in w.sessions.expired()] == ["absolute_ttl"]
            assert await w.sessions.reap() == 1
        finally:
            await w.stop()

    async def test_close_is_idempotent(self, worker):
        obs = await worker.execute({"action": "browser_open", "tenant_id": "t",
                                    "user_id": "u", "arguments": {}})
        sid = obs.browser_session_id
        c = {"action": "browser_close", "browser_session_id": sid,
             "tenant_id": "t", "user_id": "u", "arguments": {}}
        assert (await worker.execute(c)).ok
        # Second close: the session is gone, so it is not found. Not a crash.
        assert (await worker.execute(c)).error is ErrorCode.SESSION_NOT_FOUND

    async def test_reaper_is_running(self, worker):
        assert worker.sessions.stats()["reaper_running"] is True

    async def test_stop_closes_every_context(self, require_chromium):
        w = BrowserWorker()
        await w.start()
        for i in range(3):
            await w.execute({"action": "browser_open", "tenant_id": "t",
                             "user_id": f"u{i}", "arguments": {}})
        assert w.sessions.live_count == 3
        await w.stop()
        assert w.sessions.live_count == 0, "a leaked context is a leaked browser"


@pytest.mark.browser
@pytest.mark.asyncio
class TestSessionOwnership:
    """§5.1/§5.2 — enforced from day one, not conditional on anything."""

    async def test_another_identity_cannot_use_the_session(self, worker):
        obs = await worker.execute({"action": "browser_open", "tenant_id": "t1",
                                    "user_id": "alice", "arguments": {}})
        sid = obs.browser_session_id
        for tenant, user in (("t1", "bob"), ("t2", "alice"), ("t2", "bob")):
            r = await worker.execute({"action": "browser_inspect",
                                      "browser_session_id": sid,
                                      "tenant_id": tenant, "user_id": user,
                                      "arguments": {}})
            assert not r.ok, f"{tenant}/{user} reached another identity's session"
            # SESSION_NOT_FOUND, not a distinguishable denial: a distinguishable
            # denial confirms the session exists (§5.1).
            assert r.error is ErrorCode.SESSION_NOT_FOUND

    async def test_a_nonexistent_session_is_indistinguishable_from_someone_elses(self, worker):
        real = await worker.execute({"action": "browser_open", "tenant_id": "t1",
                                     "user_id": "alice", "arguments": {}})
        mine = await worker.execute({"action": "browser_inspect",
                                     "browser_session_id": real.browser_session_id,
                                     "tenant_id": "t9", "user_id": "mallory",
                                     "arguments": {}})
        fake = await worker.execute({"action": "browser_inspect",
                                     "browser_session_id": "bs_does_not_exist",
                                     "tenant_id": "t9", "user_id": "mallory",
                                     "arguments": {}})
        assert mine.error is fake.error is ErrorCode.SESSION_NOT_FOUND
        assert mine.error_message == fake.error_message


# ══════════════════════════════════════════════════════════════════════════════
# Level 1 against the lab
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestHappyPath:
    async def test_full_flow_ends_at_confirmation_and_the_server_agrees(self, driver, lab_url):
        """The required happy path. Asserts against `/_test/submissions` — what the
        server actually received — not merely that a confirmation page rendered."""
        d = driver
        assert (await d.login()).url.endswith("/profile")
        assert (await d.through_profile()).url.endswith("/application")

        await d.fill_by_name("Full name", "Ada Lovelace")
        await d.fill_by_name("Email address", "ada@browser-lab.invalid")
        await d.fill_by_name("Telephone", "+971 4 555 0100")

        dept = await d.ref("Department")
        await d.run(Action.SELECT, element_ref=dept, value="ops")
        await d.fill_by_name("Earliest start date", "2026-09-01")
        pref = await d.ref("Email", role="radio")
        await d.run(Action.CHECK, element_ref=pref, checked=True)
        box = await d.ref("Application status updates")
        await d.run(Action.CHECK, element_ref=box, checked=True)

        final = await d.submit_by_name("Submit application")
        assert final.ok, final.error_message
        assert final.url.endswith("/confirmation"), final.url

        recorded = submissions(lab_url)
        assert len(recorded) == 1, recorded
        sub = recorded[0]
        assert sub["accepted"] is True
        f = sub["fields"]
        assert f["applicant_name"] == "Ada Lovelace"
        assert f["contact_email"] == "ada@browser-lab.invalid"
        assert f["department"] == "ops"
        assert f["start_date"] == "2026-09-01"
        assert f["contact_preference"] == "email"

    async def test_inspect_is_bounded_and_truncation_is_visible(self, driver):
        await driver.login()
        await driver.through_profile()
        obs = await driver.inspect(max_elements=5)
        assert obs.ok and len(obs.elements) == 5
        assert obs.element_total > 5
        assert obs.element_truncated is True, "truncation must be visible (§7.3)"

    async def test_awkward_fields_are_reachable_by_accessible_name(self, driver):
        """§10.3: the lab has fields with no test id, resolvable only via `for=` or
        `aria-labelledby`. If the inspector cannot name them, the ref indirection is
        not usable on a real site."""
        await driver.login()
        await driver.through_profile()
        obs = await driver.inspect()
        names = {e.accessible_name for e in obs.elements}
        assert "Telephone" in names        # aria-labelledby only
        assert "Earliest start date" in names
        assert sum(1 for n in names if n == "Reference") >= 1


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestSubmitRefusal:
    """§3.4, enforced worker-side so it is not bypassable."""

    async def test_click_refuses_the_application_submit_button(self, driver):
        d = driver
        await d.login(); await d.through_profile()
        ref = await d.ref("Submit application")
        obs = await d.run(Action.CLICK, element_ref=ref)
        assert not obs.ok
        # WRONG_TOOL_FOR_SUBMIT, not AUTHZ_DENIED: this is a routing correction that
        # names the tool which works, so it must not carry AUTHZ_DENIED's
        # "terminal, retrying is an escalation attempt" meaning.
        assert obs.error is ErrorCode.WRONG_TOOL_FOR_SUBMIT
        assert obs.error_detail["use_instead"] == "browser_submit"
        assert obs.terminal is False

    async def test_click_refuses_every_submit_control_on_the_application_page(self, driver):
        """Required: tested against EVERY button on /application."""
        d = driver
        await d.login(); await d.through_profile()
        obs = await d.inspect()
        buttons = [e for e in obs.elements if e.role == "button"]
        assert buttons, "no buttons found on /application"
        for b in buttons:
            r = await d.run(Action.CLICK, element_ref=b.ref)
            if b.type == "submit":
                assert r.error is ErrorCode.WRONG_TOOL_FOR_SUBMIT, \
                    f"{b.ref} {b.accessible_name}"
            # Non-submit buttons are permitted; navigation invalidates refs, so
            # re-inspect before the next candidate.
            obs = await d.inspect()

    async def test_click_refuses_the_disabled_then_enabled_submit(self, driver, lab_url):
        """The disabled_submit failure mode: the button starts disabled and script
        enables it once the form is valid. It must be refused by browser_click in
        BOTH states — a refusal that only holds while disabled would be an accident
        of the precondition check, not the §3.4 rule."""
        d = driver
        set_flags(lab_url, disabled_submit=True)
        await d.login(); await d.through_profile()

        ref = await d.ref("Submit application")
        first = await d.run(Action.CLICK, element_ref=ref)
        assert first.error is ErrorCode.WRONG_TOOL_FOR_SUBMIT, \
            "submit-like refusal must precede the disabled check"

        # Fill everything so the enabler script enables the button.
        await d.fill_by_name("Full name", "Ada Lovelace")
        await d.fill_by_name("Email address", "ada@browser-lab.invalid")
        dept = await d.ref("Department")
        await d.run(Action.SELECT, element_ref=dept, value="ops")
        await d.fill_by_name("Earliest start date", "2026-09-01")
        pref = await d.ref("Email", role="radio")
        await d.run(Action.CHECK, element_ref=pref, checked=True)

        obs = await d.inspect()
        btn = next(e for e in obs.elements if e.accessible_name == "Submit application")
        assert btn.state["disabled"] is False, "enabler script did not run"
        again = await d.run(Action.CLICK, element_ref=btn.ref)
        assert again.error is ErrorCode.WRONG_TOOL_FOR_SUBMIT, \
            "an enabled submit button is still a submit button"

    async def test_submit_refuses_a_non_submit_target(self, driver):
        """The partition holds in both directions."""
        d = driver
        await d.login(); await d.through_profile()
        field = await d.ref("Full name")
        obs = await d.run(Action.SUBMIT, element_ref=field)
        assert not obs.ok and obs.error is ErrorCode.BAD_REQUEST
        assert obs.error_detail["use_instead"] == "browser_click"

    async def test_a_submit_cannot_be_reached_without_inspecting(self, driver):
        """There is no path to a click that skips inspection, so a caller cannot
        avoid the classification by declining to inspect."""
        d = driver
        await d.goto("/login")
        obs = await d.run(Action.CLICK, element_ref="e1")
        assert not obs.ok
        assert obs.error in (ErrorCode.ELEMENT_NOT_FOUND, ErrorCode.STALE_REF)


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestStaleRefs:
    async def test_stale_ref_after_rerender_then_reinspect_recovers(self, driver):
        """Required: a stale ref produces STALE_REF, never a wrong-element action,
        and re-inspecting recovers."""
        d = driver
        await d.login(); await d.through_profile()
        obs = await d.inspect()
        old = next(e.ref for e in obs.elements if e.accessible_name == "Full name")

        # Re-render the same page: every old ref is dead even though the URL and
        # the element inventory are identical — which is the case a path-based
        # locator would silently get WRONG rather than reporting as stale.
        await d.goto("/application")
        stale = await d.run(Action.FILL, element_ref=old, value="x")
        assert stale.error is ErrorCode.STALE_REF, stale.error
        assert stale.terminal is False, "§9.1: STALE_REF is remap-and-retry"
        assert "re-inspect" in stale.recovery

        fresh = await d.ref("Full name")
        assert fresh is not None
        ok = await d.run(Action.FILL, element_ref=fresh, value="Recovered")
        assert ok.ok, ok.error_message

    async def test_refs_are_invalidated_by_navigation(self, driver):
        d = driver
        await d.login()
        before = await d.inspect()
        gen_before = d.w.sessions.resolve(
            d.browser_session_id, tenant_id=d.tenant_id, user_id=d.user_id).ref_generation
        await d.goto("/application")
        sess = d.w.sessions.resolve(d.browser_session_id, tenant_id=d.tenant_id,
                                    user_id=d.user_id)
        assert sess.ref_generation > gen_before
        assert sess.refs == {}, "ref map must be cleared on navigation"

    async def test_a_never_issued_ref_is_not_found(self, driver):
        obs = await driver.run(Action.CLICK, element_ref="e4242")
        assert obs.error in (ErrorCode.ELEMENT_NOT_FOUND, ErrorCode.STALE_REF)


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestFailureModes:
    """Each of the six lab failure modes individually, with its §9.1 recovery."""

    async def test_dependent_field(self, driver, lab_url):
        """A field that appears only after another is filled."""
        d = driver
        set_flags(lab_url, dependent_field=True)
        await d.login(); await d.through_profile()
        assert await d.ref("Current employer") is None, "employer block should be hidden"
        await d.fill_by_name("Telephone", "+971 4 555 0100")
        assert await d.ref("Current employer") is not None, \
            "employer block should appear once phone is non-empty"

    async def test_delayed_element(self, driver, lab_url):
        """An element that appears after a fixed delay. Recovery is browser_wait."""
        d = driver
        # Long enough that the panel cannot have arrived before the first inspect.
        # A short delay makes the "absent" assertion a race the test sometimes wins.
        set_flags(lab_url, delayed_element=True, delay_ms=2500)
        await d.login(); await d.through_profile()
        assert await d.ref("I confirm the information") is None

        # `element_appears` is the recovery for a delayed element: the thing being
        # waited for has no ref yet, so `element_visible` cannot express it.
        waited = await d.run(Action.WAIT, condition="element_appears", timeout_ms=8000)
        assert waited.ok, f"{waited.error} {waited.error_message}"
        found = await d.ref("I confirm the information")
        assert found, "consent panel never arrived"

    async def test_disabled_submit(self, driver, lab_url):
        """§9.1 ELEMENT_DISABLED: do not retry blindly; re-inspect for the unmet
        precondition. Asserted via browser_submit, since browser_click refuses the
        element on §3.4 grounds before ever reaching the disabled check."""
        d = driver
        set_flags(lab_url, disabled_submit=True)
        await d.login(); await d.through_profile()

        obs = await d.inspect()
        btn = next(e for e in obs.elements if e.accessible_name == "Submit application")
        assert btn.state["disabled"] is True
        blocked = await d.run(Action.SUBMIT, element_ref=btn.ref)
        assert blocked.error is ErrorCode.ELEMENT_DISABLED
        assert "precondition" in blocked.recovery

        await d.fill_by_name("Full name", "Ada Lovelace")
        await d.fill_by_name("Email address", "ada@browser-lab.invalid")
        dept = await d.ref("Department")
        await d.run(Action.SELECT, element_ref=dept, value="ops")
        await d.fill_by_name("Earliest start date", "2026-09-01")
        pref = await d.ref("Email", role="radio")
        await d.run(Action.CHECK, element_ref=pref, checked=True)
        ok = await d.submit_by_name("Submit application")
        assert ok.ok and ok.url.endswith("/confirmation"), ok.error_message

    async def test_server_reject_is_a_validation_signal_not_a_failure(self, driver, lab_url):
        """§9.1: VALIDATION_ERROR is an expected signal. Extract the message,
        correct the field, continue — and the rejected attempt is still recorded."""
        d = driver
        set_flags(lab_url, server_reject=True)
        await d.login(); await d.through_profile()
        await d.fill_by_name("Full name", "Ada Lovelace")
        await d.fill_by_name("Email address", "ada@browser-lab.invalid")
        await d.fill_by_name("Telephone", "+971 4 555 0100")
        dept = await d.ref("Department")
        await d.run(Action.SELECT, element_ref=dept, value="ops")
        await d.fill_by_name("Earliest start date", "2026-09-01")
        pref = await d.ref("Email", role="radio")
        await d.run(Action.CHECK, element_ref=pref, checked=True)

        first = await d.submit_by_name("Submit application")
        assert first.ok is True, "a validation rejection is not a worker failure"
        assert first.error is ErrorCode.VALIDATION_ERROR
        assert first.error_message
        assert not first.url.endswith("/confirmation")

        msgs = await d.run(Action.EXTRACT, scope="errors")
        assert msgs.extracted, "the agent must be able to read the message"

        second = await d.submit_by_name("Submit application")
        assert second.url.endswith("/confirmation"), second.error_message

        recorded = submissions(lab_url)
        assert len(recorded) == 2, "the rejected attempt must be recorded too"
        assert recorded[0]["accepted"] is False
        assert recorded[1]["accepted"] is True

    async def test_confirm_dialog(self, driver, lab_url):
        """A confirmation step whose real submit needs the echoed token."""
        d = driver
        set_flags(lab_url, confirm_dialog=True)
        await d.login(); await d.through_profile()
        await d.fill_by_name("Full name", "Ada Lovelace")
        await d.fill_by_name("Email address", "ada@browser-lab.invalid")
        dept = await d.ref("Department")
        await d.run(Action.SELECT, element_ref=dept, value="ops")
        await d.fill_by_name("Earliest start date", "2026-09-01")
        pref = await d.ref("Email", role="radio")
        await d.run(Action.CHECK, element_ref=pref, checked=True)

        staged = await d.submit_by_name("Submit application")
        assert not staged.url.endswith("/confirmation"), "nothing may be submitted yet"
        assert await d.ref("Yes, submit") is not None

        accepted = await d.submit_by_name("Yes, submit")
        assert accepted.url.endswith("/confirmation"), accepted.error_message
        assert submissions(lab_url)[-1]["accepted"] is True

    async def test_unprompted_modal(self, driver, lab_url):
        """§9.1 UNEXPECTED_MODAL: inspect it, dismiss if benign. The dismiss button
        is type=button, so browser_click handles it — which is the whole reason
        §3.4 distinguishes click from submit."""
        d = driver
        set_flags(lab_url, unprompted_modal=True)
        await d.login()
        assert d.last.url.endswith("/profile")
        dismiss = await d.ref("Dismiss")
        assert dismiss is not None, "the modal should be present on /profile"
        obs = await d.run(Action.CLICK, element_ref=dismiss)
        assert obs.ok, obs.error_message
        assert await d.ref("Dismiss") is None, "modal should be gone"


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestUpload:
    async def test_upload_by_artifact_id_records_the_expected_sha256(self, driver, lab_url):
        """§11.4: artifact IDs only, never a filesystem path. The assertion is on
        the digest the SERVER recorded."""
        d = driver
        content = b"%PDF-1.4\nfake CV bytes for the lab\n"
        expected = hashlib.sha256(content).hexdigest()
        register_artifact("art-cv-1", filename="cv.pdf", content=content,
                          content_type="application/pdf")

        await d.login(); await d.through_profile()
        await d.fill_by_name("Full name", "Ada Lovelace")
        await d.fill_by_name("Email address", "ada@browser-lab.invalid")
        dept = await d.ref("Department")
        await d.run(Action.SELECT, element_ref=dept, value="ops")
        await d.fill_by_name("Earliest start date", "2026-09-01")
        pref = await d.ref("Email", role="radio")
        await d.run(Action.CHECK, element_ref=pref, checked=True)

        cv = await d.ref("Attach your CV")
        up = await d.run(Action.UPLOAD, element_ref=cv, artifact_id="art-cv-1")
        assert up.ok, up.error_message
        assert up.extracted["sha256"] == expected

        final = await d.submit_by_name("Submit application")
        assert final.url.endswith("/confirmation"), final.error_message

        files = submissions(lab_url)[-1]["files"]
        assert len(files) == 1
        assert files[0]["filename"] == "cv.pdf"
        assert files[0]["sha256"] == expected, "the server received different bytes"

    async def test_unknown_artifact_is_refused(self, driver):
        d = driver
        await d.login(); await d.through_profile()
        cv = await d.ref("Attach your CV")
        obs = await d.run(Action.UPLOAD, element_ref=cv, artifact_id="no-such-artifact")
        assert not obs.ok and obs.error is ErrorCode.ELEMENT_NOT_FOUND


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestBudgetsTerminateCleanly:
    """Required: every budget terminates cleanly with partial progress, not a crash."""

    async def _fresh(self, lab_url, **budget_kw):
        w = BrowserWorker(budgets=Budgets(**budget_kw))
        await w.start()
        reset_lab(lab_url)
        d = Driver(w, lab_url)
        assert (await d.open()).ok
        return w, d

    async def test_action_budget(self, require_chromium, lab_url):
        w, d = await self._fresh(lab_url, max_actions_per_task=3)
        try:
            results = [await d.goto("/login") for _ in range(5)]
            assert any(r.ok for r in results), "no partial progress was made"
            last = results[-1]
            assert last.error is ErrorCode.BUDGET_EXCEEDED
            assert last.error_detail["budget"] == "max_actions_per_task"
            assert last.terminal is True
            assert last.error_detail["actions_taken"] == 3
            # Still a live worker: a budget stop is terminal for the TASK, not a crash.
            assert w.sessions.live_count == 1
        finally:
            await w.stop()

    async def test_navigation_budget(self, require_chromium, lab_url):
        w, d = await self._fresh(lab_url, max_actions_per_task=100,
                                 max_navigations_per_task=2)
        try:
            await d.goto("/login"); await d.goto("/careers")
            third = await d.goto("/login")
            assert third.error is ErrorCode.BUDGET_EXCEEDED
            assert third.error_detail["budget"] == "max_navigations_per_task"
            # Non-navigating actions still work — partial progress is usable.
            assert (await d.inspect()).ok
        finally:
            await w.stop()

    async def test_wall_clock_budget(self, require_chromium, lab_url):
        w, d = await self._fresh(lab_url, task_wall_clock_ms=1)
        try:
            await asyncio.sleep(0.05)
            obs = await d.goto("/login")
            assert obs.error is ErrorCode.BUDGET_EXCEEDED
            assert obs.error_detail["budget"] == "task_wall_clock_ms"
            assert "actions_taken" in obs.error_detail, "partial progress must be reported"
        finally:
            await w.stop()

    async def test_inspect_cap_is_enforced_worker_side(self, require_chromium, lab_url):
        """A caller asking for more than the cap gets the cap, not what it asked
        for — §9.2 is enforced server-side, never by convention."""
        w, d = await self._fresh(lab_url, max_inspect_elements=4)
        try:
            await d.goto("/application")
            obs = await d.inspect(max_elements=10_000)
            assert len(obs.elements) == 4
            assert obs.element_truncated is True
        finally:
            await w.stop()

    async def test_budgets_are_reported_on_every_observation(self, driver):
        obs = await driver.goto("/login")
        assert set(obs.budgets) >= {"actions", "navigations", "wall_clock_ms"}


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestSessionIsolation:
    """Required: two sessions cannot see each other's cookies or refs."""

    async def test_cookies_do_not_leak_between_sessions(self, worker, lab_url):
        reset_lab(lab_url)
        alice = Driver(worker, lab_url, tenant_id="t1", user_id="alice")
        bob = Driver(worker, lab_url, tenant_id="t2", user_id="bob")
        assert (await alice.open()).ok
        assert (await bob.open()).ok

        assert (await alice.login()).url.endswith("/profile")

        # Bob's context has never authenticated. The lab bounces an unauthenticated
        # /profile back to /login — which is only true if the cookie jars are separate.
        obs = await bob.goto("/profile")
        assert obs.url.endswith("/login"), \
            f"bob reached {obs.url} — cookie jar leaked across sessions"

    async def test_refs_do_not_leak_between_sessions(self, worker, lab_url):
        reset_lab(lab_url)
        alice = Driver(worker, lab_url, tenant_id="t1", user_id="alice")
        bob = Driver(worker, lab_url, tenant_id="t2", user_id="bob")
        await alice.open(); await bob.open()

        await alice.goto("/login")
        a_obs = await alice.inspect()
        a_ref = a_obs.elements[0].ref

        await bob.goto("/login")
        # Bob has not inspected, so a ref that is perfectly valid in alice's session
        # must not resolve in his.
        obs = await bob.run(Action.CLICK, element_ref=a_ref)
        assert not obs.ok
        assert obs.error in (ErrorCode.ELEMENT_NOT_FOUND, ErrorCode.STALE_REF)

    async def test_a_session_cannot_be_driven_with_another_identitys_credentials(
            self, worker, lab_url):
        reset_lab(lab_url)
        alice = Driver(worker, lab_url, tenant_id="t1", user_id="alice")
        await alice.open()
        await alice.goto("/login")
        obs = await worker.execute({
            "action": "browser_inspect",
            "browser_session_id": alice.browser_session_id,
            "tenant_id": "t2", "user_id": "bob", "arguments": {}})
        assert obs.error is ErrorCode.SESSION_NOT_FOUND


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestScreenshots:
    async def test_screenshot_returns_a_reference_never_bytes(self, driver):
        d = driver
        await d.goto("/login")
        await d.inspect()
        obs = await d.run(Action.SCREENSHOT)
        assert obs.ok, obs.error_message
        assert obs.screenshot_ref and obs.screenshot_ref.startswith("shot_")
        blob = obs.as_dict()
        assert "png" not in blob and "image" not in blob
        # No base64 payload smuggled into extracted.
        assert isinstance(obs.extracted, dict) and "bytes" in obs.extracted
        assert isinstance(obs.extracted["bytes"], int)

    async def test_password_fields_are_masked_before_encoding(self, driver):
        d = driver
        await d.goto("/login")
        obs = await d.inspect()
        pw = next(e for e in obs.elements if e.type == "password")
        await d.run(Action.FILL, element_ref=pw.ref, value="not-a-real-password",
                    sensitive=True)
        shot = await d.run(Action.SCREENSHOT)
        assert shot.ok
        assert shot.extracted["masked_fields"] >= 1, "no mask was applied"

    async def test_a_screenshot_is_not_readable_by_another_identity(self, driver, worker):
        d = driver
        await d.goto("/login")
        obs = await d.run(Action.SCREENSHOT)
        with pytest.raises(WorkerError) as ei:
            worker.screenshots.get(obs.screenshot_ref, tenant_id="t9", user_id="mallory")
        assert ei.value.code is ErrorCode.SESSION_NOT_FOUND

    async def test_closing_a_session_drops_its_screenshots(self, driver, worker):
        d = driver
        await d.goto("/login")
        await d.run(Action.SCREENSHOT)
        assert worker.screenshots.count >= 1
        await d.run(Action.CLOSE)
        assert worker.screenshots.count == 0, \
            "a screenshot outliving its context is a longer-lived leak than the context"


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestSensitiveValues:
    async def test_fill_never_returns_a_sensitive_value(self, driver):
        """§7.2 — applies to fake lab credentials too; the habit is the control."""
        d = driver
        await d.goto("/login")
        obs = await d.inspect()
        pw = next(e for e in obs.elements if e.type == "password")
        r = await d.run(Action.FILL, element_ref=pw.ref, value="not-a-real-password")
        assert r.ok
        assert r.extracted["sensitive"] is True
        assert r.extracted["value"] is None
        assert r.extracted["length"] is None
        assert "not-a-real-password" not in str(r.as_dict())

    async def test_a_non_sensitive_field_reports_its_value(self, driver):
        d = driver
        await d.login(); await d.through_profile()
        ref = await d.ref("Full name")
        r = await d.run(Action.FILL, element_ref=ref, value="Ada Lovelace")
        assert r.extracted["value"] == "Ada Lovelace"


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestNavigationPolicy:
    async def test_a_domain_outside_the_allowlist_is_denied(self, worker, lab_url):
        reset_lab(lab_url)
        d = Driver(worker, lab_url)
        await d.open(allowed_domains=["127.0.0.1"])
        obs = await d.run(Action.NAVIGATE, url="http://example.com/")
        assert obs.error is ErrorCode.DOMAIN_DENIED
        assert obs.terminal is True

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
    ])
    async def test_non_http_schemes_are_denied(self, driver, url):
        obs = await driver.run(Action.NAVIGATE, url=url)
        assert obs.error is ErrorCode.DOMAIN_DENIED

    async def test_an_unreachable_host_is_navigation_failed(self, worker, lab_url):
        reset_lab(lab_url)
        d = Driver(worker, lab_url)
        await d.open(allowed_domains=[])          # no allowlist -> domain check off
        obs = await d.run(Action.NAVIGATE,
                          url="http://127.0.0.1:9/nothing-listens-here")
        assert obs.error is ErrorCode.NAVIGATION_FAILED
        assert obs.terminal is True
