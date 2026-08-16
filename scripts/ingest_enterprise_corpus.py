#!/usr/bin/env python3
"""
Ingest the generated enterprise corpus into Qdrant and/or Neo4j.

    python scripts/ingest_enterprise_corpus.py --qdrant
    python scripts/ingest_enterprise_corpus.py --graph --workers 4
    python scripts/ingest_enterprise_corpus.py --qdrant --graph
    python scripts/ingest_enterprise_corpus.py --status

Why this exists rather than `python -m backend.ingest`: that command walks
`data_vault/` with `iterdir()`, which is deliberately NOT recursive. Making it
recursive would also sweep `data_vault/user_1/` and the per-user upload folders
into the shared `__org__` corpus — turning private documents into org-wide
search results. So the corpus lives in its own subdirectory and this script feeds
it to the same `ingest_file()` the CLI uses. Nothing in the ingestion pipeline is
modified, and the ACL boundary stays where it is.

Both paths are idempotent:

  * Qdrant — `ingest_file()` derives point ids from `owner::type::source::index`
    and deletes the source's existing points before upserting, so re-running
    replaces rather than duplicates.
  * Neo4j — the pipeline MERGEs on `slugify(canonical_name)`, so re-running a
    document bumps `observations` and refreshes provenance without forking nodes.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CORPUS_DIR = ROOT / "data_vault" / "enterprise_corpus"


def ingested_doc_ids() -> set[str]:
    """Document ids already represented in the graph, read from provenance.

    The graph IS the checkpoint. A separate state file could disagree with the
    database after a crash — this cannot, because a document's id only appears in
    `source_ids` once its extraction has actually been written.
    """
    import re as _re
    from backend.knowledge_graph.service import get_graph_service
    rows = get_graph_service().run_query(
        "MATCH (n:Entity) UNWIND coalesce(n.source_ids, []) AS sid RETURN DISTINCT sid")
    return {r["sid"] for r in rows if _re.fullmatch(r"emd-\d{4}", r["sid"] or "")}


def _frontmatter(text: str) -> dict[str, str]:
    """Parse the YAML-ish frontmatter this corpus writes. Not a YAML parser —
    the generator emits a known flat shape and a dependency would buy nothing."""
    import re
    head, _, _body = text.partition("---\n\n")
    return {k: v.strip().strip('"') for k, v in
            re.findall(r"^(\w+):\s*(.+)$", head, flags=re.M)}


# ── Qdrant ────────────────────────────────────────────────────────────────────

def ingest_qdrant(files: list[Path]) -> dict[str, Any]:
    from backend.ingest import (ORG_OWNER, SHARED_TENANT, ensure_collection,
                                get_client, ingest_file)
    from config.settings import RAG_COLLECTION

    client = get_client()
    ensure_collection(client)
    before = client.count(RAG_COLLECTION).count

    chunks = 0
    failed: list[str] = []
    started = time.perf_counter()
    for i, path in enumerate(files, 1):
        try:
            # source_type "file" matches what the CLI writes, so these chunks are
            # indistinguishable from hand-dropped documents at query time — which
            # is the point: the corpus must not be a privileged special case.
            # org_id=SHARED_TENANT for the same reason the graph half declares it:
            # this corpus is org-wide. Without it these chunks land with org_id=None
            # — the exact unstamped state the 966-chunk backfill just cleaned up, so
            # an un-stamped run here would quietly re-create the problem.
            chunks += ingest_file(client, path, owner=ORG_OWNER,
                                  org_id=SHARED_TENANT, source_type="file")
        except Exception as e:  # noqa: BLE001 — one bad file must not lose the batch
            failed.append(f"{path.name}: {type(e).__name__}: {e}")
        if i % 50 == 0:
            print(f"[qdrant] {i}/{len(files)} files, {chunks} chunks")

    after = client.count(RAG_COLLECTION).count
    return {"files": len(files), "chunks_written": chunks,
            "points_before": before, "points_after": after,
            "failed": failed, "took_s": round(time.perf_counter() - started, 1)}


# ── Neo4j ─────────────────────────────────────────────────────────────────────

def ingest_graph(files: list[Path], workers: int = 1, timeout: float = 300.0,
                 retries: int = 3, backoff: float = 3.0,
                 progress_every: int = 10) -> dict[str, Any]:
    from backend.knowledge_graph.builder import SHARED_TENANT
    from backend.knowledge_graph.extractor import KnowledgeExtractor
    from backend.knowledge_graph.pipeline import KnowledgeGraphPipeline
    from backend.knowledge_graph.service import get_graph_service
    from backend.knowledge_graph.sources import KnowledgeSource

    svc = get_graph_service()
    before_n = svc.run_query("MATCH (n:Entity) RETURN count(n) AS c")[0]["c"]
    before_r = svc.run_query("MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS c")[0]["c"]

    # Concurrency here is capped by the GATEWAY, not by this process. The LiteLLM
    # route for `qwen-fast` carries `timeout: 30` (litellm config.yaml), while one
    # extraction over a ~650-word document measures 21–61s. Requests therefore only
    # pass when nothing else is queued: at 3–4 wide the gateway returns HTTP 408,
    # `complete()` swallows it and returns "", and the extractor reports
    # "unparseable JSON (0 chars)" — a timeout wearing a parser error's clothing.
    #
    # Raising the client-side timeout does not help (it is not the binding limit),
    # and raising the gateway's would change behaviour for every other consumer of
    # the shared proxy. So this runs sequentially by default and retries the
    # requests that still land on the wrong side of the 30s line.
    # SHARED, stated rather than defaulted. This script ingests the org-wide
    # enterprise corpus: documents that belong to no single tenant and are meant to
    # be readable by all of them. That is the same claim `__shared__` already makes
    # on the Qdrant side (backend/ingest.py::SHARED_TENANT), and the constant is
    # imported from the graph builder rather than re-spelled, so a future change to
    # the sentinel cannot leave this script writing a value nothing matches.
    #
    # Passing it explicitly matters even though it is also the builder's default:
    # the default protects against forgetting, while this records that shared was
    # CHOSEN. A reader of this script should not have to infer the tenancy of the
    # corpus from a default three modules away.
    pipelines = [KnowledgeGraphPipeline(source="document",
                                        tenant_id=SHARED_TENANT,
                                        extractor=KnowledgeExtractor(timeout=timeout))
                 for _ in range(max(1, workers))]

    sources: list[KnowledgeSource] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        meta = _frontmatter(text)
        _head, _, body = text.partition("---\n\n")
        sources.append(KnowledgeSource(
            id=meta.get("doc_id") or path.stem,
            type="document",
            text=body,
            metadata={"user_id": "user_1", "document_id": meta.get("doc_id") or path.stem,
                      "title": meta.get("title", ""), "project": meta.get("project", "")},
            # From the document's own frontmatter, so provenance timestamps
            # reflect the document date rather than the hour it was ingested.
            created_at=f"{meta.get('date', '2026-01-05')}T09:00:00+00:00"))

    totals = Counter()
    errors: list[str] = []
    started = time.perf_counter()
    done = 0

    def _run(idx_src):
        idx, src = idx_src
        pipe = pipelines[idx % len(pipelines)]
        result = None
        for attempt in range(retries + 1):
            result = pipe.process(src)
            # A gateway timeout is indistinguishable from a genuinely unparseable
            # response at this layer, so both are retried. A document that really
            # does yield no JSON costs `retries` extra calls and then gives up —
            # cheaper than silently dropping it from the graph.
            if result.ok and not result.error:
                return src, result, attempt
            time.sleep(backoff * (attempt + 1))
        return src, result, retries

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_run, (i, s)): s for i, s in enumerate(sources)}
        for fut in as_completed(futures):
            done += 1
            try:
                src, result, attempts = fut.result()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{futures[fut].id}: {type(e).__name__}: {e}")
                continue
            totals["retried"] += 1 if attempts else 0
            if result is None or not result.ok or result.error:
                errors.append(f"{src.id}: {result.error if result else 'no result'}")
                continue
            st = result.stats
            totals["entities"] += st.entities_after_normalization
            totals["relationships"] += st.relationships_resolved
            totals["nodes_created"] += st.nodes_created
            totals["nodes_merged"] += st.nodes_merged
            totals["rels_created"] += st.relationships_created
            totals["rels_merged"] += st.relationships_merged
            totals["llm_calls"] += st.llm_calls
            if done % progress_every == 0 or done == len(sources):
                elapsed = time.perf_counter() - started
                rate = done / max(0.001, elapsed)
                eta = (len(sources) - done) / max(rate, 1e-6)
                print(f"[graph] {done}/{len(sources)} docs "
                      f"({elapsed/60:.1f} min elapsed, eta {eta/60:.1f} min) "
                      f"+{totals['nodes_created']} nodes, +{totals['rels_created']} edges, "
                      f"{len(errors)} failed", flush=True)

    after_n = svc.run_query("MATCH (n:Entity) RETURN count(n) AS c")[0]["c"]
    after_r = svc.run_query("MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS c")[0]["c"]
    return {"documents": len(sources), **dict(totals),
            "nodes_before": before_n, "nodes_after": after_n,
            "rels_before": before_r, "rels_after": after_r,
            "errors": errors[:20], "error_count": len(errors),
            "took_s": round(time.perf_counter() - started, 1)}


# ── status ────────────────────────────────────────────────────────────────────

def status() -> dict[str, Any]:
    out: dict[str, Any] = {"corpus_files": len(list(CORPUS_DIR.glob("*.md")))}
    try:
        from backend.ingest import get_client
        from config.settings import RAG_COLLECTION
        out["qdrant_points"] = get_client().count(RAG_COLLECTION).count
    except Exception as e:  # noqa: BLE001
        out["qdrant_points"] = f"unavailable: {e}"
    try:
        from backend.knowledge_graph.service import get_graph_service
        svc = get_graph_service()
        out["graph_nodes"] = svc.run_query("MATCH (n:Entity) RETURN count(n) AS c")[0]["c"]
        out["graph_rels"] = svc.run_query(
            "MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS c")[0]["c"]
    except Exception as e:  # noqa: BLE001
        out["graph_nodes"] = f"unavailable: {e}"
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest the enterprise corpus.")
    ap.add_argument("--qdrant", action="store_true", help="embed into corporate_memory")
    ap.add_argument("--graph", action="store_true", help="extract into Neo4j")
    ap.add_argument("--status", action="store_true", help="report current state only")
    ap.add_argument("--dir", type=Path, default=CORPUS_DIR)
    ap.add_argument("--limit", type=int, default=0, help="only the first N documents")
    ap.add_argument("--workers", type=int, default=1,
                    help="graph extraction concurrency (>1 trips the gateway's 30s timeout)")
    ap.add_argument("--retries", type=int, default=3,
                    help="re-attempts for an empty/unparseable extraction")
    ap.add_argument("--ids", type=str, default="",
                    help="comma-separated doc_ids, or @file.json, to reprocess EXACTLY "
                         "(recovery: rebuild edges/provenance for specific documents)")
    ap.add_argument("--resume", action="store_true",
                    help="skip documents whose doc_id already appears in Neo4j source_ids")
    ap.add_argument("--progress-every", type=int, default=10,
                    help="emit a progress line every N documents")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="per-extraction LLM timeout (the 90s default fails under concurrency)")
    args = ap.parse_args(argv)

    if args.status or not (args.qdrant or args.graph):
        for k, v in status().items():
            print(f"{k:<18} {v}")
        return 0

    files = sorted(args.dir.glob("*.md"))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"no documents found in {args.dir}")
        return 1
    print(f"[corpus] {len(files)} document(s) from {args.dir}")

    if args.qdrant:
        r = ingest_qdrant(files)
        print(f"\n[qdrant] {r['files']} files → {r['chunks_written']} chunks in {r['took_s']}s")
        print(f"[qdrant] collection {r['points_before']} → {r['points_after']} points")
        for f in r["failed"][:10]:
            print(f"[qdrant] FAILED {f}")

    if args.graph:
        if args.ids:
            # Targeted recovery. --resume would SKIP these: their doc_ids are still
            # present in other entities' source_ids, so the resume filter reads them
            # as done even though the edges to the deleted entities are gone.
            raw = args.ids.strip()
            if raw.startswith("@"):
                import json as _json
                wanted = set(_json.loads(Path(raw[1:]).read_text()))
            else:
                wanted = {x.strip() for x in raw.split(",") if x.strip()}
            files = [p for p in files
                     if _frontmatter(p.read_text(encoding="utf-8")).get("doc_id") in wanted]
            print(f"[ids] reprocessing {len(files)} of {len(wanted)} requested document(s)")
        elif args.resume:
            done_ids = ingested_doc_ids()
            todo = [p for p in files if _frontmatter(p.read_text(encoding="utf-8"))
                    .get("doc_id") not in done_ids]
            print(f"[resume] {len(done_ids)} document(s) already in the graph; "
                  f"{len(todo)} remaining"
                  + (f" (next: {sorted(done_ids)[-1]} → "
                     f"{_frontmatter(todo[0].read_text(encoding='utf-8')).get('doc_id')})"
                     if todo and done_ids else ""))
            files = todo
        if not files:
            print("[graph] nothing to do — every document is already ingested")
            return 0
        r = ingest_graph(files, workers=args.workers, timeout=args.timeout,
                         retries=args.retries, progress_every=args.progress_every)
        print(f"\n[graph] {r['documents']} documents in {r['took_s']}s "
              f"({r.get('llm_calls', 0)} LLM calls)")
        print(f"[graph] nodes {r['nodes_before']} → {r['nodes_after']} "
              f"(+{r.get('nodes_created', 0)} created, {r.get('nodes_merged', 0)} merged)")
        print(f"[graph] edges {r['rels_before']} → {r['rels_after']} "
              f"(+{r.get('rels_created', 0)} created, {r.get('rels_merged', 0)} merged)")
        if r["error_count"]:
            print(f"[graph] {r['error_count']} error(s):")
            for e in r["errors"][:10]:
                print(f"  {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
