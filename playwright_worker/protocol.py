"""`BrowserCommand` / `BrowserObservation` — the worker's entire surface (§6.2).

Two properties matter more than anything else here, and both are enforced by this
module rather than by convention downstream:

**1. The action set is closed.** `Action` is an enum of exactly the 14 semantic
actions in §3.2. A command naming anything else is `BAD_REQUEST` before a page is
touched. There is no `evaluate`, no `script`, no `code` — S2 in §11 is a property
of the type, not of a validation pass someone can forget to call.

**2. No selector ever crosses this boundary.** §3.3 makes `element_ref` the only
addressing mode; this module additionally *rejects selector-shaped input* so that a
caller cannot smuggle CSS or XPath through a field that happens to take a string.
The rejection is deliberately aggressive — see `_looks_like_a_selector`.

`arguments` is a closed per-action set too. An unknown key is `BAD_REQUEST` rather
than being ignored, because a silently-dropped argument is how a caller comes to
believe it constrained something it did not.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .errors import RECOVERY, TERMINAL, ErrorCode, Origin, WorkerError


class Action(str, Enum):
    """The 14 semantic actions of §3.2. Closed by construction."""

    # session
    OPEN = "browser_open"
    CLOSE = "browser_close"
    # read
    NAVIGATE = "browser_navigate"
    INSPECT = "browser_inspect"
    SCREENSHOT = "browser_screenshot"
    EXTRACT = "browser_extract"
    WAIT = "browser_wait"
    BACK = "browser_back"
    # write
    CLICK = "browser_click"
    FILL = "browser_fill"
    SELECT = "browser_select"
    CHECK = "browser_check"
    UPLOAD = "browser_upload"
    # the only tool that submits
    SUBMIT = "browser_submit"


#: Actions that do not operate on an existing session.
SESSIONLESS: frozenset[Action] = frozenset({Action.OPEN})

#: Actions that consume a navigation from `max_navigations_per_task`.
NAVIGATING: frozenset[Action] = frozenset({Action.NAVIGATE, Action.BACK})

#: Actions that address an element and therefore require `element_ref`.
ELEMENT_ACTIONS: frozenset[Action] = frozenset({
    Action.CLICK, Action.FILL, Action.SELECT, Action.CHECK,
    Action.UPLOAD, Action.SUBMIT,
})

#: Allowed argument keys per action. Anything else is BAD_REQUEST.
ALLOWED_ARGS: dict[Action, frozenset[str]] = {
    Action.OPEN:       frozenset({"allowed_domains", "viewport_width", "viewport_height"}),
    Action.CLOSE:      frozenset(),
    Action.NAVIGATE:   frozenset({"url"}),
    Action.INSPECT:    frozenset({"max_elements"}),
    Action.SCREENSHOT: frozenset({"full_page"}),
    Action.EXTRACT:    frozenset({"element_ref", "attribute", "scope"}),
    Action.WAIT:       frozenset({"condition", "timeout_ms", "element_ref"}),
    Action.BACK:       frozenset(),
    Action.CLICK:      frozenset({"element_ref"}),
    Action.FILL:       frozenset({"element_ref", "value", "sensitive"}),
    Action.SELECT:     frozenset({"element_ref", "value"}),
    Action.CHECK:      frozenset({"element_ref", "checked"}),
    Action.UPLOAD:     frozenset({"element_ref", "artifact_id"}),
    Action.SUBMIT:     frozenset({"element_ref"}),
}

#: Argument names that must NEVER exist on any action. Present as an explicit
#: denylist as well as an implicit one, so a future edit to ALLOWED_ARGS that adds
#: one of these fails loudly in `test_no_action_accepts_a_code_channel` rather than
#: quietly opening S2.
FORBIDDEN_ARGS: frozenset[str] = frozenset({
    "selector", "css", "xpath", "query", "locator", "path",
    "script", "code", "js", "javascript", "expression", "eval", "evaluate",
    "function", "fn", "handler", "source",
})


# ── selector-shaped input rejection (§3.3) ────────────────────────────────────
# The threat is not only a well-formed CSS selector. It is any string that Playwright
# would interpret as a locator if it ever reached one. Playwright's own selector
# engines include `css=`, `xpath=`, `text=`, `id=`, `data-testid=`, `//`, and a bare
# string beginning with `//` or `..`. Matching "looks like CSS" alone would miss
# most of those.
#
# This runs on element_ref and on nothing else. Values typed into fields are user
# data and may legitimately contain slashes, dots or hashes — rejecting those would
# make the worker unable to fill a URL field, which is a real form input.
_SELECTOR_ENGINE = re.compile(
    r"^\s*(css|xpath|text|id|data-testid|role|link|placeholder|alt|title|nth)\s*=",
    re.I)
_XPATH_SHAPED = re.compile(r"^\s*(//|\.\.?/|\(//)")
_CSS_SHAPED = re.compile(r"[.#\[\]>+~:()*\s,\"']|::")

#: A ref is issued by the inspector and looks like `e17`. Anything else is refused.
REF_PATTERN = re.compile(r"^e[0-9]{1,6}$")


def _looks_like_a_selector(value: str) -> str | None:
    """Return the reason this string is selector-shaped, or None.

    Deliberately conservative: `element_ref` has a tiny, fully-specified grammar
    (`REF_PATTERN`), so ANY departure from it is refused. The specific patterns
    below exist only to give a caller a useful reason rather than a bare "invalid".
    """
    if _SELECTOR_ENGINE.match(value):
        return "looks like a Playwright selector engine prefix"
    if _XPATH_SHAPED.match(value):
        return "looks like an XPath expression"
    if _CSS_SHAPED.search(value):
        return "looks like a CSS selector"
    return None


def validate_element_ref(value: Any) -> str:
    """The single chokepoint every element-addressing argument passes through."""
    if not isinstance(value, str):
        raise WorkerError(ErrorCode.BAD_REQUEST,
                          f"element_ref must be a string, got {type(value).__name__}")
    reason = _looks_like_a_selector(value)
    if reason is not None:
        raise WorkerError(
            ErrorCode.SELECTOR_REJECTED,
            f"element_ref {value!r} {reason}. This worker does not accept CSS or "
            f"XPath. Address elements by the opaque ref returned by browser_inspect.")
    if not REF_PATTERN.match(value):
        raise WorkerError(
            ErrorCode.SELECTOR_REJECTED,
            f"element_ref {value!r} is not a ref issued by browser_inspect "
            f"(expected the form 'e17').")
    return value


@dataclass(frozen=True)
class BrowserCommand:
    """One action. Frozen: a command is a record of what was asked, and a worker
    that mutates its own input cannot be reasoned about after the fact."""

    action: Action
    browser_session_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    #: Ownership, carried for the audit record and for the session-owner assertion
    #: (§5.1). The worker does NOT authorize on these — §6.2 is explicit that it
    #: trusts its caller — but it does assert that the session it is handed belongs
    #: to the identity presenting it, which is a different and cheaper check.
    tenant_id: str = ""
    user_id: str = ""
    agent_id: str = ""
    session_id: str = ""

    @staticmethod
    def parse(payload: dict) -> "BrowserCommand":
        """Build from an untrusted dict. Raises WorkerError, never ValueError."""
        if not isinstance(payload, dict):
            raise WorkerError(ErrorCode.BAD_REQUEST, "command must be an object")

        raw_action = payload.get("action")
        try:
            action = Action(raw_action)
        except ValueError:
            raise WorkerError(
                ErrorCode.BAD_REQUEST,
                f"unknown action {raw_action!r}; the action set is closed "
                f"({', '.join(a.value for a in Action)})") from None

        args = payload.get("arguments") or {}
        if not isinstance(args, dict):
            raise WorkerError(ErrorCode.BAD_REQUEST, "arguments must be an object")

        # Denylist first, so the reason names the actual problem rather than
        # "unexpected argument".
        for forbidden in FORBIDDEN_ARGS & set(args):
            raise WorkerError(
                ErrorCode.SELECTOR_REJECTED,
                f"argument {forbidden!r} is never accepted by this worker. "
                f"There is no code, script or selector channel (§11 S2/S3).")

        unknown = set(args) - ALLOWED_ARGS[action]
        if unknown:
            raise WorkerError(
                ErrorCode.BAD_REQUEST,
                f"{action.value} does not accept {sorted(unknown)}; "
                f"allowed: {sorted(ALLOWED_ARGS[action])}")

        if action in ELEMENT_ACTIONS:
            if "element_ref" not in args:
                raise WorkerError(ErrorCode.BAD_REQUEST,
                                  f"{action.value} requires element_ref")
        if "element_ref" in args:
            validate_element_ref(args["element_ref"])

        return BrowserCommand(
            action=action,
            browser_session_id=str(payload.get("browser_session_id") or ""),
            arguments=dict(args),
            tenant_id=str(payload.get("tenant_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            agent_id=str(payload.get("agent_id") or ""),
            session_id=str(payload.get("session_id") or ""),
        )


@dataclass(frozen=True)
class ElementView:
    """One interactive element as the agent sees it (§3.3): role, name, state — and
    a ref. Never a selector, never raw DOM."""

    ref: str
    role: str
    accessible_name: str
    label: str
    type: str
    state: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BrowserObservation:
    """What the worker returns (§6.2). Everything bounded, nothing raw."""

    ok: bool
    action: str
    url: str = ""
    title: str = ""
    elements: list[ElementView] = field(default_factory=list)
    #: Truncation must be VISIBLE (§7.3): the agent needs to know it is looking at
    #: 60 of 214 so it can narrow scope rather than operate on a partial view
    #: believing it is complete.
    element_total: int = 0
    element_truncated: bool = False
    extracted: Any = None
    error: ErrorCode | None = None
    error_message: str = ""
    error_detail: dict = field(default_factory=dict)
    recovery: str = ""
    terminal: bool = False
    #: WHERE the outcome was produced. Always "worker" on an observation — the
    #: worker cannot report on a refusal that never reached it. The field exists
    #: here so the tool layer can carry one uniform shape rather than inventing
    #: the concept at its own boundary, and so a §11.6 audit row reads the same
    #: whichever layer refused (see errors.Origin).
    origin: Origin = Origin.WORKER
    duration_ms: int = 0
    browser_session_id: str = ""
    #: A REFERENCE, never bytes (§7.2, and the task's step 5).
    screenshot_ref: str | None = None
    budgets: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def failure(action: str, err: WorkerError, *, duration_ms: int = 0,
                browser_session_id: str = "", url: str = "") -> "BrowserObservation":
        return BrowserObservation(
            ok=False, action=action, url=url, error=err.code,
            error_message=err.message, error_detail=dict(err.detail),
            recovery=RECOVERY.get(err.code, ""),
            terminal=err.code in TERMINAL,
            duration_ms=duration_ms, browser_session_id=browser_session_id,
            origin=Origin.WORKER,
        )

    def as_dict(self) -> dict:
        d = asdict(self)
        d["error"] = self.error.value if self.error else None
        d["origin"] = self.origin.value
        d["elements"] = [e.as_dict() for e in self.elements]
        return d
