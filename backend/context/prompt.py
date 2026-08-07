"""
PromptBuilder — the only thing that knows what a prompt section looks like.

Moved verbatim out of `main.py::chat_endpoint`, where the [MEMORY] and
[COMPANY KNOWLEDGE] blocks were built inline between retrieval calls. The
strings here are byte-identical to the originals, including the trailing-space
and newline placement, because this phase must not change what the model reads.

The only structural change is the input: sections are derived from a
ContextBundle instead of from four loose local variables. Providers still know
nothing about any of this.
"""
from __future__ import annotations

from .bundle import ContextBundle, RankedContextBundle

# The sentinel `search_corporate` returns on a miss. The legacy code suppressed
# the [COMPANY KNOWLEDGE] block on exactly this string; so does this.
NO_CORPORATE_RESULT = "No specific corporate guidelines found."


# Every function below accepts EITHER a ContextBundle (phase 3.5) or a
# RankedContextBundle (phase 4) — the ranked bundle delegates `get`/`text_of`/
# `first_text`, so this module needs no branching and stays purely a formatter.
Bundle = "ContextBundle | RankedContextBundle"


def memory_section(bundle: Bundle) -> str:
    """[MEMORY] — omitted entirely when there is nothing to say."""
    text = bundle.text_of("memory").strip()
    if not text:
        return ""
    return (f"[MEMORY]\n"
            f"## Relevant Facts From Past Conversations\n"
            f"{text}")


def corporate_section(bundle: Bundle) -> str:
    """[COMPANY KNOWLEDGE] — omitted when empty or on the miss sentinel."""
    text = bundle.first_text("corporate").strip()
    if not text or text == NO_CORPORATE_RESULT:
        return ""
    return (f"[COMPANY KNOWLEDGE]\n"
            f"## Policies and Procedures (Retrieved)\n"
            f"{text}")


def graph_section(bundle: Bundle) -> str:
    """[KNOWLEDGE GRAPH] — live since phase 4.

    Wording is unchanged from the shape defined in 3.5; phase 4 only turned the
    provider on. Items arrive already fused, ranked, compressed and
    budget-trimmed, so this does nothing but render them.
    """
    items = bundle.get("graph")
    if not items:
        return ""
    lines = [f"- {i.text}" for i in items if not i.is_empty]
    if not lines:
        return ""
    return ("[KNOWLEDGE GRAPH]\n"
            "## Related Entities and Relationships (Retrieved)\n"
            + "\n".join(lines))


# Section builders in the order the legacy prompt emitted them. A new section is
# a function plus an entry here — no call site changes.
SECTION_ORDER = (
    ("memory", memory_section),
    ("corporate", corporate_section),
    ("graph", graph_section),
)


def build_sections(bundle: Bundle) -> "list[str]":
    """Non-empty prompt sections, in legacy order.

    The caller splices these into its own `prompt_parts` at the same position
    the inline blocks occupied, so surrounding sections (identity, rules, tools,
    writing style) are untouched.
    """
    out = []
    for _, fn in SECTION_ORDER:
        block = fn(bundle)
        if block:
            out.append(block)
    return out


def calendar_text(bundle: Bundle) -> str:
    """Calendar text, or "" — the caller applies its own legacy default."""
    return bundle.text_of("calendar").strip()


def tasks_text(bundle: Bundle) -> str:
    """Task summary, or "" — the caller applies its own legacy default."""
    return bundle.text_of("tasks").strip()
