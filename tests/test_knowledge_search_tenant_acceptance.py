"""POC-2 acceptance: cross-tenant isolation THROUGH knowledge_search.

Deliberately not a unit test of the two filters. Those exist already
(test_qdrant_tenant_isolation.py). What is proven here is the property a
low-level test cannot reach: that a caller of the TOOL — after resolution,
expansion, fusion, ranking, compression and budgeting have all run — never
receives another tenant's evidence from EITHER store.

Data is controlled, code is real:
  * Qdrant   — a scratch collection, deleted in teardown; the real
               `search_corporate` filter runs against it.
  * Neo4j    — a real `GraphRetrievalAPI` with a stub resolver/retriever, so the
               real tenant predicate in `retrieve()` runs on known nodes without
               needing the LLM mention extractor or touching the live graph.
  * fusion   — the real ContextBuilder pipeline.

The last test is the one that protects everybody else: a caller with NO tenant
(the eval harness, the CLI, internal tools) must still get results. Turning a
missing tenant into `tenant_id == ""` would match nothing and read as data loss.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from backend import ingest
from backend.context.bundle import ContextItem
from backend.context.providers import ContextProvider, ContextRequest, registry as prov_registry
from backend.knowledge_graph.retrieval import api as kg_api
from backend.knowledge_graph.retrieval.types import ScoredEdge, ScoredNode, Subgraph
from backend.orchestrator import knowledge

TENANT_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
DIM = 384


def _qdrant_up() -> bool:
    try:
        import httpx
        return httpx.get(f"{os.getenv('QDRANT_URL', 'http://127.0.0.1:6333')}/collections",
                         timeout=3).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _qdrant_up(), reason="needs a live Qdrant")


# ── Qdrant: a scratch corpus with three tenants' documents ───────────────────

@pytest.fixture
def scratch_corpus(monkeypatch):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    col = f"poc2accept_{uuid.uuid4().hex[:8]}"
    c = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"), timeout=30)
    c.create_collection(col, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    vec = [1.0] + [0.0] * (DIM - 1)
    owner = "__org__"
    # The three texts are deliberately DISSIMILAR. An earlier version of this
    # fixture used "ALPHA CONFIDENTIAL: tenant A margin policy" and "BETA
    # CONFIDENTIAL: tenant B margin policy", which share almost every token — the
    # frozen fusion threshold (0.60) merged them as duplicates and the test then
    # measured deduplication rather than isolation. Keep them lexically distinct
    # so a missing document means a FILTER removed it, not fusion.
    c.upsert(col, points=[
        PointStruct(id=1, vector=vec, payload={
            "user_id": owner, "org_id": TENANT_A, "source": "alpha-policy.pdf",
            "text": "ALPHASECRET zirconium quarterly rebate ladder for northern depots.",
            "document_id": "doc-a"}),
        PointStruct(id=2, vector=vec, payload={
            "user_id": owner, "org_id": TENANT_B, "source": "beta-policy.pdf",
            "text": "BETASECRET tungsten haulage surcharge waiver, coastal division.",
            "document_id": "doc-b"}),
        PointStruct(id=3, vector=vec, payload={
            "user_id": owner, "org_id": ingest.SHARED_TENANT, "source": "handbook.pdf",
            "text": "SHAREDNOTE public holiday calendar and expense claim deadlines.",
            "document_id": "doc-shared"}),
    ], wait=True)

    monkeypatch.setattr(ingest, "RAG_COLLECTION", col)
    monkeypatch.setattr(ingest, "get_client", lambda: c)
    monkeypatch.setattr(ingest, "get_embedder",
                        lambda: type("E", (), {"embed": lambda self, xs: [_V()]})())
    try:
        yield col
    finally:
        c.delete_collection(col)


class _V:
    def tolist(self):
        return [1.0] + [0.0] * (DIM - 1)


# ── Neo4j: a real API with a stub graph, so the real predicate runs ──────────

class _StubResolved:
    def __init__(self, eid):
        self.entity_id, self.resolved, self.mention = eid, True, eid


class _StubResolver:
    """Returns both tenants' seeds. The tenant predicate must remove the wrong one."""
    def resolve(self, question, allow_fuzzy=True):
        return ([_StubResolved("alpha-entity"), _StubResolved("beta-entity")],
                {"extract_ms": 0.0, "resolve_ms": 0.0,
                 "mentions_found": 2, "mentions_resolved": 2})


