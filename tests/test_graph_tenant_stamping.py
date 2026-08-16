"""POC-2: every supported graph writer stamps a tenant.

The read side was closed first (a tenant predicate in GraphRetrievalAPI), which
is the half that is easy to believe in. This is the other half, and it is the one
that decides whether the predicate ever means anything: a filter over data that
carries no tenant excludes nothing, and lenient mode reports a predicate ran
while every node stays visible.

The invariant under test is narrow and absolute:

    NO SUPPORTED GRAPH WRITE PATH MAY PRODUCE org_id = NULL

"Supported" means reachable through KnowledgeGraphPipeline or either builder.
Unstamped is not the same as shared: shared is a VALUE (`__shared__`) that a
filter can reason about, NULL is an absence it has to guess at, and the whole
strict-mode ratchet depends on being able to tell those apart.

Live Neo4j tests use `kgtest-` fixtures and delete them in teardown. The
production graph (522 entities / 3414 relationships / 0 stamped) is never
written to, and the historical backfill remains deliberately undone — see
test_the_historical_graph_is_not_touched_by_this_change.
"""
from __future__ import annotations

import inspect
import os

import pytest

from backend.knowledge_graph import builder as B
from backend.knowledge_graph.builder import (BatchKnowledgeGraphBuilder,
                                             KnowledgeGraphBuilder)
from backend.knowledge_graph.pipeline import KnowledgeGraphPipeline
from backend.knowledge_graph.types import ExtractedEntity, ExtractedRelationship

TENANT_A = "tenant-a-11111111"
TENANT_B = "tenant-b-22222222"


# ── a service stub: captures rows without touching Neo4j ─────────────────────

class _CapturingService:
    """Stands in for GraphService and records exactly what would be written."""

    def __init__(self):
        self.node_rows: list[dict] = []
        self.rel_rows: list[dict] = []
        self.nodes: list[tuple] = []
        self.rels: list[tuple] = []

    # BatchKnowledgeGraphBuilder path
    def batch_merge_nodes(self, label, rows, **kw):
        self.node_rows.extend(rows)
        return type("S", (), {"nodes_created": len(rows), "nodes_merged": 0,
                              "relationships_created": 0})()

    def batch_merge_relationships(self, rel_type, rows, **kw):
        self.rel_rows.extend(rows)
        return type("S", (), {"relationships_created": len(rows), "nodes_created": 0,
                              "nodes_merged": 0})()

    # KnowledgeGraphBuilder (legacy) path
    def merge_node_with_stats(self, label, node_id, props):
        self.nodes.append((label, node_id, props))
        return None, type("S", (), {"nodes_created": 1, "nodes_merged": 0})()

    def merge_relationship_with_stats(self, s_lbl, s_id, rel, t_lbl, t_id, props):
        self.rels.append((rel, s_id, t_id, props))
        return None, type("S", (), {"relationships_created": 1})()


def _entity(name="Widget", etype="Technology", **props):
    e = ExtractedEntity(name=name, type=etype)
    if props:
        e.properties.update(props)
    return e


def _prov():
    from backend.knowledge_graph.provenance import Provenance
    return Provenance(source_id="kgtest-src", source_type="document", confidence=0.9)


def _build_batch(svc, entities, rels=(), **kw):
    b = BatchKnowledgeGraphBuilder(service=svc, source="document", **kw)
    b.build(entities, list(rels), provenance=_prov())
    return b


# ── A. tenant-specific entity stamping ───────────────────────────────────────

def test_batch_builder_stamps_the_tenant_on_entities():
    svc = _CapturingService()
    _build_batch(svc, [_entity("KgtestAlpha")], tenant_id=TENANT_A)
    assert svc.node_rows, "no node row was written"
    assert svc.node_rows[0]["props"][B.TENANT_PROPERTY] == TENANT_A


def test_two_tenants_produce_two_different_stamps():
    a, b = _CapturingService(), _CapturingService()
    _build_batch(a, [_entity("KgtestAlpha")], tenant_id=TENANT_A)
    _build_batch(b, [_entity("KgtestAlpha")], tenant_id=TENANT_B)
    assert a.node_rows[0]["props"][B.TENANT_PROPERTY] == TENANT_A
    assert b.node_rows[0]["props"][B.TENANT_PROPERTY] == TENANT_B


# ── B. tenant-specific relationship stamping ─────────────────────────────────

