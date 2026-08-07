#!/usr/bin/env python3
"""
Phase 2 vs phase 2.5 benchmark.

Runs the SAME texts through both pipelines and reports LLM latency, Neo4j round
trips and total wall-clock. Both write under their own `source` tag and clean up.

    python3 scripts/benchmark_knowledge_graph.py
    python3 scripts/benchmark_knowledge_graph.py --runs 3 --scale 200

`--scale N` additionally measures pure write throughput for N synthetic nodes,
with no LLM involved — that isolates the batching win from extraction noise.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.knowledge_graph import (  # noqa: E402
    BatchKnowledgeGraphBuilder,
    ExtractedEntity,
    ExtractedRelationship,
    KnowledgeGraphBuilder,
    KnowledgeGraphPipeline,
    KnowledgeSource,
    Provenance,
    get_graph_service,
    verify_connectivity,
)

BOLD, DIM, GRN, YEL, OFF = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"

TEXTS = [
    "Akshay integrated Microsoft Graph into Agentic AI. Agentic AI uses LiteLLM "
    "which routes requests to qwen-fast hosted on vLLM.",
    "The Q3 planning meeting covered the Dar Al Ber dashboard. Anchu owns the "
    "reporting project and depends on PostgreSQL for the fact table.",
    "Our FastAPI backend authenticates with Microsoft Entra ID and stores "
    "embeddings in Qdrant. Neo4j holds the knowledge graph.",
]

OLD_SOURCE, NEW_SOURCE = "bench-old", "bench-new"


def clean(svc, *sources: str) -> None:
    for s in sources:
        svc.run_query("MATCH (n) WHERE n.source = $s DETACH DELETE n", {"s": s}, op="bench_clean")


class CountingService:
    """Transparent proxy that counts Neo4j round trips.

    The phase-2 builder issues one statement per node and per edge; the phase-2.5
    builder issues one per label-group. Counting at this boundary is the only
    honest way to compare them — the builders' own numbers cannot be trusted to
    mean the same thing."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.queries = 0

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def counted(*a, **kw):
            self.queries += 1
            return attr(*a, **kw)
        return counted


def fmt(ms: float) -> str:
    return f"{ms:8.0f}ms"


def bench_pipelines(runs: int) -> None:
    svc = get_graph_service()
    print(f"\n{BOLD}1. End-to-end pipeline — {len(TEXTS)} texts × {runs} run(s){OFF}")
    print(f"{DIM}   old = 2 LLM calls + per-node MERGE   |   "
          f"new = 1 LLM call + batched UNWIND{OFF}\n")

    rows = []
    for label, kwargs, source in (("old (phase 2)",
                                   {"legacy_two_call": True, "legacy_builder": True}, OLD_SOURCE),
                                  ("new (phase 2.5)", {}, NEW_SOURCE)):
        llm_ms, build_ms, total_ms, queries, nodes, edges = [], [], [], [], 0, 0
        for _ in range(runs):
            clean(svc, source)
            counting = CountingService(svc)
            pipeline = KnowledgeGraphPipeline(service=counting, source=source, **kwargs)
            # The builder holds the counting proxy; rebuild it so both paths use it.
            if kwargs.get("legacy_builder"):
                pipeline.builder = KnowledgeGraphBuilder(service=counting, source=source)
            else:
                pipeline.builder = BatchKnowledgeGraphBuilder(service=counting, source=source)

            for i, text in enumerate(TEXTS):
                src = KnowledgeSource.conversation(f"bench-{i}", text, user_id="bench")
                r = pipeline.process(src)
                llm_ms.append(r.stats.extract_ms)
                build_ms.append(r.stats.build_ms)
                total_ms.append(r.stats.total_ms)
                nodes += r.stats.nodes_created
                edges += r.stats.relationships_created
            queries.append(counting.queries)

        rows.append({
            "label": label,
            "llm": statistics.mean(llm_ms),
            "build": statistics.mean(build_ms),
            "total": statistics.mean(total_ms),
            "queries": statistics.mean(queries),
            "nodes": nodes // runs, "edges": edges // runs,
            "calls": 2 if kwargs else 1,
        })

    hdr = f"  {'':<16}{'LLM':>11}{'build':>11}{'total/text':>13}{'LLM calls':>11}{'neo4j queries':>15}"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for r in rows:
        print(f"  {r['label']:<16}{fmt(r['llm'])}{fmt(r['build'])}{fmt(r['total'])}"
              f"{r['calls'] * len(TEXTS):>11}{r['queries']:>15.0f}")

    old, new = rows[0], rows[1]
    print()
    for name, o, n, lower_better in (("LLM latency", old["llm"], new["llm"], True),
                                     ("Neo4j writes", old["queries"], new["queries"], True),
                                     ("Total per text", old["total"], new["total"], True)):
        if o == 0:
            continue
        delta = (n - o) / o * 100
        colour = GRN if (delta < 0) == lower_better else YEL
        arrow = "faster/fewer" if delta < 0 else "slower/more"
        print(f"  {name:<16} {o:9.0f} → {n:9.0f}   {colour}{delta:+6.1f}%{OFF}  ({arrow})")
    print(f"\n  {DIM}graph written: old {old['nodes']} nodes/{old['edges']} edges, "
          f"new {new['nodes']} nodes/{new['edges']} edges{OFF}")
    clean(svc, OLD_SOURCE, NEW_SOURCE)


