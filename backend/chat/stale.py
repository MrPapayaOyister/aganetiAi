"""
Mark prior turns whose data has gone stale, so the model re-calls the tool
instead of quoting figures from the transcript.

The bug this fixes is pre-existing and independent of tool toggles: ask "any new
emails?" twice and the second answer comes from the first, not from the mailbox.
Measured across tools — the dividing line is whether the previous ANSWER carried
the data. Search tools are immune (their reply is a pointer, "here are some
results"); weather, mail and calendar are not.

Prompting was tried first and abandoned. A standing instruction rescued weather
but never mail or calendar, at any strength, and it did not survive the length of
the live system prompt. Marking is 4/4 on the cases a prompt was 0/4 on, because
it sits inline next to the stale data rather than competing with a long preamble.

Marking, not trimming. Trimming was the first idea and tested worse: it removes
the antecedent, so "check again" and "what about now?" lose what they refer to
and the model stops calling at all — worse than doing nothing. Marking keeps the
context and annotates it.

THE MARKER IS NEVER PERSISTED. It is applied to the in-memory copy on its way
into the model and nowhere else. If it reached memory/store.py or chat_messages
it would compound on every turn and corrupt stored history — that is the one
failure here that is not recoverable, so it has its own test.
"""

from __future__ import annotations

MARKER = ("\n\n[STALE — this data was fetched in an earlier turn. Do not reuse it; "
          "call the tool again for current data.]")


def mark_stale(rows: list[dict]) -> list[dict]:
    """Annotate assistant turns produced by a volatile tool.

    Takes rows carrying `tools` provenance and returns clean ``{role, content}``
    messages: the marker is merged into the content and the provenance key is
    dropped, so nothing extra reaches the provider's payload.

    Rows without provenance pass through untouched. That is the honest default —
    the JSON fallback store has no provenance, and guessing from content would
    reintroduce exactly the heuristic this design avoided.
    """
    from backend.tools import ACTION_TOOLS, VOLATILE_TOOLS, WIDGET_TOOLS

    out: list[dict] = []
    for r in rows:
        content = r.get("content") or ""
        tools = set(r.get("tools") or [])
        if r.get("role") == "assistant" and content and tools:
            # An action's confirmation must never be marked: re-calling
            # create_task repeats the side effect. Belt and braces on top of the
            # assertions in tools.py keeping the sets disjoint.
            if not (tools & ACTION_TOOLS):
                # WIDGET TURNS ARE NEVER MARKED.
                #
                # A player turn's reply carries no data, so there is nothing for
                # a staleness note to be about. That is the whole justification —
                # it is NOT, as an earlier version of this comment claimed,
                # supported by a measurement showing marking suppresses the
                # re-call. Those numbers were taken on a bench that ran with
                # model thinking ENABLED; the server disables it
                # (build_payload -> chat_template_kwargs), and on the real
                # setting the same payload behaves completely differently.
                # See the STATUS note on WIDGET_TOOLS in backend/tools.py.
                #
                # The repeat-request bug this was meant to fix is still open.
                #
                # A turn that is BOTH still gets marked: get_weather renders a
                # card AND carries figures, is not in WIDGET_TOOLS, and survives
                # the subtraction below.
                #
                # The subtraction is done HERE rather than by trusting
                # VOLATILE_TOOLS to stay clean, so re-adding a widget tool to
                # that set cannot silently change this behaviour.
                data_tools = VOLATILE_TOOLS - WIDGET_TOOLS
                if tools & data_tools and MARKER not in content:
                    content += MARKER
        out.append({"role": r.get("role"), "content": content})
    return out
