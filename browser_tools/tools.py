"""The 14 tools: validate -> BrowserCommand -> worker -> ToolResult.

Each tool is a thin function. The thinking lives in three places it can be tested
independently — `schemas.py` (what the model may say), `outcomes.py` (what an error
means), `results.py` (what the model reads back) — and this module is the wiring.

Three rules hold for every tool here:

**Validation happens before the worker is called.** A malformed argument produces a
`ToolResult` without a round trip. That is not an optimisation: the worker is a
separate container, and a call that cannot possibly succeed should not consume a
session lock, an action budget, or a network hop. `BUDGET_EXCEEDED` on a call the
tool layer could have rejected would be a genuinely confusing failure.

**Ownership is passed, never ambient.** Every function takes `tenant_id`, `user_id`,
`agent_id`, `session_id` explicitly. This package does not import `TenantContext`
and does not know it exists; Phase D fills these fields at the registry handler.

**Sensitive values do not survive the call.** `browser_fill` records *that* a
sensitive field was set (§7.2), never what it was set to — including for the lab's
fake credentials, because the habit is the control.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .artifacts import ArtifactError, ArtifactState, get_artifact_store
from .client import WorkerGateway, get_gateway
from .outcomes import Origin, Outcome
from .results import ToolResult, from_observation
from .schemas import FORBIDDEN_PARAMS, SCHEMAS

log = logging.getLogger("browser_tools.tools")

_REF = re.compile(r"^e[0-9]{1,6}$")

#: Keyword arguments that are plumbing, not part of the model-facing schema. They
#: are excluded from schema validation; everything else a caller passes is checked.
_NOT_SCHEMA_ARGS = frozenset({"gateway", "store"})

#: Mirrors the worker's own rejection reasons so the tool layer can refuse
#: independently (requirement: both layers refuse on their own). Kept here rather
#: than imported from the worker on purpose — two layers sharing one constant is one
#: layer with an extra function call.
_SELECTOR_SHAPED = re.compile(
    r"^\s*(css|xpath|text|id|data-testid|role|link|placeholder|alt|title|nth)\s*="
    r"|^\s*(//|\.\.?/|\(//)"
    r"|[.#\[\]>+~:()*\s,\"']|::",
    re.I)


class ToolValidationError(Exception):
    """Refused before the worker. Carries the code the result will report."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ── validation ────────────────────────────────────────────────────────────────
def _reject(tool: str, code: str, message: str, recovery_hint: str = "") -> ToolResult:
    from .outcomes import is_terminal, recovery_for

    return ToolResult(
        tool=tool, outcome=Outcome.FAILED, summary="the call was refused before it ran",
        error_code=code, error_message=message,
        recovery=recovery_hint or recovery_for(code), terminal=is_terminal(code),
        # The worker never saw this call. Same code, different source — which is
        # exactly what `origin` exists to record (§11.6).
        origin=Origin.TOOL)


def _validate_ownership(**owner: str) -> None:
    missing = [k for k in ("tenant_id", "user_id", "agent_id", "session_id")
               if not str(owner.get(k) or "").strip()]
    if missing:
        raise ToolValidationError(
            "BAD_REQUEST",
            f"missing required ownership field(s): {', '.join(missing)}. "
            f"Every browser tool is scoped to a tenant and user.")


def _validate_ref(value: Any, *, param: str = "element_ref") -> str:
    """Refuse selector-shaped input at the SCHEMA layer.

    The worker refuses it too, for its own reasons. Both matter: the worker protects
    itself from any caller, and this protects the model from ever being told a
    selector is a thing it may send. A model that receives `SELECTOR_REJECTED` from
    two layers learns the same lesson twice; one that receives it from neither
    learns the wrong one.
    """
    if not isinstance(value, str):
        raise ToolValidationError("BAD_REQUEST",
                                  f"{param} must be a string, got {type(value).__name__}")
    if _SELECTOR_SHAPED.search(value):
        raise ToolValidationError(
            "SELECTOR_REJECTED",
            f"{param}={value!r} is selector-shaped. These tools never accept CSS, "
            f"XPath or text queries — use the opaque ref browser_inspect returned.")
    if not _REF.match(value):
        raise ToolValidationError(
            "SELECTOR_REJECTED",
            f"{param}={value!r} is not a ref from browser_inspect (expected 'e17').")
    return value


