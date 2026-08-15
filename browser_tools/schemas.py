"""The 14 model-facing tool schemas (§3.1, §3.2).

These are the contract the model sees. Three properties are load-bearing and each
is asserted by a test that walks every schema, so a fifteenth tool cannot quietly
break one:

**1. snake_case names matching the worker's enum.** §3.1 resolved this: all 43
registered tools are snake_case, and there is no namespace or prefix concept to
hang `browser.open` on. The names here are compared against
`playwright_worker.protocol.Action` at import time — a mismatch is a startup error,
not a runtime surprise on the first call.

**2. `element_ref` only.** No `selector`, `css`, `xpath`, `script`, `code`, `js`,
`evaluate` or `path` parameter may exist in any schema (§3.3, §11 S2/S3). This is
checked here as a *schema* property, independently of the worker's own refusal —
two layers that refuse for their own reasons, because a single layer is one
refactor from being none.

**3. Ownership is explicit, never ambient.** Every schema carries `tenant_id`,
`user_id`, `agent_id`, `session_id` as required arguments. This package does not
know `TenantContext` exists and must not: Phase D wires it in by filling these
fields, and a package that reached for ambient identity would make that wiring
invisible and untestable.

`risk_level` is deliberately absent. §3.2 is explicit that it is derived from
`guardrails.TOOL_CATEGORY`, never author-supplied — writing one here would be the
same category error the design already had to correct once.
"""
from __future__ import annotations

from typing import Any

#: Argument names that must never appear in any schema. Superset of the worker's
#: FORBIDDEN_ARGS: the tool layer additionally bars names a model might reach for
#: even though the worker would not recognise them, because the schema is where the
#: model learns what is possible and an absent parameter is not attempted.
FORBIDDEN_PARAMS: frozenset[str] = frozenset({
    "selector", "css", "xpath", "query", "locator", "path", "file_path", "filename",
    "script", "code", "js", "javascript", "expression", "eval", "evaluate",
    "function", "fn", "handler", "source", "html", "dom", "inner_html",
    "cookie", "cookies", "storage_state", "storagestate", "token", "headers",
})

#: Ownership. Required on every tool (requirement 2). Not a context object — four
#: plain strings the caller must supply, so Phase D's wiring is a visible edit.
_OWNERSHIP: dict[str, Any] = {
    "tenant_id": {"type": "string", "description": "Tenant that owns this work."},
    "user_id": {"type": "string", "description": "User on whose behalf the action runs."},
    "agent_id": {"type": "string", "description": "Agent making the call."},
    "session_id": {"type": "string", "description": "Conversation/task session id."},
}
_OWNERSHIP_REQUIRED = ["tenant_id", "user_id", "agent_id", "session_id"]

_SESSION_PARAM: dict[str, Any] = {
    "browser_session_id": {
        "type": "string",
        "description": "The browser session returned by browser_open.",
    },
}

_ELEMENT_REF: dict[str, Any] = {
    "element_ref": {
        "type": "string",
        "pattern": r"^e[0-9]{1,6}$",
        "description": ("Opaque element reference from browser_inspect, e.g. 'e17'. "
                        "CSS selectors, XPath and text queries are never accepted."),
    },
}


def _schema(name: str, description: str, *, props: dict[str, Any],
            required: list[str], needs_session: bool = True) -> dict[str, Any]:
    """Assemble one schema. Ownership and session are added here rather than at each
    call site, so a new tool cannot forget them."""
    properties: dict[str, Any] = dict(_OWNERSHIP)
    if needs_session:
        properties.update(_SESSION_PARAM)
    properties.update(props)
    req = list(_OWNERSHIP_REQUIRED)
    if needs_session:
        req.append("browser_session_id")
    req += [r for r in required if r not in req]
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": req,
            # No unlisted argument is accepted. A silently-ignored parameter is how
            # a model comes to believe it constrained something it did not — and
            # how a `selector` key would survive a careless merge.
            "additionalProperties": False,
        },
    }


