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
    IsEmptyCondition, PayloadField,
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

SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv",
             # Images read as OCR text plus a one-line type description; see
             # backend/services/image_read.py. Routed through the same Paddle
             # worker as PDFs so they inherit the bounded detection resolution,
             # the Arabic two-pass merge and the confidence guard.
             ".png", ".jpg", ".jpeg", ".webp"}

#: Raster formats handled by image_read rather than a text extractor.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# Formats no Python library here reads, which LibreOffice converts to PDF first.
#
# `.doc` was the visible case: the composer's accept= offered it, the backend
# accepted it, and extract_file_text fell through every branch to return "" — so
# ingest_file recorded 0 chunks and the document was marked "indexed". The user
# was told their file was indexed when nothing had been read.
#
# Handled HERE rather than in the upload route so both entry points that already
# call extract_file_text — /ingest/upload (via ingest_file) and
# /documents/summarize — are fixed by one change. integrations/document_handler
# has its own copy of this logic, but it is Telegram-shaped (needs an aiogram Bot
# and Message) and its extract_text() does NOT handle .doc; the conversion lives
# in its caller. Reaching for it would silently reproduce the bug.
LEGACY_EXTENSIONS = {".doc", ".xls", ".ppt", ".rtf", ".odt", ".ods", ".odp"}

# Bound the LibreOffice call. The measured cost on this box is ~1.0s warm /
# ~1.8s cold at 228 MiB RSS, and a hung soffice is killed by process group in
# convert_to_pdf_safe — but the bound belongs to the caller that has to answer
# an HTTP request.
LEGACY_CONVERT_TIMEOUT = 25.0
_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_URL

# ── tenancy ──────────────────────────────────────────────────────────────────
# `org_id` is the tenant on a chunk. It has existed as a payload key since P1 but
# the drop-folder path never set it, so 967 of 1009 live points carry no tenant at
# all — turning a filter on before that is fixed empties the corpus silently, with
# no error anywhere.
#
# The drop-folder corpus has no per-file owner to derive a tenant from: it is
# deliberately SHARED, which is the same reason its `user_id` is the ORG_OWNER
# sentinel. So it gets a tenant sentinel too, rather than NULL. "Shared" is then a
# value the filter can reason about, instead of an absence it has to guess at.
SHARED_TENANT = "__shared__"

# An unstamped chunk (org_id null or key absent) is treated as SHARED rather than
# as nobody's. That makes the filter a no-op on today's data and correct the moment
# the backfill lands — the only ordering that is safe in both directions. Flip to
# strict ONLY after `scripts/backfill_qdrant_tenant.py` reports zero unstamped
# points; strict on this corpus returns almost nothing.
#
# Mirrors GRAPH_TENANT_STRICT in backend/orchestrator/graph_tools.py on purpose:
# two stores, one ratchet, one mental model.
QDRANT_TENANT_STRICT = os.getenv("QDRANT_TENANT_STRICT", "false").strip().lower() in ("1", "true", "yes")

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
def _extract_via_libreoffice(path: Path) -> str:
    """Convert a legacy Office format to PDF, then read it as a PDF.

    Returns "" on any failure, matching extract_file_text's contract — every
    caller already handles empty text. The failure is LOGGED rather than
    swallowed silently, because "no text" and "conversion failed" look identical
    downstream and only one of them is the user's fault.
    """
    import asyncio
    import shutil
    import tempfile

    try:
        from integrations.libreoffice_converter import (ConversionFailed,
                                                        convert_to_pdf_safe)
    except ImportError as e:
        print(f"[ingest] libreoffice converter unavailable ({e}); cannot read {path.name}")
        return ""

    workdir = Path(tempfile.mkdtemp(prefix="aganeti-soffice-"))
    try:
        pdf = asyncio.run(convert_to_pdf_safe(path, workdir,
                                              timeout=LEGACY_CONVERT_TIMEOUT))
        # Re-enter the .pdf branch, which brings the OCR fallback with it: a
        # scanned .doc converts to an image-only PDF and still gets read.
        text = extract_file_text(pdf)
        # Content check, not just artefact check. LibreOffice converts random
        # bytes to a valid PDF with exit 0; only the extracted text reveals it.
        # Ingesting that would put searchable noise in the shared corpus.
        from integrations.libreoffice_converter import looks_like_document_text
        ok, why = looks_like_document_text(text)
        if not ok:
            print(f"[ingest] {path.name}: rejected after conversion — {why}")
            return ""
        return text
    except ConversionFailed as e:
        print(f"[ingest] {path.name}: LibreOffice produced nothing usable: {e}")
        return ""
    except TimeoutError as e:
        print(f"[ingest] {path.name}: LibreOffice timed out: {e}")
        return ""
    except Exception as e:  # noqa: BLE001
        print(f"[ingest] {path.name}: legacy conversion failed: {type(e).__name__}: {e}")
        return ""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def extract_file_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in LEGACY_EXTENSIONS:
        return _extract_via_libreoffice(path)
    if ext in IMAGE_EXTENSIONS:
        # NOT normalised here. read_image applies normalise_visual_order to the
        # OCR half itself and must not apply it to the description, which is
        # English prose from the VL model and was never bidi-reordered.
        from backend.services.image_read import read_image
        return read_image(str(path))
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
            ocr = _ocr_pdf(path)
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


