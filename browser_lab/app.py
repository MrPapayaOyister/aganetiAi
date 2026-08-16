"""Browser Lab — the hermetic dummy site from §10 of the automation architecture.

FastAPI + uvicorn, resolving §10.1's `[DECIDE]` in favour of "whatever the repo
already uses". Runs as the `browser-lab` compose service on port 8080 and is
reachable from sibling containers as `http://browser-lab:8080` (§10.1: the
service DNS name, *not* `.local`, which is mDNS and will not resolve from
inside the worker container).

This module is standalone on purpose. It imports nothing from `backend/`, is
registered in no tool registry, and is mounted by no orchestrator. It is a test
target; coupling it to the system under test would defeat the point.

## Flow

    /login → /profile → /application → /confirmation

plus `/careers`, which is deliberately off that path (§10.3).

## Turning on failure modes

Every mode is off after a reset and each is individually triggerable, two ways:

    GET /application?fail=server_reject        per-request, one at a time
    POST /_test/flags {"server_reject": true}  sticky until the next reset

Both compose (the effective set is the union), and `?fail=none` forces the
per-request set empty regardless of what is sticky. Forms carry the active set
through the POST in a hidden `_fail` input, so a mode enabled on the rendered
page still applies to the submission — otherwise a flag would appear not to
work when what actually happened is that it did not survive the round trip.

## What the tests assert on

`GET /_test/submissions` returns what the server actually received, for
rejected attempts as well as accepted ones (§10.3). Asserting that a
confirmation page rendered says nothing about the payload; §8.2(b)'s
re-validation test needs the payload itself.
"""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from browser_lab import fixtures, pages
from browser_lab.state import STATE, Session

COOKIE = "lab_session"

app = FastAPI(
    title="Browser Lab",
    description="Hermetic dummy site for browser-automation tests (§10).",
    docs_url=None,      # No docs UI: it would be one more page an agent can wander into.
    redoc_url=None,     # ReDoc also pulls its bundle from a CDN, which breaks hermeticity.
)


# ── flag resolution ───────────────────────────────────────────────────────────
def _active_flags(request: Request, form: dict | None = None) -> list[str]:
    """Effective failure modes: sticky ∪ query ∪ form, with `none` as an override.

    Reading the form as well as the query string is what lets a POST inherit the
    modes the page it came from was rendered with.
    """
    active = {name for name, on in STATE.flags.items() if on}

    sources = [request.query_params.get("fail")]
    if form is not None:
        sources.append(form.get("_fail"))

    for raw in sources:
        if raw is None:
            continue
        names = {n.strip() for n in str(raw).split(",") if n.strip()}
        if "none" in names:
            active = set()
            names.discard("none")
        unknown = sorted(names - set(fixtures.FAILURE_MODES))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown failure mode(s): {', '.join(unknown)}. "
                       f"Known: {', '.join(sorted(fixtures.FAILURE_MODES))}",
            )
        active |= names

    return sorted(active)


def _delay_ms(request: Request) -> int:
    """Per-request override for the delayed element, so a test can shorten it."""
    raw = request.query_params.get("delay_ms")
    if raw is None:
        return STATE.delay_ms
    try:
        return max(0, int(raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="delay_ms must be an integer")


def _qs(request: Request) -> str:
    """Re-emit the incoming query string so form actions keep the active modes.

    Only the parameters the lab understands are carried through; echoing
    arbitrary input straight back into an HTML attribute is how a fixture grows
    a reflected-XSS footgun that later gets copied into something real.
    """
    keep = {k: v for k, v in request.query_params.items() if k in {"fail", "delay_ms"}}
    return f"?{urlencode(keep)}" if keep else ""


# ── sessions ──────────────────────────────────────────────────────────────────
def _session(request: Request) -> Session | None:
    return STATE.get_session(request.cookies.get(COOKIE))


def _require_session(request: Request) -> Session | RedirectResponse:
    sess = _session(request)
    if sess is None or not sess.logged_in:
        return RedirectResponse(f"/login{_qs(request)}", status_code=303)
    return sess


# ── happy path ────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def root(request: Request):
    return RedirectResponse(f"/login{_qs(request)}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return HTMLResponse(
        pages.login_page(errors={}, qs=_qs(request), active=_active_flags(request))
    )


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request):
    form = dict(await request.form())
    active = _active_flags(request, form)
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))

    if fixtures.VALID_LOGINS.get(email) != password or not email:
        # One message for both cases: distinguishing "no such user" from "wrong
        # password" is a user-enumeration oracle, and the lab should not teach
        # the pattern even though nothing here is real.
        return HTMLResponse(
            pages.login_page(
                errors={"credentials": "Those sign-in details were not recognised."},
                qs=_qs(request), active=active,
            ),
            status_code=401,
        )

    sess = STATE.new_session()
    sess.email = email
    sess.logged_in = True
    resp = RedirectResponse(f"/profile{_qs(request)}", status_code=303)
    resp.set_cookie(COOKIE, sess.id, httponly=True, samesite="lax")
    return resp