def _validate_against_schema(tool: str, args: dict) -> None:
    """Unknown and forbidden arguments, checked against the published schema.

    `additionalProperties: false` says this to the model; this enforces it, because
    a schema is a description and not a gate.
    """
    schema = SCHEMAS[tool]["parameters"]
    allowed = set(schema["properties"])
    supplied = set(args)
    forbidden = FORBIDDEN_PARAMS & supplied
    if forbidden:
        raise ToolValidationError(
            "SELECTOR_REJECTED",
            f"{tool} does not accept {sorted(forbidden)}. There is no selector, "
            f"code, path or credential channel in any browser tool.")
    unknown = supplied - allowed
    if unknown:
        raise ToolValidationError(
            "BAD_REQUEST",
            f"{tool} does not accept {sorted(unknown)}; allowed: {sorted(allowed)}")
    missing = set(schema.get("required", [])) - supplied
    if missing:
        raise ToolValidationError("BAD_REQUEST",
                                  f"{tool} requires {sorted(missing)}")


# ── the common path ───────────────────────────────────────────────────────────
async def _run(tool: str, *, tenant_id: str, user_id: str, agent_id: str,
               session_id: str, browser_session_id: str = "",
               worker_args: Optional[dict] = None, summary: str = "",
               detail: Optional[dict] = None,
               gateway: Optional[WorkerGateway] = None) -> ToolResult:
    """Every tool ends here, and every tool reaches the worker only through this."""
    gw = gateway or get_gateway()
    obs = await gw.call(action=tool, tenant_id=tenant_id, user_id=user_id,
                        agent_id=agent_id, session_id=session_id,
                        browser_session_id=browser_session_id,
                        arguments=worker_args or {})
    return from_observation(tool, obs, summary=summary, detail=detail)


def _guard(tool: str):
    """Decorator: schema validation, ownership, and typed refusal without a round trip."""
    def outer(fn):
        async def inner(*, tenant_id: str = "", user_id: str = "", agent_id: str = "",
                        session_id: str = "", gateway: Optional[WorkerGateway] = None,
                        **kwargs) -> ToolResult:
            try:
                _validate_ownership(tenant_id=tenant_id, user_id=user_id,
                                    agent_id=agent_id, session_id=session_id)
                # `store` is a test/Phase-D injection point, not a model-facing
                # argument. It must not be validated against the schema, or every
                # call that injects one is refused as an unknown parameter.
                supplied = {k: v for k, v in kwargs.items()
                            if v is not None and k not in _NOT_SCHEMA_ARGS}
                supplied.update(tenant_id=tenant_id, user_id=user_id,
                                agent_id=agent_id, session_id=session_id)
                _validate_against_schema(tool, supplied)
                if "element_ref" in kwargs and kwargs["element_ref"] is not None:
                    _validate_ref(kwargs["element_ref"])
                return await fn(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                                session_id=session_id, gateway=gateway, **kwargs)
            except ToolValidationError as e:
                log.info("%s refused before the worker: %s", tool, e)
                return _reject(tool, e.code, str(e))
        inner.__name__ = tool
        inner.tool_name = tool  # type: ignore[attr-defined]
        return inner
    return outer


# ══════════════════════════════════════════════════════════════════════════════
# Session
# ══════════════════════════════════════════════════════════════════════════════
@_guard("browser_open")
async def browser_open(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                       allowed_domains: Optional[list] = None,
                       gateway: Optional[WorkerGateway] = None) -> ToolResult:
    if allowed_domains is not None:
        if (not isinstance(allowed_domains, list)
                or not all(isinstance(d, str) for d in allowed_domains)):
            return _reject("browser_open", "BAD_REQUEST",
                           "allowed_domains must be a list of hostname strings")
    res = await _run("browser_open", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     worker_args={"allowed_domains": allowed_domains or []},
                     summary="browser session opened", gateway=gateway)
    if res.ok:
        res.summary = f"browser session opened ({res.browser_session_id})"
    return res


