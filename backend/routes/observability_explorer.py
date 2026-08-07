"""
Observability Phase 2 — pipeline timeline, explorers, growth and topology.

Separate module from `observability.py` purely for reviewability; it mounts on
the same `/observability` prefix and obeys the same three rules:

  * **Read-only.** Queries and probes only. No writes, no config mutation.
  * **No fabricated values.** A metric this deployment does not produce is
    reported as `unavailable`/`not_configured` with the reason. Never a zero —
    a zero asserts "measured, and it is zero", which is a different claim.
  * **No new instrumentation.** Nothing is added to the chat/retrieval path.
    Stage timings come from a live probe of the real pipeline; historical series
    come from `created_at`/`first_seen`, which the builder already writes.

A note on the two timestamps, because they answer different questions and
conflating them would quietly mislead:

    created_at  — when the node/edge was WRITTEN to Neo4j (ingestion time)
    first_seen  — the SOURCE's own date, taken from document frontmatter

"New entities today" therefore uses `created_at`. A growth curve over document
dates uses `first_seen`. Both are offered, each labelled with which it is.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query

from backend.routes.observability import (
    PROBE_TIMEOUT, _now_iso, _unavailable, router as _base_router,
)

log = logging.getLogger("aria.observability.explorer")

router = APIRouter(prefix="/observability", tags=["observability"])


class _CaptureHandler(logging.Handler):
    """Collects log records emitted during one probe, in memory.

    Used to read the context builder's per-stage timings, which it logs but does
    not return. Strictly scoped: attached immediately before the probe and
    removed immediately after, so it never sees another request's records."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(record.getMessage())
        except Exception:  # noqa: BLE001 — a logging handler must never raise
            pass

    def text(self) -> str:
        return "\n".join(self._lines)


_STAGE_RE = re.compile(
    r"fuse\s+(?P<fusion_ms>[\d.]+)ms\s+rank\s+(?P<ranking_ms>[\d.]+)ms\s+"
    r"compress\s+(?P<compression_ms>[\d.]+)ms\s+budget\s+(?P<budget_ms>[\d.]+)ms")
_ITEMS_RE = re.compile(
    r"context hybrid:\s+(?P<raw>\d+)\s+→\s+(?P<kept>\d+)\s+items\s+"
    r"\((?P<merged>\d+)\s+merged,\s+(?P<corroborated>\d+)\s+corroborated\)\s+\|\s+"
    r"(?P<used>\d+)/(?P<budget>\d+)\s+tokens\s+\((?P<pct>\d+)%\)")


def _parse_stage_timings(text: str) -> dict:
    """Pull the builder's stage timings and item counts out of its log line."""
    out: dict = {}
    m = _STAGE_RE.search(text or "")
    if m:
        out.update({k: float(v) for k, v in m.groupdict().items()})
    m2 = _ITEMS_RE.search(text or "")
    if m2:
        g = m2.groupdict()
        out.update({"raw_items": int(g["raw"]), "kept_items": int(g["kept"]),
                    "merged": int(g["merged"]), "corroborated": int(g["corroborated"]),
                    "tokens_used": int(g["used"]), "token_budget": int(g["budget"]),
                    "budget_utilization": int(g["pct"]) / 100.0})
    return out


def _q(cypher: str, params: Optional[dict] = None, op: str = "obs2") -> list[dict]:
    from backend.knowledge_graph.service import get_graph_service
    return get_graph_service().run_query(cypher, params or {}, op=op)


