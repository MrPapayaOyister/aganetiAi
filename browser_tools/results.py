"""`ToolResult` — what the model actually reads, and the §7.2 redaction that shapes it.

The worker's `BrowserObservation` is a transport record. This is the agent-facing
one, and it differs in three ways that matter:

**1. Three-valued outcome, not a boolean** (see `outcomes.py`). A validation
rejection is neither success nor failure and must not be reported as either.

**2. Redacted by construction.** §7.2 forbids cookies, `storageState`, tokens,
passwords, raw DOM, raw HTML, unredacted screenshots and file contents from ever
entering state. `ToolResult` has no field that could carry any of them, and
`_scrub` is a second pass over whatever a tool put in `detail` — belt and braces,
because the field that leaks is always the one nobody thought about.

**3. A recovery sentence, always.** §9.1 documents a recovery per error; the agent
should read it from the result rather than infer it from a message. A result whose
outcome is not OK and which carries no recovery is a bug, asserted in the tests.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .outcomes import Origin, Outcome, is_terminal, recovery_for

#: Keys that must never appear in a result's `detail`, whatever a tool puts there.
#: Matched case-insensitively against the key path, so `{"page": {"cookie": ...}}`
#: is caught as well as a top-level `cookie`.
_FORBIDDEN_KEYS = re.compile(
    r"cookie|storage_?state|token|password|passphrase|secret|credential|"
    r"authorization|session_cookie|raw_html|inner_html|outer_html|dom|"
    r"screenshot_bytes|image_bytes|png|b64|base64",
    re.I)

#: A value that looks like an inline image, whatever it is called. The cheapest
#: reliable tell is a data: URL or a long base64 run; both are refused outright
#: rather than truncated, because a truncated image is still an image in state.
_INLINE_IMAGE = re.compile(r"^data:image/|^iVBORw0KGgo|^/9j/", re.I)

REDACTED = "[redacted]"
MAX_TEXT = 4000


def _scrub(value: Any, *, depth: int = 0) -> Any:
    """Recursively remove anything §7.2 forbids.

    Applied to every result's `detail` on construction. It is not the primary
    control — the primary control is that tools do not put these things in — but a
    second pass costs nothing and catches the field added in six months by someone
    who has not read §7.2.
    """
    if depth > 6:
        return REDACTED
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _FORBIDDEN_KEYS.search(str(k)):
                out[str(k)] = REDACTED
            else:
                out[str(k)] = _scrub(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth=depth + 1) for v in value][:100]
    if isinstance(value, str):
        if _INLINE_IMAGE.match(value.strip()):
            return REDACTED
        return value[:MAX_TEXT]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_TEXT]


@dataclass
class ElementSummary:
    """One element as the model sees it (§3.3). Refs, roles, names, state — never
    a selector and never markup."""

    ref: str
    role: str
    name: str
    type: str
    disabled: bool = False
    checked: Optional[bool] = None
    has_value: bool = False

    @staticmethod
    def from_worker(e: dict) -> "ElementSummary":
        state = e.get("state") or {}
        return ElementSummary(
            ref=e.get("ref", ""), role=e.get("role", ""),
            name=(e.get("accessible_name") or e.get("label") or "")[:200],
            type=e.get("type", ""),
            disabled=bool(state.get("disabled")),
            checked=state.get("checked"),
            has_value=bool(state.get("value_present")),
        )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolResult:
    """The agent-facing record of one tool call."""

    tool: str
    outcome: Outcome
    #: One sentence the model can act on without parsing anything else.
    summary: str = ""
    url: str = ""
    title: str = ""
    elements: list[ElementSummary] = field(default_factory=list)
    element_total: int = 0
    element_truncated: bool = False
    #: Set whenever outcome is not OK. Never empty for a non-OK result.
    error_code: Optional[str] = None
    error_message: str = ""
    recovery: str = ""
    terminal: bool = False
    #: WHERE this outcome was produced — "tool" for a refusal made before the worker
    #: was called, "worker" for anything the worker returned. The CODE is the reason;
    #: this is the source. A §11.6 audit row needs both, and without it
    #: BAD_REQUEST/SELECTOR_REJECTED cannot say whether the semantic layer's
    #: validation caught the call or missed it.
    origin: Origin = Origin.WORKER
    #: The field the page complained about, when it named one (§9.1 VALIDATION_ERROR
    #: recovery: "correct the field" — which requires knowing which).
    field_errors: dict[str, str] = field(default_factory=dict)
    browser_session_id: str = ""
    #: Durable reference, never bytes (requirement 5, §7.2).
    artifact_ref: Optional[str] = None
    detail: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    duration_ms: int = 0

    def __post_init__(self) -> None:
        self.detail = _scrub(self.detail)
        self.summary = str(self.summary)[:MAX_TEXT]
        self.error_message = str(self.error_message)[:MAX_TEXT]

    @property
    def ok(self) -> bool:
        """True ONLY for a clean action.

        Deliberately not `outcome is not FAILED`: a validation rejection must not
        satisfy `if result.ok`, which is the single most likely way an agent would
        conclude a rejected form was submitted.
        """
        return self.outcome is Outcome.OK

    @property
    def needs_correction(self) -> bool:
        return self.outcome is Outcome.NEEDS_CORRECTION

    def as_dict(self) -> dict:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        d["origin"] = self.origin.value
        d["elements"] = [e.as_dict() for e in self.elements]
        d["ok"] = self.ok
        return d

    def for_model(self) -> str:
        """The rendering an agent reads. Deliberately opens with the outcome word,
        because the first token is what a model conditions on most strongly."""
        head = {Outcome.OK: "OK", Outcome.NEEDS_CORRECTION: "NEEDS CORRECTION",
                Outcome.FAILED: "FAILED"}[self.outcome]
        lines = [f"{head} — {self.tool}: {self.summary}"]
        if self.url:
            lines.append(f"page: {self.title or '(untitled)'} <{self.url}>")
        if self.field_errors:
            lines.append("the page rejected these fields:")
            lines += [f"  - {k}: {v}" for k, v in list(self.field_errors.items())[:10]]
        elif self.error_message and self.outcome is not Outcome.OK:
            lines.append(f"reason: {self.error_message}")
        if self.elements:
            shown = f"{len(self.elements)} of {self.element_total}" \
                if self.element_truncated else str(len(self.elements))
            lines.append(f"elements ({shown}):")
            for e in self.elements:
                bits = [e.ref, e.role, repr(e.name)]
                if e.disabled:
                    bits.append("disabled")
                if e.checked is not None:
                    bits.append(f"checked={e.checked}")
                lines.append("  " + " | ".join(bits))
            if self.element_truncated:
                lines.append(f"  … {self.element_total - len(self.elements)} more not shown; "
                             f"narrow the page or scroll")
        if self.artifact_ref:
            lines.append(f"screenshot: {self.artifact_ref} (reference — fetch separately)")
        if self.recovery:
            lines.append(f"what to do: {self.recovery}")
        return "\n".join(lines)


def from_observation(tool: str, obs: dict, *, summary: str = "",
                     detail: Optional[dict] = None,
                     artifact_ref: Optional[str] = None) -> ToolResult:
    """Translate a worker observation into a tool result.

    The single place the transport's `ok` is re-interpreted. Every tool goes through
    here so the re-classification cannot be applied inconsistently.
    """
    from .outcomes import classify

    code = obs.get("error")
    outcome = classify(code, worker_ok=bool(obs.get("ok")))
    elements = [ElementSummary.from_worker(e) for e in (obs.get("elements") or [])]

    # A tool's `summary` is written for the SUCCESS case — "form submitted", "page
    # loaded". Using it on a non-OK outcome produces a line that contradicts its own
    # header ("NEEDS CORRECTION — browser_submit: form submitted"), which is exactly
    # the reading requirement 3 exists to prevent. The header alone is not enough:
    # the summary is the sentence a model is most likely to quote back.
    effective_summary = summary if (summary and outcome is Outcome.OK) else \
        _default_summary(tool, outcome, obs)

    # The worker's own structured fields, carried through so tools can act on them
    # (`use_instead` on a submit refusal, `sensitive` on a fill). Merged UNDER the
    # caller's detail so a tool can override, and scrubbed by ToolResult.__post_init__
    # like anything else.
    merged: dict = {}
    if isinstance(obs.get("error_detail"), dict):
        merged.update(obs["error_detail"])
    if isinstance(obs.get("extracted"), dict):
        merged.update(obs["extracted"])
    merged.update(detail or {})

    result = ToolResult(
        tool=tool, outcome=outcome,
        summary=effective_summary,
        url=obs.get("url") or "", title=obs.get("title") or "",
        elements=elements,
        element_total=int(obs.get("element_total") or len(elements)),
        element_truncated=bool(obs.get("element_truncated")),
        error_code=code, error_message=obs.get("error_message") or "",
        recovery=recovery_for(code), terminal=is_terminal(code),
        browser_session_id=obs.get("browser_session_id") or "",
        artifact_ref=artifact_ref,
        # An observation, by definition, came from the worker. A tool-layer refusal
        # never builds one — it goes through tools._reject.
        origin=Origin(obs.get("origin") or Origin.WORKER.value),
        detail=merged,
        budgets=dict(obs.get("budgets") or {}),
        duration_ms=int(obs.get("duration_ms") or 0),
    )
    if outcome is Outcome.NEEDS_CORRECTION:
        result.field_errors = _parse_field_errors(obs)
    return result


def _default_summary(tool: str, outcome: Outcome, obs: dict) -> str:
    if outcome is Outcome.OK:
        return "done"
    if outcome is Outcome.NEEDS_CORRECTION:
        return "the page rejected this input and is showing a validation message"
    return obs.get("error_message") or "the action did not succeed"


def _parse_field_errors(obs: dict) -> dict[str, str]:
    """Pull `field: message` pairs out of the page's validation messages.

    §9.1's recovery for VALIDATION_ERROR is "correct the field", which needs the
    field named. The lab renders `<strong>{field}</strong>: {message}`, so the
    messages arrive as "field: message"; anything that does not split is kept under
    a `_page` key rather than dropped, because an unparsed complaint is still a
    complaint the agent must see.
    """
    detail = obs.get("error_detail") or {}
    messages = detail.get("messages")
    if not messages:
        raw = obs.get("error_message") or ""
        messages = [m.strip() for m in raw.split(";") if m.strip()]
    out: dict[str, str] = {}
    for i, m in enumerate(messages[:10]):
        text = str(m).strip()
        if ":" in text:
            field_name, _, msg = text.partition(":")
            field_name, msg = field_name.strip(), msg.strip()
            if field_name and msg and len(field_name) <= 64:
                out[field_name] = msg[:400]
                continue
        out[f"_page{'' if i == 0 else i}"] = text[:400]
    return out