@_guard("browser_close")
async def browser_close(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                        browser_session_id: str,
                        gateway: Optional[WorkerGateway] = None) -> ToolResult:
    res = await _run("browser_close", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id,
                     summary="browser session closed", gateway=gateway)
    # Closing an already-closed session comes back SESSION_NOT_FOUND. The schema
    # calls this tool idempotent, so report the state the caller wanted rather than
    # an error they cannot act on — but keep the code for the audit trail.
    if res.error_code == "SESSION_NOT_FOUND":
        res.outcome = Outcome.OK
        res.summary = "browser session was already closed"
        res.recovery = ""
        res.terminal = False
    return res


# ══════════════════════════════════════════════════════════════════════════════
# Read
# ══════════════════════════════════════════════════════════════════════════════
@_guard("browser_navigate")
async def browser_navigate(*, tenant_id: str, user_id: str, agent_id: str,
                           session_id: str, browser_session_id: str, url: str,
                           gateway: Optional[WorkerGateway] = None) -> ToolResult:
    if not isinstance(url, str) or not url.strip():
        return _reject("browser_navigate", "BAD_REQUEST", "url must be a non-empty string")
    return await _run("browser_navigate", tenant_id=tenant_id, user_id=user_id,
                      agent_id=agent_id, session_id=session_id,
                      browser_session_id=browser_session_id,
                      worker_args={"url": url.strip()},
                      summary="page loaded; element refs from before are now stale",
                      gateway=gateway)


@_guard("browser_inspect")
async def browser_inspect(*, tenant_id: str, user_id: str, agent_id: str,
                          session_id: str, browser_session_id: str,
                          max_elements: Optional[int] = None,
                          gateway: Optional[WorkerGateway] = None) -> ToolResult:
    if max_elements is not None and (not isinstance(max_elements, int) or max_elements < 1):
        return _reject("browser_inspect", "BAD_REQUEST",
                       "max_elements must be a positive integer")
    args: dict = {}
    if max_elements is not None:
        args["max_elements"] = max_elements
    res = await _run("browser_inspect", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id, worker_args=args,
                     gateway=gateway)
    if res.outcome is not Outcome.FAILED:
        # §7.3: truncation must be visible, in words, not only as a boolean the
        # model has to notice.
        res.summary = (f"showing {len(res.elements)} of {res.element_total} elements"
                       if res.element_truncated
                       else f"{len(res.elements)} interactive element(s)")
    return res


@_guard("browser_extract")
async def browser_extract(*, tenant_id: str, user_id: str, agent_id: str,
                          session_id: str, browser_session_id: str,
                          element_ref: Optional[str] = None,
                          attribute: Optional[str] = None,
                          scope: Optional[str] = None,
                          gateway: Optional[WorkerGateway] = None) -> ToolResult:
    if scope is not None and scope not in ("text", "errors", "title"):
        return _reject("browser_extract", "BAD_REQUEST",
                       "scope must be one of: text, errors, title")
    if attribute is not None and not isinstance(attribute, str):
        return _reject("browser_extract", "BAD_REQUEST", "attribute must be a string")
    args: dict = {}
    if element_ref:
        args["element_ref"] = element_ref
    if attribute:
        args["attribute"] = attribute
    if scope:
        args["scope"] = scope
    res = await _run("browser_extract", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id, worker_args=args,
                     summary="extracted", gateway=gateway)
    return res


