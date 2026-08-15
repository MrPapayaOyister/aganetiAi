"""Browser Lab fixtures — fake credentials, form vocabulary, failure-mode names.

Everything here is committed on purpose and is *obviously* fake (§10.4). The
`.invalid` TLD is reserved by RFC 2606 precisely so it can never resolve, which
makes it impossible for a lab credential to be mistaken for a live one or to
accidentally reach a real host.

These live in fixtures rather than in page templates so the worker's credential
broker (§15 of the source context) has a single dict to resolve a credential
*reference* against. v1's broker is a dict; the interface being right is what
matters. Nothing here should ever be inlined into a prompt or into agent state.
"""
from __future__ import annotations

# ── Credentials ───────────────────────────────────────────────────────────────
# Keyed by the reference the agent is allowed to see. The agent receives
# "lab.applicant", never the password.
CREDENTIALS: dict[str, dict[str, str]] = {
    "lab.applicant": {
        "email": "demo@browser-lab.invalid",
        "password": "not-a-real-password",
    },
    "lab.reviewer": {
        "email": "reviewer@browser-lab.invalid",
        "password": "also-not-a-real-password",
    },
}

VALID_LOGINS: dict[str, str] = {c["email"]: c["password"] for c in CREDENTIALS.values()}


# ── Form vocabulary ───────────────────────────────────────────────────────────
DEPARTMENTS: list[tuple[str, str]] = [
    ("", "— Select a department —"),
    ("ops", "Operations"),
    ("fin", "Finance"),
    ("legal", "Legal & Compliance"),
    ("eng", "Engineering"),
]

TIMEZONES: list[tuple[str, str]] = [
    ("", "— Select —"),
    ("Asia/Dubai", "Asia/Dubai (GST)"),
    ("Europe/London", "Europe/London"),
    ("America/New_York", "America/New_York"),
]

CONTACT_PREFERENCES: list[tuple[str, str]] = [
    ("email", "Email"),
    ("phone", "Telephone"),
    ("post", "Postal mail"),
]

NOTIFICATION_TOPICS: list[tuple[str, str]] = [
    ("status", "Application status updates"),
    ("roles", "Similar roles"),
    ("newsletter", "Monthly newsletter"),
]


# ── Failure modes (§10.3) ─────────────────────────────────────────────────────
# Each is individually triggerable, either per-request via ?fail=<name> or
# stickily via POST /_test/flags. Tests turn them on one at a time; a lab where
# they can only be enabled as a set cannot isolate which one a strategy handles.
FAILURE_MODES: dict[str, str] = {
    "dependent_field": (
        "The employer block is hidden until the phone field is non-empty, and "
        "the server rejects a submission that has a phone but no employer."
    ),
    "delayed_element": (
        "The consent panel is not in the initial HTML. It is fetched from "
        "/application/consent-panel, which returns 425 until DELAY_MS has "
        "elapsed since the page was rendered."
    ),
    "disabled_submit": (
        "The submit button renders with `disabled` and is only enabled by "
        "script once every required field is filled; the server independently "
        "rejects incomplete submissions."
    ),
    "server_reject": (
        "The first submit of a session is rejected server-side with a readable "
        "message about the phone number format. The value looks entirely "
        "plausible, so only the response body reveals the problem."
    ),
    "confirm_dialog": (
        "Submitting renders a confirmation step instead of confirming. The "
        "real submit requires the token echoed by that step."
    ),
    "unprompted_modal": (
        "An unrequested interstitial modal is rendered on /profile only."
    ),
}

# How long the delayed element withholds itself, in milliseconds. Short enough
# to keep the HTTP tests quick, long enough that a naive immediate read misses it.
DELAY_MS = 1200

# The phone value the server pretends to dislike on first submit. Deliberately
# well-formed: a lab that rejects obvious garbage tests nothing.
REJECTED_PHONE_HINT = "+971-50-123-4567"