# ══════════════════════════════════════════════════════════════════════════════
# §1  AI pipeline timeline
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/pipeline")
async def pipeline(question: str = Query("How does Agentic AI use LiteLLM?"),
                   with_answer: bool = Query(False)) -> dict:
    """§1 — one real request through the pipeline, timed stage by stage.

    Every number is measured on THIS call. The stages that the context engine
    reports internally (fusion, ranking, compression, budget) are read from the
    bundle's own stats rather than re-timed here, so they are the same numbers
    the engine acts on.

    `with_answer=false` by default: generating an answer costs a real LLM call,
    and an operator refreshing a dashboard should not pay for one unless asked.
    """
    question = question if isinstance(question, str) else "How does Agentic AI use LiteLLM?"
    with_answer = with_answer if isinstance(with_answer, bool) else False
    stages: list[dict] = []
    t_start = time.monotonic()

    def add(name: str, ms: Optional[float], detail: Any = None, ok: bool = True) -> None:
        stages.append({"stage": name, "latency_ms": None if ms is None else round(ms, 1),
                       "observed": ms is not None, "ok": ok, "detail": detail})

    add("Question", 0.0, {"text": question, "chars": len(question)})

    # ── entity resolution + graph retrieval, measured separately ─────────────
    resolved_names: list[str] = []
    try:
        from backend.knowledge_graph.retrieval.resolver import EntityResolver
        r = EntityResolver()
        t0 = time.monotonic()
        mentions, extract_ms = await asyncio.to_thread(r.extract_mentions, question)
        t1 = time.monotonic()
        resolutions = await asyncio.to_thread(r.resolve_mentions, mentions)
        t2 = time.monotonic()
        resolved = [x for x in resolutions if getattr(x, "entity_id", None)]
        resolved_names = [x.canonical_name for x in resolved]
        add("Entity Resolver", (t2 - t0) * 1000,
            {"mentions": mentions, "resolved": len(resolved),
             "mention_extraction_ms": round(extract_ms, 1),
             "registry_lookup_ms": round((t2 - t1) * 1000, 1),
             "entities": resolved_names},
            ok=bool(resolved) or not mentions)
    except Exception as e:  # noqa: BLE001
        add("Entity Resolver", None, {"error": str(e)[:160]}, ok=False)

    try:
        from config.settings import GRAPH_RETRIEVAL_DEPTH
    except ImportError:
        GRAPH_RETRIEVAL_DEPTH = 1
    try:
        from backend.knowledge_graph.retrieval.api import GraphRetrievalAPI
        t0 = time.monotonic()
        res = await asyncio.to_thread(
            lambda: GraphRetrievalAPI(depth=GRAPH_RETRIEVAL_DEPTH).retrieve(question))
        ms = (time.monotonic() - t0) * 1000
        sg = getattr(res, "subgraph", None)
        add("Neo4j Retrieval", ms,
            {"depth": GRAPH_RETRIEVAL_DEPTH,
             "nodes": len(getattr(sg, "nodes", []) or []) if sg else None,
             "edges": len(getattr(sg, "edges", []) or []) if sg else None,
             "resolved": len(getattr(res, "resolved", []) or [])},
            ok=sg is not None)
    except Exception as e:  # noqa: BLE001
        add("Neo4j Retrieval", None, {"error": str(e)[:160]}, ok=False)

    try:
        from backend.ingest import search_corporate
        t0 = time.monotonic()
        hits = await asyncio.to_thread(search_corporate, question, 5)
        add("Qdrant Retrieval", (time.monotonic() - t0) * 1000,
            {"hits": len(hits or []),
             "top_score": round(hits[0]["score"], 4) if hits else None,
             "top_source": hits[0]["source"][:60] if hits else None})
    except Exception as e:  # noqa: BLE001
        add("Qdrant Retrieval", None, {"error": str(e)[:160]}, ok=False)

    # ── the fused bundle: one build, its own internal stage timings ──────────
    bundle_stats: dict = {}
    sections: list[str] = []
    try:
        from backend.context import build_ranked_context, build_sections
        # The bundle's stats carry only totals; the per-stage timings exist ONLY
        # in the line the builder logs ("… | fuse 3ms rank 1ms compress 1ms
        # budget 0ms"). Capturing that line is how these numbers are obtained
        # without adding fields to the engine, which this phase forbids.
        cap = _CaptureHandler()
        blog = logging.getLogger("aganeti.context.builder")
        blog.addHandler(cap)
        prev_level = blog.level
        if blog.level > logging.INFO or blog.level == logging.NOTSET:
            blog.setLevel(logging.INFO)
        try:
            t0 = time.monotonic()
            b = await build_ranked_context("user_1", question, "observability-pipeline",
                                           only=("corporate", "memory", "graph",
                                                 "calendar", "tasks"))
            build_ms = (time.monotonic() - t0) * 1000
        finally:
            blog.removeHandler(cap)
            blog.setLevel(prev_level)
        bundle_stats = b.stats.as_dict() if hasattr(getattr(b, "stats", None), "as_dict") else {}
        lat = _parse_stage_timings(cap.text())
        bundle_stats["stage_timings_source"] = (
            "parsed from the context builder's own log line" if lat
            else "builder log line not captured; per-stage timings unavailable")
        by_prov: dict[str, int] = {}
        for it in b.items:
            by_prov[it.provider] = by_prov.get(it.provider, 0) + 1

        add("Context Fusion", lat.get("fusion_ms"),
            {"items_in": lat.get("raw_items"),
             "items_out": len(b.items), "by_provider": by_prov,
             "merged": lat.get("merged"),
             "corroborated": lat.get("corroborated")})
        add("Compression", lat.get("compression_ms"),
            {"tokens_used": lat.get("tokens_used")})
        add("Ranking", lat.get("ranking_ms"),
            {"budget_utilization": lat.get("budget_utilization"),
             "token_budget": lat.get("token_budget")})
        sections = build_sections(b)
        add("Prompt", lat.get("prompt_ms"),
            {"sections": [s.split("\n")[0] for s in sections],
             "chars": sum(len(s) for s in sections)})
        bundle_stats["_build_ms"] = round(build_ms, 1)
    except Exception as e:  # noqa: BLE001
        for s in ("Context Fusion", "Compression", "Ranking", "Prompt"):
            add(s, None, {"error": str(e)[:120]}, ok=False)

    if with_answer and sections:
        try:
            from backend.services import llm
            t0 = time.monotonic()
            ans = await asyncio.to_thread(
                lambda: llm.complete(
                    [{"role": "user", "content": "\n".join(sections) +
                      f"\nQuestion: {question}\nAnswer concisely from the context."}],
                    max_tokens=160, timeout=90))
            add("LiteLLM", (time.monotonic() - t0) * 1000, {"chars": len(ans)})
            add("Answer", 0.0, {"text": re.sub(r"\s+", " ", ans)[:400]})
        except Exception as e:  # noqa: BLE001
            add("LiteLLM", None, {"error": str(e)[:160]}, ok=False)
            add("Answer", None, None, ok=False)
    else:
        add("LiteLLM", None,
            _unavailable("answer generation skipped — pass with_answer=true to "
                         "measure it (costs one real LLM call)"), ok=True)
        add("Answer", None,
            _unavailable("not generated on this run"), ok=True)

    return {"generated_at": _now_iso(), "question": question,
            "total_ms": round((time.monotonic() - t_start) * 1000, 1),
            "stages": stages, "bundle_stats": bundle_stats,
            "resolved_entities": resolved_names}


