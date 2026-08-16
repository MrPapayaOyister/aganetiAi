"""
Durable asynchronous document indexing.

State machine — Postgres is the source of truth, not the task registry:

    stored ──► processing ──► indexed
                   │
                   └────────► failed ──► processing   (retry, bounded)

    processing + process death ──► stale ──► processing   (recovery sweep)

`stored` means the ORIGINAL BYTES are safe in SeaweedFS. It does not mean the
document is searchable. Conflating the two is how a system tells a user their
upload worked while it is invisible to search, so the two are separate states
and the API reports the one that is actually true.

Why no queue. Measured in B5: SeaweedFS costs 56–96 ms, while embedding a 2 MB
document costs ~60 s (1281 chunks × ~47 ms). The expensive part is fastembed,
not transport — and the deployment is ONE uvicorn process. A broker would add a
second source of truth about what needs indexing, which can disagree with the
row that already records it. So: an in-process asyncio task for latency, and an
APScheduler sweep over Postgres for durability. The registry is an optimisation;
losing it costs nothing because the sweep re-derives everything from rows.

The architectural trigger to revisit — write it down rather than leaving it to
taste: a SECOND uvicorn worker, multiple replicas, a measured backlog, or a need
for cross-process job ownership. Until one of those is true, Redis buys nothing
here. The in-process duplicate guard below is correct for one process and is
NOT sufficient for several; that is the first thing that breaks on worker #2.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

log = logging.getLogger("aria.storage.indexing")

STATUS_STORED = "stored"
STATUS_PROCESSING = "processing"
STATUS_INDEXED = "indexed"
STATUS_FAILED = "failed"

# A document we stored, read, and got NOTHING USABLE out of.
#
# This exists because "indexed" used to mean "we tried". A .png, a .mp3, a
# corrupt PDF, a scan the OCR could not read — every one of them produced zero
# chunks and was then recorded status="indexed", chunks=0. The dashboard said
# indexed, the API said indexed, and the document was silently absent from every
# search. That is the failure shape this codebase keeps re-growing: a success
# that is not one.
#
# Making it a separate STATUS rather than a flag on "indexed" is deliberate.
# Anything that filters on status now has to decide what to do about this value,
# instead of inheriting a wrong answer by default — and the retrieval guard in
# backend/ingest.py can key on the absence of chunks rather than on a reader
# remembering to add `AND chunks > 0`.
STATUS_UNREADABLE = "unreadable"

# A document is retried at most this many times before it is left `failed` for a
# human. Retrying an unparseable file forever burns CPU and hides the problem.
MAX_ATTEMPTS = 3
# A `processing` row older than this had its process die mid-flight. It must be
# longer than the slowest realistic indexing run — B5 measured 60s for 2 MB, so
# 15 minutes leaves generous headroom before anything is called stale.
STALE_PROCESSING_AFTER = timedelta(minutes=15)
RETRY_BACKOFF = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
SWEEP_BATCH = 10

# document_id -> Task. Prevents two concurrent indexing runs for one document in
# THIS process, and keeps a strong reference so the task cannot be garbage
# collected mid-flight — a bug that already exists elsewhere in this codebase
# where create_task results are discarded.
_active: dict[str, asyncio.Task] = {}


def active_jobs() -> list[str]:
    return sorted(_active)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


async def _exec(sql: str, params: dict) -> Any:
    from backend.db.base import engine
    async with engine.begin() as conn:
        return await conn.execute(text(sql), params)


async def _patch_meta(document_id: str, patch: dict) -> None:
    await _exec("""UPDATE documents SET meta = meta || CAST(:p AS jsonb),
                      updated_at = now() WHERE id = CAST(:i AS uuid)""",
                {"i": document_id, "p": json.dumps(patch)})


async def _load(document_id: str) -> Optional[dict]:
    from backend.db.base import engine
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT id, user_id, org_id, title, uri, sensitivity, meta FROM documents "
            "WHERE id = CAST(:i AS uuid) AND deleted_at IS NULL"),
            {"i": document_id})).mappings().first()
    return dict(row) if row else None


def _error_category(exc: BaseException) -> str:
    """Coarse category for logs and the dashboard. Never carries the message —
    an exception string can contain a signed URL or a path."""
    from .client import StorageAuthError, StorageNotFound, StorageUnavailable
    if isinstance(exc, StorageAuthError):
        return "storage_auth"
    if isinstance(exc, StorageNotFound):
        return "object_missing"
    if isinstance(exc, StorageUnavailable):
        return "storage_unavailable"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "connect" in name or "connection" in name:
        return "dependency_unavailable"
    return "indexing_error"


def _safe_error(exc: BaseException) -> str:
    """A message safe to persist. Truncated, and stripped of anything that looks
    like a credential — last_error is surfaced in the dashboard."""
    import re
    msg = f"{type(exc).__name__}: {exc}"[:400]
    msg = re.sub(r"(?i)(signature|credential|secret|access[_-]?key|token)=\S+", r"\1=***", msg)
    msg = re.sub(r"AWS4-HMAC-SHA256[^\s]*", "AWS4-HMAC-SHA256 ***", msg)
    return msg


# ── the indexing job itself ──────────────────────────────────────────────────


def _unreadable_reason(meta: dict) -> str:
    """A sentence a USER can act on, not a log line.

    "no extractable text" is true of a .mp3, a scanned page the OCR could not
    read, and a corrupt file, and the three call for different actions — so the
    reason names which one it is where the extension makes that knowable.
    """
    from pathlib import Path as _P

    title = str(meta.get("title") or meta.get("filename") or "")
    ext = _P(title).suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic"}:
        return ("this is an image, and images are not read as documents yet — "
                "a PDF or Word file will work")
    if ext in {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".mp4", ".mov"}:
        return "this is an audio or video file, which cannot be read as a document"
    if ext == ".pdf":
        return ("no text could be read from this PDF — it may be a scan the "
                "reader could not make out, or the file may be damaged")
    if ext:
        return f"no text could be read from this {ext.lstrip('.')} file"
    return "no text could be read from this file"


async def index_document(document_id: str, *, force: bool = False) -> dict:
    """Index one document: object → temp file → existing Qdrant pipeline.

    Idempotent by inheritance, NOT by a new mechanism: ingest_file deletes the
    source's existing points and upserts with deterministic ids
    (uuid5(owner::type::source::index::version)), so re-running replaces rather
    than duplicates. That is why retry is safe and why no dedup layer is added.

    Neo4j is deliberately NOT invoked. The upload path is Qdrant-only today, and
    turning upload→Qdrant into upload→Qdrant→Neo4j is a separate decision.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from .object_store import get_storage, safe_basename, validate_key

    started = time.monotonic()
    row = await _load(document_id)
    if not row:
        return {"document_id": document_id, "status": "missing"}

    meta = dict(row["meta"] or {})
    attempts = int(meta.get("attempt_count") or 0)
    if not force and meta.get("status") == STATUS_INDEXED:
        return {"document_id": document_id, "status": STATUS_INDEXED, "skipped": True}
    if attempts >= MAX_ATTEMPTS and not force:
        return {"document_id": document_id, "status": STATUS_FAILED,
                "detail": "attempt budget exhausted"}

    owner = str(row["user_id"])
    title = row["title"] or "upload.bin"
    uri = row["uri"] or ""
    if not uri.startswith("s3://"):
        await _patch_meta(document_id, {
            "status": STATUS_FAILED, "last_error": "document has no s3:// uri",
            "error_category": "invalid_uri", "attempt_count": attempts + 1,
            "last_attempt_at": _iso()})
        return {"document_id": document_id, "status": STATUS_FAILED}

    await _patch_meta(document_id, {
        "status": STATUS_PROCESSING, "processing_started": _iso(),
        "attempt_count": attempts + 1, "last_attempt_at": _iso()})
    log.info("indexing start document_id=%s owner=%s attempt=%d",
             document_id, owner, attempts + 1)

    tmpdir = None
    try:
        key = validate_key(uri.split("/", 3)[-1])
        data = await asyncio.to_thread(get_storage().get, key)

        # The temp file keeps the ORIGINAL basename: ingest_file uses path.name
        # as the Qdrant `source` and inside the point id, so a random name would
        # break the dedup that makes retry safe.
        tmpdir = tempfile.mkdtemp(prefix="aganeti-index-")
        tmp = Path(tmpdir) / safe_basename(title)
        tmp.write_bytes(data)

        def _run() -> int:
            from backend.ingest import ensure_collection, get_client, ingest_file
            client = get_client()
            ensure_collection(client)
            return ingest_file(client, tmp, owner, org_id=str(row["org_id"]),
                               source_type="file")

        chunks = await asyncio.to_thread(_run)
        took = round((time.monotonic() - started) * 1000, 1)

        # ZERO CHUNKS IS NOT SUCCESS. The bytes are safe and the pipeline ran,
        # but nothing about this document is searchable, and calling that
        # "indexed" is what made the failure invisible for so long.
        if not chunks:
            reason = _unreadable_reason(meta)
            await _patch_meta(document_id, {
                "status": STATUS_UNREADABLE, "indexed_at": _iso(), "chunks": 0,
                "index_duration_ms": took, "extraction": "unreadable",
                "extraction_reason": reason, "last_error": None,
                "error_category": None})
            log.warning("indexing produced no text document_id=%s owner=%s "
                        "status=unreadable reason=%s duration_ms=%.0f",
                        document_id, owner, reason, took)
            return {"document_id": document_id, "status": STATUS_UNREADABLE,
                    "chunks": 0, "reason": reason, "duration_ms": took}

        await _patch_meta(document_id, {
            "status": STATUS_INDEXED, "indexed_at": _iso(), "chunks": chunks,
            "index_duration_ms": took, "extraction": "ok",
            "extraction_reason": None, "last_error": None, "error_category": None})
        log.info("indexing ok document_id=%s owner=%s status=indexed attempt=%d "
                 "chunks=%d duration_ms=%.0f", document_id, owner, attempts + 1,
                 chunks, took)
        return {"document_id": document_id, "status": STATUS_INDEXED,
                "chunks": chunks, "duration_ms": took}

    except Exception as e:  # noqa: BLE001
        took = round((time.monotonic() - started) * 1000, 1)
        attempt = attempts + 1
        exhausted = attempt >= MAX_ATTEMPTS
        backoff = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
        await _patch_meta(document_id, {
            "status": STATUS_FAILED, "last_error": _safe_error(e),
            "error_category": _error_category(e), "attempt_count": attempt,
            "last_attempt_at": _iso(), "index_duration_ms": took,
            "next_retry_at": None if exhausted else (_now() + backoff).isoformat(),
            "retry_exhausted": exhausted})
        log.warning("indexing failed document_id=%s owner=%s status=failed attempt=%d "
                    "duration_ms=%.0f category=%s exhausted=%s", document_id, owner,
                    attempt, took, _error_category(e), exhausted)
        return {"document_id": document_id, "status": STATUS_FAILED,
                "attempt": attempt, "exhausted": exhausted}
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── task registry ────────────────────────────────────────────────────────────

