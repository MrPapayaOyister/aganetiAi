"""POC-1 — `knowledge_search`: the frozen GraphRAG pipeline as an executor tool.

The claim under test is a WIRING claim, not a retrieval-quality claim:

    authoritative LangGraph → knowledge_search → existing frozen GraphRAG
        → Qdrant + Neo4j evidence → structured provenance

...with no change to validated GraphRAG behaviour.

All tests are pure. The REAL pipeline runs — real ContextBuilder, real fusion,
ranking, compression and budgeting — but the two leaf providers are stubbed, so
nothing here needs Qdrant, Neo4j or an LLM. Stubbing the providers rather than
`build_ranked_context` is deliberate: it is what makes test_graph_and_document_
evidence_both_survive_the_real_pipeline evidence of anything at all.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.context.bundle import ContextItem
from backend.context.providers import ContextProvider, ContextRequest, registry as provider_registry
from backend.orchestrator import knowledge, registry


# ── stub providers, named exactly as the two real knowledge sources ──────────

class StubCorporate(ContextProvider):
    """Stands in for the Qdrant corpus provider."""
    name = "corporate"
    timeout = 5.0

    def __init__(self):
        self.seen: list[ContextRequest] = []

    async def collect(self, request):
        self.seen.append(request)
        return [ContextItem(
            text="Applications must include an audited financial statement for the last fiscal year.",
            provider="corporate", source="policy_handbook.pdf", score=0.81,
            metadata={"document_id": "doc-77", "chunk_index": 3, "source_type": "pdf"},
        )]


class StubGraph(ContextProvider):
    """Stands in for the Neo4j graph provider — emits the same item shapes the
    real GraphProvider does (providers/graph.py:79-104): a relationship and an
    entity, each with its evidence metadata."""
    name = "graph"
    timeout = 3.0

    def __init__(self):
        self.seen: list[ContextRequest] = []

    async def collect(self, request):
        self.seen.append(request)
        return [
            ContextItem(
                text="application —REQUIRES→ audited-financial-statement",
                provider="graph", source="doc-77", score=0.72,
                metadata={"kind": "relationship", "rel_type": "REQUIRES",
                          "start_id": "application", "end_id": "audited-financial-statement",
                          "source_ids": ["doc-77"], "source_types": ["document"],
                          "confidence": 0.9, "observations": 2, "corroborated": True},
            ),
            ContextItem(
                text="Audited Financial Statement (Document)",
                provider="graph", source="doc-77", score=0.65,
                metadata={"kind": "entity", "entity_id": "audited-financial-statement",
                          "labels": ["Document"], "hop": 1, "signals": {}},
            ),
        ]


class StubPersonal(ContextProvider):
    """A NON-knowledge provider. Must never appear in knowledge_search output."""
    name = "calendar"
    timeout = 5.0

    def __init__(self):
        self.called = False

    async def collect(self, request):
        self.called = True
        return [ContextItem(text="09:00 Dentist appointment", provider="calendar",
                            source="calendar")]


@pytest.fixture
def stubbed_providers():
    """Swap the process-wide provider registry for stubs, then restore it.

    The registry is a module-global shared with every other test, so the
    snapshot/restore is not optional.
    """
    saved = dict(provider_registry._REGISTRY)
    provider_registry.clear()
    corp, graph, personal = StubCorporate(), StubGraph(), StubPersonal()
    for p in (corp, graph, personal):
        provider_registry.register(p, replace=True)
    # build_ranked_context goes through the process-wide builder singleton, which
    # reads the registry at call time — but reset it anyway so a builder cached
    # with an explicit provider list from another test cannot leak in.
    import backend.context.builder as builder_mod
    saved_builder = builder_mod._default
    builder_mod._default = None
    try:
        yield corp, graph, personal
    finally:
        builder_mod._default = saved_builder
        provider_registry.clear()
        for name, p in saved.items():
            provider_registry.register(p, replace=True)


def _run(coro):
    return asyncio.run(coro)


# ── A. Registration ──────────────────────────────────────────────────────────

def test_knowledge_search_is_registered_in_the_typed_registry():
    tool = registry.get("knowledge_search")
    assert tool is not None, "knowledge_search must be registered"
    assert tool.name == "knowledge_search"
    assert "knowledge_search" in registry.all_names()


def test_knowledge_search_is_offered_to_the_model():
    """Registration is not enough — it must reach the model's tool schema list,
    which is what the executor sends to the gateway."""
    schemas = registry.openai_schemas(["knowledge_search"])
    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "knowledge_search"
    assert fn["parameters"]["required"] == ["query"]


def test_knowledge_search_is_read_only_and_permissioned():
    tool = registry.get("knowledge_search")
    # Read-only: it must NOT go through the outbound approval gate, and it must
    # reuse the existing permission vocabulary rather than invent a new one.
    assert tool.is_outbound is False
    assert tool.required_permission == "documents.read"
    assert tool.required_permission in registry.all_permissions()


def test_knowledge_search_carries_a_risk_classification():
    """Every registered tool must be classified, or `decide_tool` silently
    defaults it to "task" (guardrails.py:185). It reads the same corpus as
    search_documents with no egress, so it takes the same category."""
    from backend import guardrails

    assert guardrails.TOOL_CATEGORY.get("knowledge_search") == "read"
    assert (guardrails.TOOL_CATEGORY["knowledge_search"]
            == guardrails.TOOL_CATEGORY["search_documents"])


# ── B. Invocation through the existing tool mechanism ────────────────────────

def test_tool_executes_through_the_handler_the_executor_calls(stubbed_providers):
    """graph.py:118 does `await tool.handler(ctx, **args)`. Drive exactly that."""
    tool = registry.get("knowledge_search")
    ctx = {"user_id": "u-1", "agent_id": "primary", "board_id": "", "tenant_id": ""}
    out = _run(tool.handler(ctx, query="what documents are required?"))

    assert isinstance(out, str) and out.strip()
    assert "[D1]" in out, "document evidence must be cited"
    assert "[G1]" in out, "graph evidence must be cited"


def test_handler_signature_matches_the_executor_calling_convention():
    tool = registry.get("knowledge_search")
    params = list(inspect.signature(tool.handler).parameters)
    assert params[0] == "ctx"
    assert "query" in params
    assert inspect.iscoroutinefunction(tool.handler)


def test_retrieval_failure_is_reported_not_raised(monkeypatch):
    """A Neo4j/Qdrant outage must not fail the turn — the executor needs a string
    answer for every tool call it made."""
    async def boom(*a, **k):
        raise RuntimeError("neo4j unreachable")

    monkeypatch.setattr(knowledge, "knowledge_search", boom)
    out = _run(knowledge._knowledge_search({"user_id": "u-1"}, query="anything"))
    assert "unavailable" in out.lower()


def test_empty_result_tells_the_model_not_to_guess(stubbed_providers):
    corp, graph, _ = stubbed_providers

    async def nothing(request):
        return []

    corp.collect = nothing
    graph.collect = nothing
    out = _run(registry.get("knowledge_search").handler({"user_id": "u-1"}, query="unknown topic"))
    assert "No matching knowledge" in out
    assert "Do not guess" in out


# ── C. The GraphRAG path: both evidence kinds survive the real pipeline ──────

def test_graph_and_document_evidence_both_survive_the_real_pipeline(stubbed_providers):
    """The whole point of POC-1: Qdrant AND Neo4j evidence, through the real
    fuse → rank → compress → budget pipeline, with provenance intact."""
    result = _run(knowledge.knowledge_search(
        "what documents are required?", user_id="u-1"))

    assert result.document_citations, "expected Qdrant/corpus evidence"
    assert result.graph_citations, "expected Neo4j/graph evidence"

    doc = result.document_citations[0]
    assert doc.provider == "corporate"
    assert doc.kind == "document"
    assert doc.document_id == "doc-77"
    assert doc.source == "policy_handbook.pdf"

    kinds = {c.kind for c in result.graph_citations}
    assert "relationship" in kinds or "entity" in kinds

    rel = next((c for c in result.graph_citations if c.kind == "relationship"), None)
    if rel is not None:
        assert rel.graph_edge == "application -REQUIRES-> audited-financial-statement"

    ent = next((c for c in result.graph_citations if c.kind == "entity"), None)
    if ent is not None:
        assert ent.entity_id == "audited-financial-statement"


def test_result_is_structured_enough_for_a_citation_ui(stubbed_providers):
    """POC-13/POC-23 consume this. Assert the serialised shape, not just objects."""
    result = _run(knowledge.knowledge_search("required documents", user_id="u-1"))
    d = result.as_dict()

    assert d["query"] == "required documents"
    assert d["user_id"] == "u-1"
    assert set(d["providers_used"]) == {"corporate", "graph"}
    assert d["counts"]["total"] == len(d["citations"])
    assert d["counts"]["documents"] >= 1
    assert d["counts"]["graph"] >= 1
    assert "fusion" in d and "stats" in d

    for c in d["citations"]:
        assert c["text"]
        assert c["provider"] in {"corporate", "graph"}
        assert "final_score" in c and "corroboration" in c
        assert "evidence" in c and isinstance(c["evidence"], list)


def test_provenance_uses_the_existing_evidence_structures(stubbed_providers):
    """Rule: do not invent a new provenance system. Fusion attaches Evidence to
    items; the citation must carry it through rather than re-derive it."""
    result = _run(knowledge.knowledge_search("required documents", user_id="u-1"))
    with_evidence = [c for c in result.citations if c.evidence]
    assert with_evidence, "fusion-produced Evidence must reach the citation"
    ev = with_evidence[0].evidence[0]
    assert "provider" in ev and "source_id" in ev


def test_only_knowledge_providers_are_consulted(stubbed_providers):
    """A knowledge lookup must not pull the user's calendar into the answer."""
    _, _, personal = stubbed_providers
    result = _run(knowledge.knowledge_search("required documents", user_id="u-1"))

    assert personal.called is False, "non-knowledge provider must not be invoked"
    assert {c.provider for c in result.citations} <= {"corporate", "graph"}


