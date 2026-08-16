#!/usr/bin/env python3
"""Stamp the shared tenant onto unstamped `corporate_memory` chunks.

WHY THIS EXISTS. `org_id` has been a payload key since P1, but the drop-folder
path (`ingest_all` -> `ingest_file`) never set it, so almost the whole corpus
carries no tenant. A tenant filter turned on before this runs returns nothing and
raises no error — the failure mode is a silently empty context, which reads as
"the assistant forgot everything" rather than as a bug.

WHAT IT STAMPS, AND WHAT IT DELIBERATELY DOES NOT.

Only chunks whose `user_id` is the ORG_OWNER sentinel are stamped. Those ARE the
shared corpus — CLI/drop-folder ingests with no owner — so `org_id = __shared__`
is a statement of fact about them.

Everything else with a missing tenant is left alone and REPORTED. On the live
corpus that is one chunk: a private per-user upload (`user_id='user_1'`, a scanned
business licence) that predates tenancy. Stamping it shared would publish one
person's document to every tenant — the exact leak this whole change exists to
prevent — so a blanket "NULL -> shared" backfill is not safe, however tempting the
99.9% hit rate looks. Those chunks stay reachable through the unchanged `user_id`
ACL and are excluded only if strict mode is later enabled, which is the correct
conservative outcome for a document whose tenant genuinely is not known.

USAGE
    python scripts/backfill_qdrant_tenant.py              # dry run (default)
    python scripts/backfill_qdrant_tenant.py --apply      # write
    python scripts/backfill_qdrant_tenant.py --verify     # report only

Idempotent: re-running stamps nothing, because stamped chunks no longer match.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qdrant_client import QdrantClient                                   # noqa: E402
from qdrant_client.models import (                                       # noqa: E402
    Filter, FieldCondition, MatchValue, IsEmptyCondition, PayloadField,
)

from backend.ingest import ORG_OWNER, SHARED_TENANT                      # noqa: E402
from config.settings import QDRANT_URL, RAG_COLLECTION                   # noqa: E402

BATCH = 512


def _client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, timeout=60)


#: Unstamped = org_id null OR the key absent. `IsEmpty` covers both; `IsNull`
#: covers only the first and on this corpus the two differ by nine chunks.
UNSTAMPED = IsEmptyCondition(is_empty=PayloadField(key="org_id"))

SHARED_UNSTAMPED = Filter(must=[
    UNSTAMPED,
    FieldCondition(key="user_id", match=MatchValue(value=ORG_OWNER)),
])
UNSTAMPED_ANY = Filter(must=[UNSTAMPED])


def survey(c: QdrantClient) -> dict:
    total = c.count(RAG_COLLECTION, exact=True).count
    unstamped = c.count(RAG_COLLECTION, count_filter=UNSTAMPED_ANY, exact=True).count
    shared = c.count(RAG_COLLECTION, count_filter=SHARED_UNSTAMPED, exact=True).count
    return {"total": total, "unstamped": unstamped,
            "shared_unstamped": shared, "other_unstamped": unstamped - shared}


def orphans(c: QdrantClient) -> Counter:
    """Unstamped chunks that are NOT the shared corpus, grouped by owner.

    These are the ones a naive backfill would leak. Printed so the decision about
    them is made by a person, with the filenames in front of them.
    """
    out: Counter = Counter()
    offset = None
    while True:
        pts, offset = c.scroll(RAG_COLLECTION, scroll_filter=UNSTAMPED_ANY,
                               limit=BATCH, offset=offset, with_payload=True)
        for p in pts:
            uid = (p.payload or {}).get("user_id")
            if uid != ORG_OWNER:
                out[f"{uid} :: {(p.payload or {}).get('source', '?')}"] += 1
        if offset is None:
            break
    return out


def apply(c: QdrantClient) -> int:
    """Set org_id=SHARED_TENANT on the shared, unstamped chunks."""
    n = c.count(RAG_COLLECTION, count_filter=SHARED_UNSTAMPED, exact=True).count
    if not n:
        return 0
    c.set_payload(collection_name=RAG_COLLECTION,
                  payload={"org_id": SHARED_TENANT},
                  points=SHARED_UNSTAMPED, wait=True)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--verify", action="store_true", help="report only, never write")
    args = ap.parse_args()

    c = _client()
    if not c.collection_exists(RAG_COLLECTION):
        print(f"collection {RAG_COLLECTION!r} does not exist")
        return 1

    before = survey(c)
    print(f"collection      : {RAG_COLLECTION}")
    print(f"total chunks    : {before['total']}")
    print(f"unstamped       : {before['unstamped']}")
    print(f"  shared corpus : {before['shared_unstamped']}  -> org_id={SHARED_TENANT!r}")
    print(f"  NOT shared    : {before['other_unstamped']}  -> LEFT ALONE (see below)")

    orph = orphans(c)
    if orph:
        print("\nunstamped chunks that are NOT the shared corpus:")
        for k, n in orph.most_common(20):
            print(f"  {n:5d}  {k}")
        print("  These have a real owner and an unknown tenant. They are NOT stamped:\n"
              "  publishing them to __shared__ would expose one user's documents to\n"
              "  every tenant. Resolve their owner's org and stamp them explicitly, or\n"
              "  leave them — the user_id ACL still scopes them to their owner.")

    if args.verify:
        return 0
    if not args.apply:
        print(f"\nDRY RUN — would stamp {before['shared_unstamped']} chunk(s). "
              f"Re-run with --apply to write.")
        return 0

    n = apply(c)
    after = survey(c)
    print(f"\nstamped         : {n}")
    print(f"unstamped now   : {after['unstamped']}  "
          f"(shared={after['shared_unstamped']}, other={after['other_unstamped']})")
    if after["shared_unstamped"]:
        print("WARNING: shared chunks remain unstamped — re-run.")
        return 1
    print("\nShared corpus is stamped. Strict mode is still NOT safe to enable while\n"
          f"{after['other_unstamped']} owner-scoped chunk(s) carry no tenant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