SCHEMAS: dict[str, dict[str, Any]] = {s["name"]: s for s in [
    # ── session ───────────────────────────────────────────────────────────────
    _schema(
        "browser_open",
        "Open a new isolated browser session and return its browser_session_id. "
        "Every other browser tool needs that id. Sessions are per-user, expire, and "
        "count against a per-user limit — close one when the task is done.",
        props={
            "allowed_domains": {
                "type": "array", "items": {"type": "string"},
                "description": "Hostnames this session may navigate to. Empty means no restriction.",
            },
        },
        required=[], needs_session=False,
    ),
    _schema(
        "browser_close",
        "Close a browser session and discard its cookies, page state and element "
        "references. Idempotent — closing an already-closed session is not an error "
        "worth retrying.",
        props={}, required=[],
    ),

    # ── read ──────────────────────────────────────────────────────────────────
    _schema(
        "browser_navigate",
        "Load a URL in the session. The URL must be http(s) and within the session's "
        "allowed domains. Navigating invalidates every element reference — inspect "
        "again afterwards.",
        props={"url": {"type": "string", "description": "Absolute http(s) URL."}},
        required=["url"],
    ),
    _schema(
        "browser_inspect",
        "List the interactive elements on the current page. This is how you see the "
        "page: it returns a bounded list of {ref, role, accessible_name, label, type, "
        "state}. Use the returned ref to act on an element. Call this again after any "
        "navigation, click or submit, because refs from before are stale.",
        props={
            "max_elements": {
                "type": "integer", "minimum": 1, "maximum": 60,
                "description": "Cap on returned elements (server maximum is 60).",
            },
        },
        required=[],
    ),
    _schema(
        "browser_screenshot",
        "Capture what the page looks like, with password and sensitive fields masked. "
        "Returns a durable reference, never the image itself. Use it as supporting "
        "evidence — browser_inspect, not this, is how you read the page.",
        props={
            "full_page": {"type": "boolean",
                          "description": "Capture the whole scrollable page rather than the viewport."},
        },
        required=[],
    ),
    _schema(
        "browser_extract",
        "Read text or an attribute from the page. With element_ref, reads that "
        "element; without, reads a whole scope. Use scope='errors' to read validation "
        "messages the page is showing. This does not execute JavaScript.",
        props={
            **_ELEMENT_REF,
            "attribute": {"type": "string",
                          "description": "Attribute to read instead of text, e.g. 'value' or 'href'."},
            "scope": {"type": "string", "enum": ["text", "errors", "title"],
                      "description": "What to read when no element_ref is given."},
        },
        required=[],
    ),
    _schema(
        "browser_wait",
        "Wait for a bounded condition. 'load' waits for the page to settle; "
        "'element_appears' waits for a NEW element to be added (use this when the "
        "page adds a field after a delay); 'element_visible' and 'element_enabled' "
        "wait on an element you already have a ref for.",
        props={
            "condition": {
                "type": "string",
                "enum": ["load", "element_appears", "element_visible", "element_enabled"],
                "description": "What to wait for.",
            },
            "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 15000,
                           "description": "Maximum wait. The server caps this."},
            **_ELEMENT_REF,
        },
        required=["condition"],
    ),
    _schema(
        "browser_back",
        "Go back one page in history. Counts against the navigation budget and "
        "invalidates every element reference.",
        props={}, required=[],
    ),

    # ── write ─────────────────────────────────────────────────────────────────
    _schema(
        "browser_click",
        "Click a non-submit element: a link, a checkbox, a dialog button. This tool "
        "REFUSES submit buttons — submitting a form is browser_submit, which requires "
        "human approval. If you get that refusal, use browser_submit.",
        props=dict(_ELEMENT_REF), required=["element_ref"],
    ),
    _schema(
        "browser_fill",
        "Type a value into a text input. Set sensitive=true for passwords and secrets: "
        "the value is then never recorded or returned, only the fact that the field "
        "was set.",
        props={
            **_ELEMENT_REF,
            "value": {"type": "string", "description": "Text to enter."},
            "sensitive": {"type": "boolean",
                          "description": "True for passwords/secrets. The value is never echoed back."},
        },
        required=["element_ref", "value"],
    ),
    _schema(
        "browser_select",
        "Choose an option in a dropdown (a <select>) by its underlying value, which "
        "browser_inspect reports — not by the label shown on screen, because the two "
        "often differ.",
        props={**_ELEMENT_REF,
               "value": {"type": "string", "description": "The option value to select."}},
        required=["element_ref", "value"],
    ),
    _schema(
        "browser_check",
        "Tick or untick a checkbox, or choose a radio option. Pass checked=false to "
        "clear a checkbox. Radio options are selected, never cleared — pick a "
        "different option in the group instead.",
        props={**_ELEMENT_REF,
               "checked": {"type": "boolean", "description": "True to tick, false to untick."}},
        required=["element_ref"],
    ),
    _schema(
        "browser_upload",
        "Attach a previously staged file to a file input, by artifact id. There is no "
        "way to name a file on disk — only an artifact that was staged for this task.",
        props={**_ELEMENT_REF,
               "artifact_id": {"type": "string",
                               "description": "Id of a staged artifact. Never a filesystem path."}},
        required=["element_ref", "artifact_id"],
    ),
    _schema(
        "browser_submit",
        "Submit a form. This is the ONLY tool that submits, and it requires the user's "
        "approval before it runs. Use it on the form's submit button — browser_click "
        "will refuse that element and point you here.",
        props=dict(_ELEMENT_REF), required=["element_ref"],
    ),
]}

TOOL_NAMES: tuple[str, ...] = tuple(SCHEMAS)


def openai_schemas(names: list[str] | None = None) -> list[dict]:
    """The function-calling shape. Phase D hands these to the registry; nothing here
    registers anything."""
    return [{"type": "function", "function": SCHEMAS[n]}
            for n in (names or TOOL_NAMES) if n in SCHEMAS]


def _verify_against_worker() -> None:
    """Import-time consistency with the worker's closed enum.

    A schema naming an action the worker does not have would fail on the first call
    with BAD_REQUEST — a runtime surprise for what is a static, checkable property.
    Two catalogues that must agree should be compared where they are defined.
    """
    from playwright_worker.protocol import Action

    worker = {a.value for a in Action}
    ours = set(SCHEMAS)
    if ours != worker:
        raise ImportError(
            f"browser_tools schemas disagree with the worker's action enum: "
            f"missing={sorted(worker - ours)} extra={sorted(ours - worker)}")


def _verify_no_forbidden_params() -> None:
    """No schema offers a selector, code or path channel (§3.3, §11 S2/S3).

    Enforced at import as well as in a test: a test proves it in CI, and this proves
    it in every process that loads the package, including one where someone ran the
    tests with `-k`.
    """
    for name, schema in SCHEMAS.items():
        offending = FORBIDDEN_PARAMS & set(schema["parameters"]["properties"])
        if offending:
            raise ImportError(
                f"{name} declares forbidden parameter(s) {sorted(offending)}; "
                f"elements are addressed only by element_ref")


_verify_against_worker()
_verify_no_forbidden_params()