class _StubRetriever:
    queries = 1

    def expand(self, ids, depth=None, rel_types=None):
        a = ScoredNode(entity_id="alpha-entity", canonical_name="Alpha Margin Rule",
                       labels=["Entity"], hop=0, score=0.9,
                       properties={"org_id": TENANT_A})
        b = ScoredNode(entity_id="beta-entity", canonical_name="Beta Margin Rule",
                       labels=["Entity"], hop=0, score=0.9,
                       properties={"org_id": TENANT_B})
        shared = ScoredNode(entity_id="shared-entity", canonical_name="Handbook Rule",
                            labels=["Entity"], hop=1, score=0.5, properties={})
        sg = Subgraph(nodes=[a, b, shared], seed_ids=["alpha-entity", "beta-entity"])
        sg.edges = [
            ScoredEdge(rel_type="GOVERNS", start_id="alpha-entity", end_id="shared-entity",
                       score=0.8),
            ScoredEdge(rel_type="GOVERNS", start_id="beta-entity", end_id="shared-entity",
                       score=0.8),
        ]
        return sg


class _TenantGraphProvider(ContextProvider):
    """The real GraphProvider contract over a real GraphRetrievalAPI."""
    name = "graph"
    timeout = 5.0

    async def collect(self, request: ContextRequest):
        api = kg_api.GraphRetrievalAPI(resolver=_StubResolver(), retriever=_StubRetriever())
        ctx = await asyncio.to_thread(api.retrieve, request.message, depth=1, top_k=8,
                                      tenant_id=(request.tenant_id or None))
        out = []
        for e in ctx.subgraph.edges:
            out.append(ContextItem(text=f"{e.start_id} —{e.rel_type}→ {e.end_id}",
                                   provider="graph", source="graph", score=e.score,
                                   metadata={"kind": "relationship", "rel_type": e.rel_type,
                                             "start_id": e.start_id, "end_id": e.end_id}))
        for n in ctx.related + [r for r in ctx.resolved]:
            name = getattr(n, "canonical_name", getattr(n, "entity_id", ""))
            out.append(ContextItem(text=f"{name}", provider="graph", source="graph",
                                   score=getattr(n, "score", 0.0),
                                   metadata={"kind": "entity",
                                             "entity_id": getattr(n, "entity_id", "")}))
        return out


@pytest.fixture
def wired(scratch_corpus):
    """Real corporate provider + real-API graph provider, in the real registry."""
    from backend.context.providers.corporate import CorporateKnowledgeProvider
    import backend.context.builder as builder_mod

    saved = dict(prov_registry._REGISTRY)
    saved_builder = builder_mod._default
    prov_registry.clear()
    prov_registry.register(CorporateKnowledgeProvider(top_k=5), replace=True)
    prov_registry.register(_TenantGraphProvider(), replace=True)
    builder_mod._default = None
    try:
        yield
    finally:
        builder_mod._default = saved_builder
        prov_registry.clear()
        for _n, p in saved.items():
            prov_registry.register(p, replace=True)


def _search(tenant):
    return asyncio.run(knowledge.knowledge_search(
        "margin policy", user_id="__org__", tenant_id=tenant))


def _texts(res):
    return " || ".join(c.text for c in res.citations)


# ── Part D: documents ────────────────────────────────────────────────────────

def test_tenant_a_sees_own_and_shared_documents_never_tenant_b(wired):
    res = _search(TENANT_A)
    docs = _texts(res)
    assert "ALPHASECRET" in docs, "tenant A must see its own document"
    assert "SHAREDNOTE" in docs, "tenant A must see the shared corpus"
    assert "BETASECRET" not in docs, "TENANT A RECEIVED TENANT B'S DOCUMENT"


def test_tenant_b_sees_own_and_shared_documents_never_tenant_a(wired):
    res = _search(TENANT_B)
    docs = _texts(res)
    assert "BETASECRET" in docs
    assert "SHAREDNOTE" in docs
    assert "ALPHASECRET" not in docs, "TENANT B RECEIVED TENANT A'S DOCUMENT"


# ── Part D: graph evidence ───────────────────────────────────────────────────

def test_tenant_a_sees_only_its_own_graph_evidence(wired):
    res = _search(TENANT_A)
    graph = " || ".join(c.text for c in res.graph_citations)
    assert "alpha-entity" in graph or "Alpha" in graph, "A must see its own graph evidence"
    assert "beta-entity" not in graph and "Beta" not in graph, \
        "TENANT A RECEIVED TENANT B'S GRAPH EVIDENCE"


def test_tenant_b_sees_only_its_own_graph_evidence(wired):
    res = _search(TENANT_B)
    graph = " || ".join(c.text for c in res.graph_citations)
    assert "beta-entity" in graph or "Beta" in graph
    assert "alpha-entity" not in graph and "Alpha" not in graph, \
        "TENANT B RECEIVED TENANT A'S GRAPH EVIDENCE"