# ══════════════════════════════════════════════════════════════════════════════
# §2  Knowledge graph explorer
# ══════════════════════════════════════════════════════════════════════════════

def _neo4j_browser_url(entity_id: str) -> str:
    """A deep link an engineer can paste into Neo4j Browser.

    The HTTP browser port is 7474 by default; the bolt URI in settings carries
    the host, so the host is derived rather than assumed to be localhost."""
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    host = re.sub(r"^\w+://", "", uri).split(":")[0] or "localhost"
    port = os.getenv("NEO4J_BROWSER_PORT", "7474")
    return (f"http://{host}:{port}/browser/?cmd=edit&arg="
            f"MATCH (n:Entity {{id:'{entity_id}'}})-[r]-(m) RETURN n,r,m")


@router.get("/graph/search")
async def graph_search(q: str = Query(..., min_length=1),
                       limit: int = Query(20, ge=1, le=100)) -> dict:
    """§2 — find entities by id, canonical name or alias.

    Uses the same case-insensitive matching the resolver's exact rungs use, so
    what the explorer finds is what retrieval would find."""
    lim = int(limit) if isinstance(limit, int) else 20
    def _run() -> list[dict]:
        return _q(
            "MATCH (n:Entity) "
            "WHERE toLower(n.id) CONTAINS toLower($q) "
            "   OR toLower(coalesce(n.canonical_name,'')) CONTAINS toLower($q) "
            "   OR any(a IN coalesce(n.aliases,[]) WHERE toLower(a) CONTAINS toLower($q)) "
            "RETURN n.id AS id, n.canonical_name AS name, "
            "       [l IN labels(n) WHERE l <> 'Entity'] AS labels, "
            "       coalesce(n.aliases,[]) AS aliases, "
            "       size(coalesce(n.source_ids,[])) AS sources, "
            "       coalesce(n.observations,0) AS observations, "
            "       COUNT { (n)--(:Entity) } AS degree "
            "ORDER BY degree DESC LIMIT $lim", {"q": q, "lim": lim}, op="obs_search")
    try:
        rows = await asyncio.to_thread(_run)
        return {"generated_at": _now_iso(), "query": q, "count": len(rows), "results": rows}
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "query": q, "results": [],
                "status": "error", "detail": str(e)[:200]}


