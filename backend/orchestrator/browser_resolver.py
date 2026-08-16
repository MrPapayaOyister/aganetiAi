"""Session-fact resolver — gathers, never decides (§4.2 option (i)).

`authorize()` must stay pure: data in, decision out, no I/O. Twenty-four tests in
`tests/test_authz_boundary.py` rest on that, and §12 makes it structural rather than
stylistic — OPA is a pure function over an input document, so anything the boundary
needs must already be *in* the document by the time policy runs. Resolving here is
meeting that constraint early, not working around it.

**This module gathers facts. It contains no decision.**

That is a rule with teeth: `test_the_resolver_contains_no_decision` reads this
module's source and fails if it mentions `Decision`, `DENY`, `ALLOW`, or
`APPROVAL`. If a branch here ever returned a verdict, the boundary would have two
authorities and §4.2's ordering guarantee would be a claim rather than a property.

What it resolves, per call:

  * `session_owner_tenant` / `session_owner_user` — who owns `browser_session_id`.
    **A session that does not exist and a session owned by someone else both
    resolve to `None`.** The boundary therefore cannot tell them apart, which is
    what makes the two denials indistinguishable (§5.1) without the boundary having
    to be careful about it.
  * `target_domain` — the host of a `url` argument, when the action has one.
  * `current_page_host` — the host the live page is **actually** on, post-redirect.

The last one is the point of the whole module. §11.2: check the post-redirect host,
not the requested URL, because redirect-based allowlist bypass is the standard way
this control fails. And check it on *every* action — a page can redirect after
`browser_navigate` passed, and a later `browser_fill` would then act on an origin
nobody checked.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

log = logging.getLogger("aganeti.orchestrator.browser_resolver")

#: Tools whose calls carry a browser session and therefore need facts resolved.
#: `browser_open` is absent: it CREATES the session, so there is nothing to resolve
#: and no page to read a host from.
_SESSION_TOOLS = frozenset({
    "browser_close", "browser_navigate", "browser_inspect", "browser_screenshot",
    "browser_extract", "browser_wait", "browser_back", "browser_click",
    "browser_fill", "browser_select", "browser_check", "browser_upload",
    "browser_submit",
})


def is_browser_tool(tool_name: str) -> bool:
    return tool_name.startswith("browser_")


def _host(url: Any) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        return (urlparse(url.strip()).hostname or "").lower() or None
    except ValueError:
        return None


class SessionFacts:
    """A plain record. Not a decision, and deliberately not named like one."""

    __slots__ = ("session_owner_tenant", "session_owner_user",
                 "target_domain", "current_page_host")

    def __init__(self, session_owner_tenant=None, session_owner_user=None,
                 target_domain=None, current_page_host=None):
        self.session_owner_tenant = session_owner_tenant
        self.session_owner_user = session_owner_user
        self.target_domain = target_domain
        self.current_page_host = current_page_host

    def as_kwargs(self) -> dict:
        return {"session_owner_tenant": self.session_owner_tenant,
                "session_owner_user": self.session_owner_user,
                "target_domain": self.target_domain,
                "current_page_host": self.current_page_host}


async def resolve(tool_name: str, arguments: dict, *,
                  requesting_tenant: str, requesting_user: str,
                  gateway: Any = None) -> SessionFacts:
    """Gather the facts the boundary needs. Returns nulls for a non-browser tool.

    `requesting_tenant`/`requesting_user` are passed because the worker's session
    lookup is itself ownership-asserting — it answers "does THIS identity own this
    session" rather than "who owns it". That is a deliberate property of the worker
    (§5.1), and it means a non-owner's lookup comes back empty, which is exactly
    the input the boundary should see.

    A resolution failure yields nulls, never a guess. The boundary then denies,
    because a null owner cannot match a requesting identity — fail-closed by the
    shape of the data rather than by a special case.
    """
    if not is_browser_tool(tool_name):
        return SessionFacts()

    facts = SessionFacts(target_domain=_host(arguments.get("url")))

    if tool_name not in _SESSION_TOOLS:
        # browser_open: no session yet, no page yet. `target_domain` stays null too
        # because open takes no url.
        return facts

    browser_session_id = str(arguments.get("browser_session_id") or "")
    if not browser_session_id:
        return facts

    gw = gateway
    if gw is None:
        from browser_tools.client import get_gateway
        gw = get_gateway()

    try:
        state = await gw.session_facts(
            browser_session_id,
            tenant_id=requesting_tenant, user_id=requesting_user)
    except Exception:  # noqa: BLE001 — a lookup failure resolves to nulls, and the
        # boundary denies on nulls. Never a guess, never a default owner.
        log.warning("session fact resolution failed for %s", browser_session_id[:12],
                    exc_info=True)
        return facts

    if not state:
        return facts

    facts.session_owner_tenant = state.get("tenant_id") or None
    facts.session_owner_user = state.get("user_id") or None
    # POST-REDIRECT host, read from the live page — not from the url argument.
    facts.current_page_host = _host(state.get("current_url"))
    return facts
