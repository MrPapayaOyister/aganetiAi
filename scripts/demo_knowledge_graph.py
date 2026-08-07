#!/usr/bin/env python3
"""
Knowledge-graph pipeline demo.

Runs the spec's sentence end to end and prints what landed in Neo4j, then proves
idempotency by running it a second time and showing that nothing new is created.

    python3 scripts/demo_knowledge_graph.py                 # run + clean up
    python3 scripts/demo_knowledge_graph.py --keep          # leave the graph populated
    python3 scripts/demo_knowledge_graph.py --dry-run       # extract only, write nothing
    python3 scripts/demo_knowledge_graph.py --text "..."    # your own sentence

Nodes are written with source="demo" so cleanup removes exactly what it created.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.knowledge_graph import (  # noqa: E402
    GraphUnavailable,
    KnowledgeGraphPipeline,
    KnowledgeSource,
    Normalizer,
    NodeLabel,
    ProvenanceSummary,
    get_graph_service,
    verify_connectivity,
)

DEMO_TEXT = (
    "Akshay integrated Microsoft Graph into Agentic AI. "
    "Agentic AI uses LiteLLM which routes requests to qwen-fast hosted on vLLM."
)

SOURCE = "demo"
BOLD, DIM, GRN, YEL, RED, OFF = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def cleanup(svc) -> int:
    """Delete only what this demo wrote (source='demo'). Returns nodes removed."""
    rows = svc.run_query(
        "MATCH (n) WHERE n.source = $source "
        "WITH n, n.id AS id, labels(n)[0] AS label DETACH DELETE n RETURN count(*) AS removed",
        {"source": SOURCE}, op="demo_cleanup")
    return rows[0]["removed"] if rows else 0


def print_graph(svc) -> None:
    """Show the demo subgraph as edges, read back from Neo4j."""
    rows = svc.run_query(
        "MATCH (a)-[r]->(b) WHERE a.source = $source AND b.source = $source "
        "RETURN a.name AS src, labels(a)[0] AS src_label, type(r) AS rel, "
        "       b.name AS tgt, labels(b)[0] AS tgt_label "
        "ORDER BY src, rel, tgt",
        {"source": SOURCE}, op="demo_read_edges")
    if not rows:
        print(f"  {DIM}(no relationships){OFF}")
        return
    for r in rows:
        print(f"  {GRN}{r['src']}{OFF} {DIM}({r['src_label']}){OFF}"
              f"  ──{BOLD}{r['rel']}{OFF}──▶  "
              f"{GRN}{r['tgt']}{OFF} {DIM}({r['tgt_label']}){OFF}")


def print_nodes(svc) -> None:
    """Show ALL labels per node — the phase-2.5 point is that a node has several."""
    rows = svc.run_query(
        "MATCH (n) WHERE n.source = $source "
        "RETURN n.id AS id, n.canonical_name AS name, labels(n) AS labels, "
        "       n.aliases AS aliases, n.confidence AS conf "
        "ORDER BY name",
        {"source": SOURCE}, op="demo_read_nodes")
    for r in rows:
        labels = ":".join(l for l in r["labels"] if l != "Entity")
        aliases = f"  {DIM}aka {', '.join(r['aliases'])}{OFF}" if r.get("aliases") else ""
        print(f"  {GRN}{r['name']:<20}{OFF} {DIM}:{labels}{OFF}"
              f"  conf={r['conf']}  {DIM}{r['id']}{OFF}{aliases}")


def print_provenance(svc) -> None:
    rows = svc.run_query(
        "MATCH (n) WHERE n.source = $source AND size(coalesce(n.source_ids, [])) > 0 "
        "RETURN n.canonical_name AS name, n.source_ids AS ids, n.source_types AS types, "
        "       n.observations AS obs, n.confidence AS conf, n.models AS models "
        "ORDER BY name", {"source": SOURCE}, op="demo_read_prov")
    for r in rows:
        s = ProvenanceSummary(source_ids=r["ids"] or [], source_types=r["types"] or [],
                              observations=r["obs"] or 0, confidence=r["conf"] or 0.0,
                              models=r["models"] or [])
        flag = f"  {GRN}corroborated{OFF}" if s.is_corroborated else ""
        print(f"  {r['name']:<20} seen {s.observations}× from {s.source_ids} "
              f"({', '.join(s.source_types)}) conf={s.confidence}{flag}")


def print_stats(stats) -> None:
    d = stats.as_dict()
    print(f"  LLM calls               : {d['llm_calls']}   {DIM}(phase 2 used 2){OFF}")
    print(f"  neo4j round trips       : {d['graph_queries']}")
    print(f"  summary                 : {d['summary'][:70]}")
    print(f"  keywords                : {', '.join(d['keywords'][:6])}")
    print(f"  extraction confidence   : {d['extraction_confidence']}")
    print(f"  entities extracted      : {d['entities_extracted']}")
    print(f"  after normalization     : {d['entities_after_normalization']} "
          f"({d['labels_merged']} label-merge(s))")
    print(f"  relationships extracted : {d['relationships_extracted']}")
    print(f"  relationships resolved  : {d['relationships_resolved']}")
    print(f"  nodes created / merged  : {d['nodes_created']} / {d['nodes_merged']}")
    print(f"  edges created / merged  : {d['relationships_created']} / {d['relationships_merged']}")
    if d["skipped_entities"]:
        print(f"  {YEL}skipped entities{OFF}        : {d['skipped_entities']}")
    if d["skipped_relationships"]:
        print(f"  {YEL}skipped relationships{OFF}   : {d['skipped_relationships']}")
    print(f"  timings (ms)            : extract {d['extract_ms']:.0f}, "
          f"normalize {d['normalize_ms']:.0f}, build {d['build_ms']:.0f}, "
          f"{BOLD}total {d['total_ms']:.0f}{OFF}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=DEMO_TEXT)
    ap.add_argument("--keep", action="store_true", help="leave the demo graph in place")
    ap.add_argument("--dry-run", action="store_true", help="extract only, write nothing")
    args = ap.parse_args()

    print(f"\n{BOLD}Knowledge-graph pipeline demo{OFF}")
    print(f"{DIM}text:{OFF} {args.text}\n")

    if not verify_connectivity():
        print(f"{RED}Neo4j unreachable — is the container up? (docker ps | grep neo4j){OFF}")
        return 1

    svc = get_graph_service()
    pipeline = KnowledgeGraphPipeline(source=SOURCE)

    if not args.dry_run:
        removed = cleanup(svc)
        if removed:
            print(f"{DIM}cleared {removed} node(s) from a previous run{OFF}\n")

    # ── pass 1 ───────────────────────────────────────────────────────────────
    print(f"{BOLD}1. Pipeline — first pass{OFF}")
    source = KnowledgeSource.conversation("demo-conv-1", args.text, user_id="demo-user")
    result = pipeline.process(source, dry_run=args.dry_run)
    if not result.ok:
        print(f"  {RED}FAILED{OFF}: {result.error}")
        return 1
    print_stats(result.stats)

    print(f"\n{BOLD}2. Entities{OFF}")
    for e in result.entities:
        raw = f"  {DIM}(from {e.raw_name!r}){OFF}" if e.raw_name != e.name else ""
        print(f"  {e.type:<14} {e.name}{raw}")

    print(f"\n{BOLD}3. Relationships{OFF}")
    for r in result.relationships:
        note = f"  {DIM}(model said {r.raw_type!r}){OFF}" if r.raw_type != r.type else ""
        print(f"  {r.source} ──{BOLD}{r.type}{OFF}──▶ {r.target}{note}")

    if args.dry_run:
        print(f"\n{DIM}--dry-run: nothing written to Neo4j{OFF}\n")
        return 0

    print(f"\n{BOLD}4. Graph, read back from Neo4j{OFF}")
    print(f"{DIM}nodes (note the MULTIPLE labels per node):{OFF}")
    print_nodes(svc)
    print(f"{DIM}edges:{OFF}")
    print_graph(svc)

    # ── pass 2 — idempotency ─────────────────────────────────────────────────
    print(f"\n{BOLD}5. Second pass — idempotency{OFF}")
    before = svc.run_query("MATCH (n) WHERE n.source = $source RETURN count(n) AS c",
                           {"source": SOURCE}, op="demo_count")[0]["c"]
    again = pipeline.process(source)
    after = svc.run_query("MATCH (n) WHERE n.source = $source RETURN count(n) AS c",
                          {"source": SOURCE}, op="demo_count")[0]["c"]
    ok_nodes = again.stats.nodes_created == 0
    ok_count = before == after
    print(f"  nodes created on re-run : {again.stats.nodes_created} "
          f"{GRN + 'PASS' + OFF if ok_nodes else RED + 'FAIL' + OFF}")
    print(f"  node count {before} → {after}      "
          f"{GRN + 'PASS' + OFF if ok_count else RED + 'FAIL' + OFF}")
    print(f"  edges created on re-run : {again.stats.relationships_created} "
          f"{GRN + 'PASS' + OFF if again.stats.relationships_created == 0 else YEL + 'note' + OFF}")

    # ── mixed sources converging on the same nodes ───────────────────────────
    print(f"\n{BOLD}6. Mixed knowledge sources → shared nodes{OFF}")
    pipeline.process(KnowledgeSource.document(
        "demo-doc-1",
        "The Agentic AI architecture document describes LiteLLM as the inference "
        "gateway and Neo4j as the knowledge store."))
    pipeline.process(KnowledgeSource.email(
        "demo-mail-1",
        "Subject: LiteLLM rollout. Akshay confirmed LiteLLM now fronts qwen-fast."))
    print(f"{DIM}provenance after conversation + document + email:{OFF}")
    print_provenance(svc)

    # ── normalizer, independent of the LLM ───────────────────────────────────
    print(f"\n{BOLD}7. Normalizer (deterministic, no LLM){OFF}")
    n = Normalizer()
    for variant in ("M365", "MS Graph", "Postgres", "Qwen Fast", "  the   FastAPI  ", "litellm proxy"):
        print(f"  {variant!r:<22} → {n.canonical_name(variant)!r}")

    if args.keep:
        print(f"\n{DIM}--keep: demo graph left in place (source='{SOURCE}'){OFF}")
        print(f"{DIM}browse it at http://localhost:7474 — "
              f"MATCH (n {{source:'demo'}}) RETURN n{OFF}\n")
    else:
        removed = cleanup(svc)
        print(f"\n{DIM}cleaned up {removed} demo node(s){OFF}\n")
    return 0 if (ok_nodes and ok_count) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GraphUnavailable as e:
        print(f"\n{RED}Neo4j unavailable:{OFF} {e}")
        sys.exit(1)
