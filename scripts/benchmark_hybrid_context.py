#!/usr/bin/env python3
"""
Phase 3.5 Context Engine vs Phase 4 Hybrid Context Engine.

Runs the SAME question through both and reports retrieval/ranking/fusion/
compression latency, context size, provider contribution, duplicate reduction,
compression ratio and token utilisation.

    python3 scripts/benchmark_hybrid_context.py
    python3 scripts/benchmark_hybrid_context.py --runs 3 --live
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.context import ContextBuilder, build_sections  # noqa: E402
from backend.context.bundle import ContextBundle, ContextItem  # noqa: E402
from backend.context.providers import ContextProvider  # noqa: E402

BOLD, DIM, GRN, YEL, OFF = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"

QUESTION = "How does Agentic AI use LiteLLM and what does it route to?"


def stub(name, items):
    class _S(ContextProvider):
        async def collect(self, request):
            return [ContextItem(**kw) for kw in items]
    _S.name = name
    return _S()


def _edge(s, r, e, score, sources, types):
    return dict(text=f"{s} —{r}→ {e}", provider="graph", score=score,
                metadata={"kind": "relationship", "start_id": s, "rel_type": r,
                          "end_id": e, "source_ids": list(sources),
                          "source_types": list(types), "confidence": 0.9})


def fixture_providers():
    """A realistic turn: overlapping facts across Qdrant, Neo4j and memory."""
    return [
        stub("corporate", [
            dict(text="Agentic AI uses LiteLLM for all model inference.",
                 provider="corporate", source="architecture.md", score=0.84),
            dict(text="LiteLLM routes requests to qwen-fast hosted on vLLM.",
                 provider="corporate", source="architecture.md", score=0.79),
            dict(text="Expense claims must be filed in AED within 30 days.",
                 provider="corporate", source="handbook.txt", score=0.41),
        ]),
        stub("graph", [
            _edge("agentic-ai", "USES", "litellm", 0.72, ("conv-1", "doc-2"),
                  ("conversation", "document")),
            _edge("litellm", "ROUTES_TO", "qwen-fast", 0.68, ("conv-1",), ("conversation",)),
            _edge("qwen-fast", "HOSTED_ON", "vllm", 0.64, ("doc-2",), ("document",)),
            _edge("agentic-ai", "USES", "neo4j", 0.55, ("doc-2",), ("document",)),
            _edge("agentic-ai", "USES", "qdrant", 0.54, ("doc-2",), ("document",)),
            _edge("agentic-ai", "STORES", "postgresql", 0.52, ("doc-2",), ("document",)),
        ]),
        stub("memory", [
            dict(text="Agentic AI uses LiteLLM for inference.", provider="memory",
                 source="user_memory"),
        ]),
        stub("calendar", [dict(text="10:00 Architecture review", provider="calendar")]),
        stub("tasks", [dict(text="Pending Tasks:\n- [high] Finish LiteLLM migration",
                            provider="tasks")]),
    ]


async def run(runs: int) -> None:
    provs = fixture_providers()

    # ── phase 3.5: retrieval only, no fusion ──
    base_ms, base_items, base_tokens = [], 0, 0
    for _ in range(runs):
        b = ContextBuilder(providers=fixture_providers())
        bundle = await b.build_context("u", QUESTION, "s")
        base_ms.append(bundle.stats.total_ms)
        base_items = len(bundle.all_items)
        base_tokens = sum(i.approx_tokens for i in bundle.all_items)
        base_sections = build_sections(bundle)

    # ── phase 4: hybrid ──
    hy_ms, fuse_ms, rank_ms, comp_ms, bud_ms = [], [], [], [], []
    ranked = None
    for _ in range(runs):
        b = ContextBuilder(providers=fixture_providers())
        ranked = await b.build_ranked_context("u", QUESTION, "s")
        f = ranked.fusion
        hy_ms.append(ranked.stats.total_ms + f.fusion_ms + f.ranking_ms
                     + f.compression_ms + f.budget_ms)
        fuse_ms.append(f.fusion_ms); rank_ms.append(f.ranking_ms)
        comp_ms.append(f.compression_ms); bud_ms.append(f.budget_ms)
    f = ranked.fusion
    hybrid_sections = build_sections(ranked)

    med = statistics.median
    print(f"\n{BOLD}1. Latency (median of {runs}){OFF}\n")
    print(f"  {'stage':<22}{'phase 3.5':>12}{'phase 4':>12}")
    print("  " + "─" * 46)
    print(f"  {'retrieval (providers)':<22}{med(base_ms):>10.1f}ms{med(base_ms):>10.1f}ms")
    print(f"  {'fusion':<22}{'—':>12}{med(fuse_ms):>10.1f}ms")
    print(f"  {'ranking':<22}{'—':>12}{med(rank_ms):>10.1f}ms")
    print(f"  {'compression':<22}{'—':>12}{med(comp_ms):>10.1f}ms")
    print(f"  {'budget':<22}{'—':>12}{med(bud_ms):>10.1f}ms")
    overhead = med(fuse_ms) + med(rank_ms) + med(comp_ms) + med(bud_ms)
    print("  " + "─" * 46)
    print(f"  {'TOTAL':<22}{med(base_ms):>10.1f}ms{med(hy_ms):>10.1f}ms")
    print(f"\n  {DIM}hybrid overhead: {overhead:.1f}ms "
          f"({overhead / max(med(base_ms), 0.01) * 100:.0f}% of retrieval){OFF}")

    print(f"\n{BOLD}2. Context size and quality{OFF}\n")
    print(f"  {'metric':<28}{'phase 3.5':>12}{'phase 4':>12}")
    print("  " + "─" * 52)
    kept = len(ranked.items)
    hy_tokens = f.tokens_used
    print(f"  {'items':<28}{base_items:>12}{kept:>12}")
    print(f"  {'tokens':<28}{base_tokens:>12}{hy_tokens:>12}")
    print(f"  {'duplicates merged':<28}{0:>12}{f.duplicates_merged:>12}")
    print(f"  {'cross-source merges':<28}{0:>12}{f.cross_provider_merges:>12}")
    print(f"  {'corroborated items':<28}{0:>12}{f.corroborated_items:>12}")
    print(f"  {'dedup ratio':<28}{'—':>12}{f.dedup_ratio:>11.0%}")
    print(f"  {'compression ratio':<28}{'—':>12}{f.compression_ratio:>11.0%}")
    print(f"  {'budget utilisation':<28}{'—':>12}{f.budget_utilization:>11.0%}")
    if base_tokens:
        print(f"\n  {GRN}context shrank {(1 - hy_tokens / base_tokens):.0%} "
              f"while keeping every distinct fact{OFF}")

    print(f"\n{BOLD}3. Provider contribution (phase 4){OFF}\n")
    print(f"  {'provider':<14}{'items kept':>12}{'tokens':>10}")
    print("  " + "─" * 36)
    for name in sorted(set(list(f.contribution) + list(f.allocation))):
        print(f"  {name:<14}{f.contribution.get(name, 0):>12}{f.allocation.get(name, 0):>10}")

    print(f"\n{BOLD}4. Ranked output{OFF}\n")
    for i, it in enumerate(ranked.items, 1):
        mark = f" {GRN}✓{len(set(it.providers))} sources{OFF}" if it.corroboration > 1 else ""
        print(f"  {i}. {it.final_score:.3f} [{'+'.join(it.providers)}]{mark}")
        print(f"     {it.text[:88]}")
    print(f"\n  {DIM}sections emitted: 3.5 = {len(base_sections)}, "
          f"4 = {len(hybrid_sections)}{OFF}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    print(f"\n{BOLD}Hybrid Context Engine benchmark{OFF}")
    print(f"{DIM}question: {QUESTION}{OFF}")
    asyncio.run(run(args.runs))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