@_guard("browser_wait")
async def browser_wait(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                       browser_session_id: str, condition: str,
                       timeout_ms: Optional[int] = None,
                       element_ref: Optional[str] = None,
                       gateway: Optional[WorkerGateway] = None) -> ToolResult:
    allowed = ("load", "element_appears", "element_visible", "element_enabled")
    if condition not in allowed:
        return _reject("browser_wait", "BAD_REQUEST",
                       f"condition must be one of: {', '.join(allowed)}")
    if condition in ("element_visible", "element_enabled") and not element_ref:
        return _reject("browser_wait", "BAD_REQUEST",
                       f"{condition} requires element_ref")
    args: dict = {"condition": condition}
    if timeout_ms is not None:
        if not isinstance(timeout_ms, int) or timeout_ms < 100:
            return _reject("browser_wait", "BAD_REQUEST",
                           "timeout_ms must be an integer of at least 100")
        args["timeout_ms"] = timeout_ms
    if element_ref:
        args["element_ref"] = element_ref
    res = await _run("browser_wait", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id, worker_args=args,
                     summary=f"waited for {condition}", gateway=gateway)
    if res.ok and condition == "element_appears":
        res.summary = "a new element appeared; re-inspect to get its ref"
    return res


@_guard("browser_back")
async def browser_back(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                       browser_session_id: str,
                       gateway: Optional[WorkerGateway] = None) -> ToolResult:
    return await _run("browser_back", tenant_id=tenant_id, user_id=user_id,
                      agent_id=agent_id, session_id=session_id,
                      browser_session_id=browser_session_id,
                      summary="went back; element refs from before are now stale",
                      gateway=gateway)


@_guard("browser_screenshot")
async def browser_screenshot(*, tenant_id: str, user_id: str, agent_id: str,
                             session_id: str, browser_session_id: str,
                             full_page: Optional[bool] = None,
                             gateway: Optional[WorkerGateway] = None,
                             store=None) -> ToolResult:
    """Capture, then persist DURABLY, and return a durable reference.

    The worker's own ref lives in worker memory and dies with the process. §8.2(c)'s
    approval payload is reviewed minutes to hours after capture, so a worker-memory
    ref would be a broken image at exactly the moment a human is deciding whether to
    submit a form. This tool therefore does three things in order:

        capture (worker, masked)  ->  fetch bytes  ->  persist durably  ->  return ref

    The bytes exist in this process only between the fetch and the put. They never
    reach the `ToolResult` (§7.2), which carries an id and provenance only.

    A failure to persist is reported as a FAILED result rather than silently
    returning the volatile worker ref: a reference that looks durable and is not is
    worse than no screenshot, because nobody checks it until the approval.
    """
    args: dict = {}
    if full_page is not None:
        if not isinstance(full_page, bool):
            return _reject("browser_screenshot", "BAD_REQUEST", "full_page must be a boolean")
        args["full_page"] = full_page

    # The only tool that does not go through `_run`, because it needs the RAW
    # observation: the worker's volatile ref lives in `obs["screenshot_ref"]`, and
    # `from_observation` deliberately does not copy it into `ToolResult.artifact_ref`
    # — a worker-memory ref must never occupy the field that means "durable".
    # It still reaches the worker only through the gateway (requirement 6).
    gw = gateway or get_gateway()
    obs = await gw.call(action="browser_screenshot", tenant_id=tenant_id, user_id=user_id,
                        agent_id=agent_id, session_id=session_id,
                        browser_session_id=browser_session_id, arguments=args)
    masked = (obs.get("extracted") or {}).get("masked_fields") \
        if isinstance(obs.get("extracted"), dict) else None
    res = from_observation("browser_screenshot", obs, summary="screenshot captured",
                           detail={"masked_fields": masked})
    if res.outcome is Outcome.FAILED:
        return res

    return await _persist_screenshot(res, gw, worker_ref=obs.get("screenshot_ref"),
                                     tenant_id=tenant_id, user_id=user_id,
                                     session_id=session_id, store=store)


