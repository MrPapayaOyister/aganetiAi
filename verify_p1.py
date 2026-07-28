"""Verify P1 rich payload + superset search + payload indexes + filters + ACL."""
import sys
from pathlib import Path
sys.path.insert(0, ".")

from backend.ingest import (get_client, ensure_collection, ingest_file,
                            search_corporate, _delete_source, RAG_COLLECTION)

U, OTHER = "p1test-user", "p1test-other"
PHRASE = "HELIOTROPE-CASCADE"


def main():
    tmp = Path("data_vault/_p1test"); tmp.mkdir(parents=True, exist_ok=True)
    doc = tmp / "p1_doc.txt"
    doc.write_text(f"The P1 metadata test phrase is {PHRASE} and it lives in a file.", encoding="utf-8")
    client = get_client()
    ensure_collection(client)  # also creates payload indexes (idempotent)
    n = ingest_file(client, doc, U, source_type="file")
    print(f"ingested {n} chunk(s) as owner={U} source_type=file")

    info = client.get_collection(RAG_COLLECTION)
    idx = sorted((info.payload_schema or {}).keys())
    print(f"payload indexes on collection: {idx}")

    q = "what is the P1 metadata test phrase"
    hits = search_corporate(q, 3, owner=U)
    h = hits[0] if hits else {}
    keys = ["text", "source", "score", "chunk_index", "source_type", "document_id", "timestamp", "sensitivity", "user_id"]
    superset_ok = bool(hits) and all(k in h for k in keys)
    print(f"superset dict complete: {superset_ok}")
    print(f"  sample: source={h.get('source')} score={h.get('score')} type={h.get('source_type')} ts={h.get('timestamp')} doc={str(h.get('document_id'))[:8]}")

    byfile = search_corporate(q, 3, owner=U, source_types=["file"])
    byemail = search_corporate(q, 3, owner=U, source_types=["email"])
    other = search_corporate(q, 3, owner=OTHER)
    # ACL: OTHER may see the shared __org__ corpus, but MUST NOT see U's PRIVATE phrase.
    other_leaks = any(PHRASE in (h.get("text") or "") for h in other)
    print(f"filters: source_type=file -> {len(byfile)} | source_type=email -> {len(byemail)}")
    print(f"other-user hits -> {len(other)} (org corpus ok); leaks private phrase -> {other_leaks} (expect False)")

    _delete_source(client, "p1_doc.txt", U)
    try:
        doc.unlink(); tmp.rmdir()
    except OSError:
        pass

    ok = (superset_ok and h.get("score") is not None and h.get("source_type") == "file"
          and {"user_id", "source_type", "timestamp"}.issubset(set(idx))
          and len(byfile) > 0 and len(byemail) == 0 and not other_leaks)
    print("P1 VERIFY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