# ── D. Frozen configuration ──────────────────────────────────────────────────

FROZEN = {"resolver_threshold": 0.82, "graph_depth": 1, "graph_top_k": 8,
          "hop_decay": 0.55, "fusion_threshold": 0.60, "max_nodes": 100}


def test_tool_schema_exposes_no_retrieval_knobs():
    """The model must not be able to re-tune GraphRAG by passing an argument.
    The ONLY input is the query."""
    params = registry.get("knowledge_search").parameters
    assert set(params["properties"]) == {"query"}
    assert params["required"] == ["query"]

    forbidden = {"depth", "top_k", "graph_top_k", "threshold", "resolver_threshold",
                 "hop_decay", "fusion", "fusion_threshold", "max_nodes", "limit",
                 "providers", "only"}
    assert not (set(params["properties"]) & forbidden)


def test_wrapper_passes_no_retrieval_configuration(monkeypatch):
    """The wrapper must supply NO frozen parameter — the production registry
    already holds them, so passing nothing is the only way to preserve them."""
    captured = {}

    async def fake_brc(user_id, message, session_id="", **kwargs):
        captured["args"] = (user_id, message, session_id)
        captured["kwargs"] = kwargs
        from backend.context.bundle import RankedContextBundle
        return RankedContextBundle()

    import backend.context as context_pkg
    monkeypatch.setattr(context_pkg, "build_ranked_context", fake_brc)

    _run(knowledge.knowledge_search("q", user_id="u-1", session_id="s-1"))

    assert captured["args"] == ("u-1", "q", "s-1")
    # Exactly two kwargs, and NEITHER is a retrieval parameter:
    #   `only`      selects PROVIDERS.
    #   `tenant_id` is the isolation boundary (POC-2).
    # Both change WHICH data is eligible; neither changes HOW it is retrieved, so
    # the frozen configuration is still supplied entirely by the provider registry.
    assert set(captured["kwargs"]) == {"only", "tenant_id"}
    assert tuple(captured["kwargs"]["only"]) == ("corporate", "graph")

    banned = {"depth", "top_k", "graph_top_k", "hop_decay", "fusion", "max_nodes",
              "resolver_threshold", "threshold", "weights", "budget_policy"}
    assert not (set(captured["kwargs"]) & banned)