# Longest edge, in pixels, of a page image sent to the vision model.
#
# WHY THIS EXISTS: rendering A4 at 150 dpi produces 1240x1755, which Qwen3-VL
# tokenises to a 110x78 patch grid = 2,145 vision tokens. On 2026-08-14 that
# input killed the shared VL engine:
#
#   UserWarning: gemm_and_bias error: CUBLAS_STATUS_INTERNAL_ERROR when calling
#   cublasLtMatmul ... m 2048 n 2145 k 4608
#   EngineCore encountered a fatal error / EngineDeadError
#
# The container restarted (docker RestartCount 1) and every other vision caller
# failed for the duration. The failure is in the vision tower's matmul at that
# token count, and this path generated it on EVERY page of EVERY scanned PDF.
#
# It failed silently end to end: `_llm.complete` defaults to raise_on_error=False
# and returns "", the per-page `except` prints, `ingest_file` then sees no text
# and returns 0 chunks, and `indexing.py` records status="indexed", chunks=0.
# A user's scanned PDF was reported as successfully indexed while it had in fact
# just taken down a shared GPU service.
#
# 640 is the largest size measured safe (0.66-1.18s per call); it yields roughly
# 280 vision tokens, an order of magnitude below the failing matmul. Raising it
# means re-measuring against the engine, not just reasoning about pixels.
OCR_MAX_EDGE_PX = 640

# LANDSCAPE PAGES ARE A KNOWN, UNFIXED FAILURE — and the guard does NOT cover it.
#
# Fitting the LONG edge to 640 crushes a landscape table: a 1650x1275 KPI sheet
# becomes 640x495, ~40 rows in 495px. The model then recognises "table" without
# being able to read it and transcribes a DIFFERENT table — plausible column
# headers that appear nowhere in the document.
#
# Landscape-aware handling was measured against marking the page unreadable:
#
#   approach                       px        calls  numeric recall  invented schema
#   ----------------------------   -------   -----  --------------  ---------------
#   A long-edge fit (kept)         640x495     1        0/6              yes
#   B short-edge fit               828x640     1        0/6              yes
#   C short-edge + 2 tiles         640x640     2        2/6              yes
#
# B also breaks the 640 cap on its long edge, so it was never a safe option. C
# doubles the VL calls to go from confidently wrong to mostly wrong, and still
# emits something that reads like a real table — the worst possible property for
# text entering retrieval. A page marked unreadable is an acceptable outcome; a
# page transcribed as a different table is not. So A is retained and the page is
# expected to land as STATUS_UNREADABLE.
#
# READ THIS BEFORE TRUSTING THE GUARD HERE:
# _confabulation_reason catches case A only by its REPETITION SIGNATURE — that
# particular render happened to emit "a single character repeats 870 times". It
# does not detect wrong transcription. B and C produce fluent, well-formed,
# entirely fabricated tables and pass the guard cleanly.
#
# So "we have a confabulation guard" does NOT mean landscape pages are handled.
# The guard covers degenerate output and the NO_TEXT sentinel. Nothing in this
# file can tell a correct transcription from a confident fabrication, and if
# landscape handling is ever adopted, it will need a different check entirely
# (cross-checking against any extractable text layer, or a second pass compared
# for agreement) — not this one.