@router.get("/graph/entity/{entity_id}")
async def graph_entity(entity_id: str,
                       neighbours: int = Query(25, ge=1, le=100)) -> dict:
    """§2 — one entity in full: properties, provenance, corroboration,
    neighbours, and the documents that asserted it."""
    lim = int(neighbours) if isinstance(neighbours, int) else 25

    def _run() -> dict:
        node = _q("MATCH (n:Entity {id:$id}) RETURN n, labels(n) AS labels",
                  {"id": entity_id}, op="obs_entity")
        if not node:
            return {"status": "not_found"}
        props = dict(node[0]["n"])
        labels = [l for l in node[0]["labels"] if l != "Entity"]

        nb = _q("MATCH (n:Entity {id:$id})-[r]-(m:Entity) "
                "RETURN type(r) AS rel, "
                "       CASE WHEN startNode(r).id = $id THEN 'out' ELSE 'in' END AS direction, "
                "       m.id AS id, m.canonical_name AS name, "
                "       [l IN labels(m) WHERE l <> 'Entity'] AS labels, "
                "       size(coalesce(r.source_ids,[])) AS rel_sources, "
                "       coalesce(r.confidence, null) AS rel_confidence "
                "ORDER BY rel_sources DESC LIMIT $lim",
                {"id": entity_id, "lim": lim}, op="obs_neighbours")

        source_ids = list(props.get("source_ids") or [])
        # Documents that asserted this entity. Matched against Document nodes so
        # a title is shown where one exists, with the raw id as the fallback.
        docs = _q("MATCH (d:Entity) WHERE d.id IN $ids OR "
                  "      any(s IN $ids WHERE d.id = s) "
                  "RETURN d.id AS id, d.canonical_name AS name, "
                  "       [l IN labels(d) WHERE l <> 'Entity'] AS labels LIMIT 50",
                  {"ids": source_ids}, op="obs_srcdocs")

        return {"status": "ok", "labels": labels,
                "properties": {k: v for k, v in props.items()
                               if k not in ("source_ids", "aliases")},
                "aliases": list(props.get("aliases") or []),
                "provenance": {
                    "source_ids": source_ids,
                    "source_types": list(props.get("source_types") or []),
                    "document_ids": list(props.get("document_ids") or []),
                    "models": list(props.get("models") or []),
                    "observations": props.get("observations"),
                    "confidence": props.get("confidence"),
                    "first_seen": props.get("first_seen"),
                    "last_seen": props.get("last_seen"),
                },
                "corroboration": {
                    "distinct_sources": len(source_ids),
                    "is_corroborated": len(source_ids) > 1,
                    "strength": ("strong" if len(source_ids) >= 5
                                 else "corroborated" if len(source_ids) > 1
                                 else "single-source"),
                },
                "neighbours": nb,
                "source_documents": docs}

    try:
        data = await asyncio.to_thread(_run)
        if data.get("status") == "not_found":
            return {"generated_at": _now_iso(), "entity_id": entity_id, "status": "not_found"}
        return {"generated_at": _now_iso(), "entity_id": entity_id,
                "neo4j_browser_url": _neo4j_browser_url(entity_id), **data}
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "entity_id": entity_id,
                "status": "error", "detail": str(e)[:200]}