def test_batch_builder_stamps_the_tenant_on_relationships():
    svc = _CapturingService()
    e1, e2 = _entity("KgtestAlpha"), _entity("KgtestBeta")
    rel = ExtractedRelationship(source="KgtestAlpha", type="USES", target="KgtestBeta")
    _build_batch(svc, [e1, e2], [rel], tenant_id=TENANT_A)
    assert svc.rel_rows, "no relationship row was written"
    assert svc.rel_rows[0]["props"][B.TENANT_PROPERTY] == TENANT_A


def test_legacy_builder_stamps_both_nodes_and_relationships():
    """The legacy writer was verified to produce org_id=None before this change."""
    svc = _CapturingService()
    e1, e2 = _entity("KgtestAlpha"), _entity("KgtestBeta")
    rel = ExtractedRelationship(source="KgtestAlpha", type="USES", target="KgtestBeta")
    KnowledgeGraphBuilder(service=svc, source="document",
                          tenant_id=TENANT_A).build([e1, e2], [rel])
    assert svc.nodes, "legacy builder wrote no node"
    for _label, _nid, props in svc.nodes:
        assert props[B.TENANT_PROPERTY] == TENANT_A
    assert svc.rels, "legacy builder wrote no relationship"
    for _rel, _s, _t, props in svc.rels:
        assert props[B.TENANT_PROPERTY] == TENANT_A


# ── C. explicit shared stamping ──────────────────────────────────────────────

def test_explicit_shared_is_stamped_as_shared():
    svc = _CapturingService()
    _build_batch(svc, [_entity("KgtestShared")], tenant_id=B.SHARED_TENANT)
    assert svc.node_rows[0]["props"][B.TENANT_PROPERTY] == B.SHARED_TENANT


def test_shared_is_a_value_not_an_absence():
    """`__shared__` must be a real string a filter can match. If it were None or
    "" the strict ratchet could not distinguish shared-on-purpose from never
    stamped, which is the distinction the whole design rests on."""
    assert isinstance(B.SHARED_TENANT, str) and B.SHARED_TENANT.strip()
    assert B.SHARED_TENANT not in (None, "", "null", "None")


# ── D. no NULL from any supported writer ─────────────────────────────────────

@pytest.mark.parametrize("tenant", [None, TENANT_A, B.SHARED_TENANT])
def test_no_supported_writer_ever_produces_a_null_tenant(tenant):
    """The invariant, across BOTH builders and every tenant argument including
    the omitted one."""
    svc = _CapturingService()
    e1, e2 = _entity("KgtestAlpha"), _entity("KgtestBeta")
    rel = ExtractedRelationship(source="KgtestAlpha", type="USES", target="KgtestBeta")

    kw = {} if tenant is None else {"tenant_id": tenant}
    _build_batch(svc, [e1, e2], [rel], **kw)
    KnowledgeGraphBuilder(service=svc, source="document",
                          **kw).build([_entity("KgtestGamma")], [])

    written = ([r["props"] for r in svc.node_rows]
               + [r["props"] for r in svc.rel_rows]
               + [p for _l, _i, p in svc.nodes]
               + [p for _r, _s, _t, p in svc.rels])
    assert written, "nothing was written — the test would be vacuous"
    for props in written:
        value = props.get(B.TENANT_PROPERTY)
        assert value not in (None, ""), f"NULL tenant written: {props}"


def test_omitting_the_tenant_yields_shared_not_null():
    svc = _CapturingService()
    _build_batch(svc, [_entity("KgtestNoTenant")])
    assert svc.node_rows[0]["props"][B.TENANT_PROPERTY] == B.SHARED_TENANT


# ── E. extractor properties cannot override the tenant ───────────────────────

def test_an_extracted_property_cannot_set_its_own_tenant():
    """An entity whose extracted properties contain `org_id` must not be able to
    choose its own visibility — the tenant is written last, deliberately."""
    svc = _CapturingService()
    hostile = _entity("KgtestHostile", **{B.TENANT_PROPERTY: TENANT_B})
    _build_batch(svc, [hostile], tenant_id=TENANT_A)
    assert svc.node_rows[0]["props"][B.TENANT_PROPERTY] == TENANT_A, \
        "an extractor-supplied org_id overrode the caller's tenant"