def test_a_cross_tenant_edge_is_dropped_whole(wired):
    """An edge survives only if BOTH endpoints do, or its text discloses the
    hidden entity's id even though the node itself was removed."""
    res = _search(TENANT_A)
    for c in res.graph_citations:
        assert "beta-entity" not in c.text, f"leaked via edge text: {c.text}"


# ── Part D: the combined path ────────────────────────────────────────────────

def test_combined_path_never_leaks_through_either_provider(wired):
    """Qdrant + Neo4j + fusion + citations, asserted together — the acceptance
    criterion for POC-2."""
    a, b = _search(TENANT_A), _search(TENANT_B)

    assert a.document_citations and a.graph_citations, "A must get BOTH kinds of evidence"
    assert b.document_citations and b.graph_citations, "B must get BOTH kinds of evidence"

    for c in a.citations:
        assert "BETASECRET" not in c.text.upper(), f"A leaked: {c.text}"
    for c in b.citations:
        assert "ALPHASECRET" not in c.text.upper(), f"B leaked: {c.text}"

    # Part C: enforcement is CLAIMED only where a predicate ran.
    assert set(a.tenant_enforced_by) == {"corporate", "graph"}
    assert set(a.as_dict()["tenant_enforced_by"]) == {"corporate", "graph"}


# ── Part D: the system caller must not be starved ────────────────────────────

def test_a_caller_with_no_tenant_still_gets_results(wired):
    """The eval harness, the CLI and internal tools pass no tenant. They must get
    the full corpus, NOT an empty result — a missing tenant must never become
    `tenant_id == ""`, which matches nothing."""
    res = asyncio.run(knowledge.knowledge_search("margin policy", user_id="__org__"))
    docs = _texts(res)

    assert res.citations, "a tenant-less caller was starved of results"
    assert "ALPHASECRET" in docs and "BETASECRET" in docs and "SHAREDNOTE" in docs, \
        "no tenant must mean NO predicate, not an empty one"
    assert res.tenant_enforced_by == [], "nothing was enforced, so nothing may be claimed"


def test_empty_string_tenant_is_treated_as_absent_not_as_a_tenant(wired):
    """`tenant_id=""` is the shape a half-wired caller produces. It must behave as
    'no tenant' rather than filtering everything away."""
    res = asyncio.run(knowledge.knowledge_search("margin policy", user_id="__org__",
                                                 tenant_id=""))
    assert res.citations, "an empty tenant string emptied the result"
    assert res.tenant_enforced_by == []


def test_an_unknown_tenant_sees_only_shared(wired):
    """A real but unrecognised tenant is not an error: it sees the shared corpus
    and nobody's private data."""
    res = _search("cccccccc-3333-4333-8333-cccccccccccc")
    docs = _texts(res)
    assert "SHAREDNOTE" in docs
    assert "ALPHASECRET" not in docs and "BETASECRET" not in docs


# ── the graph predicate under strict mode ────────────────────────────────────

def test_strict_mode_hides_unstamped_graph_nodes(monkeypatch):
    """Strict is what a fully-stamped graph will use. Verified on a stub graph
    because the live one carries no tenant at all — enabling it there returns
    nothing, which is exactly why it is off."""
    api = kg_api.GraphRetrievalAPI(resolver=_StubResolver(), retriever=_StubRetriever())

    lenient = api.retrieve("q", depth=1, top_k=8, tenant_id=TENANT_A, tenant_strict=False)
    ids = {n.entity_id for n in lenient.subgraph.nodes}
    assert "alpha-entity" in ids and "shared-entity" in ids  # unstamped == shared
    assert "beta-entity" not in ids

    strict = api.retrieve("q", depth=1, top_k=8, tenant_id=TENANT_A, tenant_strict=True)
    ids = {n.entity_id for n in strict.subgraph.nodes}
    assert ids == {"alpha-entity"}, ids  # unstamped now excluded


def test_no_tenant_applies_no_graph_predicate():
    """Backward compatibility for GraphRetrievalAPI's existing callers."""
    api = kg_api.GraphRetrievalAPI(resolver=_StubResolver(), retriever=_StubRetriever())
    ctx = api.retrieve("q", depth=1, top_k=8)
    ids = {n.entity_id for n in ctx.subgraph.nodes}
    assert ids == {"alpha-entity", "beta-entity", "shared-entity"}, ids
    assert len(ctx.resolved) == 2, "resolution must be untouched without a tenant"
