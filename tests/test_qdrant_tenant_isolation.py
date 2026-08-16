"""Tenant isolation on the knowledge path (POC-2, Qdrant half).

The boundary being tested is `org_id` on a `corporate_memory` chunk, added ON TOP
OF the pre-existing `user_id` ACL rather than replacing it. Both must hold: an org
match alone must not expose another user's private document, and a user match
alone must not reach across tenants.

Two properties matter more than the happy path and are tested first:

  * a caller with NO tenant gets NO org predicate — not an empty one. `org_id == ""`
    matches nothing, so inventing a filter for an unresolved tenant would return an
    empty context and look like data loss.
  * lenient mode (the default) admits UNSTAMPED chunks. That is what makes this
    deployable before the backfill: on today's corpus, where 967 of 1009 chunks
    carry no tenant, a strict filter returns almost nothing.

The integration test at the bottom needs a live Qdrant and skips without one; the
rest are pure and assert on the filter that WOULD be sent.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from qdrant_client.models import Filter, FieldCondition, IsEmptyCondition

from backend import ingest


TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"


# ── the org predicate itself ─────────────────────────────────────────────────

def _conditions(f: Filter):
    return list(f.should or [])


def test_lenient_admits_this_tenant_the_shared_corpus_and_unstamped():
    clause = ingest.tenant_clause(TENANT_A, strict=False)
    conds = _conditions(clause)

    org = [c for c in conds if isinstance(c, FieldCondition) and c.key == "org_id"]
    assert org, "the org predicate must name org_id"
    allowed = set(org[0].match.any)
    assert allowed == {TENANT_A, ingest.SHARED_TENANT}, allowed

    assert any(isinstance(c, IsEmptyCondition) for c in conds), (
        "lenient mode must admit unstamped chunks, or it returns nothing on the "
        "current corpus")


def test_strict_excludes_unstamped():
    conds = _conditions(ingest.tenant_clause(TENANT_A, strict=True))
    assert not any(isinstance(c, IsEmptyCondition) for c in conds)
    org = [c for c in conds if isinstance(c, FieldCondition) and c.key == "org_id"]
    assert set(org[0].match.any) == {TENANT_A, ingest.SHARED_TENANT}


def test_another_tenant_is_never_in_the_allowed_set():
    """The whole point. B must not appear in A's predicate under either mode."""
    for strict in (True, False):
        conds = _conditions(ingest.tenant_clause(TENANT_A, strict=strict))
        org = [c for c in conds if isinstance(c, FieldCondition) and c.key == "org_id"]
        assert TENANT_B not in set(org[0].match.any)


def test_default_strictness_is_lenient():
    """Shipping strict-by-default would empty the corpus on deploy."""
    assert ingest.QDRANT_TENANT_STRICT is False


# ── search_corporate: what actually gets sent ────────────────────────────────

class _CapturingClient:
    """Stands in for QdrantClient and records the filter it was handed."""

    def __init__(self):
        self.query_filter = None

    def collection_exists(self, _name):
        return True

    def query_points(self, *, collection_name, query, limit, with_payload, query_filter):
        self.query_filter = query_filter
        class _R: points = []
        return _R()


