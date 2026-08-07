"""
Scoring — turn a RunTrace plus its GoldenCase into numbers.

One CaseScore per case, one AggregateScore per run. Every metric is optional:
if a case states no `expected_graph_nodes`, graph metrics are `None` rather than
0.0, and averages skip them. Scoring an unstated expectation as zero would make
the headline number a measure of how complete the golden file is, not of how
well the system works.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import metrics as M
from .dataset import GoldenCase
from .runner import RunTrace


@dataclass(slots=True)
class CaseScore:
    case_id: str
    question: str
    coverage: list[str] = field(default_factory=list)

    # entity resolution
    entity_precision: Optional[float] = None
    entity_recall: Optional[float] = None
    entity_f1: Optional[float] = None

    # graph
    graph_node_precision: Optional[float] = None
    graph_node_recall: Optional[float] = None
    graph_rel_precision: Optional[float] = None
    graph_rel_recall: Optional[float] = None
    graph_ndcg: Optional[float] = None
    hop_accuracy: Optional[float] = None

    # qdrant
    qdrant_precision_at_k: Optional[float] = None
    qdrant_recall_at_k: Optional[float] = None
    qdrant_mrr: Optional[float] = None

    # fusion
    source_precision: Optional[float] = None
    source_recall: Optional[float] = None
    corroboration_accuracy: Optional[float] = None
    duplicate_reduction: float = 0.0
    max_corroboration: int = 0

    # ranking
    provider_order_correlation: Optional[float] = None
    ranking_ndcg: Optional[float] = None

    # compression + budget
    compression_ratio: float = 0.0
    tokens_used: int = 0
    budget_utilization: float = 0.0

    # answer
    answer_keyword_coverage: Optional[float] = None
    groundedness: Optional[float] = None
    citation_coverage: float = 0.0

    latency_ms: float = 0.0
    errors: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)   # human-readable diagnoses

    @property
    def overall(self) -> float:
        """Unweighted mean of every metric that this case could score.

        Unweighted on purpose: a weighted headline invites tuning the weights
        instead of the system. Per-family numbers are what you act on."""
        vals = [v for v in (
            self.entity_f1, self.graph_node_recall, self.graph_rel_recall,
            self.qdrant_recall_at_k, self.source_recall,
            self.corroboration_accuracy, self.provider_order_correlation,
            self.answer_keyword_coverage, self.groundedness,
        ) if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["overall"] = self.overall
        return d


def score_case(case: GoldenCase, trace: RunTrace, *, k: int = 5) -> CaseScore:
    s = CaseScore(case_id=case.id, question=case.question, coverage=case.coverage)
    s.errors = list(trace.errors)
    s.latency_ms = trace.timing.total_ms
    s.duplicate_reduction = round(trace.dedup_ratio, 4)
    s.compression_ratio = round(trace.compression_ratio, 4)
    s.max_corroboration = trace.max_corroboration
    s.tokens_used = trace.tokens_used
    s.budget_utilization = round(
        trace.tokens_used / trace.tokens_available, 4) if trace.tokens_available else 0.0
    s.citation_coverage = round(M.citation_coverage(trace.final_items), 4)

    # ── entity resolution ──
    if case.expected_entities:
        p = M.prf(trace.resolved_entities, case.expected_entities)
        s.entity_precision, s.entity_recall, s.entity_f1 = p.precision, p.recall, p.f1
        missing = M.norm_set(case.expected_entities) - M.norm_set(trace.resolved_entities)
        if missing:
            s.failures.append(f"entities not resolved: {sorted(missing)}")

    # ── graph ──
    if case.expected_graph_nodes:
        p = M.prf(trace.graph_nodes, case.expected_graph_nodes)
        s.graph_node_precision, s.graph_node_recall = p.precision, p.recall
        # Graded relevance: expected nodes are 1, everything else 0 — so nDCG
        # rewards putting the expected ones FIRST, not merely retrieving them.
        s.graph_ndcg = M.ndcg(trace.graph_nodes,
                              {n: 1.0 for n in case.expected_graph_nodes}, k=10)
        missing = M.norm_set(case.expected_graph_nodes) - M.norm_set(trace.graph_nodes)
        if missing:
            s.failures.append(f"graph nodes missing: {sorted(missing)}")
        if trace.graph_hops:
            expected = M.norm_set(case.expected_graph_nodes)
            hops = [h for n, h in trace.graph_hops.items() if M.norm(n) in expected]
            # Expected nodes should be CLOSE to the seed; far ones mean the
            # question's entities resolved to the wrong place.
            s.hop_accuracy = round(
                sum(1 for h in hops if h <= 2) / len(hops), 4) if hops else None

    if case.expected_relationships:
        p = M.prf(trace.graph_relationships, case.expected_relationships)
        s.graph_rel_precision, s.graph_rel_recall = p.precision, p.recall
        missing = M.norm_set(case.expected_relationships) - M.norm_set(trace.graph_relationships)
        if missing:
            s.failures.append(f"relationships missing: {sorted(missing)}")

    # ── qdrant ──
    if case.expected_qdrant_documents:
        s.qdrant_precision_at_k = round(
            M.precision_at_k(trace.qdrant_documents, case.expected_qdrant_documents, k), 4)
        s.qdrant_recall_at_k = round(
            M.recall_at_k(trace.qdrant_documents, case.expected_qdrant_documents, k), 4)
        s.qdrant_mrr = round(M.mrr(trace.qdrant_documents, case.expected_qdrant_documents), 4)
        if s.qdrant_recall_at_k == 0.0:
            s.failures.append(
                f"no expected document retrieved (got {trace.qdrant_documents[:3]})")

    # ── fusion ──
    if case.expected_context_sources:
        contributing = [p for p, n in trace.contribution.items() if n > 0] \
            or list(trace.provider_items)
        p = M.prf(contributing, case.expected_context_sources)
        s.source_precision, s.source_recall = p.precision, p.recall
        missing = M.norm_set(case.expected_context_sources) - M.norm_set(contributing)
        if missing:
            s.failures.append(f"providers contributed nothing: {sorted(missing)}")

    if case.expected_corroboration_count is not None:
        want = case.expected_corroboration_count
        got = trace.max_corroboration
        # Graded, not binary: 2 sources when 3 were expected is a near miss, and
        # a binary score would hide steady degradation.
        s.corroboration_accuracy = round(min(1.0, got / want) if want else
                                         (1.0 if got == 0 else 0.0), 4)
        if got < want:
            s.failures.append(f"corroboration {got} < expected {want}")

    # ── ranking ──
    if case.expected_provider_order:
        s.provider_order_correlation = round(
            M.rank_correlation(trace.provider_order, case.expected_provider_order), 4)
        if s.provider_order_correlation < 0.5:
            s.failures.append(
                f"provider order {trace.provider_order} vs expected "
                f"{case.expected_provider_order}")
    if case.expected_answer_keywords and trace.final_items:
        # Relevance by keyword PRESENCE, not identity: nDCG's relevance map is
        # keyed by item, and an item here is a whole passage — matching a
        # keyword against it exactly would score every item 0 and make the
        # metric structurally meaningless.
        texts = [i.text for i in trace.final_items]
        relevance = {
            t: sum(1.0 for kw in case.expected_answer_keywords if M.norm(kw) in M.norm(t))
            for t in texts}
        s.ranking_ndcg = round(M.ndcg(texts, relevance, k=10), 4)

    # ── answer ──
    if case.expected_answer_keywords:
        haystack = trace.answer or " ".join(trace.context_texts)
        s.answer_keyword_coverage = round(
            M.keyword_coverage(haystack, case.expected_answer_keywords), 4)
        if s.answer_keyword_coverage < 0.5:
            s.failures.append(
                f"keyword coverage {s.answer_keyword_coverage:.0%} for "
                f"{case.expected_answer_keywords}")
    if trace.answer:
        s.groundedness = round(M.groundedness(trace.answer, trace.context_texts), 4)

    # ── negative cases invert the contract ──
    if case.negative:
        leaked = bool(trace.resolved_entities or trace.graph_nodes)
        s.entity_precision = 0.0 if leaked else 1.0
        s.entity_f1 = s.entity_precision
        if leaked:
            s.failures.append(
                f"negative case matched anyway: {trace.resolved_entities[:3]}")

    return s


@dataclass(slots=True)
class AggregateScore:
    """Run-level rollup."""

    cases: int = 0
    overall: float = 0.0
    families: dict[str, float] = field(default_factory=dict)
    latency: dict[str, float] = field(default_factory=dict)
    provider_contribution: dict[str, int] = field(default_factory=dict)
    provider_useful: dict[str, float] = field(default_factory=dict)
    totals: dict[str, float] = field(default_factory=dict)
    failing_cases: list[dict] = field(default_factory=list)
    #: environment problems (a provider down), counted separately from failures
    infrastructure_errors: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_FAMILY_FIELDS = {
    "entity_resolution": ("entity_precision", "entity_recall", "entity_f1"),
    "graph": ("graph_node_precision", "graph_node_recall", "graph_rel_precision",
              "graph_rel_recall", "graph_ndcg", "hop_accuracy"),
    "qdrant": ("qdrant_precision_at_k", "qdrant_recall_at_k", "qdrant_mrr"),
    "fusion": ("source_precision", "source_recall", "corroboration_accuracy"),
    "ranking": ("provider_order_correlation", "ranking_ndcg"),
    "answer": ("answer_keyword_coverage", "groundedness"),
}


def aggregate(scores: "list[CaseScore]", traces: "list[RunTrace]") -> AggregateScore:
    agg = AggregateScore(cases=len(scores))
    if not scores:
        return agg

    for family, fields_ in _FAMILY_FIELDS.items():
        vals = [getattr(s, f) for s in scores for f in fields_
                if getattr(s, f) is not None]
        if vals:
            agg.families[family] = round(sum(vals) / len(vals), 4)

    agg.overall = round(sum(s.overall for s in scores) / len(scores), 4)

    for stage in ("entity_resolution_ms", "graph_ms", "providers_ms", "fusion_ms",
                  "ranking_ms", "compression_ms", "budget_ms", "prompt_ms", "llm_ms"):
        agg.latency[stage] = round(
            sum(getattr(t.timing, stage) for t in traces) / len(traces), 2)
    agg.latency["total_ms"] = round(
        sum(t.timing.total_ms for t in traces) / len(traces), 2)

    for t in traces:
        for provider, n in t.contribution.items():
            agg.provider_contribution[provider] = \
                agg.provider_contribution.get(provider, 0) + n
    # "Useful" = appeared in the FINAL, budget-surviving context, not merely
    # returned something. A provider whose items are always trimmed is not
    # contributing, however busy it looks.
    for provider in set(agg.provider_contribution) | {
            p for t in traces for p in t.provider_items}:
        hits = sum(1 for t in traces if t.contribution.get(provider, 0) > 0)
        agg.provider_useful[provider] = round(hits / len(traces), 4)

    agg.totals = {
        "duplicate_reduction": round(
            sum(s.duplicate_reduction for s in scores) / len(scores), 4),
        "compression_ratio": round(
            sum(s.compression_ratio for s in scores) / len(scores), 4),
        "citation_coverage": round(
            sum(s.citation_coverage for s in scores) / len(scores), 4),
        "budget_utilization": round(
            sum(s.budget_utilization for s in scores) / len(scores), 4),
        "avg_tokens": round(sum(s.tokens_used for s in scores) / len(scores), 1),
        "corroborated_max": max((s.max_corroboration for s in scores), default=0),
    }

    # Only EXPECTATION misses make a case "failing". A provider being down is an
    # environment problem — it belongs in `infrastructure_errors`, not in the
    # failure list, or one expired token makes every case look broken.
    agg.failing_cases = [
        {"case_id": s.case_id, "question": s.question, "overall": s.overall,
         "failures": s.failures, "errors": [e for e in s.errors if e.startswith("fatal")]}
        for s in sorted(scores, key=lambda x: x.overall) if s.failures
    ][:20]
    seen_env: dict[str, int] = {}
    for s in scores:
        for e in s.errors:
            if not e.startswith("fatal"):
                key = e.split(":")[0]
                seen_env[key] = seen_env.get(key, 0) + 1
    agg.infrastructure_errors = seen_env
    return agg
