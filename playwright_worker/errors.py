"""The §9.1 error taxonomy, as a closed enum.

Every failure the worker can report is one of these. The point of a closed set is
that the agent's recovery policy is documented per type (§9.1) rather than left to
the model to improvise from a prose message — so a new failure mode must be
classified here deliberately, not appear as free text.

`VALIDATION_ERROR` is deliberately in the same enum as the genuine failures even
though §9.1 calls it "not a failure". It travels on the same channel because it is
still a *typed outcome the agent must branch on*; what makes it different is the
documented recovery ("extract the message, correct the field, continue"), and that
lives in `RECOVERY` below rather than in a separate return path. A separate happy
path for validation would mean two ways to learn the same thing.
"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    # ── §9.1 taxonomy ─────────────────────────────────────────────────────────
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    STALE_REF = "STALE_REF"
    ELEMENT_NOT_VISIBLE = "ELEMENT_NOT_VISIBLE"
    ELEMENT_DISABLED = "ELEMENT_DISABLED"
    TIMEOUT = "TIMEOUT"
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNEXPECTED_MODAL = "UNEXPECTED_MODAL"
    DOMAIN_DENIED = "DOMAIN_DENIED"
    AUTHZ_DENIED = "AUTHZ_DENIED"

    # ── routing refusal, outside §9.1 ─────────────────────────────────────────
    # §3.4: browser_click refuses a submit-like target and directs the agent to
    # browser_submit.
    #
    # This used to be reported as AUTHZ_DENIED, which was wrong in a way that
    # mattered twice. §9.1 defines AUTHZ_DENIED as terminal, never-retry, and
    # "retrying a denial is an escalation attempt" — so an agent obeying the
    # taxonomy literally abandons a task it could finish. And a §11.6 audit row
    # could not tell an actual escalation attempt from an agent correctly routing
    # itself to the approval path, which are opposite behaviours that should never
    # share a code.
    #
    # Named for the meaning: the agent used the wrong tool for a submit-like
    # target. It is a routing correction, not a denial.
    WRONG_TOOL_FOR_SUBMIT = "WRONG_TOOL_FOR_SUBMIT"

    # ── worker-boundary codes, outside §9.1 ───────────────────────────────────
    # §9.1 is the taxonomy the AGENT reasons about. These four are refusals the
    # worker makes about its own contract, before any page is touched. They are
    # named distinctly so they can never be mistaken for a page-interaction
    # outcome and, more importantly, so that none of them inherits a "retry"
    # recovery — a caller that reaches these has a bug or is probing, and
    # retrying either is wrong.
    #
    # The CODE is the reason. Which layer refused is carried separately, as
    # `origin` on the observation — see `Origin` below. BAD_REQUEST from the tool
    # layer and BAD_REQUEST from the worker mean the same thing about the call;
    # they differ only in where it was caught, and that difference belongs in a
    # field rather than in two near-identical codes.
    #
    # Reported separately in §6-contradictions: §6.2 says the observation's error
    # is "typed, from the §9.1 taxonomy", which is not sufficient for a worker
    # that must also reject malformed input.
    BAD_REQUEST = "BAD_REQUEST"            # malformed command / unknown action
    SELECTOR_REJECTED = "SELECTOR_REJECTED"  # selector-shaped input at the boundary
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class Origin(str, Enum):
    """Which layer produced an outcome.

    Not a second error taxonomy — the code says WHAT was wrong, this says WHERE it
    was caught. A §11.6 audit row needs both: `BAD_REQUEST/tool` is a call the
    semantic layer rejected without a round trip, `BAD_REQUEST/worker` is one that
    reached the worker and was rejected there. Those imply different fixes and
    different things about whether the tool layer's validation is working.
    """

    TOOL = "tool"
    WORKER = "worker"


#: Documented recovery per §9.1. Returned on the observation so the agent layer
#: (Phase F) reads policy from the worker rather than re-deriving it.
RECOVERY: dict[ErrorCode, str] = {
    ErrorCode.ELEMENT_NOT_FOUND: "re-inspect once, retry with the new ref, then fail",
    ErrorCode.STALE_REF: "re-inspect, remap, retry; does not count against element retries",
    ErrorCode.ELEMENT_NOT_VISIBLE: "scroll into view, re-inspect, retry once",
    ErrorCode.ELEMENT_DISABLED: "do not retry blindly; re-inspect for an unmet precondition",
    ErrorCode.TIMEOUT: "one retry with extended wait, then fail",
    ErrorCode.NAVIGATION_FAILED: "fail; do not retry",
    ErrorCode.VALIDATION_ERROR: "not a failure; extract the message, correct the field, continue",
    ErrorCode.UNEXPECTED_MODAL: "inspect the modal, dismiss if benign, otherwise fail to human",
    ErrorCode.DOMAIN_DENIED: "terminal; never retry",
    ErrorCode.AUTHZ_DENIED: "terminal; never retry",
    ErrorCode.WRONG_TOOL_FOR_SUBMIT: "not a denial; call browser_submit with the same element_ref",
    ErrorCode.BAD_REQUEST: "terminal; fix the command",
    ErrorCode.SELECTOR_REJECTED: "terminal; address elements by ref from browser_inspect",
    ErrorCode.SESSION_NOT_FOUND: "terminal; open a new session",
    ErrorCode.BUDGET_EXCEEDED: "terminal; report partial progress",
}

#: Codes that must never be retried, whatever the caller thinks. Kept as data so
#: the agent layer cannot disagree with the worker about what is terminal.
#: WRONG_TOOL_FOR_SUBMIT is deliberately ABSENT: it is the one refusal that names
#: the tool which will succeed, so treating it as terminal would abandon a task the
#: agent can complete. That is precisely the bug that overloading AUTHZ_DENIED caused.
TERMINAL: frozenset[ErrorCode] = frozenset({
    ErrorCode.DOMAIN_DENIED,
    ErrorCode.AUTHZ_DENIED,
    ErrorCode.NAVIGATION_FAILED,
    ErrorCode.BAD_REQUEST,
    ErrorCode.SELECTOR_REJECTED,
    ErrorCode.SESSION_NOT_FOUND,
    ErrorCode.BUDGET_EXCEEDED,
})


class WorkerError(Exception):
    """Internal control flow. Never escapes `execute()` — it is caught there and
    turned into a `BrowserObservation` with `ok=False`. A worker that raises at
    its RPC boundary would make every error untyped at exactly the layer the
    taxonomy exists to serve."""

    def __init__(self, code: ErrorCode, message: str = "", *, detail: dict | None = None):
        self.code = code
        self.message = message or code.value
        self.detail = detail or {}
        super().__init__(f"{code.value}: {self.message}")
