"""
Knowledge-graph tests.

Split by what they need, so most of the suite runs anywhere:
  * pure       — normalizer, slugify, provenance, sources. No LLM, no database.
  * fake-LLM   — extractor parsing, driven by canned JSON. No network.
  * graph      — batch merge, multi-label, provenance accumulation, idempotency.
                 Skipped automatically when Neo4j is unreachable.

Graph tests write under source="pytest-kg" and delete exactly that on teardown,
so running them against a populated database is safe.
"""
from __future__ import annotations

import pytest

from backend.knowledge_graph import (
    BatchKnowledgeGraphBuilder,
    ExtractedEntity,
    ExtractedRelationship,
    KnowledgeExtractor,
    KnowledgeSource,
    Normalizer,
    Provenance,
    ProvenanceSummary,
    SourceType,
    entity_id_for,
    get_graph_service,
    is_enabled,
    known_source_types,
    register_source_type,
    slugify,
    verify_connectivity,
)

TEST_SOURCE = "pytest-kg"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _graph_available() -> bool:
    try:
        return is_enabled() and verify_connectivity()
    except Exception:  # noqa: BLE001
        return False


needs_graph = pytest.mark.skipif(not _graph_available(),
                                 reason="Neo4j unreachable — infrastructure test")


@pytest.fixture()
def svc():
    """Live GraphService, with teardown that CANNOT delete pre-existing data.

    The old teardown was `DELETE n WHERE n.source = 'pytest-kg'`, on the
    assumption that tests only ever touch nodes they created. That assumption is
    void, and the mechanism is the builder working correctly:

      * `entity_id_for()` is `slugify(canonical_name)` and deliberately
        LABEL-FREE, so a test entity named "LiteLLM" has id `litellm` — the
        SAME NODE as the production entity, by design.
      * `_entity_row` writes `props["source"] = self._source`, so the MERGE
        stamps `source='pytest-kg'` onto that production node.
      * Teardown then DETACH DELETEs it.

    That is exactly what happened: a `pytest tests/` run removed 6 real entities
    (agentic-ai, litellm, neo4j, akshay, …), the resolver stopped resolving them,
    and GraphProvider silently contributed nothing to fused context.

    The fix is to snapshot every id that exists BEFORE the test and exclude those
    from the delete. Anything the test genuinely created is still cleaned up;
    anything that was already there survives regardless of what the test stamped
    on it.
    """
    service = get_graph_service()

    def _preexisting() -> list[str]:
        return [r["id"] for r in service.run_query(
            "MATCH (n:Entity) RETURN n.id AS id", op="pytest_snapshot")]

    def _clean(protected: list[str]) -> None:
        service.run_query(
            "MATCH (n) WHERE n.source = $s AND NOT n.id IN $keep DETACH DELETE n",
            {"s": TEST_SOURCE, "keep": protected}, op="pytest_clean")

    protected = _preexisting()
    _clean(protected)
    yield service
    _clean(protected)


@pytest.fixture()
def builder(svc):
    return BatchKnowledgeGraphBuilder(service=svc, source=TEST_SOURCE)


def prov(source_id: str = "src-1", source_type: str = "conversation",
         confidence: float = 0.9, model: str = "qwen-fast", **kw) -> Provenance:
    return Provenance(source_id=source_id, source_type=source_type,
                      confidence=confidence, model=model, **kw)


# ── 1. single extraction response (fake LLM — no network) ────────────────────

class _FakeLLM:
    """Stands in for backend.services.llm.complete."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, *a, **kw) -> str:
        self.calls += 1
        return self.payload


UNIFIED_JSON = """
{"entities": [
   {"name": "Microsoft Graph", "type": "Technology", "also": ["API", "Service"],
    "aliases": ["MS Graph"], "confidence": 0.95},
   {"name": "Agentic AI", "type": "Project", "also": [], "aliases": [], "confidence": 0.9}],
 "relationships": [
   {"source": "Agentic AI", "type": "USES", "target": "MS Graph", "confidence": 0.8}],
 "summary": "Agentic AI uses Microsoft Graph.",
 "keywords": ["graph", "integration"],
 "confidence": 0.9}
