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
    Filter, FieldCondition, MatchValue,
)
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


# ── text extraction (full text, no char cap) ────────────────────────────────
def extract_file_text(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="ignore")
        if ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                return "\n\n".join((p.extract_text() or "") for p in pdf.pages)
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


def _delete_source(client: QdrantClient, source: str) -> None:
    client.delete(
        collection_name=RAG_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="source", match=MatchValue(value=source))]
        ),
    )


# ── ingest one file ─────────────────────────────────────────────────────────
def ingest_file(client: QdrantClient, path: Path) -> int:
    source = path.name
    text = extract_file_text(path)
    if not text.strip():
        print(f"[ingest] {source}: no extractable text, skipping")
        return 0

    chunks = chunk_text(text, RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP)
    if not chunks:
        return 0

    vectors = list(get_embedder().embed(chunks))
    _delete_source(client, source)  # replace, don't duplicate

    points = [
        PointStruct(
            id=str(uuid.uuid5(_NS, f"{source}::{i}")),
            vector=vec.tolist(),
            payload={"text": chunk, "source": source, "chunk": i},
        )
        for i, (chunk, vec) in enumerate(zip(chunks, vectors))
    ]
    client.upsert(collection_name=RAG_COLLECTION, points=points)
    print(f"[ingest] {source}: {len(points)} chunks")
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


def search_corporate(query: str, top_k: int = 3) -> list[dict]:
    """Semantic search over the corporate_memory collection. Returns [{text, source}]."""
    client = get_client()
    if not client.collection_exists(RAG_COLLECTION):
        return []
    try:
        vec = list(get_embedder().embed([query]))[0].tolist()
        res = client.query_points(collection_name=RAG_COLLECTION, query=vec,
                                  limit=top_k, with_payload=True)
        return [{"text": p.payload.get("text", ""), "source": p.payload.get("source", "")}
                for p in res.points]
    except Exception as e:
        print(f"[rag] search_corporate failed: {e}")
        return []


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
