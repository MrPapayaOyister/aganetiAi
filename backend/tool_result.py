"""
Split a tool's return value into the two things the chat loop treats differently:

  1. ``llm_result`` — short text the model sees and reasons over. This is what
     gets appended to ``convo`` as ``{"role": "tool", ...}``.
  2. ``embeds`` — raw HTML strings the FRONTEND renders as sandboxed iframes.
     The model never sees this markup.

Every tool in ``backend/tools.py`` predating this returns a plain string, which
passes through untouched — ``llm_result`` is the string, ``embeds`` is empty.
Nothing about the existing dispatch changes shape.

Adapted from Open WebUI's ``process_tool_result`` in
``backend/open_webui/utils/middleware.py`` (see reference/yt-tool-service/),
trimmed to the in-process Python path — the only one this codebase uses. The
external-tool-server variant (Content-Disposition header dict + Location-header
embeds) is omitted.

Why the model must not see the markup: if it did, (a) it would try to summarise
or quote hundreds of lines of HTML instead of talking about the video, and
(b) anything it copied back into its own reply would be rendered as ordinary
assistant text — react-markdown, no iframe — bypassing every sandbox property
the embed path exists to enforce. So the model gets a short context string and
the HTML travels out-of-band.

The ``Content-Disposition: inline`` marker is Open WebUI's convention and is
kept deliberately: any of their tools can be ported into services/ and split
here without touching this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.responses import HTMLResponse


@dataclass
class ToolResult:
    llm_result: object
    #: ``[{"html": str,
    #:    "link":  {"url": str, "label": str} | None,
    #:    "video": {"id": str, "start": int} | None}, ...]``
    #:
    #: `html` renders in a srcdoc iframe sandboxed to allow-scripts ONLY.
    #: `link` and `video` render OUTSIDE that frame, in the app's own DOM:
    #:   - a link inside the sandbox cannot work (popups inherit the sandbox, so
    #:     the opened tab has an opaque origin and third-party sites refuse it);
    #:   - a player inside it cannot work either (the nested frame inherits the
    #:     sandbox and YouTube fails with "writeEmbed is not defined").
    #: Both verified in-browser. See frontend/src/components/ToolEmbeds.tsx.
    embeds: list = field(default_factory=list)


def process_tool_result(tool_name: str, raw_result) -> ToolResult:
    """Normalise whatever a tool returned into (llm_result, embeds)."""
    embeds: list = []

    # (HTMLResponse, context) lets a tool hand the model something more useful
    # than the generic stub below — e.g. the video's title and channel — while
    # still rendering the widget. This 2-tuple IS Open WebUI's contract and is
    # handled byte-identically, so any of their tools can be ported into
    # services/ and split here without touching this module.
    #
    # The 3-tuple (HTMLResponse, context, link) is our own additive extension
    # for embeds that need an out-of-frame link. Tools that don't need one keep
    # returning a 2-tuple and are unaffected.
    result_context = None
    embed_meta: dict = {}
    if (
        isinstance(raw_result, tuple)
        and len(raw_result) in (2, 3)
        and isinstance(raw_result[0], HTMLResponse)
    ):
        if len(raw_result) == 3:
            raw_result, result_context, meta = raw_result
            embed_meta = meta if isinstance(meta, dict) else {}
        else:
            raw_result, result_context = raw_result

    if isinstance(raw_result, HTMLResponse):
        content_disposition = raw_result.headers.get("Content-Disposition", "")

        if "inline" in content_disposition:
            embeds.append({
                "html": raw_result.body.decode("utf-8", "replace"),
                # Rendered outside the sandboxed frame. `link` is an ordinary
                # anchor; `video` is a native player pointed at the provider's
                # own origin. Both are absent for tools that supply neither.
                "link": embed_meta.get("link"),
                "video": embed_meta.get("video"),
                # Search results the frontend renders as a picker; selecting one
                # swaps in the same player `video` would have produced.
                "results": embed_meta.get("results"),
                "query": embed_meta.get("query"),
                # NAME of a CSP profile the frontend defines. Never a policy.
                "csp": embed_meta.get("csp"),
                # Directory hits for the multi-select channel picker.
                "channels": embed_meta.get("channels"),
                # Which library the picker commits to. The component is shared
                # between TV and radio, and the follow-up it sends must name the
                # right one or a radio pick is added as a TV channel.
                "channel_kind": embed_meta.get("channel_kind"),
                # News stories, so React can render real links outside the frame.
                "articles": embed_meta.get("articles"),
                # A REPRODUCIBLE descriptor. Present only for embeds that are a
                # pure function of their input, where rehydration can rebuild the
                # widget instead of replaying a stored snapshot.
                "qr": embed_meta.get("qr"),
            })

            if 200 <= raw_result.status_code < 300:
                llm_result = result_context if result_context is not None else (
                    f"{tool_name}: embedded UI result is active and visible to "
                    f"the user."
                )
            else:
                llm_result = (
                    f"{tool_name}: error {raw_result.status_code} from embedded "
                    f"UI result."
                )
        else:
            # HTMLResponse without Content-Disposition: inline is not a widget —
            # hand its body to the model as plain text.
            llm_result = raw_result.body.decode("utf-8", "replace")

    else:
        # Plain string / dict / list — every pre-existing tool lands here.
        llm_result = raw_result

    return ToolResult(llm_result=llm_result, embeds=embeds)
