"""Browser Lab acceptance tests — docs/automation-architecture.md §10.

These hit the lab over HTTP and assert on **what the server received**, not on
whether a page rendered. §10.3's fourth bullet is the whole reason the
submission log exists, and §8.2(b)'s re-validation test later depends on it.

**No Playwright.** The lab is the target, not the driver. Everything here is
plain HTTP, which is deliberate: the lab's own correctness must be verifiable
without the browser stack, otherwise a lab bug and a driver bug look identical.

**Two transports, same tests.** By default the app is exercised in-process over
an ASGI transport. Set `BROWSER_LAB_URL` to run the identical suite against a
live container:

    docker compose up -d browser-lab
    BROWSER_LAB_URL=http://localhost:8080 pytest tests/test_browser_lab.py

The second form is what proves the Docker service actually works; the first is
what makes the suite runnable in CI without a daemon.

**Order independence.** The `client` fixture resets before every test (§10.3).
No test may depend on another having run, and the id assertions below would
fail loudly if one did.
"""
from __future__ import annotations

import hashlib
import os
import re
import time

import pytest

from browser_lab import fixtures

BASE_URL = os.environ.get("BROWSER_LAB_URL")

# A complete, entirely plausible application. Individual tests mutate copies to
# isolate one failure mode at a time.
VALID_APPLICATION = {
    "applicant_name": "Dana Reyes",
    "reference_a": "REQ-2291",
    "reference_b": "Priya Raman",
    "contact_email": "demo@browser-lab.invalid",
    "phone": "+971501234567",
    "department": "ops",
    "start_date": "2026-09-01",
    "contact_preference": "email",
    "topics": ["status", "roles"],
}

CV_BYTES = b"%PDF-1.4 fake cv for the browser lab\n"
CV_SHA256 = hashlib.sha256(CV_BYTES).hexdigest()


@pytest.fixture
def client():
    """A logged-out client against a freshly reset lab."""
    if BASE_URL:
        import httpx

        c = httpx.Client(base_url=BASE_URL, timeout=15.0, follow_redirects=True)
    else:
        from fastapi.testclient import TestClient

        from browser_lab.app import app

        c = TestClient(app, follow_redirects=True)

    with c:
        assert c.post("/_test/reset").status_code == 200
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────
def sign_in(client, *, qs: str = "") -> None:
    creds = fixtures.CREDENTIALS["lab.applicant"]
    r = client.post(
        f"/login{qs}",
        data={"email": creds["email"], "password": creds["password"]},
    )
    assert r.status_code == 200, r.text


def reach_application(client, *, qs: str = "") -> str:
    """Walk /login → /profile → /application and return the application HTML."""
    sign_in(client, qs=qs)
    r = client.post(f"/profile{qs}", data={"full_name": "Dana Reyes", "timezone": "Asia/Dubai"})
    assert r.status_code == 200, r.text
    assert 'data-testid="application-form"' in r.text
    return r.text


def submit(client, *, qs: str = "", overrides: dict | None = None, with_file: bool = True,
           extra: dict | None = None):
    payload = {**VALID_APPLICATION, **(overrides or {}), **(extra or {})}
    payload = {k: v for k, v in payload.items() if v is not None}
    files = {"resume": ("cv.pdf", CV_BYTES, "application/pdf")} if with_file else None
    return client.post(f"/application{qs}", data=payload, files=files)


def submissions(client) -> list[dict]:
    r = client.get("/_test/submissions")
    assert r.status_code == 200
    return r.json()["submissions"]


# ── 1. reset (§10.3) ──────────────────────────────────────────────────────────
def test_reset_clears_submissions_sessions_and_flags(client):
    reach_application(client)
    submit(client)
    client.post("/_test/flags", json={"server_reject": True, "unprompted_modal": True})

    assert len(submissions(client)) == 1
    assert client.get("/_test/flags").json()["flags"]["server_reject"] is True

    body = client.post("/_test/reset").json()
    assert body["ok"] is True
    assert body["submissions"] == 0
    assert all(v is False for v in body["flags"].values())
    assert submissions(client) == []


