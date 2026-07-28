"""Prove the legacy retrieve_corporate_context path is now ACL-safe (P0 leak close).
Ingest a private doc as 'alice'; alice retrieves it, bob and org(None) do NOT."""
import sys
from pathlib import Path
sys.path.insert(0, ".")

from backend.ingest import get_client, ensure_collection, ingest_file, _delete_source
from backend.main import retrieve_corporate_context

ALICE, BOB = "leaktest-alice", "leaktest-bob"
CODE = "NIGHTINGALE-77"


def main():
    tmp = Path("data_vault/_leaktest"); tmp.mkdir(parents=True, exist_ok=True)
    doc = tmp / "alice_leak.txt"
    doc.write_text(f"The classified leak-test codeword is {CODE}.", encoding="utf-8")
    client = get_client(); ensure_collection(client)
    ingest_file(client, doc, ALICE)

    q = "what is the classified leak-test codeword"
    a = CODE in (retrieve_corporate_context(q, owner=ALICE) or "")
    b = CODE in (retrieve_corporate_context(q, owner=BOB) or "")
    o = CODE in (retrieve_corporate_context(q, owner=None) or "")
    print(f"  alice (owner=alice): {a}  (expect True)")
    print(f"  bob   (owner=bob)  : {b}  (expect False)")
    print(f"  org   (owner=None) : {o}  (expect False)")

    _delete_source(client, "alice_leak.txt", ALICE)
    try:
        doc.unlink(); tmp.rmdir()
    except OSError:
        pass

    ok = a and not b and not o
    print("LEGACY LEAK CLOSED:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