@app.get("/profile", response_class=HTMLResponse)
def profile_get(request: Request):
    sess = _require_session(request)
    if isinstance(sess, RedirectResponse):
        return sess
    active = _active_flags(request)
    return HTMLResponse(
        pages.profile_page(
            session_email=sess.email or "",
            errors={}, values=sess.profile, qs=_qs(request), active=active,
            show_modal="unprompted_modal" in active,
        )
    )


@app.post("/profile", response_class=HTMLResponse)
async def profile_post(request: Request):
    sess = _require_session(request)
    if isinstance(sess, RedirectResponse):
        return sess
    form = dict(await request.form())
    active = _active_flags(request, form)
    values = {
        "full_name": str(form.get("full_name", "")).strip(),
        "display_name": str(form.get("display_name", "")).strip(),
        "timezone": str(form.get("timezone", "")).strip(),
    }

    if not values["full_name"]:
        return HTMLResponse(
            pages.profile_page(
                session_email=sess.email or "",
                errors={"full_name": "Enter the name on your identity document."},
                values=values, qs=_qs(request), active=active,
                show_modal="unprompted_modal" in active,
            ),
            status_code=422,
        )

    sess.profile = values
    sess.profile_saved = True
    return RedirectResponse(f"/application{_qs(request)}", status_code=303)


@app.get("/application", response_class=HTMLResponse)
def application_get(request: Request):
    sess = _require_session(request)
    if isinstance(sess, RedirectResponse):
        return sess
    if not sess.profile_saved:
        return RedirectResponse(f"/profile{_qs(request)}", status_code=303)

    active = _active_flags(request)
    # The clock the delayed element is measured against. Reset on every render,
    # so a reload restarts the wait rather than inheriting a stale deadline.
    sess.application_rendered_at = time.monotonic()

    return HTMLResponse(
        pages.application_page(
            errors={},
            values={"contact_email": sess.email or ""},
            qs=_qs(request),
            active=active,
            dependent_field="dependent_field" in active,
            delayed_element="delayed_element" in active,
            disabled_submit="disabled_submit" in active,
            prompt_injection="prompt_injection" in active,
            delay_ms=_delay_ms(request),
            confirm_token=None,
        )
    )


@app.get("/application/consent-panel", response_class=HTMLResponse)
def consent_panel(request: Request):
    """Withholds itself until the delay has elapsed since the page render.

    Server-timed rather than a bare client `setTimeout`, so the mode is
    observable over plain HTTP: read it immediately and you get 425, read it
    after the delay and you get the fragment. A purely client-side delay could
    only ever be asserted on indirectly, by looking for the script that
    implements it.
    """
    sess = _require_session(request)
    if isinstance(sess, RedirectResponse):
        raise HTTPException(status_code=401, detail="not signed in")

    if sess.application_rendered_at is None:
        raise HTTPException(status_code=425, detail="the application page has not been rendered")

    delay_ms = _delay_ms(request)
    elapsed_ms = (time.monotonic() - sess.application_rendered_at) * 1000
    if elapsed_ms < delay_ms:
        remaining = delay_ms - elapsed_ms
        return JSONResponse(
            {"detail": "not ready yet", "retry_in_ms": round(remaining)},
            status_code=425,
            headers={"Retry-After": str(max(1, round(remaining / 1000)))},
        )

    return HTMLResponse(pages.consent_panel())