def schedule_indexing(document_id: str, *, force: bool = False) -> bool:
    """Start indexing in the background. Returns False if already running here.

    The registry exists for latency and for the duplicate guard. It is NOT the
    recovery mechanism — if this process dies, the row stays `processing` and the
    sweep re-drives it. Losing the registry loses nothing.
    """
    if document_id in _active and not _active[document_id].done():
        log.info("indexing already active document_id=%s — not starting a second",
                 document_id)
        return False

    async def _wrapped() -> None:
        try:
            await index_document(document_id, force=force)
        except asyncio.CancelledError:
            log.warning("indexing cancelled document_id=%s", document_id)
            raise
        except Exception:  # noqa: BLE001
            # index_document already records failure; this exists so a bug in
            # the recording path cannot make the exception disappear entirely.
            log.exception("indexing task crashed document_id=%s", document_id)

    try:
        task = asyncio.get_running_loop().create_task(_wrapped())
    except RuntimeError:
        log.warning("no running loop — cannot schedule indexing for %s "
                    "(the sweep will pick it up)", document_id)
        return False

    _active[document_id] = task
    # Strong reference held in _active until done, then removed — an unreferenced
    # create_task can be garbage collected mid-flight.
    task.add_done_callback(lambda t, d=document_id: _active.pop(d, None))
    return True