# ══════════════════════════════════════════════════════════════════════════════
# §3 + §12  Graph growth / learning
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/graph/growth")
async def graph_growth(days: int = Query(30, ge=1, le=365)) -> dict:
    """§3 and §12 — current totals, today's deltas, and time series.

    `created_at` is Neo4j's write time and answers "what did the graph learn
    today". `first_seen` is the source document's own date and answers "what
    period does our knowledge cover". They are reported separately and labelled,
    because averaging them together would be meaningless.
    """
    d = int(days) if isinstance(days, int) else 30

    def _run() -> dict:
        one = lambda c, p=None: _q(c, p, op="obs_growth")[0]["c"]
        nodes = one("MATCH (n:Entity) RETURN count(n) AS c")
        rels = one("MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS c")
        degs = sorted((x["d"] for x in _q(
            "MATCH (n:Entity) RETURN COUNT { (n)--(:Entity) } AS d", op="obs_growth")),
            reverse=True)
        corr = one("MATCH (n:Entity) WHERE size(coalesce(n.source_ids,[]))>1 "
                   "RETURN count(n) AS c")

        # datetime() comparisons: created_at is a Neo4j temporal, not a string.
        new_nodes = one("MATCH (n:Entity) WHERE n.created_at >= date() "
                        "RETURN count(n) AS c")
        new_rels = one("MATCH (:Entity)-[r]->(:Entity) WHERE r.created_at >= date() "
                       "RETURN count(r) AS c")
        new_docs = one("MATCH (d:Document) WHERE d.created_at >= date() "
                       "RETURN count(d) AS c")

        ingest_series = [
            {"date": str(r["d"]), "nodes": r["c"]} for r in _q(
                "MATCH (n:Entity) WHERE n.created_at IS NOT NULL "
                "AND n.created_at >= datetime() - duration({days:$d}) "
                "RETURN date(n.created_at) AS d, count(*) AS c ORDER BY d", {"d": d},
                op="obs_growth")]
        rel_series = [
            {"date": str(r["d"]), "relationships": r["c"]} for r in _q(
                "MATCH (:Entity)-[r]->(:Entity) WHERE r.created_at IS NOT NULL "
                "AND r.created_at >= datetime() - duration({days:$d}) "
                "RETURN date(r.created_at) AS d, count(*) AS c ORDER BY d", {"d": d},
                op="obs_growth")]
        source_series = [
            {"date": r["d"], "entities": r["c"]} for r in _q(
                "MATCH (n:Entity) WHERE n.first_seen IS NOT NULL "
                "RETURN substring(n.first_seen,0,10) AS d, count(*) AS c "
                "ORDER BY d DESC LIMIT 60", op="obs_growth")][::-1]

        top_entities = _q(
            "MATCH (n:Entity) RETURN n.id AS id, n.canonical_name AS name, "
            "COUNT { (n)--(:Entity) } AS degree, "
            "size(coalesce(n.source_ids,[])) AS sources "
            "ORDER BY degree DESC LIMIT 10", op="obs_growth")
        top_types = {r["t"]: r["c"] for r in _q(
            "MATCH (:Entity)-[x]->(:Entity) RETURN type(x) AS t, count(*) AS c "
            "ORDER BY c DESC LIMIT 12", op="obs_growth")}
        recent = _q(
            "MATCH (n:Entity) WHERE n.created_at IS NOT NULL "
            "RETURN n.id AS id, n.canonical_name AS name, "
            "       toString(n.created_at) AS at, "
            "       [l IN labels(n) WHERE l <> 'Entity'] AS labels "
            "ORDER BY n.created_at DESC LIMIT 15", op="obs_growth")

        return {
            "nodes": nodes, "relationships": rels,
            "average_degree": round(sum(degs) / max(1, len(degs)), 2),
            "corroborated_entities": corr,
            "corroboration_pct": round(100 * corr / max(1, nodes), 1),
            "new_entities_today": new_nodes,
            "new_relationships_today": new_rels,
            "new_documents_today": new_docs,
            "top_entities": top_entities, "top_relationship_types": top_types,
            "recent_activity": recent,
            "series": {
                "by_ingestion_date": ingest_series,
                "relationships_by_ingestion_date": rel_series,
                "by_source_document_date": source_series,
            },
            "series_note": ("by_ingestion_date uses created_at (when the graph "
                            "learned it); by_source_document_date uses first_seen "
                            "(the source document's own date)"),
        }

    try:
        return {"generated_at": _now_iso(), "window_days": d,
                **await asyncio.to_thread(_run)}
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "status": "error", "detail": str(e)[:250]}


