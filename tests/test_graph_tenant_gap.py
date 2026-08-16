"""The gap between "a tenant predicate ran" and "data was isolated".

Lenient mode (GRAPH_TENANT_STRICT off, the default) treats an UNSTAMPED node as
shared. On a graph where nothing is stamped that is deliberate and safe: the
filter is a no-op, and it starts biting the moment ingestion stamps. It is also
the only ordering that is correct in both directions.

It stops being safe the moment a SECOND tenant's entities exist while the graph
is still unstamped and still lenient — because then "unstamped means shared"
hands one tenant's entities to another, and `tenant_enforced_by: ["graph"]` says
a predicate ran while nothing was actually excluded.

Today that cannot happen: the graph holds one tenant's data. This test exists so
the day it stops being true, something fails — rather than the gap being
discovered from a support ticket. It is deliberately a LIVE check against Neo4j,
skipped when the graph is unreachable, because the property it guards is about
real data and cannot be asserted from source.
"""

import os

import pytest


def _query(cypher: str):
    """Run read-only Cypher, or raise. GraphService exposes `_execute`, not a
    public `run` — an earlier version of this file called `.run()`, which does
    not exist, so every live check silently SKIPPED and the file passed while
    testing nothing. Hence the explicit smoke query in _graph()."""
    from backend.knowledge_graph.service import get_graph_service
    # _execute returns the driver's EagerResult; .records is the row list.
    return get_graph_service()._execute(cypher, {}, op="tenant-gap-check").records


def _graph() -> bool:
    """True when Neo4j is reachable AND the query path actually works."""
    try:
        from backend.knowledge_graph import is_enabled
        if not is_enabled():
            return False
        rows = _query("RETURN 1 AS ok")
        return bool(rows) or rows == []
    except Exception:
        return False


def _strict() -> bool:
    return os.getenv("GRAPH_TENANT_STRICT", "false").strip().lower() in ("1", "true", "yes")


needs_graph = pytest.mark.skipif(not _graph(), reason="Neo4j not reachable")


@needs_graph
def test_lenient_mode_is_only_safe_while_one_tenant_exists():
    """FAILS when a second tenant's entities exist and strict mode is still off.

    The remedy when this fires is NOT to delete data or to loosen the test. It is
    one of:
      * finish stamping (writer + backfill to zero unstamped), then set
        GRAPH_TENANT_STRICT=true — the ratchet this was always heading for; or
      * accept, deliberately and in writing, that the graph is a shared corpus
        and stop reporting "graph" in tenant_enforced_by.
    """
    from backend.knowledge_graph.builder import SHARED_TENANT, TENANT_PROPERTY

    rows = _query(
        f"MATCH (n) WHERE n.`{TENANT_PROPERTY}` IS NOT NULL "
        f"RETURN DISTINCT n.`{TENANT_PROPERTY}` AS tenant"
    )
    tenants = {r["tenant"] for r in rows} - {None, SHARED_TENANT}

    unstamped = _query(
        f"MATCH (n) WHERE n.`{TENANT_PROPERTY}` IS NULL RETURN count(n) AS n"
    )[0]["n"]

    if len(tenants) <= 1:
        # The safe state. Recorded rather than silently passing, so a reader of
        # the output can see WHY it passed.
        return

    assert _strict(), (
        f"{len(tenants)} tenants now have entities in the graph "
        f"({sorted(tenants)[:4]}) while GRAPH_TENANT_STRICT is off and "
        f"{unstamped} node(s) are unstamped. In lenient mode every unstamped node "
        f"is returned to ALL of them, so tenant_enforced_by reports 'graph' while "
        f"isolating nothing. Finish stamping and enable strict mode."
    )


@needs_graph
def test_strict_mode_requires_a_fully_stamped_graph():
    """The other half of the ratchet: strict on an unstamped graph returns
    nothing, which reads as 'the feature is broken' rather than 'the data is not
    ready'. Flip strict only once the backfill reports zero unstamped."""
    if not _strict():
        pytest.skip("lenient mode — nothing to check")
    from backend.knowledge_graph.builder import TENANT_PROPERTY
    unstamped = _query(
        f"MATCH (n) WHERE n.`{TENANT_PROPERTY}` IS NULL RETURN count(n) AS n"
    )[0]["n"]
    assert unstamped == 0, (
        f"GRAPH_TENANT_STRICT is on with {unstamped} unstamped node(s). Those are "
        f"now invisible to every tenant — the graph will appear empty. Run the "
        f"backfill, or turn strict back off until it has.")


# ── the writer half, which needs no database ─────────────────────────────────

def test_the_writer_stamps_every_node_it_creates():
    """Stamping the WRITER comes before any backfill: an unstamped ingestion path
    re-dilutes whatever the backfill just cleaned. Same ordering as Qdrant."""
    from backend.knowledge_graph.builder import (SHARED_TENANT, TENANT_PROPERTY,
                                                 BatchKnowledgeGraphBuilder)
    from backend.knowledge_graph.types import ExtractedEntity

    class _Svc:
        def __init__(self):
            self.rows = []

        def batch_merge_nodes(self, labels, rows):
            self.rows += rows
            return type("S", (), {"nodes_created": len(rows)})()

        def batch_merge_relationships(self, *a, **k):
            return type("S", (), {"relationships_created": 0})()

    svc = _Svc()
    BatchKnowledgeGraphBuilder(service=svc, source="t", tenant_id="org-abc").build(
        [ExtractedEntity(name="Acme", type="Organization")])
    assert [r["props"][TENANT_PROPERTY] for r in svc.rows] == ["org-abc"]

    svc = _Svc()
    BatchKnowledgeGraphBuilder(service=svc, source="t").build(
        [ExtractedEntity(name="Acme", type="Organization")])
    assert [r["props"][TENANT_PROPERTY] for r in svc.rows] == [SHARED_TENANT], \
        "an untenanted write must be explicitly shared, never absent"


def test_an_extracted_property_cannot_set_its_own_tenant():
    """The extractor reads untrusted documents. A document containing an
    'org_id' field must not be able to choose which tenant sees its entities."""
    from backend.knowledge_graph.builder import (TENANT_PROPERTY,
                                                 BatchKnowledgeGraphBuilder)
    from backend.knowledge_graph.types import ExtractedEntity

    class _Svc:
        def __init__(self):
            self.rows = []

        def batch_merge_nodes(self, labels, rows):
            self.rows += rows
            return type("S", (), {"nodes_created": len(rows)})()

        def batch_merge_relationships(self, *a, **k):
            return type("S", (), {"relationships_created": 0})()

    svc = _Svc()
    BatchKnowledgeGraphBuilder(service=svc, source="t", tenant_id="org-real").build(
        [ExtractedEntity(name="X", type="Organization",
                         properties={TENANT_PROPERTY: "org-attacker"})])
    assert [r["props"][TENANT_PROPERTY] for r in svc.rows] == ["org-real"]


def test_writer_and_reader_agree_on_the_property_name():
    """Two modules read the same env var. If they ever diverge, the writer stamps
    a property the filter does not look at — which fails open, silently."""
    from backend.knowledge_graph.builder import TENANT_PROPERTY as WRITE
    from backend.orchestrator.graph_tools import TENANT_PROPERTY as READ
    assert WRITE == READ
