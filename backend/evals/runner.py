"""
EvaluationRunner — executes the full pipeline and captures EVERY intermediate.

    question
      ├─ entity resolution      (GraphRetrievalAPI.resolve_only)
      ├─ graph retrieval        (GraphRetrievalAPI.retrieve)
      ├─ provider retrieval     (ContextBuilder.build_context — concurrent)
      ├─ fusion                 (HybridContextFusion)
      ├─ ranking                (WeightedRanker)
      ├─ compression            (ContextCompressor)
      ├─ budget                 (ContextBudget)
      ├─ prompt build           (build_sections)
      └─ answer                 (LiteLLM, optional)

It drives the PUBLIC components directly rather than calling
`build_ranked_context`, for two reasons: snapshots between every stage (which the
one-shot call does not expose), and the ability to vary fusion threshold or
ranking weights per run — which is what the threshold and weight sweeps need.
The components themselves are untouched, so what runs here is what runs in
production.

Nothing in this module writes to any store.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.context import ContextBuilder, build_sections
from backend.context.budget import BudgetPolicy, ContextBudget
from backend.context.bundle import ContextBundle, ContextItem
from backend.context.compression import ContextCompressor
from backend.context.fusion import HybridContextFusion
from backend.context.ranking import RankingWeights, WeightedRanker

from .dataset import GoldenCase

log = logging.getLogger("aganeti.evals.runner")


@dataclass(slots=True)
class StageTiming:
    entity_resolution_ms: float = 0.0
    graph_ms: float = 0.0
    providers_ms: float = 0.0
    fusion_ms: float = 0.0
    ranking_ms: float = 0.0
    compression_ms: float = 0.0
    budget_ms: float = 0.0
    prompt_ms: float = 0.0
    llm_ms: float = 0.0

    @property
    def retrieval_ms(self) -> float:
        return self.entity_resolution_ms + self.graph_ms + self.providers_ms

    @property
    def processing_ms(self) -> float:
        return (self.fusion_ms + self.ranking_ms + self.compression_ms
                + self.budget_ms + self.prompt_ms)

    @property
    def total_ms(self) -> float:
        return self.retrieval_ms + self.processing_ms + self.llm_ms

    def as_dict(self) -> dict[str, float]:
        from dataclasses import asdict
        return {**{k: round(v, 2) for k, v in asdict(self).items()},
                "retrieval_ms": round(self.retrieval_ms, 2),
                "processing_ms": round(self.processing_ms, 2),
                "total_ms": round(self.total_ms, 2)}


@dataclass(slots=True)
class RunTrace:
    """Everything one case produced, stage by stage."""

    case_id: str
    question: str

    resolved_entities: list[str] = field(default_factory=list)
    unresolved_mentions: list[str] = field(default_factory=list)
    resolution_detail: list[dict] = field(default_factory=list)

    graph_nodes: list[str] = field(default_factory=list)
    graph_relationships: list[str] = field(default_factory=list)   # "a|REL|b"
    graph_hops: dict[str, int] = field(default_factory=dict)

    qdrant_documents: list[str] = field(default_factory=list)
    qdrant_scores: list[float] = field(default_factory=list)

    bundle_items: int = 0
    provider_items: dict[str, int] = field(default_factory=dict)

    fused_items: list[ContextItem] = field(default_factory=list)
    ranked_items: list[ContextItem] = field(default_factory=list)
    compressed_items: list[ContextItem] = field(default_factory=list)
    final_items: list[ContextItem] = field(default_factory=list)

    duplicates_merged: int = 0
    cross_provider_merges: int = 0
    corroborated_items: int = 0
    max_corroboration: int = 0

    tokens_available: int = 0
    tokens_used: int = 0
    dropped_for_budget: int = 0
    allocation: dict[str, int] = field(default_factory=dict)
    contribution: dict[str, int] = field(default_factory=dict)

    prompt_sections: list[str] = field(default_factory=list)
    answer: str = ""

    timing: StageTiming = field(default_factory=StageTiming)
    errors: list[str] = field(default_factory=list)

    @property
    def provider_order(self) -> list[str]:
        """Providers in the order they first appear in the FINAL ranked list."""
        seen, out = set(), []
        for item in self.final_items:
            if item.provider not in seen:
                seen.add(item.provider)
                out.append(item.provider)
        return out

    @property
    def context_texts(self) -> list[str]:
        return [i.text for i in self.final_items]

    @property
    def compression_ratio(self) -> float:
        if not self.fused_items:
            return 0.0
        return 1 - (len(self.compressed_items) / len(self.fused_items))

    @property
    def dedup_ratio(self) -> float:
        if not self.bundle_items:
            return 0.0
        return 1 - (len(self.fused_items) / self.bundle_items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "question": self.question,
            "resolved_entities": self.resolved_entities,
            "unresolved_mentions": self.unresolved_mentions,
            "graph_nodes": self.graph_nodes,
            "graph_relationships": self.graph_relationships,
            "qdrant_documents": self.qdrant_documents,
            "bundle_items": self.bundle_items,
            "provider_items": self.provider_items,
            "counts": {"fused": len(self.fused_items),
                       "compressed": len(self.compressed_items),
                       "final": len(self.final_items)},
            "duplicates_merged": self.duplicates_merged,
            "cross_provider_merges": self.cross_provider_merges,
            "corroborated_items": self.corroborated_items,
            "max_corroboration": self.max_corroboration,
            "tokens": {"available": self.tokens_available, "used": self.tokens_used,
                       "dropped": self.dropped_for_budget},
            "allocation": self.allocation, "contribution": self.contribution,
            "provider_order": self.provider_order,
            "prompt_sections": [s.split("\n")[0] for s in self.prompt_sections],
            "answer": self.answer,
            "timing": self.timing.as_dict(),
            "ratios": {"dedup": round(self.dedup_ratio, 4),
                       "compression": round(self.compression_ratio, 4)},
            "errors": self.errors,
        }


@dataclass(slots=True)
class RunnerConfig:
    """What to vary. The sweeps construct one of these per trial."""

    fusion_threshold: float = 0.6
    weights: Optional[RankingWeights] = None
    budget_policy: Optional[BudgetPolicy] = None
    graph_depth: int = 1
    graph_top_k: int = 8
    qdrant_top_k: int = 5
    compress: bool = True
    apply_budget: bool = True
    with_answer: bool = False
    providers: Optional[list] = None     # override for offline/fixture runs


class EvaluationRunner:
    """Runs golden cases through the real pipeline, capturing everything."""

    def __init__(self, config: Optional[RunnerConfig] = None) -> None:
        self.config = config or RunnerConfig()
        self._graph_api = None

    def _graph(self):
        if self._graph_api is None:
            from backend.knowledge_graph import GraphRetrievalAPI
            self._graph_api = GraphRetrievalAPI(depth=self.config.graph_depth)
        return self._graph_api

    # ── stages ───────────────────────────────────────────────────────────────

    async def _stage_graph(self, case: GoldenCase, trace: RunTrace) -> None:
        """Entity resolution + graph retrieval, measured separately."""
        try:
            from backend.knowledge_graph import is_enabled
            if not is_enabled():
                trace.errors.append("graph: Neo4j disabled")
                return
        except Exception as e:  # noqa: BLE001
            trace.errors.append(f"graph: unavailable ({e})")
            return

        api = self._graph()
        try:
            t0 = time.perf_counter()
            entities, timings = await asyncio.to_thread(api.resolve_only, case.question)
            trace.timing.entity_resolution_ms = (time.perf_counter() - t0) * 1000
            trace.resolved_entities = [e.canonical_name for e in entities if e.resolved]
            trace.unresolved_mentions = [e.mention for e in entities if not e.resolved]
            trace.resolution_detail = [
                {"mention": e.mention, "entity_id": e.entity_id,
                 "canonical_name": e.canonical_name, "match_type": e.match_type,
                 "score": e.score} for e in entities]
        except Exception as e:  # noqa: BLE001 — a stage failure must not abort the case
            trace.errors.append(f"entity_resolution: {e}")
            return

        try:
            t0 = time.perf_counter()
            ctx = await asyncio.to_thread(api.retrieve, case.question,
                                          depth=self.config.graph_depth,
                                          top_k=self.config.graph_top_k)
            trace.timing.graph_ms = (time.perf_counter() - t0) * 1000
            trace.graph_nodes = [n.canonical_name for n in ctx.subgraph.nodes]
            trace.graph_hops = {n.canonical_name: n.hop for n in ctx.subgraph.nodes}
            trace.graph_relationships = [
                f"{e.start_id}|{e.rel_type}|{e.end_id}" for e in ctx.subgraph.edges]
        except Exception as e:  # noqa: BLE001
            trace.errors.append(f"graph_retrieval: {e}")

    async def _stage_providers(self, case: GoldenCase, trace: RunTrace) -> ContextBundle:
        builder = ContextBuilder(providers=self.config.providers)
        t0 = time.perf_counter()
        bundle = await builder.build_context(case.user_id, case.question, case.session_id)
        trace.timing.providers_ms = (time.perf_counter() - t0) * 1000

        trace.bundle_items = len(bundle.all_items)
        trace.provider_items = {n: s.items for n, s in bundle.stats.providers.items()}
        for name, stat in bundle.stats.providers.items():
            if not stat.ok and not stat.skipped:
                trace.errors.append(f"provider {name}: {stat.error}")

        corporate = bundle.get("corporate")
        trace.qdrant_documents = [
            (i.metadata.get("document_id") or i.source or "") for i in corporate]
        trace.qdrant_scores = [i.score for i in corporate if i.score is not None]
        return bundle

    def _stage_fuse_rank_compress_budget(self, case: GoldenCase, bundle: ContextBundle,
                                         trace: RunTrace) -> None:
        cfg = self.config

        t0 = time.perf_counter()
        fusion = HybridContextFusion(similarity_threshold=cfg.fusion_threshold)
        fused, fstats = fusion.fuse(bundle)
        trace.timing.fusion_ms = (time.perf_counter() - t0) * 1000
        trace.fused_items = list(fused)
        trace.duplicates_merged = fstats.duplicates_merged
        trace.cross_provider_merges = fstats.cross_provider_merges
        trace.corroborated_items = fstats.corroborated_items
        trace.max_corroboration = max((i.corroboration for i in fused), default=0)

        t0 = time.perf_counter()
        ranker = WeightedRanker(weights=cfg.weights)
        ranked = ranker.rank(list(fused), case.question)
        trace.timing.ranking_ms = (time.perf_counter() - t0) * 1000
        trace.ranked_items = list(ranked)

        items = ranked
        if cfg.compress:
            t0 = time.perf_counter()
            result = ContextCompressor().compress(items)
            items = ranker.rank(result.items, case.question)
            trace.timing.compression_ms = (time.perf_counter() - t0) * 1000
        trace.compressed_items = list(items)

        if cfg.apply_budget:
            t0 = time.perf_counter()
            budgeted = ContextBudget(cfg.budget_policy).apply(items)
            trace.timing.budget_ms = (time.perf_counter() - t0) * 1000
            items = budgeted.items
            trace.tokens_available = budgeted.tokens_available
            trace.tokens_used = budgeted.tokens_used
            trace.dropped_for_budget = budgeted.dropped
            trace.allocation = budgeted.allocation
            trace.contribution = budgeted.contribution
        trace.final_items = list(items)

    def _stage_prompt(self, bundle: ContextBundle, trace: RunTrace) -> None:
        """Regroup the final items and build the prompt sections."""
        t0 = time.perf_counter()
        grouped: dict[str, list[ContextItem]] = {}
        for item in trace.final_items:
            grouped.setdefault(item.provider, []).append(item)
        for name in list(bundle._FIELDS) + list(bundle.extra):
            bundle.put(name, grouped.get(name, []))
        trace.prompt_sections = build_sections(bundle)
        trace.timing.prompt_ms = (time.perf_counter() - t0) * 1000

    async def _stage_answer(self, case: GoldenCase, trace: RunTrace) -> None:
        """Optional. Uses the SAME gateway as production, with a neutral instruction.

        The prompt here is an EVAL harness prompt, not a product prompt — it is
        deliberately minimal so the score reflects the retrieved context rather
        than prompt engineering, and it never touches the chat prompt."""
        from backend.services import llm as _llm
        context_block = "\n\n".join(trace.prompt_sections)
        t0 = time.perf_counter()
        try:
            trace.answer = await _llm.acomplete(
                [{"role": "system", "content":
                  "Answer using ONLY the context provided. If the context does not "
                  "contain the answer, say you do not know."},
                 {"role": "user", "content": f"{context_block}\n\nQuestion: {case.question}"}],
                temperature=0.0, max_tokens=300)
        except Exception as e:  # noqa: BLE001
            trace.errors.append(f"llm: {e}")
        trace.timing.llm_ms = (time.perf_counter() - t0) * 1000

    # ── entry points ─────────────────────────────────────────────────────────

    async def run_case(self, case: GoldenCase) -> RunTrace:
        trace = RunTrace(case_id=case.id, question=case.question)
        try:
            await self._stage_graph(case, trace)
            bundle = await self._stage_providers(case, trace)
            self._stage_fuse_rank_compress_budget(case, bundle, trace)
            self._stage_prompt(bundle, trace)
            if self.config.with_answer:
                await self._stage_answer(case, trace)
        except Exception as e:  # noqa: BLE001 — one bad case never kills a run
            log.exception("eval: case %s failed", case.id)
            trace.errors.append(f"fatal: {type(e).__name__}: {e}")
        return trace

    async def run_all(self, dataset, *, progress: bool = False) -> "list[RunTrace]":
        traces = []
        total = len(dataset)
        for i, case in enumerate(dataset, 1):
            if progress:
                print(f"  [{i}/{total}] {case.id}", flush=True)
            traces.append(await self.run_case(case))
        return traces