@pytest.fixture
def captured(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(ingest, "get_client", lambda: client)
    monkeypatch.setattr(ingest, "get_embedder",
                        lambda: type("E", (), {"embed": lambda self, xs: [_Vec()]})())
    return client


class _Vec:
    def tolist(self):
        return [0.0] * 384


def _org_conditions(f: Filter):
    """Every org_id predicate anywhere in the filter (they sit in a nested should)."""
    found = []
    for cond in list(f.must or []):
        if isinstance(cond, Filter):
            found.extend(c for c in (cond.should or [])
                         if isinstance(c, FieldCondition) and c.key == "org_id")
        elif isinstance(cond, FieldCondition) and cond.key == "org_id":
            found.append(cond)
    return found


def test_no_tenant_means_no_org_predicate(captured):
    """The compatibility guarantee: every existing caller keeps working unchanged.

    An empty tenant must produce NO condition rather than `org_id == ""`, which
    would match nothing and silently return an empty context.
    """
    ingest.search_corporate("q", 3, "user-a")
    assert not _org_conditions(captured.query_filter)

    ingest.search_corporate("q", 3, "user-a", tenant_id="")
    assert not _org_conditions(captured.query_filter)

    ingest.search_corporate("q", 3, "user-a", tenant_id=None)
    assert not _org_conditions(captured.query_filter)


def test_a_tenant_adds_the_org_predicate(captured):
    ingest.search_corporate("q", 3, "user-a", tenant_id=TENANT_A)
    org = _org_conditions(captured.query_filter)
    assert org, "a supplied tenant must reach the query"
    assert set(org[0].match.any) == {TENANT_A, ingest.SHARED_TENANT}


def test_the_user_acl_is_not_replaced_by_the_tenant(captured):
    """Both boundaries, ANDed. A tenant match must not widen the user ACL."""
    ingest.search_corporate("q", 3, "user-a", tenant_id=TENANT_A)
    user = [c for c in (captured.query_filter.must or [])
            if isinstance(c, FieldCondition) and c.key == "user_id"]
    assert user, "the user ACL must survive"
    assert set(user[0].match.any) == {"user-a", ingest.ORG_OWNER}


def test_tenant_composes_with_source_type_and_since(captured):
    ingest.search_corporate("q", 3, "user-a", source_types=["file"], since=1000,
                            tenant_id=TENANT_A)
    keys = [c.key for c in (captured.query_filter.must or []) if isinstance(c, FieldCondition)]
    assert "user_id" in keys and "source_type" in keys and "timestamp" in keys
    assert _org_conditions(captured.query_filter)


# ── ingestion stamps a tenant ────────────────────────────────────────────────

def test_ingest_all_stamps_the_shared_tenant(monkeypatch, tmp_path):
    """The regression that matters: `ingest_all` must never write a NULL tenant
    again, or every backfill is undone by the next scheduler tick."""
    seen = {}

    def fake_ingest_file(client, path, owner=ingest.ORG_OWNER, **kw):
        seen["org_id"] = kw.get("org_id")
        return 1

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "doc.txt").write_text("hello")

    monkeypatch.setattr(ingest, "DATA_VAULT_DIR", str(vault))
    monkeypatch.setattr(ingest, "ingest_file", fake_ingest_file)
    monkeypatch.setattr(ingest, "get_client", lambda: object())
    monkeypatch.setattr(ingest, "ensure_collection", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "_load_state", lambda: {})
    monkeypatch.setattr(ingest, "_save_state", lambda s: None)
    monkeypatch.setattr(ingest, "_delete_source", lambda *a, **k: None)

    ingest.ingest_all()
    assert seen["org_id"] == ingest.SHARED_TENANT, (
        "ingest_all must stamp the shared tenant, not None")


def test_ingest_file_still_accepts_a_real_org():
    """The per-user paths (storage/indexing.py, main.py) already pass a real org;
    the signature must not have changed under them."""
    import inspect
    sig = inspect.signature(ingest.ingest_file)
    assert "org_id" in sig.parameters
    assert sig.parameters["org_id"].default is None


# ── propagation through the context engine ───────────────────────────────────

def test_context_request_carries_a_tenant():
    from backend.context.providers import ContextRequest
    r = ContextRequest(user_id="u", message="m")
    assert r.tenant_id == ""
    assert ContextRequest(user_id="u", message="m", tenant_id=TENANT_A).tenant_id == TENANT_A


def test_corporate_provider_forwards_the_tenant(monkeypatch):
    from backend.context.providers.corporate import CorporateKnowledgeProvider
    from backend.context.providers import ContextRequest

    got = {}

    def fake_search(query, top_k, owner, source_types=None, since=None,
                    tenant_id=None, tenant_strict=None):
        got["owner"], got["tenant_id"] = owner, tenant_id
        return []

    monkeypatch.setattr("backend.ingest.search_corporate", fake_search)
    p = CorporateKnowledgeProvider()

    asyncio.run(p.collect(ContextRequest(user_id="u-1", message="q", tenant_id=TENANT_A)))
    assert got == {"owner": "u-1", "tenant_id": TENANT_A}

    # No tenant must arrive as None, not "" — see test_no_tenant_means_no_org_predicate.
    asyncio.run(p.collect(ContextRequest(user_id="u-1", message="q")))
    assert got["tenant_id"] is None


def test_builder_threads_the_tenant_to_providers():
    from backend.context.builder import ContextBuilder
    from backend.context.providers import ContextProvider

    seen = {}

    class Probe(ContextProvider):
        name = "corporate"
        async def collect(self, request):
            seen["tenant_id"] = request.tenant_id
            return []

    b = ContextBuilder(providers=[Probe()])
    asyncio.run(b.build_ranked_context("u", "q", "s", tenant_id=TENANT_A))
    assert seen["tenant_id"] == TENANT_A