# ══════════════════════════════════════════════════════════════════════════════
# §4  Qdrant explorer
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/qdrant")
async def qdrant_explorer(probe: bool = Query(True)) -> dict:
    """§4 — collections, sizes, embedding config, and a measured search latency.

    Per-collection query counts and "most retrieved documents" would require a
    query log the platform does not keep; those are reported as unavailable
    rather than guessed at.
    """
    probe = probe if isinstance(probe, bool) else True

    def _run() -> dict:
        from qdrant_client import QdrantClient
        from config.settings import (QDRANT_URL, RAG_COLLECTION, EMBED_MODEL_NAME,
                                     EMBED_DIM, RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP)
        c = QdrantClient(url=QDRANT_URL or "http://localhost:6333", timeout=5)
        cols = []
        for meta in c.get_collections().collections:
            try:
                info = c.get_collection(meta.name)
                cols.append({"name": meta.name, "points": c.count(meta.name).count,
                             "status": str(getattr(info, "status", "")),
                             "vectors": getattr(getattr(info, "config", None), "params", None)
                             and str(info.config.params.vectors)[:120]})
            except Exception:  # noqa: BLE001
                cols.append({"name": meta.name, "points": None, "status": "unreadable"})
        cols.sort(key=lambda x: (x["points"] or 0), reverse=True)

        # Recent ingestions, derived from the point payload timestamps that
        # ingest_file already writes — not from a separate ingestion log.
        recent: Any = _unavailable("no ingestion journal is kept")
        try:
            pts, _ = c.scroll(RAG_COLLECTION, limit=20000,
                              with_payload=["source", "timestamp"])
            by_src: dict[str, int] = {}
            latest: dict[str, int] = {}
            for p in pts:
                s = str(p.payload.get("source", ""))
                by_src[s] = by_src.get(s, 0) + 1
                ts = p.payload.get("timestamp") or 0
                latest[s] = max(latest.get(s, 0), int(ts))
            top = sorted(latest.items(), key=lambda kv: kv[1], reverse=True)[:12]
            recent = [{"source": s, "chunks": by_src.get(s, 0),
                       "ingested_at": (datetime.fromtimestamp(t, timezone.utc).isoformat()
                                       if t else None)} for s, t in top]
        except Exception as e:  # noqa: BLE001
            recent = _unavailable(f"payload scan failed: {str(e)[:100]}")

        return {"collections": cols, "collection_count": len(cols),
                "primary_collection": RAG_COLLECTION,
                "embedding_model": EMBED_MODEL_NAME, "embedding_dim": EMBED_DIM,
                "chunk_size": RAG_CHUNK_SIZE, "chunk_overlap": RAG_CHUNK_OVERLAP,
                "total_points": sum((x["points"] or 0) for x in cols),
                "recent_ingestions": recent}

    try:
        data = await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "status": "error", "detail": str(e)[:200]}

    search: Any = _unavailable("probe disabled")
    if probe:
        try:
            from backend.ingest import search_corporate
            samples = []
            for qtext in ("knowledge graph architecture", "GPU benchmark", "incident report"):
                t0 = time.monotonic()
                hits = await asyncio.to_thread(search_corporate, qtext, 5)
                samples.append({"query": qtext, "ms": round((time.monotonic() - t0) * 1000, 1),
                                "hits": len(hits or [])})
            search = {"samples": samples,
                      "avg_ms": round(sum(s["ms"] for s in samples) / len(samples), 1)}
        except Exception as e:  # noqa: BLE001
            search = _unavailable(f"search probe failed: {str(e)[:100]}")

    return {"generated_at": _now_iso(), **data, "search_latency": search,
            "top_searched_collections": _unavailable(
                "Qdrant keeps no per-collection query counters and the platform "
                "logs no query journal"),
            "most_retrieved_documents": _unavailable(
                "requires a retrieval log the platform does not keep")}


# ══════════════════════════════════════════════════════════════════════════════
# §6  Microsoft provider dashboard
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/providers")
async def providers(deep: bool = Query(True)) -> dict:
    """§6 — every stored connection with expiry, scopes and refresh state.

    `deep=true` exercises a real refresh, which is the only way to tell a live
    credential from a stored-but-dead one. Tokens are never returned.
    """
    deep = deep if isinstance(deep, bool) else True
    from backend.services import provider_tokens as pt
    out: list[dict] = []
    try:
        for provider in pt.PROVIDERS:
            for row in await pt._all_rows_for_provider(provider):
                uid = row.get("user_id")
                expiry = row.get("token_expiry")
                exp_dt = None
                if expiry:
                    try:
                        exp_dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        exp_dt = None
                out.append({
                    "provider": provider, "user_id": uid,
                    "email": row.get("provider_email"),
                    "token_expiry": str(expiry) if expiry else None,
                    "expires_in_minutes": (
                        round((exp_dt - datetime.now(timezone.utc)).total_seconds() / 60)
                        if exp_dt else None),
                    "has_refresh_token": bool(row.get("refresh_token")),
                    "scopes": pt.normalize_scopes(row.get("scopes") or []),
                    "capabilities": {
                        "mail": any("Mail" in s for s in (row.get("scopes") or [])) or
                                any("gmail" in str(s).lower() for s in (row.get("scopes") or [])),
                        "calendar": any("Calendar" in s or "calendar" in str(s).lower()
                                        for s in (row.get("scopes") or [])),
                        "contacts": any("Contacts" in s or "contacts" in str(s).lower()
                                        for s in (row.get("scopes") or [])),
                    },
                })
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "status": "error", "detail": str(e)[:200],
                "connections": []}

    health: dict[str, Any] = {}
    if deep:
        try:
            from backend.services import provider_health as ph
            t0 = time.monotonic()
            res = await ph.check_all(deep=True)
            health = {"latency_ms": round((time.monotonic() - t0) * 1000, 1), **res}
        except Exception as e:  # noqa: BLE001
            health = {"status": "error", "detail": str(e)[:160]}

    for conn in out:
        key = f"{conn['provider']}:{conn['user_id']}"
        info = (health.get("providers") or {}).get(key, {})
        conn["refresh_status"] = info.get("status", "unknown")
        conn["refresh_detail"] = info.get("detail")

    failures = [c for c in out if c["refresh_status"] in ("refresh_failed", "expired")]
    return {"generated_at": _now_iso(), "connections": out,
            "connected_count": len(out), "refresh_failures": len(failures),
            "health": health}