# TILING WAS MEASURED AND REJECTED — do not re-propose it without reading this.
#
# The obvious objection to a 640px cap is that it costs OCR accuracy on small
# text, and it does: on a synthetic invoice the single render read 2026-0042 as
# 2020-0542. The obvious fix is to slice the page into several <=640px tiles and
# OCR each. That was built and measured against REAL document layouts (real PDFs
# rasterised at 150 dpi, scored on numeric-token recall against their own text
# layer as ground truth — reference numbers, dates and amounts, the fields people
# actually consult a scan for).
#
#   real page (numeric tokens)   single render   2 tiles + 64px overlap
#   --------------------------   -------------   ----------------------
#   dense numeric / tables (35)      91.4%              82.9%   <-- WORSE
#   mixed report page      (13)      61.5%             100.0%
#   small print + refs     (10)      80.0%             100.0%
#   fourth layout           (6)       0.0%               0.0%   <-- both fail
#
# Tiling wins on flowing text (more pixels per glyph) and LOSES on tabular
# layout, which is the case that matters most for scanned documents. A horizontal
# tile boundary cuts through table rows, so figures arrive at the model separated
# from the row labels that give them meaning; it transcribed what it could see and
# dropped 153,343 / 2857 / 52.2 / 80.2. Overlap does not fix this — the overlap
# would have to be a whole row, and rows are not a fixed height.
#
# Cost was 2x VL calls for +13% latency. Paying that for a layout-dependent trade
# that is negative on tables was not worth it. If revisited, the only version
# worth building is table-aware tiling (detect rows, cut between them), and the
# detection is its own source of error.