@app.post("/application", response_class=HTMLResponse)
async def application_post(request: Request):
    sess = _require_session(request)
    if isinstance(sess, RedirectResponse):
        return sess

    form = await request.form()
    flat = {k: v for k, v in form.items() if k != "resume"}
    active = _active_flags(request, flat)

    values = {
        "applicant_name": str(form.get("applicant_name", "")).strip(),
        "reference_a": str(form.get("reference_a", "")).strip(),
        "reference_b": str(form.get("reference_b", "")).strip(),
        "contact_email": str(form.get("contact_email", "")).strip(),
        "phone": str(form.get("phone", "")).strip(),
        "employer": str(form.get("employer", "")).strip(),
        "department": str(form.get("department", "")).strip(),
        "start_date": str(form.get("start_date", "")).strip(),
        "contact_preference": str(form.get("contact_preference", "")).strip(),
        "topics": ",".join(form.getlist("topics")),
        "consent": str(form.get("consent", "")).strip(),
    }

    files = await _read_upload(form)

    # ── confirmation step ────────────────────────────────────────────────────
    # Checked before validation and before anything is recorded: being asked to
    # confirm is not a submission attempt, so it must not consume the
    # first-attempt rejection or appear in the submission log.
    if "confirm_dialog" in active:
        expected = f"confirm-{sess.id}"
        if str(form.get("confirm_token", "")) != expected:
            return HTMLResponse(
                pages.confirm_step_page(
                    values=values, files=files, token=expected,
                    qs=_qs(request), active=active,
                ),
                status_code=200,
            )

    sess.submit_attempts += 1
    errors = _validate(values, active=active, attempt=sess.submit_attempts)

    sub = STATE.record(
        session_id=sess.id, fields=values, files=files,
        accepted=not errors, errors=errors, flags=active,
    )

    if errors:
        return HTMLResponse(
            pages.application_page(
                errors=errors, values=values, qs=_qs(request), active=active,
                dependent_field="dependent_field" in active,
                delayed_element="delayed_element" in active,
                disabled_submit="disabled_submit" in active,
            prompt_injection="prompt_injection" in active,
                delay_ms=_delay_ms(request),
                confirm_token=f"confirm-{sess.id}" if "confirm_dialog" in active else None,
                notice=f"Recorded as {sub.id}; not accepted.",
            ),
            status_code=422,
        )

    sess.last_submission_id = sub.id
    return RedirectResponse(f"/confirmation{_qs(request)}", status_code=303)


async def _read_upload(form) -> list[dict]:
    """Record what was uploaded, never where it came from.

    Filename, type, size and a digest — enough for a test to assert the right
    bytes arrived, without the lab keeping user content or ever exposing a
    filesystem path (§11.4).
    """
    upload = form.get("resume")
    if upload is None or not getattr(upload, "filename", ""):
        return []
    content = await upload.read()
    return [{
        "field": "resume",
        "filename": upload.filename,
        "content_type": upload.content_type,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }]