# ── recovery sweep ───────────────────────────────────────────────────────────

async def sweep_pending(limit: int = SWEEP_BATCH) -> dict:
    """Find recoverable documents and re-drive them. Bounded and idempotent.

    Three recoverable conditions, all derived from Postgres:
      * stored      — the object exists but indexing never started
      * processing  — older than STALE_PROCESSING_AFTER, i.e. its process died
      * failed      — attempts remaining and next_retry_at has passed

    Never scans the filesystem: SeaweedFS + Postgres are the source of truth.
    Documents already running in this process are skipped via the registry, so
    overlapping sweeps cannot double-start one. With multiple workers that guard
    is insufficient — see the module docstring.
    """
    from backend.db.base import engine
    stale_before = (_now() - STALE_PROCESSING_AFTER).isoformat()
    now_iso = _iso()

    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT id, meta->>'status' AS status,
                   COALESCE((meta->>'attempt_count')::int, 0) AS attempts
              FROM documents
             WHERE deleted_at IS NULL
               AND uri IS NOT NULL
               AND (
                     meta->>'status' = 'stored'
                  OR (meta->>'status' = 'processing'
                      AND COALESCE(meta->>'processing_started', '') < :stale)
                  OR (meta->>'status' = 'failed'
                      AND COALESCE((meta->>'attempt_count')::int, 0) < :max_attempts
                      AND COALESCE(meta->>'next_retry_at', '') <> ''
                      AND meta->>'next_retry_at' <= :now)
                   )
             ORDER BY updated_at ASC
             LIMIT :lim
        """), {"stale": stale_before, "now": now_iso,
               "max_attempts": MAX_ATTEMPTS, "lim": int(limit)})).mappings().all()

    started, skipped = [], []
    for r in rows:
        did = str(r["id"])
        if schedule_indexing(did):
            started.append({"document_id": did, "was": r["status"],
                            "attempts": r["attempts"]})
        else:
            skipped.append(did)
    if started:
        log.info("indexing sweep: re-driving %d document(s) %s", len(started),
                 [s["was"] for s in started])
    return {"checked": len(rows), "started": started, "skipped_active": skipped,
            "swept_at": now_iso}


async def indexing_stats() -> dict:
    """Real counts from Postgres for the observability dashboard.

    Everything here is a query. No queue depth, no throughput, no worker count —
    there is no queue and one process, and inventing those numbers would be
    worse than omitting them.
    """
    from backend.db.base import engine
    async with engine.connect() as conn:
        counts = {r["status"]: r["n"] for r in (await conn.execute(text("""
            SELECT COALESCE(meta->>'status', 'unknown') AS status, count(*) AS n
              FROM documents WHERE deleted_at IS NULL GROUP BY 1"""))).mappings()}
        agg = (await conn.execute(text("""
            SELECT
              min(meta->>'processing_started') FILTER (WHERE meta->>'status'='processing')
                AS oldest_processing,
              max(meta->>'indexed_at')  FILTER (WHERE meta->>'status'='indexed')
                AS latest_indexed,
              max(meta->>'last_attempt_at') FILTER (WHERE meta->>'status'='failed')
                AS latest_failure,
              sum(COALESCE((meta->>'attempt_count')::int,0))
                FILTER (WHERE COALESCE((meta->>'attempt_count')::int,0) > 1)
                AS retry_attempts,
              avg((meta->>'index_duration_ms')::numeric)
                FILTER (WHERE meta->>'status'='indexed') AS avg_index_ms
              FROM documents WHERE deleted_at IS NULL"""))).mappings().first()
        recent = [dict(r) for r in (await conn.execute(text("""
            SELECT id, title, meta->>'error_category' AS category,
                   meta->>'last_error' AS last_error,
                   meta->>'last_attempt_at' AS at,
                   COALESCE((meta->>'attempt_count')::int,0) AS attempts
              FROM documents
             WHERE deleted_at IS NULL AND meta->>'status' = 'failed'
             ORDER BY meta->>'last_attempt_at' DESC NULLS LAST LIMIT 10"""))).mappings()]

    for r in recent:
        r["id"] = str(r["id"])
    return {
        "counts": {k: counts.get(k, 0) for k in
                   (STATUS_STORED, STATUS_PROCESSING, STATUS_INDEXED, STATUS_FAILED)},
        "total_documents": sum(counts.values()),
        "active_in_process": active_jobs(),
        "oldest_processing": agg["oldest_processing"] if agg else None,
        "latest_indexed": agg["latest_indexed"] if agg else None,
        "latest_failure": agg["latest_failure"] if agg else None,
        "retry_attempts": int(agg["retry_attempts"] or 0) if agg else 0,
        "avg_index_duration_ms": (round(float(agg["avg_index_ms"]), 1)
                                  if agg and agg["avg_index_ms"] is not None else None),
        "recent_failures": recent,
        "max_attempts": MAX_ATTEMPTS,
        "stale_processing_after_s": int(STALE_PROCESSING_AFTER.total_seconds()),
    }


def register_sweep(scheduler: Any, *, minutes: int = 2) -> bool:
    """Attach the recovery sweep to an APScheduler instance.

    Lives here rather than being written inline in main.py so the registration
    is one call at a stable location. Passing a coroutine function directly
    matters: APScheduler's AsyncIOExecutor only dispatches to the event loop
    when iscoroutinefunction_partial(func) is True, and a lambda wrapper would
    be handed to a worker thread where create_task raises "no running event
    loop" — the defect that left eight job types never running.
    """
    async def _sweep() -> None:
        try:
            await sweep_pending()
        except Exception as e:  # noqa: BLE001 — a sweep must never kill the scheduler
            log.warning("index sweep failed: %s", e)
    try:
        scheduler.add_job(_sweep, "interval", minutes=minutes, id="index_sweep",
                          replace_existing=True, max_instances=1, coalesce=True)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("could not register index sweep: %s", e)
        return False