def test_an_extracted_relationship_property_cannot_set_its_own_tenant():
    svc = _CapturingService()
    e1, e2 = _entity("KgtestAlpha"), _entity("KgtestBeta")
    rel = ExtractedRelationship(source="KgtestAlpha", type="USES", target="KgtestBeta")
    rel.properties[B.TENANT_PROPERTY] = TENANT_B
    _build_batch(svc, [e1, e2], [rel], tenant_id=TENANT_A)
    assert svc.rel_rows[0]["props"][B.TENANT_PROPERTY] == TENANT_A


def test_the_legacy_builder_has_the_same_override_protection():
    svc = _CapturingService()
    hostile = _entity("KgtestHostile", **{B.TENANT_PROPERTY: TENANT_B})
    KnowledgeGraphBuilder(service=svc, source="document",
                          tenant_id=TENANT_A).build([hostile], [])
    assert svc.nodes[0][2][B.TENANT_PROPERTY] == TENANT_A


# ── F. pipeline propagation ──────────────────────────────────────────────────

def test_pipeline_passes_the_tenant_to_the_batch_builder():
    p = KnowledgeGraphPipeline(service=_CapturingService(), source="document",
                               tenant_id=TENANT_A)
    assert isinstance(p.builder, BatchKnowledgeGraphBuilder)
    assert p.builder._tenant_id == TENANT_A


def test_pipeline_passes_the_tenant_to_the_legacy_builder():
    p = KnowledgeGraphPipeline(service=_CapturingService(), source="document",
                               legacy_builder=True, tenant_id=TENANT_A)
    assert isinstance(p.builder, KnowledgeGraphBuilder)
    assert p.builder._tenant_id == TENANT_A


def test_pipeline_without_a_tenant_defaults_to_shared():
    p = KnowledgeGraphPipeline(service=_CapturingService(), source="document")
    assert p.builder._tenant_id == B.SHARED_TENANT


def test_pipeline_does_not_restamp_an_injected_builder():
    """An injected builder already carries its author's decision. Silently
    re-stamping it would make the constructor argument lie about what is written."""
    injected = BatchKnowledgeGraphBuilder(service=_CapturingService(),
                                          source="document", tenant_id=TENANT_B)
    p = KnowledgeGraphPipeline(builder=injected, tenant_id=TENANT_A)
    assert p.builder is injected
    assert p.builder._tenant_id == TENANT_B


def test_the_tenant_reaches_neo4j_through_a_full_pipeline_build():
    """End-to-end through the pipeline's own builder, not a hand-made one."""
    svc = _CapturingService()
    p = KnowledgeGraphPipeline(service=svc, source="document", tenant_id=TENANT_A)
    p.builder.build([_entity("KgtestPipelineThing")], [], provenance=_prov())
    assert svc.node_rows[0]["props"][B.TENANT_PROPERTY] == TENANT_A


# ── G. reader / writer agree on the property name ────────────────────────────

def test_writer_and_reader_use_the_same_property_name():
    """A writer stamping `org_id` and a filter reading `tenant_id` would produce a
    graph that looks stamped and filters nothing."""
    from backend.knowledge_graph.retrieval import api as kg_api
    assert B.TENANT_PROPERTY == kg_api.TENANT_PROPERTY

    from backend.orchestrator import graph_tools
    assert B.TENANT_PROPERTY == graph_tools.TENANT_PROPERTY


def test_the_graph_and_qdrant_shared_sentinels_match():
    """One vocabulary across both stores, or `__shared__` means two things."""
    from backend import ingest
    assert B.SHARED_TENANT == ingest.SHARED_TENANT


# ── H. backward compatibility ────────────────────────────────────────────────

def test_existing_callers_that_pass_no_tenant_still_work():
    """Every pre-POC-2 construction site — tests, benchmarks, eval fixtures —
    passes no tenant. They must keep working, with shared semantics."""
    for kwargs in ({"service": _CapturingService(), "source": "conversation"},
                   {"service": _CapturingService()}):
        assert BatchKnowledgeGraphBuilder(**kwargs)._tenant_id == B.SHARED_TENANT
        assert KnowledgeGraphBuilder(**kwargs)._tenant_id == B.SHARED_TENANT


def test_tenant_id_is_keyword_and_optional_on_every_writer():
    for cls in (BatchKnowledgeGraphBuilder, KnowledgeGraphBuilder, KnowledgeGraphPipeline):
        p = inspect.signature(cls.__init__).parameters["tenant_id"]
        assert p.default is None, f"{cls.__name__}.tenant_id must default to None"


