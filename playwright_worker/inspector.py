"""The inspector and the ref→locator map (§3.3), and submit-like classification (§3.4).

`browser_inspect` is the agent's primary sense. It returns at most
`max_inspect_elements` interactive elements as `{ref, role, accessible_name, label,
type, state}` — never raw DOM, never a selector.

**Why refs and not selectors.** §3.3: a selector parameter is a small
arbitrary-code channel, and an opaque ref cannot address anything the agent was not
shown. This module is where that guarantee is actually kept: the ref map lives
worker-side, keyed by session, and `resolve()` is the only way to get from a ref to
something clickable.

**Staleness is explicit.** Every ref carries the generation it was issued under. A
ref from an older generation is `STALE_REF` — never a wrong-element action, which is
the failure this whole indirection exists to prevent. Generation bumps on navigation
and on detected DOM mutation.

**How elements are located after inspection.** Each ref is bound to a *nonce
attribute* written onto the element during the inspect pass (`data-pw-ref`), not to
a CSS path. A CSS path is exactly the selector we refuse to accept from the caller,
and it is also fragile across re-render in the way §3.3 complains about. The nonce
is generated per generation, so a re-render that drops the attribute produces
`STALE_REF` rather than silently matching a different element that happens to sit at
the same path.

The one piece of JavaScript in this worker lives here. It is a fixed, closed script
that takes no caller input — see `_COLLECT_JS`. There is no `page.evaluate` reachable
from a command, and no argument anywhere flows into it (§11 S2).
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from .errors import ErrorCode, WorkerError
from .protocol import ElementView
from .session import BrowserSession

log = logging.getLogger("playwright_worker.inspector")


#: Fixed collection script. Takes ONE argument — the nonce — which the worker
#: generates itself; nothing from a command reaches it. It stamps `data-pw-ref` on
#: each interactive element and returns the bounded descriptor list.
#:
#: Submit-like classification (§3.4) happens HERE rather than at click time, because
#: the classification needs the element's form context and that is cheapest to read
#: while we are already walking the DOM. The click path then only has to consult the
#: stored flag, which is what makes the refusal non-bypassable: there is no code path
#: that clicks an element whose descriptor was never produced by this walk.
_COLLECT_JS = """
(nonce) => {
  const SELECTABLE = 'a[href], button, input, select, textarea, [role=button], [contenteditable=true]';
  const out = [];
  let seq = 0;

  const visible = (el) => {
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const accName = (el) => {
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\\s+/)
        .map(id => document.getElementById(id))
        .filter(Boolean)
        .map(n => (n.textContent || '').trim());
      if (parts.length) return parts.join(' ');
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return (lab.textContent || '').trim();
    }
    const wrapping = el.closest('label');
    if (wrapping) return (wrapping.textContent || '').trim();
    const txt = (el.textContent || '').trim();
    if (txt) return txt;
    return (el.getAttribute('placeholder') || el.getAttribute('name') || '').trim();
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button' || t === 'image') return 'button';
      if (t === 'file') return 'file';
      return 'textbox';
    }
    return 'generic';
  };

  // §3.4: <button type=submit>, <input type=submit>, and form-activating elements.
  // A bare <button> inside a form is submit-like too — that is the HTML default
  // when `type` is omitted, and it is the case a naive check misses.
  const submitLike = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && (type === 'submit' || type === 'image')) return true;
    if (tag === 'button') {
      if (type === 'submit') return true;
      if (type === 'button' || type === 'reset') return false;
      return !!el.closest('form');   // omitted type inside a form defaults to submit
    }
    if (el.getAttribute('role') === 'button' && el.closest('form') && !type) return true;
    return false;
  };

  const sensitive = (el) => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'password') return true;
    const hay = ((el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '') + ' ' +
                 (el.getAttribute('autocomplete') || '')).toLowerCase();
    return /pass|secret|token|otp|cvv|ssn|card/.test(hay);
  };

  for (const el of document.querySelectorAll(SELECTABLE)) {
    if (!visible(el)) continue;
    const ref = 'e' + (++seq);
    el.setAttribute('data-pw-ref', nonce + ':' + ref);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    out.push({
      ref: ref,
      role: roleOf(el),
      accessible_name: accName(el).slice(0, 200),
      label: (el.getAttribute('aria-label') || el.getAttribute('name') ||
              el.getAttribute('id') || '').slice(0, 200),
      type: type || tag,
      tag: tag,
      submit_like: submitLike(el),
      sensitive: sensitive(el),
      state: {
        disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
        checked: (el.type === 'checkbox' || el.type === 'radio') ? !!el.checked : null,
        value_present: !!(el.value && String(el.value).length),
        required: !!el.required,
        in_form: !!el.closest('form')
      }
    });
  }
  return { total: out.length, elements: out };
}
"""


#: Counts interactive elements. Fixed script, no arguments at all — used by the
#: `element_appears` wait condition so an agent can wait for a DELAYED element
#: without naming it. Naming it would mean accepting a locator from the caller,
#: which is the thing §3.3 exists to prevent; counting is semantic and needs no
#: addressing at all.
_COUNT_JS = """
() => document.querySelectorAll(
  'a[href], button, input, select, textarea, [role=button], [contenteditable=true]'
).length
"""


class Inspector:
    """Owns the ref map for one session. Stateless across sessions by construction —
    it takes the session and reads/writes only that session's `refs`."""

    def __init__(self, max_elements: int):
        self.max_elements = max_elements

    async def count_interactive(self, sess: BrowserSession) -> int:
        try:
            return int(await sess.page.evaluate(_COUNT_JS))
        except Exception as e:  # noqa: BLE001
            raise WorkerError(ErrorCode.TIMEOUT,
                              f"counting elements failed: {type(e).__name__}") from e

    async def inspect(self, sess: BrowserSession, *, limit: int | None = None
                      ) -> tuple[list[ElementView], int, bool]:
        """Walk the page, issue refs, return bounded views.

        Bumps the generation FIRST: every previously issued ref becomes stale the
        moment we start a new walk, whether or not the page changed. Refs are cheap
        to reissue and a wrong-element action is not recoverable.
        """
        cap = min(limit or self.max_elements, self.max_elements)
        sess.ref_generation += 1
        nonce = f"g{sess.ref_generation}-{secrets.token_hex(4)}"
        sess.refs.clear()

        try:
            result = await sess.page.evaluate(_COLLECT_JS, nonce)
        except Exception as e:  # noqa: BLE001
            raise WorkerError(ErrorCode.TIMEOUT,
                              f"page inspection failed: {type(e).__name__}") from e

        total = int(result.get("total", 0))
        raw = result.get("elements", [])[:cap]
        views: list[ElementView] = []
        for item in raw:
            ref = item["ref"]
            sess.refs[ref] = {
                "nonce": nonce,
                "generation": sess.ref_generation,
                "submit_like": bool(item.get("submit_like")),
                "sensitive": bool(item.get("sensitive")),
                "role": item.get("role", ""),
                "tag": item.get("tag", ""),
                "type": item.get("type", ""),
                "accessible_name": item.get("accessible_name", ""),
            }
            views.append(ElementView(
                ref=ref, role=item.get("role", ""),
                accessible_name=item.get("accessible_name", ""),
                label=item.get("label", ""), type=item.get("type", ""),
                state=dict(item.get("state") or {}),
            ))

        sess.last_inspect_url = sess.page.url
        truncated = total > len(views)
        return views, total, truncated

    def invalidate(self, sess: BrowserSession, reason: str) -> None:
        """Drop the ref map (§3.3: invalidated on navigation or DOM mutation).

        The generation bump is what makes a surviving ref stale rather than merely
        absent — a caller holding `e17` gets STALE_REF, not ELEMENT_NOT_FOUND, and
        those have different documented recoveries in §9.1.
        """
        sess.ref_generation += 1
        sess.refs.clear()
        log.debug("refs invalidated for %s (%s) -> generation %d",
                  sess.id[:12], reason, sess.ref_generation)

    def describe(self, sess: BrowserSession, ref: str) -> dict:
        """The stored descriptor, or STALE_REF / ELEMENT_NOT_FOUND.

        A ref the session never issued is ELEMENT_NOT_FOUND; a ref that was issued
        under an older generation is STALE_REF. §9.1 gives them different recoveries
        ("re-inspect once then fail" vs "re-inspect, remap, retry — does not count
        against element retries"), so conflating them would make the agent's retry
        accounting wrong.
        """
        entry = sess.refs.get(ref)
        if entry is None:
            if sess.ref_generation > 0:
                raise WorkerError(
                    ErrorCode.STALE_REF,
                    f"{ref} is not in the current ref map (generation "
                    f"{sess.ref_generation}); re-inspect and remap",
                    detail={"element_ref": ref, "generation": sess.ref_generation})
            raise WorkerError(ErrorCode.ELEMENT_NOT_FOUND,
                              f"{ref} was never issued; call browser_inspect first",
                              detail={"element_ref": ref})
        if entry["generation"] != sess.ref_generation:
            raise WorkerError(
                ErrorCode.STALE_REF,
                f"{ref} was issued under generation {entry['generation']}, "
                f"current is {sess.ref_generation}; re-inspect and remap",
                detail={"element_ref": ref})
        return entry

    async def locator(self, sess: BrowserSession, ref: str):
        """Resolve a ref to a Playwright locator, or raise a typed error.

        The locator is built from the worker's own nonce attribute. It is a selector
        string, but it is one the *worker* authored from a value the worker
        generated — no part of it comes from the caller (§3.3, §11 S3).
        """
        entry = self.describe(sess, ref)
        marker = f"{entry['nonce']}:{ref}"
        loc = sess.page.locator(f"[data-pw-ref='{marker}']")
        try:
            count = await loc.count()
        except Exception as e:  # noqa: BLE001
            raise WorkerError(ErrorCode.TIMEOUT,
                              f"resolving {ref} failed: {type(e).__name__}") from e
        if count == 0:
            # The element carried this nonce when we walked the page and does not
            # now: it was re-rendered or removed. That is staleness, not absence.
            raise WorkerError(
                ErrorCode.STALE_REF,
                f"{ref} no longer resolves — the page re-rendered; re-inspect",
                detail={"element_ref": ref})
        if count > 1:
            # Cannot happen with a per-element nonce unless the page cloned a node
            # wholesale. Refusing beats picking one at random.
            raise WorkerError(
                ErrorCode.STALE_REF,
                f"{ref} resolves to {count} elements after a re-render; re-inspect",
                detail={"element_ref": ref, "matches": count})
        return loc.first

    @staticmethod
    def is_submit_like(entry: dict) -> bool:
        return bool(entry.get("submit_like"))

    @staticmethod
    def is_sensitive(entry: dict) -> bool:
        return bool(entry.get("sensitive"))