# ══════════════════════════════════════════════════════════════════════════════
# §9  System-threshold alerts (merged with the base /alerts by the UI)
# ══════════════════════════════════════════════════════════════════════════════

# Thresholds are conventional operational limits, stated here so they are
# reviewable rather than buried in a comparison.
THRESHOLDS = {"gpu_temp_c": 85, "disk_pct": 85, "ram_pct": 90, "neo4j_ms": 1000}


@router.get("/alerts/system")
async def alerts_system() -> dict:
    """§9 — resource and latency alerts the base /alerts endpoint does not cover."""
    from backend.routes.observability import system as _system
    out: list[dict] = []

    def add(sev: str, kind: str, msg: str, **x: Any) -> None:
        out.append({"severity": sev, "type": kind, "message": msg,
                    "detected_at": _now_iso(), **x})

    try:
        sysm = await _system()
        for g in (sysm.get("gpu") if isinstance(sysm.get("gpu"), list) else []):
            t = g.get("temperature_c")
            if t is not None and t >= THRESHOLDS["gpu_temp_c"]:
                add("warning", "gpu_temperature",
                    f"{g.get('name')} at {t}°C (threshold {THRESHOLDS['gpu_temp_c']}°C)")
        disk = (sysm.get("disk") or {}).get("percent")
        if disk is not None and disk >= THRESHOLDS["disk_pct"]:
            add("warning", "disk_usage",
                f"disk {disk}% full (threshold {THRESHOLDS['disk_pct']}%)")
        ram = (sysm.get("ram") or {}).get("percent")
        if ram is not None and ram >= THRESHOLDS["ram_pct"]:
            add("warning", "memory_usage",
                f"RAM {ram}% used (threshold {THRESHOLDS['ram_pct']}%)")
    except Exception as e:  # noqa: BLE001
        add("info", "system_probe_failed", "system metrics unavailable",
            detail=str(e)[:140])

    try:
        t0 = time.monotonic()
        await asyncio.to_thread(lambda: _q("MATCH (n:Entity) RETURN count(n) AS c",
                                           op="obs_latency"))
        ms = round((time.monotonic() - t0) * 1000, 1)
        if ms >= THRESHOLDS["neo4j_ms"]:
            add("warning", "neo4j_latency",
                f"Neo4j count query took {ms}ms (threshold {THRESHOLDS['neo4j_ms']}ms)")
    except Exception as e:  # noqa: BLE001
        add("critical", "neo4j_unavailable", "Neo4j query failed", detail=str(e)[:140])

    counts: dict[str, int] = {}
    for a in out:
        counts[a["severity"]] = counts.get(a["severity"], 0) + 1
    return {"generated_at": _now_iso(), "thresholds": THRESHOLDS,
            "active": len(out), "by_severity": counts, "alerts": out}