def test_reset_restores_deterministic_ids(client):
    """Ids restart, so a failure reproduces under the same names every run."""
    reach_application(client)
    submit(client)
    assert [s["id"] for s in submissions(client)] == ["sub-1"]
    assert submissions(client)[0]["session_id"] == "sess-1"

    client.post("/_test/reset")
    reach_application(client)
    submit(client)
    assert [s["id"] for s in submissions(client)] == ["sub-1"]
    assert submissions(client)[0]["session_id"] == "sess-1"


def test_reset_invalidates_the_previous_session(client):
    """A stale cookie must not survive a reset into the next test."""
    sign_in(client)
    client.post("/profile", data={"full_name": "Dana Reyes"})
    client.post("/_test/reset")

    r = client.get("/application", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_reset_is_idempotent(client):
    for _ in range(3):
        assert client.post("/_test/reset").json()["submissions"] == 0


# ── 2. happy path and recorded submissions (§10.3) ────────────────────────────
def test_happy_path_records_exactly_what_was_sent(client):
    reach_application(client)
    r = submit(client)

    assert r.status_code == 200
    assert 'data-testid="submission-id"' in r.text

    recorded = submissions(client)
    assert len(recorded) == 1
    sub = recorded[0]

    assert sub["accepted"] is True
    assert sub["errors"] == {}
    # The payload itself — not merely that a confirmation page rendered.
    assert sub["fields"]["applicant_name"] == "Dana Reyes"
    assert sub["fields"]["contact_email"] == "demo@browser-lab.invalid"
    assert sub["fields"]["phone"] == "+971501234567"
    assert sub["fields"]["department"] == "ops"
    assert sub["fields"]["start_date"] == "2026-09-01"
    assert sub["fields"]["contact_preference"] == "email"
    assert sub["fields"]["topics"] == "status,roles"
    assert sub["fields"]["reference_a"] == "REQ-2291"
    assert sub["fields"]["reference_b"] == "Priya Raman"


def test_upload_is_recorded_by_digest_not_by_path(client):
    """Enough to prove the right bytes arrived; never a filesystem path (§11.4)."""
    reach_application(client)
    submit(client)

    files = submissions(client)[0]["files"]
    assert len(files) == 1
    assert files[0]["filename"] == "cv.pdf"
    assert files[0]["content_type"] == "application/pdf"
    assert files[0]["size"] == len(CV_BYTES)
    assert files[0]["sha256"] == CV_SHA256
    assert "path" not in files[0]
    assert "content" not in files[0]


def test_rejected_attempts_are_recorded_too(client):
    """"What did the agent try to send" is unanswerable if only successes log."""
    reach_application(client)
    r = submit(client, overrides={"applicant_name": ""})
    assert r.status_code == 422

    recorded = submissions(client)
    assert len(recorded) == 1
    assert recorded[0]["accepted"] is False
    assert "applicant_name" in recorded[0]["errors"]


def test_confirmation_page_reflects_the_stored_submission(client):
    reach_application(client)
    r = submit(client)
    sub_id = submissions(client)[0]["id"]
    assert sub_id in r.text
    assert "Dana Reyes" in r.text


# ── 3. credentials (§10.4) ────────────────────────────────────────────────────
def test_credentials_are_obviously_fake():
    for ref, cred in fixtures.CREDENTIALS.items():
        assert cred["email"].endswith("@browser-lab.invalid"), ref
        assert "not-a-real-password" in cred["password"], ref


def test_credentials_are_served_by_reference(client):
    body = client.get("/_test/credentials").json()
    assert "lab.applicant" in body["references"]
    assert body["credentials"]["lab.applicant"]["email"] == "demo@browser-lab.invalid"


def test_bad_credentials_are_refused_without_enumeration(client):
    wrong_pw = client.post(
        "/login", data={"email": "demo@browser-lab.invalid", "password": "nope"}
    )
    no_user = client.post(
        "/login", data={"email": "ghost@browser-lab.invalid", "password": "nope"}
    )
    assert wrong_pw.status_code == 401
    assert no_user.status_code == 401
    # Identical message: the lab should not model a user-enumeration oracle.
    assert "not recognised" in wrong_pw.text
    assert "not recognised" in no_user.text


# ── 4. mixed addressability (§10.3) ───────────────────────────────────────────
def test_password_field_has_no_testid_and_only_a_for_association(client):
    html = client.get("/login").text
    assert 'data-testid="login-email"' in html          # the easy one
    assert 'id="pw-field"' in html                       # the awkward one
    assert '<label for="pw-field">Pass phrase</label>' in html
    # No test id anywhere on the password input.
    pw = re.search(r"<input[^>]*id=\"pw-field\"[^>]*>", html).group(0)
    assert "data-testid" not in pw


def test_profile_has_two_fields_with_the_same_visible_label(client):
    sign_in(client)
    html = client.get("/profile").text
    assert html.count(">Name<") == 2
    display = re.search(r"<input[^>]*id=\"display-name\"[^>]*>", html, re.S).group(0)
    assert "data-testid" not in display
    assert 'aria-labelledby="display-name-lbl"' in display


def test_application_reference_labels_are_duplicated(client):
    html = reach_application(client)
    assert html.count(">Reference<") == 2
    ref_b = re.search(r"<input[^>]*id=\"reference-b\"[^>]*>", html, re.S).group(0)
    assert "data-testid" not in ref_b
    assert 'aria-labelledby="reference-b-lbl"' in ref_b


def test_phone_is_addressable_only_via_aria_labelledby(client):
    html = reach_application(client)
    phone = re.search(r"<input[^>]*id=\"phone\"[^>]*>", html, re.S).group(0)
    assert "data-testid" not in phone
    assert 'aria-labelledby="phone-lbl"' in phone
    # Nothing points at the phone input with for=; the aria hop is the only route.
    assert 'for="phone"' not in html


def test_radio_group_carries_no_test_ids(client):
    html = reach_application(client)
    for value, _label in fixtures.CONTACT_PREFERENCES:
        radio = re.search(rf"<input[^>]*id=\"pref-{value}\"[^>]*>", html, re.S).group(0)
        assert "data-testid" not in radio
        assert f'<label for="pref-{value}">' in html


def test_one_checkbox_of_three_has_no_test_id(client):
    html = reach_application(client)
    with_id = [
        v for v, _ in fixtures.NOTIFICATION_TOPICS
        if f'data-testid="app-topic-{v}"' in html
    ]
    assert len(with_id) == 2, "exactly one checkbox should be left bare"


def test_the_majority_of_controls_still_have_test_ids(client):
    """The lab is awkward on purpose, but not uniformly awkward.

    §10.3 asks for stable test ids *and* a subset without them. If this ratio
    ever inverts the lab has stopped being a realistic target and become a
    puzzle.
    """
    html = reach_application(client)
    controls = re.findall(r"<(?:input|select|button)\b[^>]*>", html, re.S)
    tagged = [c for c in controls if "data-testid" in c]
    assert len(tagged) > len(controls) - len(tagged) > 0


# ── 5. failure mode: dependent_field ──────────────────────────────────────────
def test_dependent_field_off_allows_a_phone_without_an_employer(client):
    reach_application(client)
    r = submit(client, overrides={"employer": ""})
    assert r.status_code == 200
    assert submissions(client)[0]["accepted"] is True


def test_dependent_field_on_hides_the_block_and_enforces_it(client):
    html = reach_application(client, qs="?fail=dependent_field")
    # Present in the DOM but hidden until the phone is filled.
    block = re.search(r"<div[^>]*id=\"employer-block\"[^>]*>", html, re.S).group(0)
    assert "hidden" in block
    assert 'data-requires="phone"' in block

    r = submit(client, qs="?fail=dependent_field", overrides={"employer": ""})
    assert r.status_code == 422
    assert "employer" in r.text
    assert submissions(client)[-1]["errors"]["employer"]


def test_dependent_field_accepts_once_the_employer_is_supplied(client):
    reach_application(client, qs="?fail=dependent_field")
    r = submit(client, qs="?fail=dependent_field", extra={"employer": "Meerana"})
    assert r.status_code == 200
    sub = submissions(client)[-1]
    assert sub["accepted"] is True
    assert sub["fields"]["employer"] == "Meerana"


# ── 6. failure mode: delayed_element ──────────────────────────────────────────
def test_delayed_element_is_absent_when_the_mode_is_off(client):
    html = reach_application(client)
    assert "consent-slot" not in html


def test_delayed_element_appears_only_after_the_delay(client):
    qs = "?fail=delayed_element&delay_ms=700"
    html = reach_application(client, qs=qs)

    # The panel is not in the initial HTML at all — only a slot and a deadline.
    assert 'data-testid="consent-slot"' in html
    assert 'data-appear-after-ms="700"' in html
    assert 'data-testid="consent-panel"' not in html

    early = client.get(f"/application/consent-panel{qs}")
    assert early.status_code == 425, "the panel must withhold itself before the delay"
    assert early.json()["retry_in_ms"] > 0

    time.sleep(0.9)
    late = client.get(f"/application/consent-panel{qs}")
    assert late.status_code == 200
    assert 'data-testid="consent-panel"' in late.text


def test_delayed_element_deadline_restarts_on_reload(client):
    qs = "?fail=delayed_element&delay_ms=700"
    reach_application(client, qs=qs)
    time.sleep(0.9)
    assert client.get(f"/application/consent-panel{qs}").status_code == 200

    client.get(f"/application{qs}")           # re-render restarts the clock
    assert client.get(f"/application/consent-panel{qs}").status_code == 425


# ── 7. failure mode: disabled_submit ──────────────────────────────────────────
def _submit_button(html: str) -> str:
    return re.search(r"<button[^>]*data-testid=\"app-submit\"[^>]*>", html, re.S).group(0)


def test_submit_button_is_enabled_when_the_mode_is_off(client):
    html = reach_application(client)
    assert "disabled" not in _submit_button(html)


def test_disabled_submit_renders_the_button_disabled_with_an_enabler(client):
    html = reach_application(client, qs="?fail=disabled_submit")
    button = _submit_button(html)
    assert "disabled" in button
    assert 'aria-disabled="true"' in button
    # The script that lifts it once the required fields are filled.
    assert "btn.disabled = !ok" in html


def test_the_server_never_trusts_the_disabled_attribute(client):
    """A disabled button is a hint to a human; the rule lives on the server."""
    reach_application(client, qs="?fail=disabled_submit")
    r = submit(
        client,
        qs="?fail=disabled_submit",
        overrides={"applicant_name": "", "department": "", "contact_preference": ""},
    )
    assert r.status_code == 422
    errors = submissions(client)[-1]["errors"]
    assert {"applicant_name", "department", "contact_preference"} <= set(errors)


# ── 8. failure mode: server_reject ────────────────────────────────────────────
def test_first_submit_is_accepted_when_the_mode_is_off(client):
    reach_application(client)
    assert submit(client).status_code == 200
    assert submissions(client)[0]["accepted"] is True


def test_server_reject_refuses_a_plausible_value_on_the_first_submit(client):
    reach_application(client, qs="?fail=server_reject")
    r = submit(client, qs="?fail=server_reject")

    assert r.status_code == 422
    sub = submissions(client)[-1]
    assert sub["accepted"] is False
    # The value looks entirely fine — only the message reveals the problem,
    # which is what makes this worth practising against (§9.1 VALIDATION_ERROR).
    assert sub["fields"]["phone"] == "+971501234567"
    message = sub["errors"]["phone"]
    assert "international format" in message
    assert fixtures.REJECTED_PHONE_HINT in message
    assert message in r.text, "the error must be readable on the page, not just in the log"


def test_server_reject_lets_the_second_attempt_through(client):
    reach_application(client, qs="?fail=server_reject")
    assert submit(client, qs="?fail=server_reject").status_code == 422
    r = submit(client, qs="?fail=server_reject", overrides={"phone": "+971-50-123-4567"})

    assert r.status_code == 200
    recorded = submissions(client)
    assert [s["accepted"] for s in recorded] == [False, True]
    assert recorded[1]["fields"]["phone"] == "+971-50-123-4567"


# ── 9. failure mode: confirm_dialog ───────────────────────────────────────────
def test_confirm_dialog_intercepts_the_submit_without_recording_it(client):
    reach_application(client, qs="?fail=confirm_dialog")
    r = submit(client, qs="?fail=confirm_dialog")

    assert r.status_code == 200
    assert 'data-testid="confirm-dialog"' in r.text
    assert 'data-testid="confirm-summary"' in r.text
    # Being asked to confirm is not a submission.
    assert submissions(client) == []


def test_confirm_dialog_summary_shows_what_would_be_sent(client):
    reach_application(client, qs="?fail=confirm_dialog")
    r = submit(client, qs="?fail=confirm_dialog")
    assert "Dana Reyes" in r.text
    assert "demo@browser-lab.invalid" in r.text
    assert "cv.pdf" in r.text


def test_confirm_dialog_cannot_be_skipped_by_posting_directly(client):
    reach_application(client, qs="?fail=confirm_dialog")
    r = submit(client, qs="?fail=confirm_dialog", extra={"confirm_token": "guessed"})
    assert 'data-testid="confirm-dialog"' in r.text
    assert submissions(client) == []


def test_confirm_dialog_accepts_with_the_echoed_token(client):
    reach_application(client, qs="?fail=confirm_dialog")
    dialog = submit(client, qs="?fail=confirm_dialog")
    token = re.search(r'name="confirm_token" value="([^"]+)"', dialog.text).group(1)

    r = submit(client, qs="?fail=confirm_dialog", extra={"confirm_token": token})
    assert r.status_code == 200
    assert 'data-testid="submission-id"' in r.text
    assert submissions(client)[-1]["accepted"] is True


# ── 10. failure mode: unprompted_modal ────────────────────────────────────────
def test_no_modal_when_the_mode_is_off(client):
    sign_in(client)
    assert 'data-testid="interstitial-modal"' not in client.get("/profile").text


def test_unprompted_modal_appears_on_profile_only(client):
    sign_in(client, qs="?fail=unprompted_modal")
    profile = client.get("/profile?fail=unprompted_modal")
    assert 'data-testid="interstitial-modal"' in profile.text
    assert 'data-testid="interstitial-dismiss"' in profile.text

    # Path-specific: an "unexpected modal" that is everywhere is just furniture.
    application = reach_application(client, qs="?fail=unprompted_modal")
    assert 'data-testid="interstitial-modal"' not in application
    assert 'data-testid="interstitial-modal"' not in client.get("/careers").text


# ── 11. a page outside the happy path (§10.3) ─────────────────────────────────
def test_careers_is_reachable_and_leads_nowhere_useful(client):
    r = client.get("/careers")
    assert r.status_code == 200
    assert "Not part of the application flow" in r.text
    # Linked from every page, so a navigation-limit test has somewhere to go.
    assert 'data-testid="nav-careers"' in client.get("/login").text
    assert "/careers?role=ops" in r.text


def test_careers_needs_no_session(client):
    """Wandering off the path must not be blocked by auth — the budget stops it."""
    assert client.get("/careers", follow_redirects=False).status_code == 200


# ── 12. flag plumbing ─────────────────────────────────────────────────────────
def test_every_declared_failure_mode_is_individually_triggerable(client):
    """Turning one on must not turn any other on.

    This is the property that lets a test isolate a single mode; without it the
    suite could only ever assert on the whole set at once.
    """
    reach_application(client)
    for mode in fixtures.FAILURE_MODES:
        client.post("/_test/reset")
        client.post("/_test/flags", json={mode: True})
        flags = client.get("/_test/flags").json()["flags"]
        assert flags[mode] is True, mode
        assert [k for k, v in flags.items() if v] == [mode], f"{mode} leaked into others"


def test_sticky_flags_survive_requests_but_not_reset(client):
    client.post("/_test/flags", json={"unprompted_modal": True})
    sign_in(client)
    assert 'data-testid="interstitial-modal"' in client.get("/profile").text

    client.post("/_test/reset")
    sign_in(client)
    assert 'data-testid="interstitial-modal"' not in client.get("/profile").text


def test_fail_none_overrides_a_sticky_flag(client):
    client.post("/_test/flags", json={"unprompted_modal": True})
    sign_in(client)
    assert 'data-testid="interstitial-modal"' not in client.get("/profile?fail=none").text


def test_unknown_failure_modes_are_rejected_not_ignored(client):
    """A typo'd flag that silently does nothing produces a green test that

    proves nothing, which is worse than a red one."""
    assert client.get("/application?fail=nonexistent_mode").status_code == 400
    assert client.post("/_test/flags", json={"nonexistent_mode": True}).status_code == 400


def test_the_catalog_matches_the_modes_the_tests_cover(client):
    declared = set(client.get("/_test/failure-modes").json()["failure_modes"])
    assert declared == {
        "dependent_field", "delayed_element", "disabled_submit",
        "server_reject", "confirm_dialog", "unprompted_modal",
    }
    assert declared == set(fixtures.FAILURE_MODES)


# ── 13. hermeticity (§10.1) ───────────────────────────────────────────────────
def test_no_page_references_an_external_origin(client):
    """The lab must work with egress fully blocked.

    Scanned rather than asserted by inspection, because a single pasted CDN
    link in a later edit would silently make the fixture non-hermetic and the
    failure would only show up as a flaky test on an air-gapped runner.
    """
    reach_application(client, qs="?fail=dependent_field,delayed_element,disabled_submit")
    documents = [
        client.get("/login").text,
        client.get("/profile").text,
        client.get("/application?fail=dependent_field,delayed_element,disabled_submit").text,
        client.get("/careers").text,
        submit(client, qs="?fail=confirm_dialog").text,
    ]

    for html in documents:
        urls = re.findall(r"""(?:src|href|action)\s*=\s*["']([^"']+)["']""", html)
        for url in urls:
            assert not url.startswith(("http://", "https://", "//")), f"external ref: {url}"
        assert "@import" not in html
        assert "<script src=" not in html
        assert "<link" not in html
        # Catches url(...) inside the inline stylesheet as well as stray literals.
        assert not re.search(r"https?://(?!127\.0\.0\.1|localhost)", html), "external URL"


def test_the_only_fetch_target_is_this_origin(client):
    html = reach_application(client, qs="?fail=delayed_element")
    assert 'data-src="/application/consent-panel"' in html
    for target in re.findall(r"fetch\(([^)]*)\)", html):
        assert "http" not in target, f"fetch reaches outside the lab: {target}"


# ── 14. service shape ─────────────────────────────────────────────────────────
def test_health_endpoint_identifies_the_service(client):
    body = client.get("/_test/health").json()
    assert body == {"status": "ok", "service": "browser-lab"}


def test_pages_are_never_cached(client):
    """A cached /application would survive a reset and undo it."""
    r = client.get("/login")
    assert "no-store" in r.headers["cache-control"]


def test_interactive_docs_are_not_exposed(client):
    """/docs pulls Swagger's bundle from a CDN, and is one more page to wander into."""
    assert client.get("/docs", follow_redirects=False).status_code == 404
    assert client.get("/redoc", follow_redirects=False).status_code == 404


def test_the_flow_is_gated_in_order(client):
    """/application is not reachable before /profile, which is not reachable
    before /login — so a reset genuinely returns to the start of the flow."""
    r = client.get("/application", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")

    sign_in(client)
    r = client.get("/application", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/profile")