async def _persist_screenshot(res: ToolResult, gw: WorkerGateway, *, worker_ref: Any,
                              tenant_id: str, user_id: str, session_id: str,
                              store=None) -> ToolResult:
    """Fetch the captured bytes and write them to durable storage."""
    if not worker_ref:
        res.outcome = Outcome.FAILED
        res.summary = "the screenshot was captured but no reference came back"
        res.error_code = "TIMEOUT"
        res.error_message = "worker returned no screenshot reference"
        res.recovery = "Retry once; if it recurs, continue without the screenshot."
        return res
    try:
        png = await gw.fetch_screenshot(worker_ref, tenant_id=tenant_id, user_id=user_id)
    except Exception as e:  # noqa: BLE001
        log.warning("screenshot fetch failed: %s", type(e).__name__)
        res.outcome = Outcome.FAILED
        res.summary = "the screenshot was captured but could not be retrieved for storage"
        res.error_code = "TIMEOUT"
        res.error_message = f"could not read the captured image ({type(e).__name__})"
        res.recovery = "Retry once; if it recurs, continue without the screenshot."
        return res

    st = store or get_artifact_store()
    try:
        ref = st.put(data=png, kind="browser_screenshot", content_type="image/png",
                     tenant_id=tenant_id, user_id=user_id,
                     meta={"url": res.url, "title": res.title,
                           "session_id": session_id,
                           "masked_fields": res.detail.get("masked_fields")})
    except ArtifactError as e:  # noqa: BLE001
        res.outcome = Outcome.FAILED
        res.summary = "the screenshot could not be stored durably"
        res.error_code = "BAD_REQUEST"
        res.error_message = str(e)
        res.recovery = "Stop and report. A reference that is not durable is worse than none."
        return res

    res.artifact_ref = ref.artifact_id
    res.summary = (f"screenshot stored ({ref.byte_size} bytes, "
                   f"{res.detail.get('masked_fields') or 0} field(s) masked)")
    # The volatile worker ref does not travel further: in a payload it would be
    # indistinguishable from the durable one and it expires far sooner.
    res.detail["artifact"] = {"id": ref.artifact_id, "bytes": ref.byte_size,
                              "sha256": ref.sha256, "expires_at": ref.expires_at}
    return res


def fetch_screenshot(artifact_id: str, *, tenant_id: str, user_id: str,
                     store=None) -> tuple[Optional[bytes], ArtifactState]:
    """Read a stored screenshot back. Returns (bytes|None, state).

    The state is the point: a reviewer told only "not found" cannot tell whether the
    evidence never existed or has aged out, and those need different responses.
    """
    st = store or get_artifact_store()
    state = st.state(artifact_id, tenant_id=tenant_id, user_id=user_id)
    if state is not ArtifactState.AVAILABLE:
        return None, state
    return st.get(artifact_id, tenant_id=tenant_id, user_id=user_id), state


# ══════════════════════════════════════════════════════════════════════════════
# Write
# ══════════════════════════════════════════════════════════════════════════════
@_guard("browser_click")
async def browser_click(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                        browser_session_id: str, element_ref: str,
                        gateway: Optional[WorkerGateway] = None) -> ToolResult:
    res = await _run("browser_click", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id,
                     worker_args={"element_ref": element_ref},
                     summary="clicked; element refs from before are now stale",
                     gateway=gateway)
    # §3.4's refusal arrives as its own code now, so this only has to make the
    # summary readable. It used to rewrite AUTHZ_DENIED's terminality and recovery
    # here — patching a taxonomy error at the last layer, which meant the worker's
    # own observation and any §11.6 audit row still carried the wrong meaning.
    if res.error_code == "WRONG_TOOL_FOR_SUBMIT":
        res.summary = "that element submits the form, so browser_click will not press it"
        res.detail.setdefault("use_instead", "browser_submit")
        res.detail.setdefault("element_ref", element_ref)
    return res


@_guard("browser_fill")
async def browser_fill(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                       browser_session_id: str, element_ref: str, value: str,
                       sensitive: Optional[bool] = None,
                       gateway: Optional[WorkerGateway] = None) -> ToolResult:
    if not isinstance(value, str):
        return _reject("browser_fill", "BAD_REQUEST", "value must be a string")
    args: dict = {"element_ref": element_ref, "value": value}
    if sensitive is not None:
        if not isinstance(sensitive, bool):
            return _reject("browser_fill", "BAD_REQUEST", "sensitive must be a boolean")
        args["sensitive"] = sensitive

    res = await _run("browser_fill", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id, worker_args=args,
                     gateway=gateway)

    # §7.2. The worker already withholds a sensitive value; this is the second lock,
    # and it is the one that governs what enters agent state. `was_sensitive` comes
    # from the worker's own classification, so a field the model did not flag but the
    # page marked `type=password` is still protected.
    worker_said_sensitive = bool(res.detail.get("sensitive"))
    treat_sensitive = bool(sensitive) or worker_said_sensitive
    res.detail = {"element_ref": element_ref, "sensitive": treat_sensitive,
                  "value_recorded": False if treat_sensitive else True}
    if treat_sensitive:
        res.detail["value"] = None
        if res.outcome is Outcome.OK:
            res.summary = "value entered (sensitive — not recorded)"
    else:
        res.detail["value"] = value[:200]
        if res.outcome is Outcome.OK:
            res.summary = f"entered {value[:80]!r}"
    return res