# ── knowledge_search: the tool surface ───────────────────────────────────────

def test_knowledge_search_passes_the_tenant_into_retrieval(monkeypatch):
    from backend.orchestrator import knowledge

    got = {}

    async def fake_brc(user_id, message, session_id="", **kw):
        got.update(kw)
        from backend.context.bundle import RankedContextBundle
        return RankedContextBundle()

    import backend.context as ctx_pkg
    monkeypatch.setattr(ctx_pkg, "build_ranked_context", fake_brc)

    asyncio.run(knowledge.knowledge_search("q", user_id="u", tenant_id=TENANT_A))
    assert got.get("tenant_id") == TENANT_A
    assert tuple(got.get("only")) == ("corporate", "graph")


def test_result_reports_which_providers_actually_enforced_the_tenant(monkeypatch):
    """`tenant_id` being populated is NOT the same as it being enforced.

    Both stores now carry a predicate — Qdrant on the `org_id` payload key, Neo4j
    on the node property — so both are claimed. When only Qdrant did, "graph" was
    deliberately absent; this list must keep tracking what is actually enforced
    rather than what is intended, because a caller has no other way to tell.
    """
    from backend.orchestrator import knowledge

    async def fake_brc(user_id, message, session_id="", **kw):
        from backend.context.bundle import RankedContextBundle
        return RankedContextBundle()

    import backend.context as ctx_pkg
    monkeypatch.setattr(ctx_pkg, "build_ranked_context", fake_brc)

    with_tenant = asyncio.run(knowledge.knowledge_search("q", user_id="u", tenant_id=TENANT_A))
    assert set(with_tenant.tenant_enforced_by) == {"corporate", "graph"}

    # Every claimed provider must actually be queried, or the claim is empty.
    assert set(with_tenant.tenant_enforced_by) <= set(knowledge.KNOWLEDGE_PROVIDERS)

    without = asyncio.run(knowledge.knowledge_search("q", user_id="u"))
    assert without.tenant_enforced_by == [], "no tenant means nothing was enforced"
    assert without.as_dict()["tenant_enforced_by"] == []


# ── live Qdrant: two tenants, real isolation ─────────────────────────────────

def _qdrant_up() -> bool:
    try:
        import httpx
        url = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
        return httpx.get(f"{url}/collections", timeout=3).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _qdrant_up(), reason="needs a live Qdrant")
def test_two_tenants_cannot_see_each_others_documents():
    """End-to-end against a real Qdrant, in a scratch collection.

    A pure test can prove the filter is SHAPED right; only this proves Qdrant
    agrees with that shape.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct, VectorParams, Distance

    col = f"tenanttest_{uuid.uuid4().hex[:8]}"
    c = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"), timeout=30)
    c.create_collection(col, vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    try:
        owner = "shared-owner"
        c.upsert(col, points=[
            PointStruct(id=1, vector=[1, 0, 0, 0],
                        payload={"user_id": owner, "org_id": TENANT_A, "text": "A doc"}),
            PointStruct(id=2, vector=[1, 0, 0, 0],
                        payload={"user_id": owner, "org_id": TENANT_B, "text": "B doc"}),
            PointStruct(id=3, vector=[1, 0, 0, 0],
                        payload={"user_id": owner, "org_id": ingest.SHARED_TENANT,
                                 "text": "shared doc"}),
            PointStruct(id=4, vector=[1, 0, 0, 0],
                        payload={"user_id": owner, "text": "legacy unstamped"}),
        ], wait=True)

        def ids_for(tenant, strict):
            f = Filter(must=[
                FieldCondition(key="user_id", match=__import__(
                    "qdrant_client.models", fromlist=["MatchAny"]).MatchAny(any=[owner])),
                ingest.tenant_clause(tenant, strict=strict)])
            return {p.id for p in c.query_points(col, query=[1, 0, 0, 0], limit=10,
                                                 query_filter=f).points}

        lenient_a = ids_for(TENANT_A, False)
        assert 2 not in lenient_a, "tenant A retrieved tenant B's document"
        assert lenient_a == {1, 3, 4}, lenient_a   # own + shared + unstamped

        strict_a = ids_for(TENANT_A, True)
        assert strict_a == {1, 3}, strict_a        # unstamped excluded
        assert 2 not in strict_a

        lenient_b = ids_for(TENANT_B, False)
        assert 1 not in lenient_b, "tenant B retrieved tenant A's document"
        assert lenient_b == {2, 3, 4}, lenient_b
    finally:
        c.delete_collection(col)
