"""Scorer tests for the dataset-v2 false-premise contract, plus dataset invariants.

These exist because v2 added the first piece of scoring that can FAIL a case on
the content of the answer rather than on what was retrieved. That is exactly the
kind of check that silently stops working — a typo in the field name, a filtered
key in `from_dict`, an early return in the scorer — and nothing else in the suite
would notice, because a check that never fires looks identical to a check that
always passes.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from backend.evals.dataset import GoldenCase, load_dataset
from backend.evals.scoring import score_case


def v2_cases():
    """The frozen v2 dataset. Loading cases/ now yields the v3 union, so every
    assertion about v2's own invariants has to exclude the v3 additions —
    otherwise freezing v2 would be enforced by a test that silently drifts."""
    return [c for c in load_dataset() if "v3" not in c.tags]

VALID_PROVIDERS = {"graph", "corporate", "tasks", "calendar", "memory",
                   "history", "sql", "qdrant"}


def trace(answer: str = "", **over):
    """A complete, minimal trace. Every attribute score_case() reads is present,
    so a test failure means the scorer changed, not that the stub is short."""
    base = dict(answer=answer, compression_ratio=1.0,
                context_texts=["Neo4j is the graph database in use."],
                contribution={}, dedup_ratio=0.0, errors=[], final_items=[],
                graph_hops={}, graph_nodes=["neo4j"], graph_relationships=[],
                max_corroboration=0, provider_items={}, provider_order=[],
                qdrant_documents=[], resolved_entities=["neo4j"],
                timing=NS(total_ms=1.0), tokens_available=1, tokens_used=0)
    base.update(over)
    return NS(**base)


def premise_case(**over) -> GoldenCase:
    kw = dict(id="fp", question="Why did we migrate from Neo4j to MongoDB?",
              expected_entities=["Neo4j"],
              forbidden_answer_keywords=["we migrated", "migrated to mongodb"])
    kw.update(over)
    return GoldenCase(**kw)


# ── the false-premise contract ───────────────────────────────────────────────

def test_affirming_a_false_premise_fails():
    s = score_case(premise_case(), trace("We migrated to MongoDB in 2025 for scale."))
    assert s.negative_premise_ok is False
    assert any("unsupported premise" in f for f in s.failures)


def test_refusing_a_false_premise_passes():
    s = score_case(premise_case(),
                   trace("There is no evidence of a migration; Neo4j is still in use."))
    assert s.negative_premise_ok is True
    assert not any("unsupported premise" in f for f in s.failures)


def test_forbidden_keywords_are_case_insensitive():
    """The scorer lowercases both sides. Without this an LLM that starts a
    sentence with the phrase would slip past the check."""
    s = score_case(premise_case(), trace("We Migrated To MongoDB last year."))
    assert s.negative_premise_ok is False


def test_case_without_forbidden_keywords_is_unaffected():
    """None, not False. The 113 cases that declare no forbidden keywords must not
    acquire a failing sub-score, or v2 would depress every aggregate it touches."""
    plain = GoldenCase(id="p", question="What is Neo4j?", expected_entities=["Neo4j"])
    assert score_case(plain, trace("Neo4j is a graph database.")).negative_premise_ok is None


def test_retrieval_only_run_is_not_judged_on_a_premise():
    """No answer means the answer contract was never exercised. Scoring it False
    would make every retrieval-only run look like a groundedness regression."""
    assert score_case(premise_case(), trace("")).negative_premise_ok is None


def test_false_premise_case_still_requires_its_entities_to_resolve():
    """The distinction v2 exists to encode: these are NOT negative cases. Neo4j
    must resolve; only the premise must be refused. If someone 'simplifies' this
    to negative=True, entity_precision would invert and this test fails."""
    s = score_case(premise_case(), trace("No migration occurred.",
                                         resolved_entities=[], graph_nodes=[]))
    assert s.entity_recall == 0.0, "resolving nothing must not be rewarded"


# ── dataset invariants ───────────────────────────────────────────────────────

def test_dataset_loads_with_unique_ids():
    cases = list(load_dataset())
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))
    assert len(cases) == 174        # v2 116 + v3 58
    assert len(v2_cases()) == 116   # v2 stays exactly as Step 4 left it


def test_no_expectation_names_an_unknown_provider():
    for c in load_dataset():
        for field in (c.expected_context_sources, c.expected_provider_order):
            assert not set(field or []) - VALID_PROVIDERS, c.id


def test_adversarial_cases_carry_a_machine_checkable_contract():
    """Guards the v1 defect directly: these three encoded their intent only in
    prose `notes`, so nothing scored them."""
    by_id = {c.id: c for c in load_dataset()}
    for cid in ("adv-leading-false", "adv-leading-person", "adv-invented-rel"):
        c = by_id[cid]
        assert c.forbidden_answer_keywords, f"{cid} has no enforceable contract"
        assert not c.negative, f"{cid} must resolve its entities; negative=True is wrong"
        assert c.expected_entities, cid


@pytest.mark.parametrize("tag,count", [("requires:semantic-resolution", 8),
                                       ("requires:provider-", 11)])
def test_known_limitation_cohorts_are_tagged(tag, count):
    """These cases are expected to fail today. They are tagged so a baseline can
    segment them out instead of quietly relabelling them as passing."""
    n = sum(1 for c in v2_cases() if any(t.startswith(tag) for t in c.tags))
    assert n == count


def test_depth_dependent_cases_are_tagged_and_exceed_production_depth():
    from config import settings
    depth = getattr(settings, "GRAPH_RETRIEVAL_DEPTH", 1)
    tagged = [c for c in v2_cases()
              if any(t.startswith("requires:depth-") for t in c.tags)]
    assert len(tagged) == 14
    for c in tagged:
        want = max(int(t.split("-")[-1]) for t in c.tags if t.startswith("requires:depth-"))
        assert want > depth, f"{c.id} is tagged depth-dependent but {want} <= {depth}"


def test_semantic_cases_keep_their_original_expectations():
    """v2 must not have 'fixed' these by weakening them to whatever the lexical
    resolver currently returns."""
    by_id = {c.id: c for c in load_dataset()}
    for cid, entity in [("syn-vector-db", "Qdrant"), ("syn-graph-db", "Neo4j"),
                        ("syn-gateway", "LiteLLM"), ("syn-relational", "PostgreSQL"),
                        ("syn-mail-api", "Microsoft Graph")]:
        assert by_id[cid].expected_entities == [entity], cid


# ── dataset v3: discriminating benchmark ─────────────────────────────────────

EXPERIMENTS = ("exp:resolver-threshold", "exp:doc-pollution", "exp:semantic-resolution",
               "exp:mention-extraction", "exp:graph-depth", "exp:context-explosion",
               "exp:fusion", "exp:ranking", "exp:negative-premise", "exp:provenance")


def v3_cases():
    return [c for c in load_dataset() if "v3" in c.tags]


def test_v3_loads_and_v2_is_untouched():
    """v3 is the union of the frozen v2 files plus discrimination.json. If a v2
    case leaked a v3 tag, or a v3 case landed in a v2 file, this catches it."""
    allc = list(load_dataset())
    assert len(allc) == 174
    assert len(v3_cases()) == 58
    assert len([c for c in allc if "v3" not in c.tags]) == 116


def test_every_v3_case_maps_to_an_experiment_and_says_why():
    """§14: every new case must have a reason for existing and map to an
    experiment family. A case with neither cannot be acted on in Steps 7-12."""
    for c in v3_cases():
        fams = [t for t in c.tags if t.startswith("exp:")]
        assert fams, f"{c.id} maps to no experiment"
        assert set(fams) <= set(EXPERIMENTS), f"{c.id} has an unknown family: {fams}"
        assert len(c.notes) > 40, f"{c.id} does not justify its existence"


def test_v3_negative_cases_expect_nothing():
    """negative=True inverts the contract; pairing it with expected entities or
    with forbidden keywords is contradictory."""
    for c in v3_cases():
        if c.negative:
            assert not c.expected_entities, c.id
            assert not c.expected_graph_nodes, c.id
            assert not c.forbidden_answer_keywords, c.id


def test_resolver_band_cases_declare_a_band():
    for c in v3_cases():
        if "exp:resolver-threshold" in c.tags:
            assert any(t.startswith("band:") for t in c.tags), c.id
            assert "MEASURED" in c.notes, f"{c.id} states no measured similarity"


def test_the_080_collision_pair_is_intact():
    """The two cases that prove the resolver boundary is a real trade-off: both
    measured at similarity 0.8000, one must resolve and one must not. If either
    is deleted or retagged, Step 7 loses its only proof that lowering the
    threshold has a cost."""
    by = {c.id: c for c in v3_cases()}
    tp, tn = by["res-tp-0800-k8"], by["res-tn-0800-db-collision"]
    assert tp.expected_entities == ["Kubernetes"] and not tp.negative
    assert tn.negative and not tn.expected_entities


def test_pollution_family_guards_the_naive_fix():
    """Excluding :Document from candidates would break the hybrid-node case, so
    that case must keep expecting a resolution."""
    by = {c.id: c for c in v3_cases()}
    hybrid = by["poll-hybrid-ajman-police"]
    assert hybrid.expected_entities == ["Ajman Police"]
    assert not hybrid.negative


def test_depth_cases_declare_depth_and_exceed_production_where_claimed():
    from config import settings
    depth = getattr(settings, "GRAPH_RETRIEVAL_DEPTH", 1)
    for c in v3_cases():
        if "exp:graph-depth" not in c.tags:
            continue
        assert any(t.startswith("depth:") for t in c.tags), c.id
        req = [int(t.split("-")[-1]) for t in c.tags if t.startswith("requires:depth-")]
        if req:
            assert max(req) > depth, f"{c.id} claims depth-dependence but fits {depth}"
        else:  # a depth-1 control must NOT be marked expected-fail
            assert "expected-fail" not in c.tags, f"{c.id} is a depth-1 control that fails"


def test_qdrant_cases_name_a_real_corpus_file():
    """§14: grounded in the real corpus. A typo'd filename would silently score
    zero recall forever and look like a retrieval regression."""
    from pathlib import Path
    corpus = {p.name for p in Path("data_vault/enterprise_corpus").iterdir()}
    for c in v3_cases():
        for doc in c.expected_qdrant_documents:
            assert doc in corpus, f"{c.id} cites a document not in the corpus: {doc}"


# ── §9 context-explosion guards ──────────────────────────────────────────────

def test_graph_noise_ratio_rises_with_irrelevant_context():
    case = GoldenCase(id="g", question="q", expected_graph_nodes=["neo4j"])
    tight = score_case(case, trace(graph_nodes=["neo4j"]))
    flood = score_case(case, trace(graph_nodes=["neo4j"] + [f"n{i}" for i in range(99)]))
    assert tight.graph_noise_ratio == 0.0
    assert flood.graph_noise_ratio > 0.98
    assert flood.context_nodes == 100


def test_retrieving_everything_does_not_raise_the_headline_score():
    """The §9 requirement, asserted directly. Both runs have perfect recall; the
    flooding one must score strictly WORSE overall. Before v3 `overall` averaged
    graph_node_recall and this assertion failed."""
    case = GoldenCase(id="g", question="q", expected_graph_nodes=["neo4j"])
    tight = score_case(case, trace(graph_nodes=["neo4j"]))
    flood = score_case(case, trace(graph_nodes=["neo4j"] + [f"n{i}" for i in range(99)]))
    assert tight.graph_node_recall == flood.graph_node_recall == 1.0
    assert flood.overall < tight.overall


def test_context_nodes_recorded_even_without_graph_expectations():
    """The depth experiment needs a size axis on every case, including the ones
    that state no graph expectation."""
    plain = GoldenCase(id="p", question="q", expected_entities=["Neo4j"])
    assert score_case(plain, trace(graph_nodes=["a", "b", "c"])).context_nodes == 3


# ── document identity (Step 6 evaluation defect) ─────────────────────────────
# A retrieved chunk carries a source filename AND a storage UUID. The runner
# used to emit only the UUID, so a case naming the filename scored zero recall
# even when that exact document was retrieved and ranked first. These tests fix
# the identity semantics in place so the defect cannot silently return.

SRC = "emd-0293-gpu_benchmark-gpu-benchmark-qwen-fast-on-rtx-pro-6000-int4-awq.md"
UUID = "347c5512-d227-5ab8-9eed-4a69310d5f8f"


def doc_trace(identities, **over):
    """identities: [(source, document_id), ...] in rank order."""
    return trace(qdrant_documents=[s for s, _ in identities],
                 qdrant_document_ids=[i for _, i in identities],
                 qdrant_identities=identities, **over)


def doc_case(expected):
    return GoldenCase(id="d", question="q", expected_qdrant_documents=expected)


def test_case_a_expected_source_filename_matches_retrieved_source():
    """The defect's headline symptom: retrieval is correct, so it must score."""
    s = score_case(doc_case([SRC]), doc_trace([(SRC, UUID)]))
    assert s.qdrant_recall_at_k == 1.0
    assert s.qdrant_mrr == 1.0
    assert not any("no expected document" in f for f in s.failures)