"""


def test_single_extraction_response_is_one_call(monkeypatch):
    fake = _FakeLLM(UNIFIED_JSON)
    monkeypatch.setattr("backend.knowledge_graph.extractor._llm.complete", fake.complete)

    result = KnowledgeExtractor().extract("irrelevant", "conversation")

    assert fake.calls == 1, "unified extraction must issue exactly ONE LLM call"
    assert result.llm_calls == 1
    assert result.summary == "Agentic AI uses Microsoft Graph."
    assert result.keywords == ["graph", "integration"]
    assert result.confidence == 0.9
    assert len(result.entities) == 2
    assert len(result.relationships) == 1


def test_extraction_parses_multilabel_aliases_and_confidence(monkeypatch):
    fake = _FakeLLM(UNIFIED_JSON)
    monkeypatch.setattr("backend.knowledge_graph.extractor._llm.complete", fake.complete)
    result = KnowledgeExtractor().extract("x")

    graph = next(e for e in result.entities if e.name == "Microsoft Graph")
    assert graph.type == "Technology"
    assert graph.secondary_labels == ["API", "Service"]
    assert graph.all_labels == ["Technology", "API", "Service"]
    assert graph.aliases == ["MS Graph"]
    assert graph.confidence == 0.95

    # The relationship named the ALIAS; it must still bind to the canonical entity.
    rel = result.relationships[0]
    assert rel.source == "Agentic AI" and rel.target == "Microsoft Graph"
    assert rel.confidence == 0.8


def test_extraction_survives_malformed_and_unknown_vocabulary(monkeypatch):
    fenced = ('Here you go:\n```json\n{"entities": '
              '[{"name": "X", "type": "NotALabel", "confidence": 2.5}], '
              '"relationships": [{"source": "X", "type": "FROBNICATES", '
              '"target": "X", "confidence": 0.5}], '
              '"summary": "", "keywords": [], "confidence": 0.5}\n```')
    monkeypatch.setattr("backend.knowledge_graph.extractor._llm.complete",
                        _FakeLLM(fenced).complete)
    result = KnowledgeExtractor().extract("x")

    assert result.error is None, "fenced JSON must still parse"
    assert result.entities[0].type == "Entity", "unknown label degrades to Entity"
    assert result.entities[0].confidence == 1.0, "out-of-range confidence is clamped"
    # source == target is a self-loop and is dropped, so no relationship survives.
    assert result.relationships == []


def test_unparseable_response_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr("backend.knowledge_graph.extractor._llm.complete",
                        _FakeLLM("not json at all").complete)
    result = KnowledgeExtractor().extract("x")
    assert result.error == "unparseable_json"
    assert result.entities == []


# ── 2. alias resolution + multi-label merge (pure) ───────────────────────────

def test_alias_resolution_maps_variants_to_canonical():
    n = Normalizer()
    assert n.canonical_name("M365") == "Microsoft 365"
    assert n.canonical_name("MS Graph") == "Microsoft Graph"
    assert n.canonical_name("Postgres") == "PostgreSQL"
    assert n.canonical_name("Qwen Fast") == "qwen-fast"
    assert n.canonical_name("  the   FastAPI  ") == "FastAPI"


def test_casing_never_mangles_deliberate_names():
    n = Normalizer()
    for name in ("LiteLLM", "vLLM", "qwen-fast", "gpt-4.1", "Neo4j"):
        assert n.canonical_name(name) == name


def test_multilabel_merge_produces_one_entity_not_two():
    """The phase-2 duplicate-node bug, at the normalizer level."""
    n = Normalizer()
    entities, merged, rename = n.resolve([
        ExtractedEntity(name="Microsoft Graph", type="Technology", confidence=0.6),
        ExtractedEntity(name="MS Graph", type="API", confidence=0.9),
    ])
    assert len(entities) == 1, "same thing under two labels must collapse to ONE entity"
    assert merged == 1
    e = entities[0]
    assert e.canonical_name == "Microsoft Graph"
    assert set(e.all_labels) == {"Technology", "API"}, "the loser's label is kept"
    assert e.confidence == 0.9, "confidence is the max, not the latest"
    assert "MS Graph" in e.aliases
    # Both spellings must repoint relationships at the canonical name.
    assert rename["MS Graph"] == "Microsoft Graph"
    assert rename["ms graph"] == "Microsoft Graph"


def test_entity_label_loses_to_a_specific_one():
    n = Normalizer()
    entities, _, _ = n.resolve([
        ExtractedEntity(name="Agentic AI", type="Entity"),
        ExtractedEntity(name="Agentic AI", type="Project"),
    ])
    assert entities[0].type == "Project"


def test_relationships_repoint_through_rename_map():
    n = Normalizer()
    _, _, rename = n.resolve([ExtractedEntity(name="MS Graph", type="API")])
    rels = n.normalize_relationships(
        [ExtractedRelationship(source="Agentic AI", type="USES", target="MS Graph")], rename)
    assert rels[0].target == "Microsoft Graph"


def test_id_is_label_free_and_stable():
    """Identity is the thing, not its classification."""
    assert entity_id_for("Microsoft Graph") == "microsoft-graph"
    assert entity_id_for("microsoft  graph") == "microsoft-graph"
    assert slugify("Café Über") == "cafe-uber"
    assert entity_id_for("???") == ""


# ── 3. knowledge sources (pure) ──────────────────────────────────────────────

def test_sources_carry_their_own_identifiers():
    conv = KnowledgeSource.conversation("conv-1", "text", user_id="user_1")
    assert conv.conversation_id == "conv-1" and conv.user_id == "user_1"
    doc = KnowledgeSource.document("doc-9", "text")
    assert doc.document_id == "doc-9" and doc.type == SourceType.DOCUMENT.value
    mail = KnowledgeSource.email("msg-3", "text")
    assert mail.message_id == "msg-3"


def test_source_id_defaults_to_a_content_hash():
    a = KnowledgeSource(id="", text="same text")
    b = KnowledgeSource(id="", text="same text")
    c = KnowledgeSource(id="", text="different")
    assert a.id == b.id, "same content must yield the same id (re-ingest merges)"
    assert a.id != c.id


def test_source_types_are_pluggable():
    name = register_source_type("Slack Message", "one Slack message")
    assert name == "slack_message"
    assert "slack_message" in known_source_types()


# ── 4. provenance (pure) ─────────────────────────────────────────────────────

def test_provenance_params_drop_empty_identifiers():
    p = Provenance(source_id="s1", source_type="conversation", conversation_id="c1")
    params = p.as_params()
    assert params["add_source_ids"] == ["s1"]
    assert params["add_document_ids"] == [], "empty ids must not become '' array entries"


def test_provenance_confidence_is_clamped():
    assert Provenance(confidence=5).confidence == 1.0
    assert Provenance(confidence=-1).confidence == 0.0
    assert Provenance(confidence="junk").confidence == 1.0


# ── 5. graph writes (need Neo4j) ─────────────────────────────────────────────

@needs_graph
def test_batch_merge_uses_few_queries_not_one_per_node(builder):
    """The whole point of batching: query count must not scale with node count."""
    entities = [ExtractedEntity(name=f"Thing {i}", type="Technology") for i in range(25)]
    rels = [ExtractedRelationship(source="Thing 0", type="USES", target=f"Thing {i}")
            for i in range(1, 25)]
    result = builder.build(entities, rels, provenance=prov())

    assert result.nodes_created == 25
    assert result.relationships_created == 24
    # 1 node group (all Technology) + 1 relationship group (all USES).
    assert result.graph_queries == 2, f"expected 2 round trips, got {result.graph_queries}"


@needs_graph
def test_batch_groups_by_label_set(builder):
    entities = [
        ExtractedEntity(name="A", type="Technology"),
        ExtractedEntity(name="B", type="Technology", secondary_labels=["API"]),
        ExtractedEntity(name="C", type="Person"),
    ]
    result = builder.build(entities, [], provenance=prov())
    assert result.nodes_created == 3
    assert result.graph_queries == 3, "three distinct label-sets → three queries"


@needs_graph
def test_multilabel_merge_does_not_fork_the_node(builder, svc):
    """Same id seen under different labels must gain a label, not duplicate."""
    builder.build([ExtractedEntity(name="Microsoft Graph", type="Technology")],
                  [], provenance=prov())
    builder.build([ExtractedEntity(name="Microsoft Graph", type="API",
                                   secondary_labels=["Service"])], [], provenance=prov())

    rows = svc.run_query("MATCH (n {id: $id}) RETURN count(n) AS c",
                         {"id": "microsoft-graph"}, op="pytest")
    assert rows[0]["c"] == 1, "must be ONE node, not one per label"

    found = svc.find_entity("microsoft-graph")
    assert found is not None
    _, labels = found
    assert set(labels) >= {"Entity", "Technology", "API", "Service"}


@needs_graph
def test_duplicate_ingestion_is_idempotent(builder, svc):
    entities = [ExtractedEntity(name="Microsoft Graph", type="Technology"),
                ExtractedEntity(name="Agentic AI", type="Project")]
    rels = [ExtractedRelationship(source="Agentic AI", type="USES",
                                  target="Microsoft Graph")]

    first = builder.build(entities, rels, provenance=prov())
    second = builder.build(entities, rels, provenance=prov())

    assert first.nodes_created == 2 and first.relationships_created == 1
    assert second.nodes_created == 0, "re-ingesting must create no new nodes"
    assert second.relationships_created == 0, "nor new edges"
    assert second.nodes_merged == 2

    count = svc.run_query("MATCH (n) WHERE n.source=$s RETURN count(n) AS c",
                          {"s": TEST_SOURCE}, op="pytest")[0]["c"]
    assert count == 2


@needs_graph
def test_provenance_accumulates_across_sources(builder, svc):
    """Two different sources asserting the same fact must both be recorded."""
    entity = [ExtractedEntity(name="LiteLLM", type="Technology")]
    builder.build(entity, [], provenance=prov(source_id="conv-1",
                                              source_type="conversation",
                                              confidence=0.9, model="qwen-fast"))
    builder.build(entity, [], provenance=Provenance(
        source_id="doc-2", source_type="document", document_id="doc-2",
        confidence=0.4, model="gpt-4.1"))

    node = svc.find_entity("litellm")
    assert node is not None
    props = node[0].properties
    summary = ProvenanceSummary.from_properties(props)

    assert set(summary.source_ids) == {"conv-1", "doc-2"}, "both sources retained"
    assert set(summary.source_types) == {"conversation", "document"}
    assert set(summary.models) == {"qwen-fast", "gpt-4.1"}
    assert summary.observations == 2
    assert summary.confidence == 0.9, "MAX confidence kept, not the later 0.4"
    assert summary.is_corroborated
    assert props["document_ids"] == ["doc-2"], "empty ids filtered from the first write"


@needs_graph
def test_provenance_first_seen_is_never_overwritten(builder, svc):
    entity = [ExtractedEntity(name="Neo4j", type="Technology")]
    builder.build(entity, [], provenance=Provenance(source_id="s1",
                                                    created_at="2020-01-01T00:00:00Z"))
    builder.build(entity, [], provenance=Provenance(source_id="s2",
                                                    created_at="2030-01-01T00:00:00Z"))
    props = svc.find_entity("neo4j")[0].properties
    assert props["first_seen"] == "2020-01-01T00:00:00Z"
    assert props["last_seen"] == "2030-01-01T00:00:00Z"


@needs_graph
def test_mixed_knowledge_sources_converge_on_one_node(builder, svc):
    """A conversation and a document mentioning the same thing share a node."""
    for source in (KnowledgeSource.conversation("conv-7", "t", user_id="u1"),
                   KnowledgeSource.document("doc-7", "t"),
                   KnowledgeSource.email("mail-7", "t")):
        builder.build([ExtractedEntity(name="Microsoft Graph", type="Technology")],
                      [], provenance=Provenance.from_source(source, model="qwen-fast"))

    count = svc.run_query("MATCH (n {id:'microsoft-graph'}) RETURN count(n) AS c",
                          op="pytest")[0]["c"]
    assert count == 1
    summary = ProvenanceSummary.from_properties(svc.find_entity("microsoft-graph")[0].properties)
    assert set(summary.source_types) == {"conversation", "document", "email"}
    assert summary.observations == 3


@needs_graph
def test_aliases_are_stored_and_deduplicated(builder, svc):
    e = ExtractedEntity(name="Microsoft Graph", type="Technology",
                        aliases=["MS Graph", "the Graph API"])
    builder.build([e], [], provenance=prov())
    builder.build([ExtractedEntity(name="Microsoft Graph", type="Technology",
                                   aliases=["MS Graph", "msgraph"])], [], provenance=prov())
    props = svc.find_entity("microsoft-graph")[0].properties
    assert sorted(props["aliases"]) == ["MS Graph", "msgraph", "the Graph API"]


@needs_graph
def test_relationship_to_unknown_entity_is_skipped_not_invented(builder):
    result = builder.build(
        [ExtractedEntity(name="A", type="Technology")],
        [ExtractedRelationship(source="A", type="USES", target="Nonexistent")],
        provenance=prov())
    assert result.relationships_created == 0
    assert len(result.skipped_relationships) == 1
    assert "Nonexistent" in result.skipped_relationships[0]
