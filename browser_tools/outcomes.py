"""Worker error code -> tool-layer outcome, recovery, and terminality.

This module exists because of one asymmetry the design creates deliberately and
this layer must then correct.

**The worker returns `VALIDATION_ERROR` with `ok=True`.** That is right at the
transport layer: §9.1 classes it "not a failure, expected signal", and a worker that
returned `ok=False` would make every caller's error branch treat a form needing
correction as something that went wrong.

**It is wrong at the tool layer.** `ok=True` is what an agent reads as "done". A
`browser_fill` whose value the server rejected is not done, and a result that reads
as done is how an agent proceeds to `browser_submit` on a form the server has
already refused. So the tool layer re-classifies: the transport's boolean becomes a
three-valued `Outcome`, and `VALIDATION_ERROR` gets its own value that is neither
success nor failure and cannot be mistaken for either.

The second job here is the four worker-boundary codes. `BAD_REQUEST`,
`SELECTOR_REJECTED`, `SESSION_NOT_FOUND` and `BUDGET_EXCEEDED` are not in §9.1's
table at all — they are refusals the worker makes about its own contract. None may
inherit a retry recovery: a caller that reaches one has a bug or is probing, and
retrying either is wrong.
"""
from __future__ import annotations

from enum import Enum


class Origin(str, Enum):
    """Which layer refused. Mirrors `playwright_worker.errors.Origin`.

    Duplicated rather than imported so `browser_tools` keeps its own vocabulary —
    the same reason the selector check is not shared. The two are asserted equal in
    the tests, which is a check that costs nothing and catches a rename.
    """

    TOOL = "tool"
    WORKER = "worker"


class Outcome(str, Enum):
    """What the agent should conclude. Three values, not two.

    `NEEDS_CORRECTION` is the one that matters. Collapsing it into either `OK` or
    `FAILED` loses the distinction §9.1 depends on: it is not success (the action
    did not achieve its goal) and it is not failure (the tool worked, the page
    disagreed, and the documented response is to fix the input and continue).
    """

    OK = "ok"
    NEEDS_CORRECTION = "needs_correction"
    FAILED = "failed"


#: §9.1's recovery column, verbatim in intent. The tool layer returns this on the
#: result so the agent reads policy from the contract rather than re-deriving it
#: from a prose message.
RECOVERY: dict[str, str] = {
    "ELEMENT_NOT_FOUND": "Re-inspect once with browser_inspect, retry with the new ref, then stop.",
    "STALE_REF": "Re-inspect with browser_inspect, remap the ref, retry. This does not count against element retries.",
    "ELEMENT_NOT_VISIBLE": "Scroll it into view, re-inspect, retry once.",
    "ELEMENT_DISABLED": "Do not retry blindly. Re-inspect and satisfy the unmet precondition first.",
    "TIMEOUT": "Retry once with a longer wait, then stop.",
    "NAVIGATION_FAILED": "Fail. Do not retry — the target is unreachable or the domain is not permitted.",
    "VALIDATION_ERROR": "Not a failure. Read the message, correct the named field, and continue.",
    "UNEXPECTED_MODAL": "Inspect the modal. Dismiss it with browser_click if benign, otherwise stop and report to the user.",
    "DOMAIN_DENIED": "Terminal. Never retry. This domain is not permitted for this session.",
    "AUTHZ_DENIED": "Stop. This action is refused. Do not retry — retrying a denial is an escalation attempt.",
    # A routing correction, not a denial: the tool that WILL work is named. Kept
    # out of TERMINAL for exactly that reason — see the set below.
    "WRONG_TOOL_FOR_SUBMIT": "Call browser_submit with the same element_ref. It is the only tool that submits, and it requires the user's approval.",
    # Worker-boundary codes. Every recovery is "fix the call" or "stop" — never
    # "try again", because the same call will be refused identically.
    "BAD_REQUEST": "Stop and fix the call. The arguments are malformed; retrying unchanged will be refused identically.",
    "SELECTOR_REJECTED": "Stop. This tool never accepts CSS, XPath, or code. Address elements only by the ref browser_inspect returned.",
    "SESSION_NOT_FOUND": "Stop. The browser session does not exist or is not yours. Open a new one with browser_open.",
    "BUDGET_EXCEEDED": "Stop and report partial progress. The budget for this task is spent; it does not refill.",
}

#: Codes after which retrying is always wrong. Superset of the worker's own
#: TERMINAL set: the worker marks transport-terminality, this marks
#: agent-terminality, and the two differ for `ELEMENT_NOT_FOUND` (recoverable once
#: via re-inspect) which is in neither.
TERMINAL: frozenset[str] = frozenset({
    "DOMAIN_DENIED", "AUTHZ_DENIED", "NAVIGATION_FAILED",
    "BAD_REQUEST", "SELECTOR_REJECTED", "SESSION_NOT_FOUND", "BUDGET_EXCEEDED",
})

#: Codes the agent may act on and continue. Explicit rather than "not terminal", so
#: a new code has to be classified rather than defaulting into the retryable set.
RETRYABLE: frozenset[str] = frozenset({
    "ELEMENT_NOT_FOUND", "STALE_REF", "ELEMENT_NOT_VISIBLE",
    "ELEMENT_DISABLED", "TIMEOUT", "UNEXPECTED_MODAL",
    # Not a retry of the SAME call — a redirect to a different tool. It lives here
    # rather than in TERMINAL because the agent should act, and rather than in
    # CORRECTABLE because nothing about the page needs correcting.
    "WRONG_TOOL_FOR_SUBMIT",
})

#: Neither terminal nor a retry — the page is talking back.
CORRECTABLE: frozenset[str] = frozenset({"VALIDATION_ERROR"})


def classify(error_code: str | None, *, worker_ok: bool) -> Outcome:
    """The re-classification. `worker_ok` is the transport's boolean.

    Order matters: VALIDATION_ERROR is checked BEFORE `worker_ok`, because that is
    precisely the case where the transport says True and the tool layer must not.
    """
    if error_code in CORRECTABLE:
        return Outcome.NEEDS_CORRECTION
    if error_code is None:
        return Outcome.OK if worker_ok else Outcome.FAILED
    return Outcome.FAILED


def recovery_for(error_code: str | None) -> str:
    if not error_code:
        return ""
    return RECOVERY.get(error_code, "Stop and report. This error has no documented recovery.")


def is_terminal(error_code: str | None) -> bool:
    return bool(error_code) and error_code in TERMINAL


def assert_taxonomy_complete() -> None:
    """Every worker code is classified exactly once.

    Called from the tests. A worker code that is in none of the three sets would
    silently get the "no documented recovery" fallback, which reads like a bug
    report to the agent and is a real one to us.
    """
    from playwright_worker.errors import ErrorCode

    known = {e.value for e in ErrorCode}
    classified = TERMINAL | RETRYABLE | CORRECTABLE
    missing = known - classified
    extra = classified - known
    overlap = ((TERMINAL & RETRYABLE) | (TERMINAL & CORRECTABLE)
               | (RETRYABLE & CORRECTABLE))
    if missing or extra or overlap:
        raise AssertionError(
            f"taxonomy drift — unclassified={sorted(missing)} "
            f"unknown={sorted(extra)} double-classified={sorted(overlap)}")
    no_recovery = known - set(RECOVERY)
    if no_recovery:
        raise AssertionError(f"no recovery documented for {sorted(no_recovery)}")
