"""Registers the 14 browser tools in the canonical Runtime B registry (Phase D).

This is the ONLY file where browser code touches `backend/`. It exists so that
`browser_tools/` can stay ignorant of three things it must not learn:

  * `TenantContext` — ownership arrives there as four plain strings, filled here;
  * the registry — `browser_tools` registers nothing;
  * `backend.storage` — the artifact adapter is injected here, not imported there.

**No authorization lives here.** `WorkerGateway._authorize()` is still empty and
Phase E fills it. The handlers below do exactly one authorization-adjacent thing:
they REFUSE when there is no tenant, before the worker is reached. That is not a
policy decision — it is the §5.2 rule that an unowned browser session must not
exist, and it fails closed rather than defaulting to a shared tenant.

**No second registry.** These go into `orchestrator.registry` alongside the other
43, with `required_permission` strings the existing `grant_matches` already
understands (§4.3: per-tool grants, which work today at zero cost).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from browser_tools import SCHEMAS, TOOLS
from browser_tools.outcomes import Outcome
from browser_tools.results import ToolResult

from .registry import Tool, register

log = logging.getLogger("aganeti.orchestrator.browser")

#: Per-tool permission strings (§4.3). Deliberately NOT one shared `browser.use`:
#: a shared string carries the same "a future tool is silently granted to every
#: browser-capable agent" hazard the wildcard was rejected for, and per-tool grants
#: are free. A wildcard is impossible anyway — `grant_matches` is exact set
#: membership and the write-time filter drops unknown strings.
PERMISSIONS: dict[str, str] = {
    "browser_open": "browser.session",
    "browser_close": "browser.session",
    "browser_navigate": "browser.read",
    "browser_inspect": "browser.read",
    "browser_screenshot": "browser.read",
    "browser_extract": "browser.read",
    "browser_wait": "browser.read",
    "browser_back": "browser.read",
    "browser_click": "browser.write",
    "browser_fill": "browser.write",
    "browser_select": "browser.write",
    "browser_check": "browser.write",
    "browser_upload": "browser.write",
    "browser_submit": "browser.submit",
}

#: Hosts a browser session may reach. §11.2: for v1 exactly the lab.
#:
#: Guarded by `test_browser_domain_allowlist_is_internal_only`, which fails if a
#: non-internal host appears here. That failure is the signal to revisit
#: `guardrails.TOOL_CATEGORY` — see the browser block there. Adding an external
#: host without changing the classification would leave 14 tools marked
#: non-outbound while they egress.
ALLOWED_DOMAINS: tuple[str, ...] = ("browser-lab", "localhost", "127.0.0.1")


def _schema_for(name: str) -> dict:
    """The registry's `parameters` for one tool.

    Ownership is stripped: `tenant_id`/`user_id`/`agent_id`/`session_id` are
    required by `browser_tools`' schema because that package takes them
    explicitly, but the MODEL must never supply them — they come from the
    executor's ctx. Leaving them in the model-facing schema would invite a model
    to name a tenant, which is the whole class of bug §5.2 is about.
    """
    params = SCHEMAS[name]["parameters"]
    props = {k: v for k, v in params["properties"].items()
             if k not in ("tenant_id", "user_id", "agent_id", "session_id")}
    required = [r for r in params.get("required", [])
                if r not in ("tenant_id", "user_id", "agent_id", "session_id")]
    return {"type": "object", "properties": props, "required": required}


class MissingTenant(Exception):
    """No tenant on the executor ctx. Refused before the worker (§5.2)."""


def _identity(ctx: dict) -> dict:
    """Pull the four ownership fields off the executor ctx, or refuse.

    `_tools_node` builds ctx as `{user_id, agent_id, board_id, tenant_id}`
    (`graph.py:85-86`). `_agent_node`'s ctx omits `tenant_id` — a browser tool only
    ever runs from the tools node, but this asserts rather than assumes, because
    "the ctx dict" is not one thing (§4.1).

    Absent or empty tenant is a REFUSAL, never a default and never a fallback to a
    shared tenant. A browser session is created by this system, so there is no
    unstamped-legacy problem to accommodate: the equivalent of stamping at
    ingestion happens at `browser_open` (§5.2).
    """
    tenant_id = str(ctx.get("tenant_id") or "").strip()
    user_id = str(ctx.get("user_id") or "").strip()
    if not tenant_id:
        raise MissingTenant(
            "this call has no tenant context; browser tools refuse rather than "
            "run unowned (§5.2)")
    if not user_id:
        raise MissingTenant("this call has no authenticated user")
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        # The executor's agent identity; falls back to a stable literal rather than
        # empty so the audit row is never blank. Not security-bearing.
        "agent_id": str(ctx.get("agent_id") or "primary"),
        # graph.py's ctx carries `board_id` as its session-ish key. Browser sessions
        # are keyed by the worker's own browser_session_id, so this is provenance
        # for the artifact row, not an ownership field.
        "session_id": str(ctx.get("session_id") or ctx.get("board_id") or ""),
    }


def _handler_for(name: str) -> Callable:
    """Wrap a `browser_tools` function as a registry handler.

    The registry contract is `async handler(ctx, **args) -> str`. The tool returns
    a `ToolResult`; `for_model()` is the rendering an agent reads, and it is what
    the executor puts in the tool message.
    """
    fn = TOOLS[name]

    async def handler(ctx, **args) -> str:
        try:
            ident = _identity(ctx)
        except MissingTenant as e:
            # Refused here, before `browser_tools` — which means before the
            # gateway, and therefore before the worker. Shaped like any other
            # tool-layer refusal so an agent reads it the same way.
            log.warning("%s refused: %s", name, e)
            return ToolResult(
                tool=name, outcome=Outcome.FAILED,
                summary="the call was refused before it ran",
                error_code="BAD_REQUEST", error_message=str(e),
                recovery="Stop. This is a configuration fault, not something to retry.",
                terminal=True,
            ).for_model()

        if name == "browser_open" and "allowed_domains" not in args:
            # §11.2: the allowlist is policy, not a model choice. A model that could
            # widen it could navigate anywhere, so the default is applied here and
            # the parameter stays in the schema only for a caller that wants to
            # NARROW it. Phase E moves the check into the boundary; this is the
            # session-scoped backstop the worker already enforces.
            args["allowed_domains"] = list(ALLOWED_DOMAINS)

        result = await fn(**ident, **args)
        return result.for_model()

    handler.__name__ = f"{name}_handler"
    return handler


_registered = False


def register_browser_tools() -> list[str]:
    """Idempotently register all 14. Returns the names registered."""
    global _registered
    if _registered:
        return list(SCHEMAS)
    _install_artifact_store()
    for name in SCHEMAS:
        register(Tool(
            name=name,
            description=SCHEMAS[name]["description"],
            parameters=_schema_for(name),
            handler=_handler_for(name),
            required_permission=PERMISSIONS[name],
            # NOT outbound — see the reasoning in guardrails.TOOL_CATEGORY.
            is_outbound=False,
        ))
    _registered = True
    log.info("registered %d browser tools", len(SCHEMAS))
    return list(SCHEMAS)


def _install_artifact_store() -> None:
    """Point `browser_tools` at the real object store.

    `browser_tools` ships a filesystem default so it can be finished and tested
    without `backend/`. Here — and only here — it is replaced by an adapter over
    the existing `SeaweedFSStorage` + `chat_artifacts`, so a screenshot lands in
    the same place every other artifact does.
    """
    from browser_tools import set_artifact_store

    from .browser_artifacts import SeaweedArtifactStore

    try:
        set_artifact_store(SeaweedArtifactStore())
        log.info("browser artifacts -> SeaweedFS + chat_artifacts")
    except Exception:  # noqa: BLE001
        # A store that cannot be constructed must not take the whole registry down
        # at import. The filesystem default remains, which is durable across a
        # restart but local to one host — logged loudly because it is a degraded
        # mode, not a supported one.
        log.exception("could not install the SeaweedFS artifact store; "
                      "browser screenshots will use the local filesystem default")