def test_production_graph_provider_defaults_are_the_frozen_values():
    """Guards the premise the wrapper relies on: passing nothing yields the
    frozen configuration. If this fails, the wrapper is no longer safe."""
    from backend.context.providers.graph import GraphProvider
    p = GraphProvider()
    assert p.top_k == FROZEN["graph_top_k"]
    assert p.depth == FROZEN["graph_depth"]


def test_invoking_the_tool_does_not_mutate_the_registered_graph_provider():
    """The strongest available production-impact assertion: the live provider
    instance is unchanged by a tool call."""
    real = provider_registry.get("graph")
    if real is None:                                    # pragma: no cover
        pytest.skip("graph provider not registered in this environment")
    before = (real.top_k, real.depth, real.enabled)
    try:
        _run(registry.get("knowledge_search").handler({"user_id": "u-1"}, query="ping"))
    except Exception:                                   # noqa: BLE001
        pass                                            # outage is fine; mutation is not
    assert (real.top_k, real.depth, real.enabled) == before


def test_module_declares_no_frozen_retrieval_constants():
    """Drift guard: a future edit that hard-codes 0.82/8/0.55/… in this wrapper
    would be a second, divergent source of truth for a frozen value."""
    src = inspect.getsource(knowledge)
    code = "\n".join(line for line in src.splitlines()
                     if not line.strip().startswith("#"))
    # strip the module docstring, which legitimately names the frozen values
    if '"""' in code:
        code = code.split('"""', 2)[-1]
    for literal in ("0.82", "0.55", "0.60", "max_nodes"):
        assert literal not in code, f"frozen value {literal!r} must not be restated here"