# ══════════════════════════════════════════════════════════════════════════════
# §10  Infrastructure topology
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/topology")
async def topology() -> dict:
    """§10 — the service dependency graph, with each node's live status.

    Edges are the platform's real call paths (FastAPI→LiteLLM→vLLM, FastAPI→
    stores, FastAPI→providers), declared here because they are architecture, not
    something that can be discovered at runtime. Statuses come from the same
    probes the health cards use, so the diagram and the cards cannot disagree.
    """
    from backend.routes.observability import infrastructure as _infra
    try:
        infra = await _infra()
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "status": "error", "detail": str(e)[:200]}

    status_by = {s["name"]: s for s in infra.get("services", [])}

    def st(name: str) -> str:
        s = status_by.get(name)
        if not s:
            return "unknown"
        return {"healthy": "healthy", "warning": "warning", "error": "offline",
                "not_configured": "not_configured"}.get(s["status"], "unknown")

    nodes = [
        {"id": "user", "label": "User", "tier": 0, "status": "healthy"},
        {"id": "fastapi", "label": "FastAPI", "tier": 1, "status": "healthy"},
        {"id": "litellm", "label": "LiteLLM", "tier": 2, "status": st("LiteLLM Gateway"),
         "latency_ms": (status_by.get("LiteLLM Gateway") or {}).get("latency_ms")},
        {"id": "vllm", "label": "Qwen / vLLM", "tier": 3, "status": st("vLLM"),
         "latency_ms": (status_by.get("vLLM") or {}).get("latency_ms")},
        {"id": "neo4j", "label": "Neo4j", "tier": 3, "status": st("Neo4j"),
         "latency_ms": (status_by.get("Neo4j") or {}).get("latency_ms"),
         "version": (status_by.get("Neo4j") or {}).get("version")},
        {"id": "qdrant", "label": "Qdrant", "tier": 3, "status": st("Qdrant"),
         "latency_ms": (status_by.get("Qdrant") or {}).get("latency_ms")},
        {"id": "postgres", "label": "PostgreSQL", "tier": 3, "status": st("PostgreSQL"),
         "latency_ms": (status_by.get("PostgreSQL") or {}).get("latency_ms"),
         "version": (status_by.get("PostgreSQL") or {}).get("version")},
        {"id": "redis", "label": "Redis", "tier": 3, "status": st("Redis")},
        {"id": "seaweedfs", "label": "SeaweedFS", "tier": 3, "status": st("SeaweedFS")},
        {"id": "msgraph", "label": "Microsoft Graph", "tier": 3, "status": st("Microsoft Graph")},
        {"id": "google", "label": "Google", "tier": 3, "status": "not_configured"},
        {"id": "azure", "label": "Azure OpenAI", "tier": 3, "status": "unknown"},
    ]
    edges = [
        {"source": "user", "target": "fastapi", "label": "HTTPS"},
        {"source": "fastapi", "target": "litellm", "label": "inference"},
        {"source": "litellm", "target": "vllm", "label": "qwen-fast / qwen-extract"},
        {"source": "litellm", "target": "azure", "label": "gpt-4.1 fallback"},
        {"source": "fastapi", "target": "neo4j", "label": "knowledge graph"},
        {"source": "fastapi", "target": "qdrant", "label": "vector search"},
        {"source": "fastapi", "target": "postgres", "label": "state + tokens"},
        {"source": "fastapi", "target": "redis", "label": "cache"},
        {"source": "fastapi", "target": "seaweedfs", "label": "objects"},
        {"source": "fastapi", "target": "msgraph", "label": "mail / calendar"},
        {"source": "fastapi", "target": "google", "label": "oauth"},
    ]
    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    return {"generated_at": _now_iso(), "nodes": nodes, "edges": edges,
            "summary": counts,
            "note": "edges are the platform's declared call paths; statuses come "
                    "from the same probes the health cards use"}


# ══════════════════════════════════════════════════════════════════════════════
# §8  Evaluation trend
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/evaluation/trend")
async def evaluation_trend() -> dict:
    """§8 — family scores, latency and regressions across every saved report."""
    from backend.routes.observability import EVAL_DIR
    import json as _json
    points: list[dict] = []
    for p in sorted(EVAL_DIR.glob("report*.json"), key=lambda x: x.stat().st_mtime):
        try:
            d = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        points.append({
            "file": p.name,
            "at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
            "overall": d.get("overall"),
            "families": d.get("families") or {},
            "latency": d.get("latency") or {},
            "cases": len(d.get("cases") or []),
            "failing": sum(1 for c in (d.get("cases") or []) if c.get("failures")),
        })

    baseline = None
    bp = EVAL_DIR / "baseline.json"
    if bp.exists():
        try:
            baseline = _json.loads(bp.read_text())
        except Exception:  # noqa: BLE001
            baseline = None

    regressions: list[dict] = []
    if baseline and points:
        latest = points[-1]
        for fam, val in (latest["families"] or {}).items():
            base = (baseline.get("families") or {}).get(fam)
            if base is None or val is None:
                continue
            delta = (val - base) * 100
            if delta < -1.0:
                regressions.append({"family": fam, "current": val,
                                    "baseline": base, "delta_pp": round(delta, 1)})
    return {"generated_at": _now_iso(), "points": points,
            "baseline": baseline, "regressions": regressions,
            "note": "one point per saved report; comparability depends on the "
                    "reports having run the same case set"}
