"""The evaluation harness must control the graph provider that reaches the prompt.

Step 11.5 found that `EvaluationRunner` retrieves the graph twice: `_stage_graph`
(measured, and configured by `RunnerConfig`) and the `ContextBuilder`'s
`GraphProvider` (which actually builds the prompt, and was NOT configured by
`RunnerConfig`). A `graph_top_k` or `graph_depth` sweep therefore moved the
metrics while the prompt stayed byte-identical — every end-to-end reading came
back "no effect", for plumbing reasons rather than real ones.

These tests pin the corrected wiring and, just as importantly, the invariants that
make it safe: only the graph provider may change, and an un-overridden config must
leave the registry alone.
"""
from __future__ import annotations

import pytest

from backend.context.providers import registry as provider_registry
from backend.context.providers.graph import GraphProvider
from backend.evals.runner import EvaluationRunner, RunnerConfig


def registry_providers():
    import backend.context.providers  # noqa: F401 — ensure registration side effect
    return provider_registry.all_providers()


def graph_of(provs):
    return next((p for p in provs if p.name == "graph"), None)


# ── the wiring the defect was missing ───────────────────────────────────────

def test_graph_top_k_reaches_the_provider_that_builds_the_prompt():
    """The core regression. If someone reverts the substitution, `_stage_graph`
    still honours graph_top_k and the metrics still look right — only this fails."""
    provs = EvaluationRunner(RunnerConfig(graph_top_k=4))._answer_path_providers()
    assert provs is not None, "config differs from the registry; a list must be built"
    assert graph_of(provs).top_k == 4


def test_graph_depth_reaches_the_provider_that_builds_the_prompt():
    provs = EvaluationRunner(RunnerConfig(graph_depth=2))._answer_path_providers()
    assert provs is not None
    assert graph_of(provs).depth == 2


def test_both_graph_parameters_are_applied_together():
    provs = EvaluationRunner(
        RunnerConfig(graph_top_k=4, graph_depth=2))._answer_path_providers()
    g = graph_of(provs)
    assert (g.top_k, g.depth) == (4, 2)


# ── provider-set invariant (the Step 11.5 mistake, pinned) ──────────────────

def test_every_other_provider_is_preserved_by_identity():
    """The mistake this guards against: replacing the whole provider list drops
    corporate/tasks/calendar and silently changes what the prompt contains."""
    base = registry_providers()
    provs = EvaluationRunner(
        RunnerConfig(graph_top_k=4, graph_depth=2))._answer_path_providers()

    assert [p.name for p in provs] == [p.name for p in base], "order or membership changed"
    for old, new in zip(base, provs):
        if old.name == "graph":
            continue
        assert old is new, f"{old.name} was replaced; only the graph provider may change"


def test_named_providers_survive_the_substitution():
    provs = EvaluationRunner(RunnerConfig(graph_top_k=4))._answer_path_providers()
    names = {p.name for p in provs}
    for required in ("corporate", "tasks", "calendar", "graph"):
        assert required in names, f"{required} provider disappeared"


def test_the_shared_registry_instance_is_never_mutated():
    """A mutated singleton would leak the experiment's settings into production
    code paths and into every later test in the session."""
    before = graph_of(registry_providers())
    top_k_before, depth_before = before.top_k, before.depth

    provs = EvaluationRunner(
        RunnerConfig(graph_top_k=4, graph_depth=3))._answer_path_providers()
    assert graph_of(provs) is not before, "the registry instance was reused and mutated"

    after = graph_of(registry_providers())
    assert after is before
    assert (after.top_k, after.depth) == (top_k_before, depth_before)


# ── default behaviour must not move ─────────────────────────────────────────

def test_default_config_leaves_the_registry_untouched():
    """§11: an un-overridden RunnerConfig must behave exactly as before the fix.
    Returning None means the builder resolves the registry itself, so the default
    path is unchanged by construction rather than by coincidence."""
    assert EvaluationRunner(RunnerConfig())._answer_path_providers() is None


def test_config_matching_the_registry_is_a_no_op():
    g = graph_of(registry_providers())
    cfg = RunnerConfig(graph_top_k=g.top_k, graph_depth=g.depth)
    assert EvaluationRunner(cfg)._answer_path_providers() is None


def test_explicit_providers_override_is_passed_through_untouched():
    """The pre-existing escape hatch for offline/fixture runs must keep working."""
    sentinel = [GraphProvider(top_k=99, depth=7)]
    provs = EvaluationRunner(
        RunnerConfig(providers=sentinel, graph_top_k=4))._answer_path_providers()
    assert provs is sentinel
    assert provs[0].top_k == 99, "an explicit override must not be rewritten"


def test_production_defaults_are_unchanged():
    """Guards the values every prior baseline was measured against."""
    cfg = RunnerConfig()
    assert (cfg.graph_depth, cfg.graph_top_k) == (1, 8)
    assert (cfg.fusion_threshold, cfg.qdrant_top_k) == (0.6, 5)
    import inspect
    assert inspect.signature(
        GraphProvider.__init__).parameters["top_k"].default == 8


def test_graph_enabled_flag_is_carried_over():
    """The replacement must inherit the deployment's CONTEXT_GRAPH_ENABLED state
    rather than silently re-enabling a deliberately disabled graph."""
    base = graph_of(registry_providers())
    provs = EvaluationRunner(RunnerConfig(graph_top_k=4))._answer_path_providers()
    assert graph_of(provs).enabled == base.enabled