def test_case_b_expected_document_id_matches_retrieved_document_id():
    """A case may legitimately address a document by its storage UUID."""
    s = score_case(doc_case([UUID]), doc_trace([(SRC, UUID)]))
    assert s.qdrant_recall_at_k == 1.0
    assert s.qdrant_mrr == 1.0


def test_case_c_filename_must_not_match_a_uuid():
    """The fix must not become permissive. A case naming the filename must NOT
    be satisfied by a document whose only identity is a different UUID."""
    s = score_case(doc_case([SRC]), doc_trace([("", "some-other-uuid")]))
    assert s.qdrant_recall_at_k == 0.0
    assert s.qdrant_mrr == 0.0
    assert any("no expected document" in f for f in s.failures)


def test_case_d_wrong_identifiers_fail():
    s = score_case(doc_case([SRC]), doc_trace([("emd-0001-other.md", "different-uuid")]))
    assert s.qdrant_recall_at_k == 0.0
    assert s.qdrant_precision_at_k == 0.0


def test_identity_resolution_does_not_inflate_precision():
    """Only the genuinely matching chunk may count. Three retrieved, one right
    → precision@5 must reflect the two wrong ones, not be rounded up to 1.0."""
    s = score_case(doc_case([SRC]),
                   doc_trace([("emd-0001-a.md", "u1"), (SRC, UUID), ("emd-0002-b.md", "u2")]))
    assert s.qdrant_recall_at_k == 1.0
    assert s.qdrant_precision_at_k < 1.0
    assert s.qdrant_mrr == 0.5          # correct doc is second