@_guard("browser_select")
async def browser_select(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                         browser_session_id: str, element_ref: str, value: str,
                         gateway: Optional[WorkerGateway] = None) -> ToolResult:
    if not isinstance(value, str) or not value:
        return _reject("browser_select", "BAD_REQUEST", "value must be a non-empty string")
    res = await _run("browser_select", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id,
                     worker_args={"element_ref": element_ref, "value": value},
                     gateway=gateway)
    if res.ok:
        res.summary = f"selected {value!r}"
    return res


@_guard("browser_check")
async def browser_check(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                        browser_session_id: str, element_ref: str,
                        checked: Optional[bool] = None,
                        gateway: Optional[WorkerGateway] = None) -> ToolResult:
    if checked is not None and not isinstance(checked, bool):
        return _reject("browser_check", "BAD_REQUEST", "checked must be a boolean")
    want = True if checked is None else checked
    res = await _run("browser_check", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id,
                     worker_args={"element_ref": element_ref, "checked": want},
                     gateway=gateway)
    if res.ok:
        res.summary = "ticked" if want else "unticked"
    return res


@_guard("browser_upload")
async def browser_upload(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                         browser_session_id: str, element_ref: str, artifact_id: str,
                         gateway: Optional[WorkerGateway] = None) -> ToolResult:
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        return _reject("browser_upload", "BAD_REQUEST",
                       "artifact_id must be a non-empty string")
    # A path masquerading as an artifact id is the §11.4 threat. The schema has no
    # `path` parameter, so this is the remaining way one could arrive.
    if "/" in artifact_id or "\\" in artifact_id or artifact_id.startswith("."):
        return _reject("browser_upload", "SELECTOR_REJECTED",
                       f"artifact_id={artifact_id!r} looks like a filesystem path. "
                       f"Uploads take a staged artifact id, never a path.")
    res = await _run("browser_upload", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id,
                     worker_args={"element_ref": element_ref, "artifact_id": artifact_id},
                     gateway=gateway)
    if res.ok:
        res.summary = f"attached artifact {artifact_id}"
    return res


@_guard("browser_submit")
async def browser_submit(*, tenant_id: str, user_id: str, agent_id: str, session_id: str,
                         browser_session_id: str, element_ref: str,
                         gateway: Optional[WorkerGateway] = None) -> ToolResult:
    res = await _run("browser_submit", tenant_id=tenant_id, user_id=user_id,
                     agent_id=agent_id, session_id=session_id,
                     browser_session_id=browser_session_id,
                     worker_args={"element_ref": element_ref},
                     summary="form submitted", gateway=gateway)
    if res.error_code == "BAD_REQUEST" and res.detail.get("use_instead") == "browser_click":
        res.summary = "that element does not submit a form"
        res.recovery = "Use browser_click for this element."
    return res


#: Name -> callable. Phase D registers these; this package registers nothing.
TOOLS: dict[str, Any] = {
    "browser_open": browser_open,
    "browser_close": browser_close,
    "browser_navigate": browser_navigate,
    "browser_inspect": browser_inspect,
    "browser_screenshot": browser_screenshot,
    "browser_extract": browser_extract,
    "browser_wait": browser_wait,
    "browser_back": browser_back,
    "browser_click": browser_click,
    "browser_fill": browser_fill,
    "browser_select": browser_select,
    "browser_check": browser_check,
    "browser_upload": browser_upload,
    "browser_submit": browser_submit,
}