def bench_writes(scale: int) -> None:
    """Pure write throughput — no LLM, so the batching effect is unmixed."""
    svc = get_graph_service()
    print(f"\n{BOLD}2. Write throughput — {scale} nodes + {scale - 1} edges, no LLM{OFF}\n")

    entities = [ExtractedEntity(name=f"Bench Node {i}", type="Technology") for i in range(scale)]
    rels = [ExtractedRelationship(source="Bench Node 0", type="USES",
                                  target=f"Bench Node {i}") for i in range(1, scale)]

    results = []
    for label, make in (("old (per-node MERGE)",
                         lambda s: KnowledgeGraphBuilder(service=s, source=OLD_SOURCE)),
                        ("new (UNWIND batch)",
                         lambda s: BatchKnowledgeGraphBuilder(service=s, source=NEW_SOURCE))):
        source = OLD_SOURCE if "old" in label else NEW_SOURCE
        clean(svc, source)
        counting = CountingService(svc)
        builder = make(counting)
        started = time.perf_counter()
        try:
            builder.build(entities, rels, provenance=Provenance(source_id="bench"))
        except TypeError:
            builder.build(entities, rels)       # phase-2 builder has no provenance arg
        took = (time.perf_counter() - started) * 1000
        results.append((label, took, counting.queries))
        clean(svc, source)

    print(f"  {'':<24}{'wall clock':>13}{'neo4j queries':>16}{'per node':>12}")
    print("  " + "─" * 63)
    for label, took, queries in results:
        print(f"  {label:<24}{took:11.0f}ms{queries:>16}{took / scale:10.2f}ms")

    (_, old_ms, old_q), (_, new_ms, new_q) = results
    print(f"\n  round trips : {old_q} → {new_q}  "
          f"{GRN}{old_q / max(new_q, 1):.0f}× fewer{OFF}")
    print(f"  wall clock  : {old_ms:.0f}ms → {new_ms:.0f}ms  "
          f"{GRN}{old_ms / max(new_ms, 1):.1f}× faster{OFF}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--scale", type=int, default=200)
    args = ap.parse_args()

    print(f"\n{BOLD}Knowledge-graph benchmark — phase 2 vs phase 2.5{OFF}")
    if not verify_connectivity():
        print("Neo4j unreachable.")
        return 1

    bench_pipelines(args.runs)
    bench_writes(args.scale)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
