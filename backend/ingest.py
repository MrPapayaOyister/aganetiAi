"""
RAG ingestion pipeline for the corporate second-brain.

Walks ``data_vault/`` for documents (txt, md, pdf, docx, xlsx, csv), chunks them
with overlap, embeds with fastembed, and upserts into the Qdrant
``corporate_memory`` collection.

Improvements over the original one-shot script:
  * multi-file + multiple formats (not just company_handbook.txt)
  * word-overlap chunking instead of one giant vector per document
  * **idempotent / incremental** — a content hash per file is tracked in
    ``data_vault/.ingest_state.json`` so unchanged files are skipped, changed
    files are re-embedded (old chunks deleted first), and deleted files are pruned
  * stable point IDs (uuid5 of ``source::chunk_index``) so re-ingesting replaces
    rather than duplicates
  * importable functions + a ``--watch`` mode for a live drop-folder

Usage:
    python -m backend.ingest                 # incremental ingest (default)
    python -m backend.ingest --force         # re-ingest everything
    python -m backend.ingest --reset         # drop & recreate the collection
    python -m backend.ingest --watch 30      # poll data_vault every 30s
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointStruct, VectorParams, Distance,
    Filter, FieldCondition, MatchValue, MatchAny, Range,
)

# Sentinel owner for the shared, org-wide corpus (CLI-ingested docs + legacy chunks).
# Per-user uploads are tagged with the uploader's identity; search returns the
# caller's own chunks PLUS the shared org corpus — never another user's private docs.
ORG_OWNER = "__org__"
from fastembed import TextEmbedding

from config.settings import (
    QDRANT_URL, RAG_COLLECTION, RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP,
    DATA_VAULT_DIR, RAG_STATE_FILE, EMBED_MODEL_NAME, EMBED_DIM,
)

SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv"}
_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_URL

_embedder: TextEmbedding | None = None


# ── infra ───────────────────────────────────────────────────────────────────
def get_embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        print(f"[ingest] loading embedding model {EMBED_MODEL_NAME} ...")
        _embedder = TextEmbedding(model_name=EMBED_MODEL_NAME)
    return _embedder


def get_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL or "http://localhost:6333")


def ensure_collection(client: QdrantClient, reset: bool = False) -> None:
    exists = client.collection_exists(collection_name=RAG_COLLECTION)
    if exists and reset:
        client.delete_collection(collection_name=RAG_COLLECTION)
        exists = False
    if not exists:
        client.create_collection(
            collection_name=RAG_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
        print(f"[ingest] created collection '{RAG_COLLECTION}' (dim={EMBED_DIM})")
    _ensure_payload_indexes(client)


def _ensure_payload_indexes(client: QdrantClient) -> None:
    """Additive, idempotent payload indexes for ACL + metadata filtering. Safe on an
    existing collection (builds over current points). The tenant-HNSW optimization
    (is_tenant / payload_m) needs a collection recreate and is deferred until scale."""
    for field, schema in (("user_id", "keyword"), ("source_type", "keyword"),
                          ("sensitivity", "keyword"), ("timestamp", "integer")):
        try:
            client.create_payload_index(collection_name=RAG_COLLECTION,
                                        field_name=field, field_schema=schema)
        except Exception:
            pass  # already exists / older Qdrant — filtering still works, just unindexed


# ── text extraction (full text, no char cap) ────────────────────────────────
def extract_file_text(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="ignore")
        if ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                text = "\n\n".join((p.extract_text() or "") for p in pdf.pages)
            # Scanned/image PDFs (e.g. a photographed business licence) yield no text
            # from pdfplumber → OCR them with the local vision model so they're readable.
            if len(text.strip()) >= 40:
                return text
            ocr = _ocr_pdf_via_vision(path)
            return ocr if ocr.strip() else text
        if ext == ".docx":
            from docx import Document
            doc = Document(path)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True)
            lines: list[str] = []
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                lines.append(f"[Sheet: {sheet}]")
                for row in ws.iter_rows(values_only=True):
                    rs = " | ".join(str(c) if c is not None else "" for c in row)
                    if rs.strip(" |"):
                        lines.append(rs)
            return "\n".join(lines)
        if ext == ".csv":
            import pandas as pd
            return pd.read_csv(path).to_string(index=False)
    except Exception as e:
        print(f"[ingest] extract failed for {path.name}: {e}")
    return ""


def _ocr_pdf_via_vision(path: Path, max_pages: int = 6) -> str:
    """OCR a scanned/image PDF with the local vision model (qwen2.5-vl): render each
    page to PNG, ask the VLM to transcribe it verbatim. Multilingual (handles the
    Chinese business-licence case). Best-effort — returns '' if anything is unavailable."""
    import base64
    import os as _os
    try:
        import fitz  # pymupdf
        import httpx
    except ImportError as e:
        print(f"[ocr] deps missing ({e}); cannot OCR {path.name}")
        return ""
    vl_url = _os.getenv("VLLM_VL_URL", "http://localhost:9001/v1").rstrip("/")
    vl_model = _os.getenv("VLLM_VL_MODEL", "qwen2.5-vl-32b")
    prompt = ("Transcribe ALL text in this document image verbatim (OCR), preserving "
              "line order. Output ONLY the transcribed text — no commentary, no translation.")
    out: list[str] = []
    try:
        doc = fitz.open(path)
    except Exception as e:  # noqa: BLE001
        print(f"[ocr] cannot open {path.name}: {e}")
        return ""
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        try:
            png = page.get_pixmap(dpi=150).tobytes("png")
            data_url = "data:image/png;base64," + base64.b64encode(png).decode()
            r = httpx.post(f"{vl_url}/chat/completions", timeout=120.0, json={
                "model": vl_model, "temperature": 0, "max_tokens": 2048,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}}]}]})
            r.raise_for_status()
            txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
            if txt:
                out.append(txt)
        except Exception as e:  # noqa: BLE001
            print(f"[ocr] page {i} of {path.name} failed: {e}")
    if out:
        print(f"[ocr] {path.name}: OCR'd {len(out)} page(s) via vision model")
    return "\n\n".join(out)


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    if len(words) <= size:
        return [" ".join(words)]
    step = max(1, size - overlap)
    chunks = []
    for start in range(0, len(words), step):
        piece = " ".join(words[start:start + size]).strip()
        if piece:
            chunks.append(piece)
        if start + size >= len(words):
            break
    return chunks


# ── state (incremental) ─────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        return json.loads(Path(RAG_STATE_FILE).read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    Path(RAG_STATE_FILE).write_text(json.dumps(state, indent=2))


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _delete_source(client: QdrantClient, source: str, owner: str = ORG_OWNER) -> None:
    # Scope the delete to THIS owner's chunks for the source, so re-uploading your
    # file only replaces your own copy and never touches another user's documents.
    client.delete(
        collection_name=RAG_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=source)),
                  FieldCondition(key="user_id", match=MatchValue(value=owner))]
        ),
    )


# ── ingest one file ─────────────────────────────────────────────────────────
def ingest_file(client: QdrantClient, path: Path, owner: str = ORG_OWNER, *,
                org_id=None, source_type: str = "file", sensitivity: str = "internal",
                version: int = 1) -> int:
    source = path.name
    text = extract_file_text(path)
    if not text.strip():
        print(f"[ingest] {source}: no extractable text, skipping")
        return 0

    chunks = chunk_text(text, RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP)
    if not chunks:
        return 0

    vectors = list(get_embedder().embed(chunks))
    _delete_source(client, source, owner)  # replace THIS owner's copy, don't duplicate
    try:
        ts = int(path.stat().st_mtime)
    except OSError:
        ts = 0
    # deterministic document id groups this owner's chunks for one source+version
    document_id = str(uuid.uuid5(_NS, f"{owner}::{source_type}::{source}::v{version}"))

    points = [
        PointStruct(
            # id namespaced by owner+type+version so two users' same filename (or a new
            # version) don't collide/overwrite each other's chunks.
            id=str(uuid.uuid5(_NS, f"{owner}::{source_type}::{source}::{i}::v{version}")),
            vector=vec.tolist(),
            payload={
                # legacy keys kept for backward-compat with pre-P1 readers
                "text": chunk, "source": source, "chunk": i,
                # canonical metadata superset (retrieve→cite→act boundary)
                "chunk_index": i, "user_id": owner,
                "org_id": (str(org_id) if org_id else None),
                "document_id": document_id, "source_type": source_type,
                "sensitivity": sensitivity, "version": version, "timestamp": ts,
            },
        )
        for i, (chunk, vec) in enumerate(zip(chunks, vectors))
    ]
    client.upsert(collection_name=RAG_COLLECTION, points=points)
    print(f"[ingest] {source}: {len(points)} chunks (owner={owner}, type={source_type})")
    return len(points)


# ── ingest all (incremental) ────────────────────────────────────────────────
def ingest_all(reset: bool = False, force: bool = False) -> dict:
    vault = Path(DATA_VAULT_DIR)
    vault.mkdir(parents=True, exist_ok=True)
    client = get_client()
    ensure_collection(client, reset=reset)

    state = {} if (reset or force) else _load_state()
    present = {p.name: p for p in vault.iterdir()
               if p.is_file() and p.suffix.lower() in SUPPORTED}

    ingested_files = 0
    total_chunks = 0

    for name, path in sorted(present.items()):
        digest = _file_hash(path)
        if not force and state.get(name, {}).get("hash") == digest:
            continue
        n = ingest_file(client, path)
        state[name] = {"hash": digest, "chunks": n, "ts": int(time.time())}
        ingested_files += 1
        total_chunks += n

    # prune files that disappeared from the vault
    for gone in [n for n in state if n not in present]:
        _delete_source(client, gone)
        state.pop(gone, None)
        print(f"[ingest] pruned removed file: {gone}")

    _save_state(state)
    summary = {"files_ingested": ingested_files, "chunks": total_chunks,
               "tracked_files": len(state)}
    print(f"[ingest] done: {summary}")
    return summary


def search_corporate(query: str, top_k: int = 3, owner: str | None = None,
                     source_types: list | None = None, since: int | None = None) -> list[dict]:
    """Semantic search over corporate_memory, ACL-scoped. Returns a superset dict per hit:
    {text, source, score, chunk_index, source_type, document_id, timestamp, sensitivity,
    user_id}. 'text'/'source' are kept byte-identical for backward-compat.

    Results are restricted to the caller's own documents PLUS the shared org corpus.
    owner=None (unauthenticated/legacy) returns ONLY the shared org corpus — never another
    user's private content. Optional source_types (['email','meeting','file']) and since
    (epoch seconds) narrow the search ('what did X say 3 days ago')."""
    client = get_client()
    if not client.collection_exists(RAG_COLLECTION):
        return []
    allowed = [ORG_OWNER] if owner is None else [owner, ORG_OWNER]
    must = [FieldCondition(key="user_id", match=MatchAny(any=allowed))]
    if source_types:
        must.append(FieldCondition(key="source_type", match=MatchAny(any=list(source_types))))
    if since is not None:
        must.append(FieldCondition(key="timestamp", range=Range(gte=int(since))))
    acl = Filter(must=must)
    try:
        vec = list(get_embedder().embed([query]))[0].tolist()
        res = client.query_points(collection_name=RAG_COLLECTION, query=vec,
                                  limit=top_k, with_payload=True, query_filter=acl)
    except Exception as e:
        print(f"[rag] search_corporate failed: {e}")
        return []
    out = []
    for p in res.points:
        pl = p.payload or {}
        out.append({
            "text": pl.get("text", ""), "source": pl.get("source", ""),   # legacy keys
            "score": getattr(p, "score", None),
            "chunk_index": pl.get("chunk_index", pl.get("chunk")),
            "source_type": pl.get("source_type", "file"),
            "document_id": pl.get("document_id"),
            "timestamp": pl.get("timestamp"),
            "sensitivity": pl.get("sensitivity", "internal"),
            "user_id": pl.get("user_id"),
        })
    return out


def watch(interval: int = 30) -> None:
    print(f"[ingest] watching {DATA_VAULT_DIR} every {interval}s (Ctrl-C to stop)")
    ingest_all()  # initial pass
    while True:
        time.sleep(interval)
        try:
            ingest_all()
        except Exception as e:  # keep the watcher alive
            print(f"[ingest] watch cycle error: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="RAG ingestion for corporate_memory")
    ap.add_argument("--reset", action="store_true", help="drop & recreate the collection")
    ap.add_argument("--force", action="store_true", help="re-ingest all files (ignore hash cache)")
    ap.add_argument("--watch", type=int, nargs="?", const=30, default=None,
                    metavar="SECONDS", help="poll data_vault on an interval")
    args = ap.parse_args()

    if args.watch is not None:
        watch(args.watch)
    else:
        ingest_all(reset=args.reset, force=args.force)


if __name__ == "__main__":
    main()
