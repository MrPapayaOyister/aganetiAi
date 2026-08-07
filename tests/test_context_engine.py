"""
Context Engine tests.

The important one is `test_prompt_sections_are_byte_identical`: this phase is a
refactor, so the contract is that the assembled prompt does not change. That
test reimplements the ORIGINAL inline logic from main.py and asserts the new
path produces the same strings for every edge case the old code handled.

All tests are pure — providers are stubbed, so no Qdrant, LLM or Neo4j.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.context import ContextBuilder, build_sections, calendar_text, tasks_text
from backend.context.bundle import ContextBundle, ContextItem
from backend.context.providers import ContextProvider, ContextRequest, registry


# ── the original inline logic, copied verbatim from main.py pre-refactor ─────

def legacy_assemble(long_term_context, context, calendar_context, tasks_context):
    parts = []
    if long_term_context and long_term_context.strip():
        parts.append(f"[MEMORY]\n"
                     f"## Relevant Facts From Past Conversations\n"
                     f"{long_term_context.strip()}")
    clean_rag = ""
    if context and context.strip() and context != "No specific corporate guidelines found.":
        clean_rag = context.strip()
    if clean_rag:
        parts.append(f"[COMPANY KNOWLEDGE]\n"
                     f"## Policies and Procedures (Retrieved)\n"
                     f"{clean_rag}")
    cal = calendar_context if calendar_context and calendar_context.strip() \
        else "No events scheduled today."
    tsk = tasks_context if tasks_context and tasks_context.strip() else "No pending tasks."
    return parts, cal, tsk


def stub(name, texts, *, fail=False, delay=0.0, timeout=5.0, enabled=True):
    """A provider that returns fixed text. `collect` MUST be defined in the class
    body — ABCMeta freezes __abstractmethods__ at class creation, so assigning it
    afterwards leaves the class abstract and uninstantiable."""

    class _Stub(ContextProvider):
        async def collect(self, request):
            if delay:
                await asyncio.sleep(delay)
            if fail:
                raise RuntimeError(f"{name} exploded")
            return [ContextItem(text=t, provider=name) for t in texts if t]

    _Stub.name = name
    _Stub.timeout = timeout
    _Stub.enabled = enabled
    return _Stub()


async def build_with(*providers, **kw):
    return await ContextBuilder(providers=list(providers)).build_context(
        kw.get("user_id", "u"), kw.get("message", "q"), kw.get("session_id", "s"))


CASES = [
    ("all present", "- likes AED", "Expenses in AED", "10:00 standup", "Pending Tasks:\n- [high] X"),
    ("memory empty", "", "Policy X", "10:00 standup", "Pending Tasks:\n- [low] Y"),
    ("rag miss sentinel", "- fact", "No specific corporate guidelines found.", "", ""),
    ("all empty", "", "", "", ""),
    ("whitespace only", "   ", "  \n ", "  ", " "),
    ("multiline", "- a\n- b", "L1\nL2", "09:00 A\n11:00 B", "Pending Tasks:\n- [med] Z"),
    ("rag needs strip", "- f", "  padded policy  ", "cal", "tsk"),
]


@pytest.mark.parametrize("label,mem,rag,cal,tsk", CASES, ids=[c[0] for c in CASES])
def test_prompt_sections_are_byte_identical(label, mem, rag, cal, tsk):
    """THE refactor contract: same inputs → same prompt strings as before."""
    bundle = asyncio.run(build_with(
        stub("memory", [mem]), stub("corporate", [rag]),
        stub("calendar", [cal]), stub("tasks", [tsk])))

    new_parts = build_sections(bundle)
    new_cal = calendar_text(bundle) or "No events scheduled today."
    new_tsk = tasks_text(bundle) or "No pending tasks."
    old_parts, old_cal, old_tsk = legacy_assemble(mem, rag, cal, tsk)

    assert new_parts == old_parts, f"prompt sections diverged for {label}"
    assert new_cal == old_cal
    assert new_tsk == old_tsk


def test_section_order_matches_legacy():
    """[MEMORY] came before [COMPANY KNOWLEDGE]; it still must."""
    bundle = asyncio.run(build_with(stub("memory", ["m"]), stub("corporate", ["c"])))
    parts = build_sections(bundle)
    assert parts[0].startswith("[MEMORY]")
    assert parts[1].startswith("[COMPANY KNOWLEDGE]")


def test_graph_section_appears_only_when_graph_returns_items():
    """Phase 4 turned graph context ON, but an empty graph must still emit no
    section — a bare header with nothing under it is worse than silence."""
    empty = asyncio.run(build_with(stub("memory", ["m"])))
    assert not any("[KNOWLEDGE GRAPH]" in p for p in build_sections(empty))

    populated = asyncio.run(build_with(stub("graph", ["a —USES→ b"])))
    assert any("[KNOWLEDGE GRAPH]" in p for p in build_sections(populated))


def test_graph_provider_is_enabled_by_default_but_switchable():
    """Phase 4 change: graph contributes by default; a Neo4j outage must still be
    survivable by turning it off without a deploy of anything else."""
    prov = registry.get("graph")
    assert prov is not None and prov.enabled is True
    from backend.context.providers import GraphProvider
    assert GraphProvider(enabled=False).enabled is False


def test_sql_provider_ships_disabled_and_empty():
    prov = registry.get("sql")
    assert prov is not None and prov.enabled is False
    assert asyncio.run(prov.collect(ContextRequest(user_id="u", message="m"))) == []


# ── isolation ────────────────────────────────────────────────────────────────

def test_one_provider_failing_does_not_affect_others():
    bundle = asyncio.run(build_with(
        stub("corporate", ["c"], fail=True), stub("memory", ["m"]),
        stub("calendar", ["cal"]), stub("tasks", ["t"])))
    assert bundle.text_of("memory") == "m"
    assert bundle.text_of("calendar") == "cal"
    assert bundle.text_of("tasks") == "t"
    assert bundle.text_of("corporate") == ""
    assert bundle.stats.failed == ["corporate"]


def test_timeout_is_isolated_and_recorded():
    bundle = asyncio.run(build_with(
        stub("graph", ["g"], delay=1.0, timeout=0.05), stub("memory", ["m"])))
    assert bundle.text_of("memory") == "m", "a hung provider must not block another"
    assert bundle.stats.timed_out == ["graph"]
    assert bundle.stats.providers["graph"].status == "timeout"


def test_disabled_provider_is_skipped_not_failed():
    bundle = asyncio.run(build_with(stub("sql", ["x"], enabled=False)))
    stat = bundle.stats.providers["sql"]
    assert stat.skipped and stat.ok, "disabled is not an error"


def test_builder_never_raises_even_if_everything_fails():
    bundle = asyncio.run(build_with(
        stub("corporate", ["c"], fail=True), stub("memory", ["m"], fail=True)))
    assert isinstance(bundle, ContextBundle)
    assert build_sections(bundle) == []


# ── concurrency ──────────────────────────────────────────────────────────────

def test_providers_run_concurrently_not_sequentially():
    import time
    provs = [stub(n, [n], delay=0.15) for n in ("corporate", "memory", "calendar", "tasks")]
    t0 = time.perf_counter()
    asyncio.run(build_with(*provs))
    took = time.perf_counter() - t0
    assert took < 0.45, f"4x150ms ran in {took:.2f}s — looks sequential"


# ── stats ────────────────────────────────────────────────────────────────────

def test_stats_capture_latency_items_and_tokens():
    bundle = asyncio.run(build_with(stub("memory", ["a fact worth many chars"])))
    stat = bundle.stats.providers["memory"]
    assert stat.items == 1 and stat.latency_ms >= 0 and stat.approx_tokens > 0
    assert bundle.stats.total_items == 1
    assert bundle.stats.as_dict()["providers"]["memory"]["status"] == "ok"


# ── extensibility ────────────────────────────────────────────────────────────

def test_unknown_provider_lands_in_extra_without_builder_changes():
    """Adding EmailProvider/SlackProvider must need no edit to builder or bundle."""
    bundle = asyncio.run(build_with(stub("slack", ["#eng: deploy done"])))
    assert bundle.extra["slack"][0].text == "#eng: deploy done"
    assert bundle.text_of("slack") == "#eng: deploy done"


def test_registry_rejects_duplicate_names():
    with pytest.raises(ValueError):
        registry.register(stub("corporate", []))


def test_only_filter_selects_a_subset():
    b = ContextBuilder(providers=[stub("memory", ["m"]), stub("tasks", ["t"])])
    bundle = asyncio.run(b.build_context("u", "q", "s", only=("memory",)))
    assert bundle.text_of("memory") == "m"
    assert "tasks" not in bundle.stats.providers


# ── bundle shape ─────────────────────────────────────────────────────────────

def test_items_are_structured_not_raw_strings():
    bundle = asyncio.run(build_with(stub("corporate", ["policy"])))
    item = bundle.corporate[0]
    assert item.provider == "corporate"
    assert hasattr(item, "score") and hasattr(item, "timestamp") and hasattr(item, "metadata")


def test_first_text_applies_the_caller_default():
    bundle = asyncio.run(build_with(stub("memory", [])))
    assert bundle.first_text("corporate", "fallback") == "fallback"
