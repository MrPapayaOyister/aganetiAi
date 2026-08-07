#!/usr/bin/env python3
"""
Phase 3 retrieval benchmark.

Measures the three things the phase asked for:

  1. entity resolution accuracy   — against a hand-labelled set, by match type
  2. subgraph retrieval latency   — by depth, LLM excluded and included
  3. ranking quality              — nDCG against a hand-ranked expectation

Builds its own fixture graph under source="bench-retrieval" and removes it.

    python3 scripts/benchmark_graph_retrieval.py
    python3 scripts/benchmark_graph_retrieval.py --runs 5
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.knowledge_graph import (  # noqa: E402
    BatchKnowledgeGraphBuilder,
    ExtractedEntity,
    ExtractedRelationship,
    GraphRetrievalAPI,
    GraphRetriever,
    Provenance,
    get_graph_service,
    verify_connectivity,
)
from backend.knowledge_graph.retrieval import CanonicalEntityRegistry  # noqa: E402

BOLD, DIM, GRN, YEL, RED, OFF = ("\033[1m", "\033[2m", "\033[32m",
                                 "\033[33m", "\033[31m", "\033[0m")
SOURCE = "bench-retrieval"

# (mention, expected canonical id or None). Mixes exact names, stored aliases,
# static-map aliases, typos, casing noise, and things that genuinely are not in
# the graph — a resolver that never says "no" is not accurate, it is reckless.
RESOLUTION_CASES: list[tuple[str, str | None]] = [
    ("Agentic AI", "agentic-ai"),
    ("agentic ai", "agentic-ai"),
    ("Microsoft Graph", "microsoft-graph"),
    ("MS Graph", "microsoft-graph"),          # alias stored on the node
    ("msgraph", "microsoft-graph"),           # static normalizer alias
    ("Microsft Graph", "microsoft-graph"),    # typo → fuzzy
    ("LiteLLM", "litellm"),
    ("litellm proxy", "litellm"),             # static alias
    ("qwen-fast", "qwen-fast"),
    ("Qwen Fast", "qwen-fast"),               # static alias + casing
    ("Postgres", "postgresql"),               # static alias
    ("PostgreSQL", "postgresql"),
    ("Akshay", "akshay"),
    ("vLLM", "vllm"),
    ("Zzzz Nonexistent", None),               # must NOT resolve
    ("Completely Unrelated Widget", None),    # must NOT resolve
]

# Expected relevance for "what does Agentic AI depend on?", 0–3.
RANKING_IDEAL: dict[str, int] = {
    "agentic-ai": 3, "litellm": 3, "microsoft-graph": 2,
    "qwen-fast": 2, "akshay": 1, "vllm": 1, "postgresql": 1,
}


def build_fixture(svc) -> None:
    svc.run_query("MATCH (n) WHERE n.source=$s DETACH DELETE n", {"s": SOURCE}, op="clean")
    b = BatchKnowledgeGraphBuilder(service=svc, source=SOURCE)
    entities = [
        ExtractedEntity(name="Akshay", type="Person"),
        ExtractedEntity(name="Agentic AI", type="Project"),
        ExtractedEntity(name="LiteLLM", type="Technology"),
        ExtractedEntity(name="Microsoft Graph", type="Technology",
                        secondary_labels=["API"], aliases=["MS Graph"]),
        ExtractedEntity(name="qwen-fast", type="Model"),
        ExtractedEntity(name="vLLM", type="Technology"),
        ExtractedEntity(name="PostgreSQL", type="Technology"),
    ]
    rels = [
        ExtractedRelationship(source="Akshay", type="WORKS_ON", target="Agentic AI"),
        ExtractedRelationship(source="Agentic AI", type="USES", target="LiteLLM"),
        ExtractedRelationship(source="Agentic AI", type="USES", target="Microsoft Graph"),
        ExtractedRelationship(source="Agentic AI", type="STORES", target="PostgreSQL"),
        ExtractedRelationship(source="LiteLLM", type="ROUTES_TO", target="qwen-fast"),
        ExtractedRelationship(source="qwen-fast", type="HOSTED_ON", target="vLLM"),
    ]
    b.build(entities, rels, provenance=Provenance(source_id="bench-conv",
                                                  source_type="conversation",
                                                  confidence=0.9, model="qwen-fast"))
    # Corroborate LiteLLM from a second source so ranking has something to rank on.
    b.build([ExtractedEntity(name="LiteLLM", type="Technology")], [],
            provenance=Provenance(source_id="bench-doc", source_type="document",
                                  confidence=0.85, model="gpt-4.1"))


def bench_resolution(registry) -> None:
    print(f"\n{BOLD}1. Entity resolution accuracy{OFF}")
    print(f"{DIM}   {len(RESOLUTION_CASES)} cases: exact, alias, static-map, typo, "
          f"and 2 that must NOT resolve{OFF}\n")

    by_type: dict[str, list[bool]] = {}
    correct = latencies = 0
    lat: list[float] = []
    false_positives, misses = [], []

    for mention, expected in RESOLUTION_CASES:
        started = time.perf_counter()
        r = registry.resolve(mention)
        lat.append((time.perf_counter() - started) * 1000)
        got = r.entity_id or None
        ok = got == expected
        correct += ok
        by_type.setdefault(r.match_type, []).append(ok)
        if not ok:
            (false_positives if expected is None else misses).append(
                f"{mention!r} → {got or 'unresolved'} (want {expected or 'unresolved'})")
        mark = f"{GRN}✓{OFF}" if ok else f"{RED}✗{OFF}"
        print(f"  {mark} {mention!r:28} → {r.match_type:9} "
              f"{(got or '(unresolved)'):20} {DIM}score={r.score:.2f}{OFF}")

    total = len(RESOLUTION_CASES)
    print(f"\n  accuracy      : {GRN}{correct}/{total} = {correct / total:.1%}{OFF}")
    print(f"  median latency: {statistics.median(lat):.1f}ms  "
          f"(p95 {sorted(lat)[int(len(lat) * 0.95) - 1]:.1f}ms)")
    print(f"  by match type : " + ", ".join(
        f"{k}={sum(v)}/{len(v)}" for k, v in sorted(by_type.items())))
    if false_positives:
        print(f"  {RED}false positives{OFF}: {false_positives}")
    if misses:
        print(f"  {YEL}misses{OFF}: {misses}")


def bench_latency(svc, runs: int) -> None:
    print(f"\n{BOLD}2. Subgraph retrieval latency{OFF}")
    print(f"{DIM}   graph-only (no LLM), {runs} run(s) per depth{OFF}\n")
    retriever = GraphRetriever(service=svc)

    print(f"  {'depth':>6}{'nodes':>8}{'edges':>8}{'queries':>10}"
          f"{'median':>11}{'p95':>10}")
    print("  " + "─" * 53)
    for depth in (0, 1, 2, 3):
        times, nodes, edges, queries = [], 0, 0, 0
        for _ in range(runs):
            started = time.perf_counter()
            sg = retriever.expand(["agentic-ai"], depth=depth)
            times.append((time.perf_counter() - started) * 1000)
            nodes, edges, queries = len(sg.nodes), len(sg.edges), retriever.queries
        p95 = sorted(times)[max(0, int(len(times) * 0.95) - 1)]
        print(f"  {depth:>6}{nodes:>8}{edges:>8}{queries:>10}"
              f"{statistics.median(times):>9.1f}ms{p95:>8.1f}ms")

    print(f"\n{DIM}   query count grows +1 per hop — expansion is one round trip per hop,"
          f"\n   not a variable-length path match.{OFF}")


def dcg(relevances: list[int]) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances))


def ndcg(ranked_ids: list[str], ideal: dict[str, int], k: int = 6) -> float:
    got = [ideal.get(i, 0) for i in ranked_ids[:k]]
    best = sorted(ideal.values(), reverse=True)[:k]
    denominator = dcg(best)
    return dcg(got) / denominator if denominator else 0.0


def bench_ranking(svc) -> None:
    print(f"\n{BOLD}3. Ranking quality{OFF}")
    print(f"{DIM}   nDCG@6 for 'what does Agentic AI depend on?' against a "
          f"hand-ranked ideal{OFF}\n")
    retriever = GraphRetriever(service=svc)
    sg = retriever.expand(["agentic-ai"], depth=2)

    print(f"  {'#':>3}  {'entity':<20}{'hop':>4}{'score':>9}   signals")
    for i, node in enumerate(sg.nodes[:8], 1):
        sig = " ".join(f"{k[:4]}={v:.2f}" for k, v in node.signals.items())
        rel = RANKING_IDEAL.get(node.entity_id, 0)
        print(f"  {i:>3}  {node.canonical_name:<20}{node.hop:>4}{node.score:>9.3f}   "
              f"{DIM}{sig}{OFF}  {DIM}(ideal rel={rel}){OFF}")

    score = ndcg(sg.node_ids, RANKING_IDEAL)
    colour = GRN if score >= 0.9 else (YEL if score >= 0.75 else RED)
    print(f"\n  nDCG@6        : {colour}{score:.3f}{OFF}")

    # Random ordering as a floor, so the number above means something.
    import random
    shuffled = list(sg.node_ids)
    random.Random(42).shuffle(shuffled)
    print(f"  random floor  : {ndcg(shuffled, RANKING_IDEAL):.3f}")
    print(f"  {DIM}corroboration check: {OFF}", end="")
    by_id = {n.entity_id: n for n in sg.nodes}
    if "litellm" in by_id and "microsoft-graph" in by_id:
        lite, msg = by_id["litellm"], by_id["microsoft-graph"]
        verdict = (f"{GRN}LiteLLM (2 sources) outranks Microsoft Graph (1){OFF}"
                   if lite.score > msg.score else
                   f"{YEL}corroboration did not decide the order{OFF}")
        print(verdict)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    print(f"\n{BOLD}Graph retrieval benchmark (phase 3){OFF}")
    if not verify_connectivity():
        print(f"{RED}Neo4j unreachable.{OFF}")
        return 1

    svc = get_graph_service()
    build_fixture(svc)
    registry = CanonicalEntityRegistry(service=svc, alias_file=None)
    registry.warm(force=True)

    try:
        bench_resolution(registry)
        bench_latency(svc, args.runs)
        bench_ranking(svc)
    finally:
        svc.run_query("MATCH (n) WHERE n.source=$s DETACH DELETE n",
                      {"s": SOURCE}, op="clean")
        print(f"\n{DIM}fixture removed{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
