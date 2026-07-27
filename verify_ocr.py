"""Verify scanned-PDF OCR via the vision model: extract text from the business-licence
PDF, re-ingest, and confirm it becomes searchable."""
import sys
from pathlib import Path
sys.path.insert(0, ".")
from backend.ingest import extract_file_text, get_client, ensure_collection, ingest_file, search_corporate

p = Path("data_vault/user_1/邀请单位公司营业执照.pdf")
print("OCR extracting via vision model (~30-90s)...")
t = extract_file_text(p)
print(f"extracted chars: {len(t.strip())}")
print("first 240 chars:", repr(t[:240]))

if t.strip():
    c = get_client()
    ensure_collection(c)
    n = ingest_file(c, p, "user_1", source_type="file")
    print(f"ingested chunks: {n}")
    hits = search_corporate("company business license registration 营业执照", 3, owner="user_1")
    srcs = [h.get("source") for h in hits]
    print(f"search hits: {len(hits)} | sources: {srcs}")
    found = any("营业执照" in (h.get("source") or "") for h in hits)
    print("OCR + retrieval:", "PASS" if (n > 0 and found) else "PARTIAL/FAIL")
else:
    print("OCR returned no text — FAIL")