# ── E. Existing tool compatibility ───────────────────────────────────────────

def test_search_documents_is_unchanged_and_still_registered():
    tool = registry.get("search_documents")
    assert tool is not None
    assert tool.is_outbound is False
    assert tool.required_permission == "documents.read"
    # It must still be the Qdrant-only implementation — knowledge_search is
    # ADDITIVE, not a replacement.
    assert tool.handler.__name__ == "_search_documents"
    assert tool.handler.__module__ == "backend.orchestrator.registry"


def test_search_documents_still_uses_plain_qdrant_search(monkeypatch):
    """Pin the behaviour: search_documents calls ingest.search_corporate and does
    NOT go anywhere near the graph."""
    calls = {}

    def fake_search_corporate(query, top_k, owner=None, *a, **k):
        calls["args"] = (query, top_k, owner)
        return [{"source": "policy.pdf", "text": "some policy text"}]

    import backend.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "search_corporate", fake_search_corporate)

    out = _run(registry.get("search_documents").handler({"user_id": "u-9"}, query="policy"))
    assert calls["args"] == ("policy", 6, "u-9")
    assert "policy.pdf" in out


def test_the_two_tools_are_distinct_registrations():
    ks, sd = registry.get("knowledge_search"), registry.get("search_documents")
    assert ks is not sd
    assert ks.handler is not sd.handler


# ── F. Scoping ───────────────────────────────────────────────────────────────

def test_caller_identity_reaches_the_retrieval_request(stubbed_providers):
    """The corporate provider scopes Qdrant by `request.user_id`
    (providers/corporate.py:36-37 → search_corporate(query, top_k, user_id)).
    The tool must pass the caller through unchanged."""
    corp, graph, _ = stubbed_providers
    _run(registry.get("knowledge_search").handler(
        {"user_id": "u-42", "agent_id": "primary"}, query="required documents"))

    assert corp.seen, "corporate provider was not consulted"
    assert corp.seen[0].user_id == "u-42"
    assert graph.seen[0].user_id == "u-42"


def test_missing_caller_identity_does_not_become_someone_else(stubbed_providers):
    """A ctx with no user must not fall back to a default account — that is the
    exact class of defect tests/test_identity_normalization.py exists to stop."""
    corp, _, _ = stubbed_providers
    _run(registry.get("knowledge_search").handler({}, query="required documents"))
    assert corp.seen[0].user_id == ""


def test_tenant_is_carried_for_poc2_without_being_invented(stubbed_providers):
    """POC-2 seam: ctx already has tenant_id (graph.py:85-86). It is recorded on
    the result so the filter can be threaded later without redesigning the tool
    — and it is never fabricated when absent."""
    result = _run(knowledge.knowledge_search("q", user_id="u-1", tenant_id="org-a"))
    assert result.tenant_id == "org-a"
    assert result.as_dict()["tenant_id"] == "org-a"

    out_ctx = {"user_id": "u-1", "tenant_id": "org-b"}
    res2 = _run(knowledge.knowledge_search(
        "q", user_id=out_ctx["user_id"], tenant_id=out_ctx["tenant_id"]))
    assert res2.tenant_id == "org-b"

    res3 = _run(knowledge.knowledge_search("q", user_id="u-1"))
    assert res3.tenant_id == ""
