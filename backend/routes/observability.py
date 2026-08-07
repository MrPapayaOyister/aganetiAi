"""
Observability API — read-only telemetry for the admin Observability dashboard.

STRICTLY READ-ONLY. Every handler here queries, probes or reads; none writes,
mutates or configures anything. That is the contract the dashboard depends on:
an operator opening a monitoring page must never be able to change the system
they are inspecting.

Design rules, all load-bearing:

  * **No fabricated numbers.** Where a metric genuinely does not exist in this
    deployment (SeaweedFS is not installed; LiteLLM exposes no per-minute
    counters), the field is reported as `unavailable` or `not_configured` with a
    `detail` explaining why. A monitoring dashboard that invents a plausible
    number is worse than one that admits a gap, because the gap is then
    invisible forever.
  * **Nothing new is instrumented.** Adding counters to the chat path would be a
    backend architecture change. Everything here is derived from what the system
    already records: live probes, Neo4j/Qdrant/Postgres queries, the evaluation
    reports on disk, and the structured log lines the KG and context layers
    already emit.
  * **Every probe is bounded and isolated.** A hung dependency must not hang the
    dashboard, so probes run concurrently with per-probe timeouts and a failure
    is reported as a status rather than raised.

Endpoints (all GET, all under /observability):

    /infrastructure   §1  service health cards
    /llm              §2  gateway + model telemetry
    /graph            §3  knowledge-graph statistics
    /graph/preview    §3  small subgraph for the interactive preview
    /context          §4  hybrid context-engine provider stats
    /storage          §5  SeaweedFS (not configured in this deployment)
    /evaluation       §6  latest baseline + historical reports
    /activity         §7  recent pipeline events from the structured logs
    /trace/{sid}      §8  per-conversation execution trace
    /system           §9  GPU / CPU / RAM / disk / network
    /alerts          §10  active warnings
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Query

log = logging.getLogger("aria.observability")

router = APIRouter(prefix="/observability", tags=["observability"])

ROOT = Path(__file__).resolve().parents[2]
PROBE_TIMEOUT = 2.5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unavailable(reason: str, **extra: Any) -> dict:
    """The canonical shape for "this metric does not exist here"."""
    return {"status": "unavailable", "detail": reason, **extra}


# ══════════════════════════════════════════════════════════════════════════════
# §1  Infrastructure health
# ══════════════════════════════════════════════════════════════════════════════

async def _probe_http(name: str, url: str, *, expect_below: int = 500) -> dict:
    started = time.monotonic()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
                r = await c.get(url)
        ms = round((time.monotonic() - started) * 1000, 1)
        ok = r.status_code < expect_below
        return {"name": name, "status": "healthy" if ok else "error",
                "latency_ms": ms, "connection": "connected",
                "http_status": r.status_code, "last_check": _now_iso()}
    except (asyncio.TimeoutError, TimeoutError):
        return {"name": name, "status": "error", "latency_ms": None,
                "connection": "timeout", "detail": f"no response within {PROBE_TIMEOUT}s",
                "last_check": _now_iso()}
    except Exception as e:  # noqa: BLE001
        return {"name": name, "status": "error", "latency_ms": None,
                "connection": "unreachable", "detail": str(e)[:160],
                "last_check": _now_iso()}


def _probe_neo4j() -> dict:
    started = time.monotonic()
    try:
        from backend.knowledge_graph.service import get_graph_service
        svc = get_graph_service()
        rows = svc.run_query("CALL dbms.components() YIELD name, versions, edition "
                             "RETURN name, versions[0] AS version, edition", op="obs_version")
        ms = round((time.monotonic() - started) * 1000, 1)
        v = rows[0] if rows else {}
        return {"name": "Neo4j", "status": "healthy", "latency_ms": ms,
                "connection": "connected", "version": v.get("version"),
                "edition": v.get("edition"), "last_check": _now_iso()}
    except Exception as e:  # noqa: BLE001
        return {"name": "Neo4j", "status": "error", "latency_ms": None,
                "connection": "unreachable", "detail": str(e)[:160],
                "last_check": _now_iso()}


async def _probe_postgres() -> dict:
    started = time.monotonic()
    try:
        from sqlalchemy import text
        from backend.db.base import engine
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with engine.connect() as c:
                v = (await c.execute(text("SHOW server_version"))).scalar()
        ms = round((time.monotonic() - started) * 1000, 1)
        return {"name": "PostgreSQL", "status": "healthy", "latency_ms": ms,
                "connection": "connected", "version": str(v), "last_check": _now_iso()}
    except Exception as e:  # noqa: BLE001
        return {"name": "PostgreSQL", "status": "error", "latency_ms": None,
                "connection": "unreachable", "detail": str(e)[:160],
                "last_check": _now_iso()}


def _probe_qdrant() -> dict:
    started = time.monotonic()
    try:
        from qdrant_client import QdrantClient
        from config.settings import QDRANT_URL, RAG_COLLECTION
        c = QdrantClient(url=QDRANT_URL or "http://localhost:6333", timeout=PROBE_TIMEOUT)
        cols = [x.name for x in c.get_collections().collections]
        pts = c.count(RAG_COLLECTION).count if RAG_COLLECTION in cols else 0
        ms = round((time.monotonic() - started) * 1000, 1)
        return {"name": "Qdrant", "status": "healthy", "latency_ms": ms,
                "connection": "connected", "collections": len(cols),
                "points": pts, "last_check": _now_iso()}
    except Exception as e:  # noqa: BLE001
        return {"name": "Qdrant", "status": "error", "latency_ms": None,
                "connection": "unreachable", "detail": str(e)[:160],
                "last_check": _now_iso()}


def _probe_redis() -> dict:
    started = time.monotonic()
    try:
        import redis  # type: ignore
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = redis.Redis.from_url(url, socket_timeout=PROBE_TIMEOUT)
        r.ping()
        info = r.info(section="server")
        ms = round((time.monotonic() - started) * 1000, 1)
        return {"name": "Redis", "status": "healthy", "latency_ms": ms,
                "connection": "connected", "version": info.get("redis_version"),
                "last_check": _now_iso()}
    except ImportError:
        return {"name": "Redis", "status": "unknown", "latency_ms": None,
                "connection": "no client library",
                "detail": "redis-py not installed; cannot probe",
                "last_check": _now_iso()}
    except Exception as e:  # noqa: BLE001
        return {"name": "Redis", "status": "error", "latency_ms": None,
                "connection": "unreachable", "detail": str(e)[:160],
                "last_check": _now_iso()}


async def _probe_rabbitmq() -> dict:
    """RabbitMQ is not part of this deployment. Reported, not invented."""
    url = os.getenv("RABBITMQ_MANAGEMENT_URL", "")
    if not url:
        return {"name": "RabbitMQ", "status": "not_configured", "latency_ms": None,
                "connection": "not deployed",
                "detail": "no RABBITMQ_MANAGEMENT_URL set and no broker container running",
                "last_check": _now_iso()}
    return await _probe_http("RabbitMQ", f"{url.rstrip('/')}/api/overview")


async def _probe_provider(kind: str) -> dict:
    """Microsoft Graph / Mail / Calendar, via the real provider-health module."""
    try:
        from backend.services import provider_health as ph
        started = time.monotonic()
        # deep=True actually exercises a refresh, which is the only way to tell a
        # live credential from a stored-but-dead one.
        res = await ph.check_all(deep=True)
        ms = round((time.monotonic() - started) * 1000, 1)
        ms_states = [v for k, v in res["providers"].items() if k.startswith("microsoft")]
        state = ms_states[0] if ms_states else None
        if state is None:
            return {"name": kind, "status": "not_configured", "latency_ms": ms,
                    "connection": "no Microsoft connection stored", "last_check": _now_iso()}
        healthy = state["status"] == "ok"
        return {"name": kind, "status": "healthy" if healthy else "warning",
                "latency_ms": ms, "connection": "connected" if healthy else state["status"],
                "detail": state.get("detail"), "account": state.get("email"),
                "last_check": _now_iso()}
    except Exception as e:  # noqa: BLE001
        return {"name": kind, "status": "error", "latency_ms": None,
                "connection": "probe failed", "detail": str(e)[:160],
                "last_check": _now_iso()}


def _seaweed_base() -> str:
    return os.getenv("SEAWEEDFS_MASTER_URL", "").rstrip("/")


@router.get("/infrastructure")
async def infrastructure() -> dict:
    """§1 — one health card per dependency. Probes run concurrently."""
    from config.settings import LLM_BASE_URL
    gw = LLM_BASE_URL.rstrip("/")
    gw = gw[:-3].rstrip("/") if gw.endswith("/v1") else gw

    vllm_url = os.getenv("VLLM_FAST_URL", "http://localhost:9002")
    sw = _seaweed_base()

    http_probes = [
        _probe_http("LiteLLM Gateway", f"{gw}/health/liveliness"),
        _probe_http("vLLM", f"{vllm_url.rstrip('/')}/health"),
        _probe_rabbitmq(),
        _probe_provider("Microsoft Graph"),
    ]
    if sw:
        http_probes.append(_probe_http("SeaweedFS", f"{sw}/cluster/status"))

    results = await asyncio.gather(*http_probes, return_exceptions=True)
    cards: list[dict] = [r for r in results if isinstance(r, dict)]

    # Synchronous drivers run in threads so one slow socket cannot block the loop.
    sync = await asyncio.gather(
        asyncio.to_thread(_probe_neo4j),
        asyncio.to_thread(_probe_qdrant),
        asyncio.to_thread(_probe_redis),
        _probe_postgres(),
        return_exceptions=True)
    cards += [r for r in sync if isinstance(r, dict)]

    if not sw:
        cards.append({"name": "SeaweedFS", "status": "not_configured", "latency_ms": None,
                      "connection": "not deployed",
                      "detail": "no SEAWEEDFS_MASTER_URL set; storage tier not yet installed",
                      "last_check": _now_iso()})

    # Mail and Calendar ride the same Microsoft credential, so their health is the
    # provider's health — reported separately because they fail independently at
    # the API level and an operator looks for them by name.
    ms = next((c for c in cards if c["name"] == "Microsoft Graph"), None)
    for sub in ("Mail", "Calendar"):
        cards.append({**(ms or {}), "name": sub} if ms else
                     {"name": sub, "status": "unknown", "last_check": _now_iso()})

    order = ["LiteLLM Gateway", "vLLM", "Neo4j", "PostgreSQL", "Qdrant", "Redis",
             "RabbitMQ", "SeaweedFS", "Microsoft Graph", "Mail", "Calendar"]
    cards.sort(key=lambda c: order.index(c["name"]) if c["name"] in order else 99)

    counts: dict[str, int] = {}
    for c in cards:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return {"generated_at": _now_iso(), "services": cards, "summary": counts}


# ══════════════════════════════════════════════════════════════════════════════
# §2  LLM monitoring
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/llm")
async def llm_metrics() -> dict:
    """§2 — models in use plus whatever the gateway genuinely exposes.

    Request rate, p95, queue depth and stream counts are NOT tracked by this
    deployment: LiteLLM is running without a metrics backend and the app adds no
    counters of its own. Instrumenting the chat path to produce them would be a
    backend change, which this phase forbids — so they are reported as
    unavailable rather than estimated.
    """
    from config.settings import LLM_BASE_URL, LLM_MODEL
    try:
        from config.settings import KG_EXTRACT_MODEL, KG_EXTRACT_TIMEOUT
    except ImportError:
        KG_EXTRACT_MODEL, KG_EXTRACT_TIMEOUT = None, None

    gw = LLM_BASE_URL.rstrip("/")
    gw_root = gw[:-3].rstrip("/") if gw.endswith("/v1") else gw

    models: list[str] = []
    gw_status = "unknown"
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
                r = await c.get(f"{gw}/models",
                                headers={"Authorization": f"Bearer {os.getenv('LLM_API_KEY','')}"})
        if r.status_code == 200:
            models = [m["id"] for m in r.json().get("data", [])]
            gw_status = "healthy"
        else:
            gw_status = f"http_{r.status_code}"
    except Exception as e:  # noqa: BLE001
        gw_status = f"unreachable: {str(e)[:80]}"

    # Prometheus metrics are optional in LiteLLM; absent unless explicitly enabled.
    prom: dict[str, Any] = _unavailable(
        "LiteLLM is running without its Prometheus exporter enabled, so it "
        "publishes no request/latency counters")
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
                m = await c.get(f"{gw_root}/metrics")
        if m.status_code == 200 and "litellm" in m.text:
            prom = {"status": "available", "raw_bytes": len(m.text)}
    except Exception:  # noqa: BLE001
        pass

    unavailable_note = ("not instrumented in this deployment — adding counters to "
                        "the chat path would be a backend change")
    return {
        "generated_at": _now_iso(),
        "gateway": {"base_url": LLM_BASE_URL, "status": gw_status, "routes": models},
        "chat_model": LLM_MODEL,
        "extraction_model": KG_EXTRACT_MODEL,
        "extraction_timeout_s": KG_EXTRACT_TIMEOUT,
        "prometheus": prom,
        "requests_per_min": _unavailable(unavailable_note),
        "avg_latency_ms": _unavailable(unavailable_note),
        "p95_latency_ms": _unavailable(unavailable_note),
        "token_throughput": _unavailable(unavailable_note),
        "active_streams": _unavailable(unavailable_note),
        "queue_depth": _unavailable(unavailable_note),
        "timeout_count": _unavailable(unavailable_note),
        "error_count": _unavailable(unavailable_note),
        "retry_count": _unavailable(unavailable_note),
    }


# ══════════════════════════════════════════════════════════════════════════════
# §3  Knowledge graph
# ══════════════════════════════════════════════════════════════════════════════

def _graph_stats() -> dict:
    from backend.knowledge_graph.service import get_graph_service
    s = get_graph_service()
    q1 = lambda c: s.run_query(c, op="obs_graph")[0]["c"]
    nodes = q1("MATCH (n:Entity) RETURN count(n) AS c")
    rels = q1("MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS c")
    degs = sorted((d["d"] for d in s.run_query(
        "MATCH (n:Entity) RETURN COUNT { (n)--(:Entity) } AS d", op="obs_deg")), reverse=True)
    labels = {r["l"]: r["c"] for r in s.run_query(
        "MATCH (n:Entity) UNWIND labels(n) AS l WITH l WHERE l <> 'Entity' "
        "RETURN l, count(*) AS c ORDER BY c DESC", op="obs_labels")}
    types = {r["t"]: r["c"] for r in s.run_query(
        "MATCH (:Entity)-[x]->(:Entity) RETURN type(x) AS t, count(*) AS c "
        "ORDER BY c DESC", op="obs_types")}
    corr = q1("MATCH (n:Entity) WHERE size(coalesce(n.source_ids,[]))>1 RETURN count(n) AS c")
    strong = q1("MATCH (n:Entity) WHERE size(coalesce(n.source_ids,[]))>=5 RETURN count(n) AS c")
    aliases = q1("MATCH (n:Entity) RETURN sum(size(coalesce(n.aliases,[]))) AS c")
    hubs = [{"id": r["id"], "degree": r["d"]} for r in s.run_query(
        "MATCH (n:Entity) RETURN n.id AS id, COUNT { (n)--(:Entity) } AS d "
        "ORDER BY d DESC LIMIT 10", op="obs_hubs")]
    return {"nodes": nodes, "relationships": rels,
            "average_degree": round(sum(degs) / max(1, len(degs)), 2),
            "median_degree": degs[len(degs) // 2] if degs else 0,
            "max_degree": degs[0] if degs else 0,
            "labels": labels, "relationship_types": types,
            "label_count": len(labels), "relationship_type_count": len(types),
            "corroborated_entities": corr, "strongly_corroborated": strong,
            "alias_registry_size": aliases, "top_connected": hubs}


@router.get("/graph")
async def graph() -> dict:
    """§3 — graph statistics plus the retrieval knobs currently in force."""
    try:
        stats = await asyncio.to_thread(_graph_stats)
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "status": "error", "detail": str(e)[:200]}

    try:
        from config.settings import GRAPH_RETRIEVAL_DEPTH
    except ImportError:
        GRAPH_RETRIEVAL_DEPTH = 1
    try:
        from backend.knowledge_graph.retrieval.registry import CanonicalEntityRegistry
        fuzzy = CanonicalEntityRegistry.__init__.__defaults__[-1]
    except Exception:  # noqa: BLE001
        fuzzy = None

    # A live probe rather than a stored score: run one known query and time it.
    latency_ms, resolved = None, None
    try:
        from backend.knowledge_graph.retrieval.api import GraphRetrievalAPI
        t0 = time.monotonic()
        res = await asyncio.to_thread(
            lambda: GraphRetrievalAPI(depth=GRAPH_RETRIEVAL_DEPTH).retrieve(
                "How does Agentic AI use LiteLLM?"))
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        resolved = len(getattr(res, "resolved", []) or [])
    except Exception as e:  # noqa: BLE001
        log.debug("observability: graph latency probe failed: %s", e)

    return {"generated_at": _now_iso(), "status": "ok", **stats,
            "graph_depth": GRAPH_RETRIEVAL_DEPTH,
            "fuzzy_threshold": fuzzy,
            "retrieval_latency_ms": latency_ms,
            "resolution_probe": (
                {"query": "How does Agentic AI use LiteLLM?", "entities_resolved": resolved}
                if resolved is not None else
                _unavailable("live resolution probe failed; see server logs"))}


@router.get("/graph/preview")
async def graph_preview(limit: int = Query(40, ge=5, le=150)) -> dict:
    """§3 — a small connected subgraph for the interactive preview.

    Seeded from the highest-degree nodes so the preview shows the graph's real
    structure rather than an arbitrary corner of it."""
    lim = int(limit) if isinstance(limit, int) else 40

    def _run() -> dict:
        from backend.knowledge_graph.service import get_graph_service
        s = get_graph_service()
        rows = s.run_query(
            "MATCH (n:Entity) WITH n, COUNT { (n)--(:Entity) } AS d "
            "ORDER BY d DESC LIMIT $lim "
            "MATCH (n)-[r]-(m:Entity) "
            "RETURN DISTINCT n.id AS src, labels(n) AS srcl, type(r) AS rel, "
            "       m.id AS dst, labels(m) AS dstl LIMIT $edges",
            {"lim": max(5, lim // 4), "edges": lim * 3}, op="obs_preview")
        nodes: dict[str, dict] = {}
        edges = []
        for r in rows:
            for nid, labs in ((r["src"], r["srcl"]), (r["dst"], r["dstl"])):
                nodes.setdefault(nid, {"id": nid,
                                       "label": next((l for l in labs if l != "Entity"), "Entity")})
            edges.append({"source": r["src"], "target": r["dst"], "type": r["rel"]})
        return {"nodes": list(nodes.values()), "edges": edges}
    try:
        return {"generated_at": _now_iso(), **await asyncio.to_thread(_run)}
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "nodes": [], "edges": [],
                "status": "error", "detail": str(e)[:200]}


# ══════════════════════════════════════════════════════════════════════════════
# §4  Hybrid context engine
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/context")
async def context(probe: bool = Query(True, description="run one live context build")) -> dict:
    """§4 — provider registry plus a live end-to-end context build.

    The measurement is a real `build_ranked_context` call against a fixed probe
    question, so the numbers are what the chat path would actually experience.
    It is a read: building context queries providers and writes nothing.
    """
    providers: list[dict] = []
    try:
        from backend.context.providers.registry import default_providers  # type: ignore
        for p in default_providers():
            providers.append({"name": getattr(p, "name", type(p).__name__),
                              "enabled": bool(getattr(p, "enabled", True))})
    except Exception:  # noqa: BLE001
        for name in ("corporate", "memory", "graph", "calendar", "tasks", "history", "sql"):
            providers.append({"name": name, "enabled": None})

    if not probe:
        return {"generated_at": _now_iso(), "providers": providers,
                "probe": _unavailable("probe disabled by query parameter")}

    try:
        from backend.context import build_ranked_context
        t0 = time.monotonic()
        b = await build_ranked_context(
            "user_1", "How does Agentic AI use LiteLLM?", "observability-probe",
            only=("corporate", "memory", "graph", "calendar", "tasks"))
        total = round((time.monotonic() - t0) * 1000, 1)
        stats = b.stats.as_dict() if hasattr(getattr(b, "stats", None), "as_dict") else {}
        by_provider: dict[str, int] = {}
        for item in b.items:
            by_provider[item.provider] = by_provider.get(item.provider, 0) + 1
        return {"generated_at": _now_iso(), "providers": providers,
                "probe": {"question": "How does Agentic AI use LiteLLM?",
                          "total_ms": total,
                          "context_items": len(b.items),
                          "items_by_provider": by_provider,
                          "contributing_providers": sorted(by_provider),
                          **stats}}
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "providers": providers,
                "probe": {"status": "error", "detail": str(e)[:200]}}


# ══════════════════════════════════════════════════════════════════════════════
# §5  SeaweedFS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/storage")
async def storage() -> dict:
    """§5 — SeaweedFS. Not installed in this deployment.

    The endpoint is live and will populate itself the moment SEAWEEDFS_MASTER_URL
    points at a cluster; until then it reports `not_configured` rather than zeros,
    because a dashboard full of zeroes reads as "healthy and empty" when the truth
    is "absent".
    """
    base = _seaweed_base()
    if not base:
        return {"generated_at": _now_iso(), "status": "not_configured",
                "detail": "SEAWEEDFS_MASTER_URL is not set; the storage tier is not "
                          "deployed yet. This panel activates automatically once it is.",
                "master": None, "volume_servers": None, "filer": None,
                "collections": None, "buckets": None, "objects": None,
                "total_storage": None, "used_storage": None, "free_storage": None,
                "upload_rate": None, "download_rate": None, "replication": None}

    async def _get(path: str) -> Optional[dict]:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
                    r = await c.get(f"{base}{path}")
            return r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            return None

    cluster, vols = await asyncio.gather(_get("/cluster/status"), _get("/dir/status"))
    if cluster is None and vols is None:
        return {"generated_at": _now_iso(), "status": "error",
                "detail": f"SEAWEEDFS_MASTER_URL is set to {base} but the master did not respond"}

    topo = (vols or {}).get("Topology", {}) or {}
    return {"generated_at": _now_iso(), "status": "healthy",
            "master": {"url": base, "leader": (cluster or {}).get("Leader"),
                       "peers": (cluster or {}).get("Peers")},
            "volume_servers": topo.get("DataCenters"),
            "total_storage": topo.get("Max"), "used_storage": topo.get("Free") and
            (topo.get("Max", 0) - topo.get("Free", 0)),
            "free_storage": topo.get("Free"),
            "filer": _unavailable("filer address not configured"),
            "collections": _unavailable("requires a filer query"),
            "buckets": _unavailable("requires a filer query"),
            "objects": _unavailable("requires a filer query"),
            "upload_rate": _unavailable("SeaweedFS master exposes no rate counters"),
            "download_rate": _unavailable("SeaweedFS master exposes no rate counters"),
            "replication": topo.get("layouts")}


# ══════════════════════════════════════════════════════════════════════════════
# §6  Evaluation
# ══════════════════════════════════════════════════════════════════════════════

EVAL_DIR = ROOT / "reports" / "evals"


@router.get("/evaluation")
async def evaluation() -> dict:
    """§6 — the saved regression baseline plus every report on disk."""
    def _read(p: Path) -> Optional[dict]:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    baseline = _read(EVAL_DIR / "baseline.json") if (EVAL_DIR / "baseline.json").exists() else None

    reports = []
    for p in sorted(EVAL_DIR.glob("report*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        d = _read(p)
        if not d:
            continue
        reports.append({
            "file": p.name,
            "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
            "overall": d.get("overall"),
            "families": d.get("families"),
            "cases": len(d.get("cases") or []),
            "failing": sum(1 for c in (d.get("cases") or []) if c.get("failures")),
            "took_s": d.get("took_s"),
            "html": p.with_suffix(".html").name if p.with_suffix(".html").exists() else None,
        })

    latest = reports[0] if reports else None
    return {"generated_at": _now_iso(),
            "baseline": baseline or _unavailable("no baseline.json saved yet"),
            "latest": latest or _unavailable("no evaluation reports on disk"),
            "history": reports}


@router.get("/evaluation/report/{filename}")
async def evaluation_report(filename: str) -> dict:
    """§6 — one historical report. Filename is constrained to the reports dir."""
    safe = Path(filename).name                      # strip any path traversal
    p = EVAL_DIR / safe
    if not p.exists() or p.suffix != ".json":
        return {"status": "not_found", "file": safe}
    try:
        return {"status": "ok", "file": safe, "report": json.loads(p.read_text())}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "file": safe, "detail": str(e)[:200]}


# ══════════════════════════════════════════════════════════════════════════════
# §7  Live activity
# ══════════════════════════════════════════════════════════════════════════════

# The KG and context layers already emit structured, parseable lines. Reading
# them is what makes a live feed possible WITHOUT adding an event bus.
_ACTIVITY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("entity_resolution", re.compile(r"kg resolver: (\d+) mention\(s\) → (\d+) resolved \((\d+)ms extract, (\d+)ms resolve\)")),
    ("graph_retrieval", re.compile(r"kg api: '(?P<q>[^']*)' → (?P<res>\d+) resolved, (?P<rel>\d+) related, (?P<edges>\d+) edge\(s\), (?P<ms>\d+)ms")),
    ("graph_retrieval", re.compile(r"kg retriever: (?P<seeds>\d+) seed\(s\) → (?P<nodes>\d+) node\(s\), (?P<edges>\d+) edge\(s\)")),
    ("context_fusion", re.compile(r"context hybrid: (?P<raw>\d+) → (?P<kept>\d+) items \((?P<merged>\d+) merged, (?P<corr>\d+) corroborated\) \| (?P<used>\d+)/(?P<budget>\d+) tokens")),
    ("llm_error", re.compile(r"llm complete failed \[(?P<kind>\w+)\]")),
    ("extraction", re.compile(r"kg batch build: (?P<nodes>\d+) nodes .* (?P<ms>\d+)ms")),
]

_LOG_CANDIDATES = ["backend.log", "logs/backend.log", "logs/graph_recovery.log",
                   "logs/evals_final.log"]


@router.get("/activity")
async def activity(limit: int = Query(100, ge=1, le=500)) -> dict:
    """§7 — recent pipeline events, parsed from the structured logs.

    Derived rather than instrumented: the resolver, retriever and context builder
    already log one line per stage with its timing, so a feed can be assembled
    without touching the request path. The trade-off is honest and worth stating —
    this reflects what has been logged, so it is near-real-time rather than an
    event stream, and it is bounded by the log's retention.
    """
    lim = int(limit) if isinstance(limit, int) else 100
    events: list[dict] = []
    for rel in _LOG_CANDIDATES:
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            # Read only the tail; these logs can be large.
            with p.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 512_000))
                chunk = fh.read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            continue
        for line in chunk.splitlines():
            ts = None
            m = re.match(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
            if m:
                ts = m.group(1)
            for kind, pat in _ACTIVITY_PATTERNS:
                hit = pat.search(line)
                if not hit:
                    continue
                g = hit.groupdict()
                latency = g.get("ms") or (hit.group(3) if kind == "entity_resolution" else None)
                events.append({"type": kind, "timestamp": ts, "source": rel,
                               "latency_ms": int(latency) if latency and str(latency).isdigit() else None,
                               "detail": {k: v for k, v in g.items() if k != "ms"} or
                                         {"raw": line.strip()[-120:]}})
                break
    events = events[-lim:]
    events.reverse()
    return {"generated_at": _now_iso(), "count": len(events), "events": events,
            "note": "parsed from structured application logs; no request-path "
                    "instrumentation was added for this dashboard"}


# ══════════════════════════════════════════════════════════════════════════════
# §8  Agent trace
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/sessions")
async def sessions(limit: int = Query(25, ge=1, le=200)) -> dict:
    """§8 — conversations available to trace."""
    try:
        from sqlalchemy import text
        from backend.db.base import engine
        async with engine.connect() as c:
            rows = (await c.execute(text(
                "SELECT session_id, user_id, max(created_at) AS last_at, count(*) AS messages "
                "FROM chat_messages GROUP BY session_id, user_id "
                "ORDER BY last_at DESC LIMIT :lim"), {"lim": limit})).fetchall()
        return {"generated_at": _now_iso(),
                "sessions": [{"session_id": r[0], "user_id": r[1],
                              "last_at": r[2].isoformat() if r[2] else None,
                              "messages": r[3]} for r in rows]}
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "sessions": [],
                "status": "error", "detail": str(e)[:200]}


@router.get("/trace/{session_id}")
async def trace(session_id: str) -> dict:
    """§8 — the execution pipeline for one conversation.

    IMPORTANT, and stated rather than hidden: per-stage latency is NOT persisted
    per conversation. The chat path logs its stage timings but does not store
    them against a session id, and recording them would be a backend change this
    phase forbids. So each stage reports whether it is *observable* for this
    session, and the timings come from a live probe of the same pipeline — they
    are representative of the pipeline, not a replay of that specific turn.
    """
    stages = ["User", "Planner", "Memory", "Qdrant", "Neo4j", "Calendar",
              "Fusion", "Prompt", "LiteLLM", "Final Answer"]
    messages: list[dict] = []
    try:
        from sqlalchemy import text
        from backend.db.base import engine
        async with engine.connect() as c:
            rows = (await c.execute(text(
                "SELECT role, left(content, 400) AS content, created_at FROM chat_messages "
                "WHERE session_id = :s ORDER BY created_at LIMIT 50"),
                {"s": session_id})).fetchall()
        messages = [{"role": r[0], "content": r[1],
                     "at": r[2].isoformat() if r[2] else None} for r in rows]
    except Exception as e:  # noqa: BLE001
        return {"generated_at": _now_iso(), "session_id": session_id,
                "status": "error", "detail": str(e)[:200], "stages": []}

    probe: dict[str, Any] = {}
    try:
        from backend.context import build_ranked_context
        t0 = time.monotonic()
        b = await build_ranked_context("user_1", "How does Agentic AI use LiteLLM?",
                                       f"trace-probe-{session_id}",
                                       only=("corporate", "memory", "graph",
                                             "calendar", "tasks"))
        probe["context_total_ms"] = round((time.monotonic() - t0) * 1000, 1)
        probe.update(b.stats.as_dict() if hasattr(getattr(b, "stats", None), "as_dict") else {})
        probe["contributing"] = sorted({i.provider for i in b.items})
    except Exception as e:  # noqa: BLE001
        probe = {"status": "error", "detail": str(e)[:160]}

    lat = probe.get("latency", {}) if isinstance(probe.get("latency"), dict) else {}
    mapping = {
        "User": None, "Planner": None,
        "Memory": lat.get("memory_ms"), "Qdrant": lat.get("corporate_ms"),
        "Neo4j": lat.get("graph_ms"), "Calendar": lat.get("calendar_ms"),
        "Fusion": lat.get("fusion_ms"), "Prompt": lat.get("prompt_ms"),
        "LiteLLM": lat.get("llm_ms"), "Final Answer": None,
    }
    return {"generated_at": _now_iso(), "session_id": session_id,
            "messages": messages,
            "stages": [{"stage": s, "latency_ms": mapping.get(s),
                        "observed": mapping.get(s) is not None} for s in stages],
            "probe": probe,
            "note": "stage timings come from a live pipeline probe; per-turn "
                    "timings are not persisted per session in this deployment"}


# ══════════════════════════════════════════════════════════════════════════════
# §9  System metrics
# ══════════════════════════════════════════════════════════════════════════════

def _gpu() -> Any:
    if not shutil.which("nvidia-smi"):
        return _unavailable("nvidia-smi not present on this host")
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        gpus = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            def _num(v: str) -> Optional[float]:
                # DGX Spark reports [N/A] for VRAM because host and GPU share one
                # unified pool — reported as null rather than 0, which would read
                # as "no memory in use".
                try:
                    return float(v)
                except ValueError:
                    return None
            gpus.append({"name": parts[0], "utilization_pct": _num(parts[1]),
                         "temperature_c": _num(parts[2]),
                         "vram_used_mb": _num(parts[3]), "vram_total_mb": _num(parts[4]),
                         "vram_note": (None if _num(parts[4]) is not None else
                                       "unified memory architecture — VRAM is not "
                                       "reported separately from system RAM")})
        return gpus or _unavailable("nvidia-smi returned no GPUs")
    except Exception as e:  # noqa: BLE001
        return _unavailable(f"nvidia-smi failed: {str(e)[:120]}")


@router.get("/system")
async def system() -> dict:
    """§9 — host metrics. psutil where available, /proc as the fallback."""
    data: dict[str, Any] = {"generated_at": _now_iso(), "gpu": await asyncio.to_thread(_gpu)}
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(str(ROOT))
        net = psutil.net_io_counters()
        data.update({
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "cpu_count": psutil.cpu_count(),
            "load_avg": list(os.getloadavg()),
            "ram": {"total_mb": round(vm.total / 1e6), "used_mb": round(vm.used / 1e6),
                    "percent": vm.percent},
            "disk": {"total_gb": round(du.total / 1e9, 1), "used_gb": round(du.used / 1e9, 1),
                     "percent": du.percent},
            "network": {"bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv,
                        "note": "cumulative counters since boot; rate requires two samples"},
        })
    except ImportError:
        try:
            load = list(os.getloadavg())
            mem = {}
            for line in Path("/proc/meminfo").read_text().splitlines()[:3]:
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0]) // 1024
            du = shutil.disk_usage(str(ROOT))
            data.update({
                "cpu_percent": _unavailable("psutil not installed"),
                "load_avg": load,
                "ram": {"total_mb": mem.get("MemTotal"),
                        "available_mb": mem.get("MemAvailable")},
                "disk": {"total_gb": round(du.total / 1e9, 1),
                         "used_gb": round(du.used / 1e9, 1)},
                "network": _unavailable("psutil not installed"),
            })
        except Exception as e:  # noqa: BLE001
            data["error"] = str(e)[:200]
    return data


# ══════════════════════════════════════════════════════════════════════════════
# §10  Alerts
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/alerts")
async def alerts() -> dict:
    """§10 — active warnings, derived from the same probes the cards use.

    Every alert here corresponds to a condition something else in this file has
    actually measured. Nothing is scheduled, nothing is remembered, and nothing
    fires on a threshold this endpoint invented.
    """
    out: list[dict] = []

    def add(severity: str, kind: str, message: str, **extra: Any) -> None:
        out.append({"severity": severity, "type": kind, "message": message,
                    "detected_at": _now_iso(), **extra})

    infra = await infrastructure()
    for card in infra["services"]:
        if card["status"] == "error":
            add("critical", f"{card['name'].lower().replace(' ', '_')}_unavailable",
                f"{card['name']} is unreachable", detail=card.get("detail"),
                connection=card.get("connection"))
        elif card["status"] == "warning":
            add("warning", "provider_degraded",
                f"{card['name']} is degraded", detail=card.get("detail"))
        elif card["status"] == "not_configured":
            add("info", "not_configured", f"{card['name']} is not deployed",
                detail=card.get("detail"))

    # OAuth specifically — the deep check is the only thing that separates a live
    # credential from a stored-but-dead one.
    try:
        from backend.services import provider_health as ph
        ph_res = await ph.check_all(deep=True)
        for key, info in ph_res["providers"].items():
            if info["status"] in ("expired", "refresh_failed"):
                add("critical", "oauth_expired",
                    f"{key} — {info['status']}", detail=info.get("detail"),
                    account=info.get("email"))
            elif info["status"] == "expiring_soon":
                add("warning", "oauth_expiring", f"{key} expires soon",
                    detail=info.get("detail"))
    except Exception as e:  # noqa: BLE001
        add("warning", "provider_health_unavailable",
            "provider health check could not run", detail=str(e)[:160])

    # Graph retrieval health — a resolver that resolves nothing is the exact
    # failure mode that once went unnoticed for an entire session.
    try:
        g = await graph()
        probe = g.get("resolution_probe") or {}
        if isinstance(probe, dict) and probe.get("entities_resolved") == 0:
            add("critical", "graph_retrieval_failure",
                "graph resolver resolved 0 entities for the probe query — "
                "graph context is silently empty")
        if g.get("status") == "error":
            add("critical", "neo4j_unavailable", "knowledge graph query failed",
                detail=g.get("detail"))
    except Exception as e:  # noqa: BLE001
        add("warning", "graph_probe_failed", "graph probe error", detail=str(e)[:160])

    counts: dict[str, int] = {}
    for a in out:
        counts[a["severity"]] = counts.get(a["severity"], 0) + 1
    return {"generated_at": _now_iso(), "active": len(out),
            "by_severity": counts, "alerts": out}


@router.get("/summary")
async def summary() -> dict:
    """Everything the dashboard needs for its header, in one round trip."""
    infra, al = await asyncio.gather(infrastructure(), alerts())
    return {"generated_at": _now_iso(),
            "services": infra["summary"],
            "alerts": al["by_severity"],
            "healthy": infra["summary"].get("healthy", 0),
            "total_services": len(infra["services"])}