def test_entity_identity_is_unchanged():
    """Tenancy is additive metadata. If it entered the node id, every
    `expected_graph_nodes` value in the 174 frozen cases would be invalidated."""
    svc = _CapturingService()
    _build_batch(svc, [_entity("KgtestIdentity")], tenant_id=TENANT_A)
    a_id = svc.node_rows[0]["id"]

    svc2 = _CapturingService()
    _build_batch(svc2, [_entity("KgtestIdentity")], tenant_id=TENANT_B)
    assert svc2.node_rows[0]["id"] == a_id, "the tenant leaked into entity identity"

    from backend.knowledge_graph import queries
    assert B.TENANT_PROPERTY not in queries.batch_merge_nodes(["Technology"]), \
        "the tenant must not appear in the MERGE key"


# ── live Neo4j: the stamp actually lands, and production is untouched ────────

def _graph_up() -> bool:
    try:
        from backend.knowledge_graph import is_enabled
        return bool(is_enabled())
    except Exception:
        return False


@pytest.mark.skipif(not _graph_up(), reason="needs a live Neo4j")
def test_the_stamp_lands_in_neo4j_and_production_is_untouched():
    from backend.knowledge_graph import get_graph_service
    from backend.knowledge_graph.client import get_driver

    d = get_driver()
    with d.session() as s:
        before = s.run("MATCH (n:Entity) RETURN count(n) AS c").single()["c"]
        before_rels = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]

    svc = get_graph_service()
    try:
        BatchKnowledgeGraphBuilder(service=svc, source="document", tenant_id=TENANT_A)\
            .build([_entity("KgtestLiveAlpha")], [], provenance=_prov())
        BatchKnowledgeGraphBuilder(service=svc, source="document")\
            .build([_entity("KgtestLiveShared")], [], provenance=_prov())

        with d.session() as s:
            rows = {r["nm"]: r["org"] for r in s.run(
                "MATCH (n) WHERE toLower(coalesce(n.canonical_name,'')) STARTS WITH 'kgtestlive' "
                "RETURN n.canonical_name AS nm, n.org_id AS org")}
        assert rows.get("KgtestLiveAlpha") == TENANT_A, rows
        assert rows.get("KgtestLiveShared") == B.SHARED_TENANT, rows
        assert None not in rows.values(), f"a NULL tenant reached Neo4j: {rows}"
    finally:
        with d.session() as s:
            s.run("MATCH (n) WHERE toLower(coalesce(n.canonical_name,'')) STARTS WITH 'kgtestlive' "
                  "OR toLower(coalesce(n.name,'')) STARTS WITH 'kgtestlive' "
                  "OR toLower(coalesce(n.id,'')) CONTAINS 'kgtestlive' DETACH DELETE n")

    with d.session() as s:
        after = s.run("MATCH (n:Entity) RETURN count(n) AS c").single()["c"]
        after_rels = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        residue = s.run("MATCH (n) WHERE toLower(coalesce(n.id,'')) CONTAINS 'kgtest' "
                        "RETURN count(n) AS c").single()["c"]
    assert after == before, f"production Entity count changed: {before} -> {after}"
    assert after_rels == before_rels, "production relationship count changed"
    assert residue == 0, "kgtest fixtures were left behind"


@pytest.mark.skipif(not _graph_up(), reason="needs a live Neo4j")
def test_the_historical_graph_is_not_touched_by_this_change():
    """POC-2 stamps FUTURE writes only. The 522 historical entities belong to
    `user_1`, whose organisation is unresolved, so they stay unstamped and strict
    mode stays off. This test fails if someone backfills them without resolving
    that ownership first — which is the mistake it exists to prevent."""
    from backend.knowledge_graph.client import get_driver
    with get_driver().session() as s:
        stamped = s.run("MATCH (n:Entity) WHERE n.org_id IS NOT NULL "
                        "RETURN count(n) AS c").single()["c"]
    assert stamped == 0, (
        f"{stamped} historical entities carry a tenant. If this is a deliberate, "
        f"authorised backfill, update this test with the ownership decision that "
        f"justified it.")


@pytest.mark.skipif(not _graph_up(), reason="needs a live Neo4j")
def test_strict_mode_is_off_while_the_graph_is_unstamped():
    from backend.knowledge_graph.retrieval import api as kg_api
    assert kg_api.TENANT_STRICT is False, (
        "strict mode hides every unstamped node; on this graph that is all 522 of "
        "them. It may only be enabled after the historical backfill.")