def test_rank_order_is_preserved_by_identity_resolution():
    s_first = score_case(doc_case([SRC]), doc_trace([(SRC, UUID), ("x.md", "u")]))
    s_third = score_case(doc_case([SRC]),
                         doc_trace([("x.md", "u"), ("y.md", "u2"), (SRC, UUID)]))
    assert s_first.qdrant_mrr == 1.0
    assert round(s_third.qdrant_mrr, 4) == round(1 / 3, 4)


def test_resolve_document_identity_is_exact_not_substring():
    """A prefix of a real filename must not match it."""
    from backend.evals import metrics as M
    got = M.resolve_document_identity([("emd-0293-gpu_benchmark.md", UUID)], [SRC])
    assert SRC not in got


def test_legacy_documents_without_a_uuid_still_score():
    """Pre-corpus documents carry no document_id; they must keep working."""
    s = score_case(doc_case(["company_handbook.txt"]),
                   doc_trace([("company_handbook.txt", "")]))
    assert s.qdrant_recall_at_k == 1.0


# ── latent-inference cohort ──────────────────────────────────────────────────

def test_latent_inference_cases_are_tagged_and_not_weakened():
    """These fail because the question never NAMES the expected entity, not
    because retrieval regressed. They must stay failing with their original
    expectations — the tag exists to classify them, not to excuse them."""
    tagged = [c for c in load_dataset() if "requires:latent-inference" in c.tags]
    assert len(tagged) == 9
    for c in tagged:
        assert c.expected_entities, f"{c.id} lost its expectation"
        q = c.question.lower()
        named = [e for e in c.expected_entities if e.lower().split()[0] in q]
        assert not named, (
            f"{c.id} DOES name {named} in its question — it is not latent inference")
