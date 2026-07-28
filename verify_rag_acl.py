"""Verify per-user RAG isolation end-to-end: ingest a private doc as 'alice', then
prove alice sees it but 'bob' and an anonymous/org caller do NOT. Self-cleaning."""
import sys
from pathlib import Path
sys.path.insert(0, ".")

from backend.ingest import (get_client, ensure_collection, ingest_file,
                            search_corporate, _delete_source)

ALICE, BOB = "acltest-alice", "acltest-bob"
SECRET = "The alpha launch codename is ZEPHYR-9 and the sealed budget is 4.2 million dollars."
QUERY = "what is the secret launch codename and the sealed budget"


def main():
    tmp = Path("data_vault/_acltest")
    tmp.mkdir(parents=True, exist_ok=True)
    doc = tmp / "alice_secret.txt"
    doc.write_text(SECRET, encoding="utf-8")

    client = get_client()
    ensure_collection(client)
    n = ingest_file(client, doc, ALICE)
    print(f"ingested alice's private doc: {n} chunk(s) tagged owner={ALICE}")

    def _sees(owner):
        hits = search_corporate(QUERY, 3, owner=owner)
        return any("ZEPHYR" in (h.get("text") or "") for h in hits)

    a = _sees(ALICE)
    b = _sees(BOB)
    o = _sees(None)
    print(f"  alice sees her own doc : {a}   (expect True)")
    print(f"  bob   sees alice's doc : {b}   (expect False)")
    print(f"  anon  sees alice's doc : {o}   (expect False)")

    # cleanup
    _delete_source(client, "alice_secret.txt", ALICE)
    try:
        doc.unlink(); tmp.rmdir()
    except OSError:
        pass

    ok = a and not b and not o
    print("RAG per-user isolation:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