def _downscale_png(png: bytes, max_edge: int = OCR_MAX_EDGE_PX) -> bytes:
    """Fit a page render inside max_edge x max_edge, preserving aspect ratio.

    Returns the original bytes unchanged if Pillow is unavailable or the image is
    already small enough — a missing optional dep must not silently disable OCR,
    but it must also not send an oversized image, so the caller logs and skips
    instead (see _ocr_pdf_via_vision)."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return png
    try:
        im = Image.open(io.BytesIO(png))
        if max(im.size) <= max_edge:
            return png
        im = im.convert("RGB")
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — a render we cannot resize is one we must not send
        return png


def _too_large(png: bytes) -> bool:
    """True when an image still exceeds the safe edge after a resize attempt."""
    try:
        import io

        from PIL import Image
        return max(Image.open(io.BytesIO(png)).size) > OCR_MAX_EDGE_PX
    except Exception:  # noqa: BLE001 — cannot measure it, so assume unsafe
        return True


# The sentinel the hedged OCR prompt returns for a page with nothing on it.
OCR_EMPTY_SENTINEL = "NO_TEXT"

#: A line repeated more than this share of the output is degenerate, not text.
OCR_REPETITION_RATIO = 0.5


def _confabulation_reason(text: str) -> str | None:
    """Why this OCR output should be thrown away, or None to keep it.

    An INDEPENDENT check, deliberately not relying on the prompt. The prompt is
    one edit away from losing its "reply NO_TEXT" clause, and the failure it
    prevents is silent — fabricated text entering the corpus and being retrieved
    later as fact. Two things are caught:

      * the sentinel itself, which means the model reported an empty page;
      * DEGENERATE REPETITION, which is what fabrication on a blank page actually
        looked like: 1023 characters of "The\nThe\nThe…" running to the token
        cap. A real document does not repeat one line for a whole page.

    It does NOT catch plausible fabrication — the Korean address invented from a
    truncated file was well-formed text and no structural check would reject it.
    That case is the prompt's job, which is why both exist.
    """
    t = (text or "").strip()
    if not t:
        return None                      # empty is handled by the caller, not an error
    if t.upper().startswith(OCR_EMPTY_SENTINEL):
        return "the model reported no legible text on this page"

    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 6:
        top = max(set(lines), key=lines.count)
        share = lines.count(top) / len(lines)
        if share > OCR_REPETITION_RATIO:
            return (f"degenerate output — {lines.count(top)} of {len(lines)} lines "
                    f"are the same ({top[:24]!r})")

    # A single unbroken run of one character is the same failure without newlines.
    if len(t) > 200:
        longest = 1
        run = 1
        for a, b in zip(t, t[1:]):
            run = run + 1 if a == b else 1
            longest = max(longest, run)
        if longest > len(t) * 0.5:
            return f"degenerate output — a single character repeats {longest} times"
    return None


def _ocr_pdf_via_vision(path: Path, max_pages: int = 6) -> str:
    """OCR a scanned/image PDF with the local vision model: render each page to
    PNG, downscale it, ask the VLM to transcribe it verbatim. Multilingual
    (handles the Chinese business-licence case). Best-effort — returns '' if
    anything is unavailable.

    Every page image is capped at OCR_MAX_EDGE_PX; see that constant for the
    engine crash that made the cap necessary."""
    import base64
    import os as _os
    try:
        import fitz  # pymupdf
        import httpx
    except ImportError as e:
        print(f"[ocr] deps missing ({e}); cannot OCR {path.name}")
        return ""
    from backend.services import llm as _llm
    from config.settings import LLM_VISION_MODEL as vl_model
    # HEDGED PROMPT. The previous wording ("Transcribe ALL text…") gave the model
    # no way to say "there is nothing here", and it duly invented something:
    #
    #   blank page   -> 1023 chars of "The\nThe\nThe…" (ran to the token cap)
    #   faint noise  -> the same
    #   truncated .doc -> a well-formed Korean postal address
    #
    # Granting an explicit empty answer fixed all of it — measured 5/5 across
    # blank, near-blank, noise and scan-artefact pages, with no false NO_TEXT on
    # a legible control. Keep the sentinel exact; _looks_confabulated matches it.
    prompt = ("Transcribe any text in this image verbatim, preserving line order. "
              "If the image contains no legible text, reply with exactly: NO_TEXT. "
              "Do not guess, do not describe the image, and do not invent plausible "
              "content. Output ONLY the transcribed text.")
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
            small = _downscale_png(png)
            if small is png and _too_large(png):
                # Could not resize (Pillow missing or a render Pillow rejected).
                # Skipping one page beats crashing the engine for every caller.
                print(f"[ocr] page {i} of {path.name}: cannot downscale; skipping "
                      f"rather than risking the vision engine")
                continue
            png = small
            data_url = "data:image/png;base64," + base64.b64encode(png).decode()
            txt = _llm.complete(
                [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}}]}],
                model=vl_model, temperature=0, max_tokens=2048, timeout=120.0).strip()
            bad = _confabulation_reason(txt)
            if bad:
                print(f"[ocr] page {i} of {path.name}: discarded — {bad}")
                continue
            if txt:
                out.append(txt)
        except Exception as e:  # noqa: BLE001
            print(f"[ocr] page {i} of {path.name} failed: {e}")
    if out:
        print(f"[ocr] {path.name}: OCR'd {len(out)} page(s) via vision model")
    return "\n\n".join(out)


def _ocr_pdf(path: Path) -> str:
    """The ONE place OCR output enters the pipeline.

    Every engine returns through here, so the post-processing that is true of all
    of them — bidi reordering today, more later — is applied once instead of
    being reimplemented per adapter and drifting.

    This exists as a seam rather than a convenience. Qwen3-VL, PaddleOCR and
    Surya were all measured returning `0042-2026` for a page whose text is
    `2026-0042`: OCR reads VISUAL order, and Arabic lines are laid out with their
    numeric segments swapped. Normalising inside an engine adapter would mean
    doing it three times and forgetting it the fourth.

    It must NOT move up into extract_file_text's common return path. A PDF text
    layer is stored in logical order and is already correct; normalising it would
    corrupt every Arabic invoice number that arrives as real text.
    """
    from backend.services.ocr_text import normalise_visual_order

    text = _ocr_pdf_via_paddle(path)
    if not text.strip():
        # Paddle found nothing it would stand behind. That is either a page with
        # no text (correct, and the VL model will say NO_TEXT too) or one it
        # could not detect on. VL is kept for the second case — it is the weaker
        # transcriber but it is a genuinely different method, and a page neither
        # can read should be marked unreadable rather than guessed at.
        text = _ocr_pdf_via_vision(path)
    return normalise_visual_order(text)


def _ocr_pdf_via_paddle(path: Path, max_pages: int = 6) -> str:
    """OCR a scanned PDF with PaddleOCR, at FULL render resolution.

    THE RESOLUTION IS THE POINT. The VL path caps every page at
    OCR_MAX_EDGE_PX=640 because 1240x1755 crashed the vision engine. That cap is
    a property of that engine, not of the page, and inheriting it here would
    throw away the reason for the change: at 640 Paddle scores 0% on the
    landscape KPI table and at full resolution 100%. Memory is bounded at the
    DETECTION stage instead (ocr_paddle.DET_LIMIT), which costs no accuracy
    because recognition still crops from the full-size page.

    Runs in a subprocess — see ocr_paddle.read_pdf for why that is not optional.

    Pages whose output fails `assess` are DROPPED, not returned. The signal is
    per-line recognition confidence: real pages put 0.0% of their lines below
    0.80, the poisoned garbage.doc render put 98% there, and
    looks_like_document_text cannot tell the difference because Paddle's junk is
    made of assigned CJK codepoints rather than unmapped glyphs.
    """
    from backend.services import ocr_paddle
    from backend.services.ocr_text import assess

    if not ocr_paddle.available():
        print("[ocr] paddleocr not installed; falling back to the vision model")
        return ""

    out: list[str] = []
    for page in ocr_paddle.read_pdf(str(path), max_pages=max_pages):
        i = page.get("page")
        if page.get("error"):
            print(f"[ocr] page {i} of {path.name} failed: {page['error']}")
            continue
        text = page.get("text") or ""
        ok, why = assess(text, page.get("scores") or [])
        if not ok:
            print(f"[ocr] page {i} of {path.name}: discarded — {why}")
            continue
        if text.strip():
            out.append(text)
    if out:
        print(f"[ocr] {path.name}: OCR'd {len(out)} page(s) via paddleocr")
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

    # THE INDEX BOUNDARY. Text that fails the sanity checks stops HERE, before it
    # is embedded, rather than being filtered out later at retrieval.
    #
    # Both placements were available and this one is strictly stronger. A
    # retrieval filter is a line of code every future query path has to remember
    # — search_knowledge, the context engine's corporate provider, the
    # observability explorer, the orchestrator registry, and whatever comes next
    # — and the one that forgets returns fiction as fact. Nothing can be
    # retrieved that was never written.
    #
    # What this catches, all of it measured rather than imagined:
    #   * mojibake from a "successful" LibreOffice conversion of random bytes —
    #     14,422 characters that pass every artefact-level check;
    #   * degenerate OCR output ("The\nThe\nThe…" to the token cap) if the
    #     hedged prompt is ever edited away;
    #   * anything else that is bytes wearing the shape of a document.
    #
    # It deliberately does NOT catch plausible fabrication — a well-formed
    # invented address is indistinguishable from a real one at this layer. That
    # is the OCR prompt's job, and the two guards are independent on purpose.
    from integrations.libreoffice_converter import looks_like_document_text
    ok, why = looks_like_document_text(text)
    if not ok:
        print(f"[ingest] {source}: refusing to index — {why}")
        return 0

    bad = _confabulation_reason(text)
    if bad:
        print(f"[ingest] {source}: refusing to index — {bad}")
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
        # org_id=SHARED_TENANT, not None. This call is the only reason 95% of the
        # corpus has no tenant: the vault is a single shared drop-folder with no
        # per-file owner, so there is nothing to derive a real org from — but
        # "shared" is a fact about these files, not an unknown. Stamping it here is
        # what stops the backfill being re-polluted on the next scheduler tick
        # (main.py:699 runs this every RAG_WATCH_INTERVAL).
        n = ingest_file(client, path, org_id=SHARED_TENANT)
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


def tenant_clause(tenant_id: str, *, strict: bool | None = None) -> Filter:
    """The org predicate: this tenant's chunks, plus the shared corpus.

    Deliberately shaped like the `user_id` ACL above it — "yours PLUS shared" — so
    the two boundaries compose instead of fighting. A chunk must satisfy both: an
    org match alone does not expose another user's private document, and a user
    match alone does not reach across tenants.

    `strict=False` (the default, see QDRANT_TENANT_STRICT) also admits unstamped
    chunks. That is what makes this safe to deploy BEFORE the backfill: on today's
    corpus it changes nothing, and it starts excluding the moment chunks carry a
    real tenant. Strict mode drops unstamped chunks entirely — correct only once
    the backfill reports zero.
    """
    strict = QDRANT_TENANT_STRICT if strict is None else strict
    should = [FieldCondition(key="org_id", match=MatchAny(any=[tenant_id, SHARED_TENANT]))]
    if not strict:
        # `IsEmpty` covers null AND a missing key. On the live corpus the two differ
        # by 9 points — 958 are explicitly null, 967 have no usable org — so
        # IsNullCondition here would silently drop those 9.
        should.append(IsEmptyCondition(is_empty=PayloadField(key="org_id")))
    return Filter(should=should)


def search_corporate(query: str, top_k: int = 3, owner: str | None = None,
                     source_types: list | None = None, since: int | None = None,
                     tenant_id: str | None = None,
                     tenant_strict: bool | None = None) -> list[dict]:
    """Semantic search over corporate_memory, ACL-scoped. Returns a superset dict per hit:
    {text, source, score, chunk_index, source_type, document_id, timestamp, sensitivity,
    user_id}. 'text'/'source' are kept byte-identical for backward-compat.

    Results are restricted to the caller's own documents PLUS the shared org corpus.
    owner=None (unauthenticated/legacy) returns ONLY the shared org corpus — never another
    user's private content. Optional source_types (['email','meeting','file']) and since
    (epoch seconds) narrow the search ('what did X say 3 days ago').

    `tenant_id` adds the ORG boundary on top of that user ACL. It is OPTIONAL and
    omitted means "no tenant predicate at all", which keeps every existing caller —
    the search_documents tool, the eval harness, the CLI — byte-identical. A tenant
    is added where one is known, never inferred, and never defaulted: guessing a
    tenant is how a filter starts returning another org's documents.
    """
    client = get_client()
    if not client.collection_exists(RAG_COLLECTION):
        return []
    allowed = [ORG_OWNER] if owner is None else [owner, ORG_OWNER]
    must = [FieldCondition(key="user_id", match=MatchAny(any=allowed))]
    if source_types:
        must.append(FieldCondition(key="source_type", match=MatchAny(any=list(source_types))))
    if since is not None:
        must.append(FieldCondition(key="timestamp", range=Range(gte=int(since))))
    if tenant_id:
        must.append(tenant_clause(str(tenant_id), strict=tenant_strict))
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
