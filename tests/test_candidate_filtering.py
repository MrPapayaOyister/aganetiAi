"""Candidate-filtering semantics for the Step 8 experiment.

Step 8 evaluated filtering `:Document` nodes out of entity-resolution candidates
*after* full-text retrieval. Production is unchanged; these tests pin the
properties any future implementation must preserve, and encode the two findings
that make the naive version wrong:

  * `:Document` nodes carry the base `:Entity` label, so they are reachable
    through the registry cache and exact-name lookup as well as through fuzzy
    matching — filtering only the fuzzy rung is incomplete.
  * 7 nodes carry `:Document` AND a real entity label (`ajman-police` has eight
    labels). Excluding every `:Document` node loses 5 legitimate resolutions.

Graph-dependent tests skip cleanly when Neo4j is unavailable so the suite stays
runnable offline.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.filterwarnings("ignore")

REAL = lambda labels: [l for l in labels if l not in ("Entity", "Document")]


def is_document(labels):
    return "Document" in labels


def is_pure_document(labels):
    """A node that is ONLY a document — no real entity role alongside it."""
    return is_document(labels) and not REAL(labels)


# ── filter semantics (no graph needed) ──────────────────────────────────────

def test_pure_document_filter_keeps_hybrid_nodes():
    """The distinction the whole experiment turns on. `ajman-police` really is a
    document AND an organization; a filter that cannot tell those apart deletes
    a correct resolution."""
    pure = ["Entity", "Document"]
    hybrid = ["Entity", "Document", "Organization", "Project"]
    plain = ["Entity", "Technology"]

    assert is_pure_document(pure)
    assert not is_pure_document(hybrid)
    assert not is_pure_document(plain)
    # the naive filter cannot make the distinction
    assert is_document(pure) and is_document(hybrid)


def test_filtering_is_exact_not_fuzzy():
    """Filtering must be a label-membership decision. Nothing about it may
    depend on name similarity, or it becomes a second, hidden threshold."""
    for labels in (["Entity", "Document"], ["Entity", "Document", "Service"],
                   ["Entity", "Model"]):
        a = is_pure_document(labels)
        b = is_pure_document(list(reversed(labels)))
        assert a == b, "label ORDER must not change the verdict"
    # a name that merely looks document-ish is irrelevant to the decision
    assert not is_pure_document(["Entity", "Technology"])


def test_production_threshold_is_unchanged():
    """Step 7 concluded: keep 0.82. Step 8 must not have moved it."""
    import inspect

    from backend.knowledge_graph.retrieval.registry import CanonicalEntityRegistry
    default = inspect.signature(
        CanonicalEntityRegistry.__init__).parameters["fuzzy_threshold"].default
    assert default == 0.82


def test_production_resolver_does_not_filter_candidates_yet():
    """Step 8 is an experiment. If someone lands the filter in production without
    a controlled rollout, this fails and says so."""
    import inspect

    from backend.knowledge_graph.retrieval import registry as reg
    src = inspect.getsource(reg.CanonicalEntityRegistry.fuzzy_candidates)
    assert "Document" not in src, (
        "fuzzy_candidates() now references Document — production filtering was "
        "introduced; Step 8 recommended a controlled change, not a silent one")


# ── graph-dependent invariants (read-only) ─────────────────────────────────

@pytest.fixture(scope="module")
def graph():
    try:
        from neo4j import GraphDatabase

        import config.settings as S
        drv = GraphDatabase.driver(S.NEO4J_URI, auth=(S.NEO4J_USERNAME, S.NEO4J_PASSWORD))
        with drv.session(database=S.NEO4J_DATABASE) as s:
            s.run("RETURN 1").consume()
        yield drv
        drv.close()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Neo4j unavailable: {e}")


def test_documents_carry_the_base_entity_label(graph):
    """The root cause. If this ever stops being true the filter is unnecessary —
    and the full-text index would no longer reach Documents at all."""
    import config.settings as S
    with graph.session(database=S.NEO4J_DATABASE) as s:
        n = s.run("MATCH (n:Document) WHERE NOT n:Entity RETURN count(n) AS c").single()["c"]
    assert n == 0, "some :Document nodes no longer carry :Entity — re-check the index"


def test_hybrid_document_nodes_exist_and_would_be_lost_by_naive_filtering(graph):
    """Guards the specific regression: excluding every :Document node removes
    nodes that are legitimately resolvable entities."""
    import config.settings as S
    with graph.session(database=S.NEO4J_DATABASE) as s:
        rows = list(s.run(
            "MATCH (n:Document) WHERE size([l IN labels(n) WHERE NOT l IN "
            "['Entity','Document']]) > 0 RETURN n.id AS id, labels(n) AS labels"))
    assert rows, "expected hybrid Document/entity nodes to exist"
    ids = {r["id"] for r in rows}
    assert "ajman-police" in ids
    for r in rows:
        assert not is_pure_document(r["labels"])


def test_filtering_candidates_does_not_remove_documents_from_traversal(graph):
    """Candidate filtering is a resolution-time concern only. Documents must stay
    reachable by graph traversal, or the graph loses its document evidence."""
    import config.settings as S
    with graph.session(database=S.NEO4J_DATABASE) as s:
        reachable = s.run(
            "MATCH (n:Document)-[]-(x) RETURN count(DISTINCT n) AS c").single()["c"]
        total = s.run("MATCH (n:Document) RETURN count(n) AS c").single()["c"]
    assert total > 0
    assert reachable > 0, "Documents must remain reachable through relationships"


def test_wrong_entity_candidates_are_not_documents(graph):
    """The remaining collisions (DB -> Dar Al Ber Society, VLM -> vLLM) are real
    entities. Document filtering cannot address them, and this test records why:
    if either ever became a Document, the conclusion would change."""
    import config.settings as S
    with graph.session(database=S.NEO4J_DATABASE) as s:
        for eid in ("dar-al-ber-society", "vllm"):
            r = s.run("MATCH (n {id:$i}) RETURN labels(n) AS labels", i=eid).single()
            if r is None:
                pytest.skip(f"{eid} not present")
            assert not is_document(r["labels"]), f"{eid} is a Document — revisit Step 8"


def test_fulltext_index_is_untouched(graph):
    """Step 8 must not have modified the index (spec §4)."""
    import config.settings as S
    from backend.knowledge_graph import queries as q
    with graph.session(database=S.NEO4J_DATABASE) as s:
        rows = list(s.run("SHOW INDEXES YIELD name, labelsOrTypes, properties "
                          "WHERE name = $n RETURN labelsOrTypes, properties",
                          n=q.FULLTEXT_INDEX))
    if not rows:
        pytest.skip("full-text index not present in this environment")
    assert rows[0]["labelsOrTypes"] == ["Entity"]
    assert sorted(rows[0]["properties"]) == ["aliases", "canonical_name"]