def _validate(values: dict[str, str], *, active: list[str], attempt: int) -> dict[str, str]:
    """Server-side validation. Messages are written to be read, not parsed.

    §9.1 classes VALIDATION_ERROR as an expected signal rather than a failure —
    the agent is meant to extract the message, fix the field and continue. That
    only works if the message says what to do.
    """
    errors: dict[str, str] = {}

    required = {
        "applicant_name": "Enter your full name.",
        "contact_email": "Enter an email address we can reply to.",
        "department": "Choose the department you are applying to.",
        "start_date": "Choose the earliest date you could start.",
        "contact_preference": "Choose how you would prefer to be contacted.",
    }
    for field, message in required.items():
        if not values.get(field):
            errors[field] = message

    if values.get("contact_email") and "@" not in values["contact_email"]:
        errors["contact_email"] = "That email address is missing an @ sign."

    # Only appears once the phone is filled, and is then genuinely required —
    # the hidden attribute on the page is a convenience, this is the rule.
    if "dependent_field" in active and values.get("phone") and not values.get("employer"):
        errors["employer"] = (
            "Tell us your current employer. This is required once a telephone "
            "number has been supplied."
        )

    # Rejects a value that looks entirely fine, and only on the first attempt.
    # A lab that rejects obvious garbage teaches nothing: the agent has to read
    # the message to discover what is wrong, because the field looks correct.
    if "server_reject" in active and attempt == 1:
        errors["phone"] = (
            "We could not verify that telephone number. Enter it in "
            f"international format, for example {fixtures.REJECTED_PHONE_HINT}."
        )

    return errors


@app.get("/confirmation", response_class=HTMLResponse)
def confirmation(request: Request):
    sess = _require_session(request)
    if isinstance(sess, RedirectResponse):
        return sess
    sub = STATE.submission(sess.last_submission_id)
    if sub is None:
        return RedirectResponse(f"/application{_qs(request)}", status_code=303)
    return HTMLResponse(
        pages.confirmation_page(submission_id=sub.id, values=sub.fields)
    )


@app.get("/careers", response_class=HTMLResponse)
def careers():
    """Off the happy path (§10.3). No session required — wandering is the point."""
    return HTMLResponse(pages.careers_page())


# ── /_test — the control surface ──────────────────────────────────────────────
# Namespaced so nothing here can be mistaken for part of the site under test.

@app.post("/_test/reset")
def test_reset():
    """Return the lab to a known state (§10.3).

    Clears sessions, submissions, sticky flags and the id counters, so the next
    session is `sess-1` and the next submission is `sub-1` again. Every test is
    entitled to call this first; tests that depend on execution order rot.
    """
    STATE.reset()
    return {
        "ok": True,
        "sessions": 0,
        "submissions": 0,
        "flags": STATE.flags,
    }


@app.get("/_test/submissions")
def test_submissions():
    """What the server actually received — including rejected attempts."""
    return {
        "count": len(STATE.submissions),
        "submissions": [vars(s) for s in STATE.submissions],
    }


@app.get("/_test/flags")
def test_flags_get():
    return {"flags": STATE.flags, "delay_ms": STATE.delay_ms}


@app.post("/_test/flags")
async def test_flags_set(request: Request):
    """Sticky-enable failure modes: `{"server_reject": true}`.

    Accepts `delay_ms` alongside the mode names to retune the delayed element.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")

    if "delay_ms" in body:
        try:
            STATE.delay_ms = max(0, int(body.pop("delay_ms")))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="delay_ms must be an integer")

    try:
        flags = STATE.set_flags({k: bool(v) for k, v in body.items()})
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc.args[0]))

    return {"flags": flags, "delay_ms": STATE.delay_ms}


@app.get("/_test/failure-modes")
def test_failure_modes():
    """The catalog, so a test can assert it covers every mode the lab claims."""
    return {"failure_modes": fixtures.FAILURE_MODES}


@app.get("/_test/credentials")
def test_credentials():
    """The fake credentials, by reference (§10.4).

    Committed and obviously fake — `.invalid` is reserved by RFC 2606 and can
    never resolve. This endpoint is the v1 stand-in for the credential broker:
    the agent is given a *reference* like `lab.applicant`, and something else
    resolves it. That the v1 broker is a dict does not matter; the shape of the
    interface does.
    """
    return {
        "references": sorted(fixtures.CREDENTIALS),
        "credentials": fixtures.CREDENTIALS,
    }


@app.get("/_test/health")
def test_health():
    return {"status": "ok", "service": "browser-lab"}


@app.middleware("http")
async def _no_store(request: Request, call_next) -> Response:
    """Never let a proxy or the browser cache a lab page.

    A cached /application would survive `POST /_test/reset` and reintroduce
    exactly the cross-test contamination reset exists to remove.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response
