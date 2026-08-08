#!/usr/bin/env python3
"""
B3 verification: one controlled document through the full storage flow, plus a
read-only reconciliation report.

    python scripts/verify_b3_storage.py              # test document + reconcile
    python scripts/verify_b3_storage.py --reconcile  # reconciliation only

Scope discipline, because this runs against live infrastructure:

  * Creates exactly ONE document, titled `b3-verification-<ts>.txt`, and deletes
    it again — row and object — in a finally block.
  * Touches nothing in data_vault/, corporate_memory, Neo4j, or any pre-existing
    object. The reconciliation pass is read-only and deletes nothing, ever.
  * Uses a real user/org from the database because documents.user_id and
    .org_id are NOT NULL foreign keys; it creates no users and no orgs.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
PASS, FAIL = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}"
_res: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _res.append((ok, label))
    print(f"  {PASS if ok else FAIL} {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    return ok


async def _pick_identity() -> "tuple[str, str]":
    """A real (user_id, org_id). Both columns are NOT NULL with FKs, so the test
    must borrow an existing identity rather than invent one."""
    from backend.db.base import engine
    async with engine.connect() as c:
        row = (await c.execute(text(
            "SELECT u.id AS uid, o.id AS oid FROM users u "
            "JOIN organizations o ON TRUE ORDER BY u.created_at LIMIT 1"))).mappings().first()
    if not row:
        raise SystemExit("no users/organizations in the database — cannot run B3 test")
    return str(row["uid"]), str(row["oid"])


async def run_document_test() -> int:
    from backend.storage import (SCOPE_PRIVATE, STATUS_STORED, delete_document,
                                 fetch_document, get_storage, store_document)
    from backend.db.base import engine

    user_id, org_id = await _pick_identity()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"b3-verification-{stamp}.txt"
    body = (f"B3 storage verification\ncreated={stamp}\n"
            f"This object is created and deleted by scripts/verify_b3_storage.py\n").encode()

    print(f"\n{DIM}identity{RESET}")
    print(f"  user={user_id}  org={org_id}")
    print(f"\n{DIM}upload flow{RESET}")

    doc = None
    try:
        t0 = time.monotonic()
        doc = await store_document(data=body, filename=filename, user_id=user_id,
                                   org_id=org_id, scope=SCOPE_PRIVATE,
                                   content_type="text/plain")
        up_ms = round((time.monotonic() - t0) * 1000, 1)
        check(True, "1. upload succeeded", f"{up_ms}ms  doc={doc.document_id}")
        check(doc.status == STATUS_STORED, "   status is 'stored'", doc.status)

        # 2 + 3. the database row
        async with engine.connect() as c:
            row = (await c.execute(text(
                "SELECT id, user_id, org_id, title, uri, sensitivity, content_hash, meta "
                "FROM documents WHERE id = CAST(:i AS uuid)"),
                {"i": doc.document_id})).mappings().first()
        check(row is not None, "2. DB row created")
        check(bool(row and row["uri"]), "3. Document.uri populated", row["uri"] if row else "")
        m = dict(row["meta"] or {})
        check(m.get("status") == STATUS_STORED, "   meta.status == stored", str(m.get("status")))
        check(row["sensitivity"] == "private", "   sensitivity == private (ACL scope)",
              row["sensitivity"])

        key = row["uri"].split("/", 3)[-1]
        check(key.startswith(f"users/{user_id}/"),
              "   object key sits under the owner's prefix", key)
        check(filename not in key,
              "   filename is NOT part of the object key", f"title={filename}")

        # 4 + 5. the object itself
        storage = get_storage()
        check(storage.exists(key), "4. object exists in SeaweedFS")
        meta = storage.head(key)
        check(meta.size == len(body) and meta.content_type.startswith("text/plain"),
              "5. object metadata correct",
              f"{meta.size} bytes, {meta.content_type}, etag={meta.etag}")
        check(meta.metadata.get("document-id") == doc.document_id,
              "   custom metadata round-tripped", f"document-id={meta.metadata.get('document-id')}")

        # 6 + 8. download and byte-identity
        t0 = time.monotonic()
        data, info = await fetch_document(doc.document_id, requester_user_id=user_id,
                                          requester_org_id=org_id)
        dn_ms = round((time.monotonic() - t0) * 1000, 1)
        check(True, "6. download works", f"{dn_ms}ms")
        check(data == body, "8. content is byte-identical",
              f"{len(data)} bytes, sha match")

        # 7. authorization — another user must be refused
        other = "00000000-0000-4000-8000-000000000000"
        try:
            await fetch_document(doc.document_id, requester_user_id=other,
                                 requester_org_id=org_id)
            check(False, "7. another user is denied", "a foreign user could read it")
        except PermissionError:
            check(True, "7. another user is denied", "PermissionError as expected")
        except Exception as e:  # noqa: BLE001
            check(False, "7. another user is denied", f"unexpected {type(e).__name__}: {e}")

    finally:
        # 9 + 10. delete and confirm no orphan
        if doc is not None:
            print(f"\n{DIM}cleanup{RESET}")
            key = f"users/{user_id}/documents/{doc.document_id}/original"
            try:
                r = await delete_document(doc.document_id, requester_user_id=user_id,
                                          hard=True)
                check(r["object_removed"], "9. delete works", str(r))
            except Exception as e:  # noqa: BLE001
                check(False, "9. delete works", str(e)[:120])
            try:
                from backend.storage import get_storage as gs
                check(not gs().exists(key), "10. no orphan object remains")
            except Exception as e:  # noqa: BLE001
                check(False, "10. no orphan object remains", str(e)[:100])
            async with engine.connect() as c:
                n = (await c.execute(text(
                    "SELECT count(*) FROM documents WHERE id = CAST(:i AS uuid)"),
                    {"i": doc.document_id})).scalar()
            check(n == 0, "    no orphan DB row remains", f"rows={n}")
    return 0


async def reconcile() -> int:
    """Read-only. Reports drift between the database and the object store.

    Deletes nothing and repairs nothing — a reconciliation tool that mutates is
    how you turn a reporting bug into data loss.
    """
    from backend.storage import get_storage, validate_key, InvalidObjectKey
    from backend.db.base import engine

    print(f"\n{DIM}reconciliation (read-only){RESET}")
    async with engine.connect() as c:
        rows = (await c.execute(text(
            "SELECT id, user_id, org_id, uri, sensitivity, meta FROM documents "
            "WHERE deleted_at IS NULL"))).mappings().all()

    storage = get_storage()
    try:
        keys = set(storage.list_keys(limit=1000))
    except Exception as e:  # noqa: BLE001
        print(f"  {FAIL} cannot list bucket: {str(e)[:120]}")
        return 1

    db_keys, missing, malformed, mismatched = set(), [], [], []
    for r in rows:
        uri = r["uri"] or ""
        meta = dict(r["meta"] or {})
        if not uri:
            if meta.get("status") in ("pending", "failed"):
                continue                      # expected: never claimed to be stored
            malformed.append((str(r["id"]), "no uri but status=" + str(meta.get("status"))))
            continue
        if not uri.startswith("s3://"):
            malformed.append((str(r["id"]), f"uri is not s3://: {uri[:40]}"))
            continue
        key = uri.split("/", 3)[-1]
        try:
            validate_key(key)
        except InvalidObjectKey as e:
            malformed.append((str(r["id"]), str(e)[:70])); continue
        db_keys.add(key)
        if key not in keys:
            missing.append((str(r["id"]), key))
        scope = meta.get("scope")
        owner = str(r["org_id"]) if scope == "org" else str(r["user_id"])
        prefix = f"{'org' if scope == 'org' else 'users'}/{owner}/"
        if not key.startswith(prefix):
            mismatched.append((str(r["id"]), key))

    orphans = sorted(k for k in keys if k not in db_keys)
    print(f"  documents (not deleted) : {len(rows)}")
    print(f"  objects in bucket       : {len(keys)}")
    check(not missing, "no DB documents with a missing object", f"{len(missing)} missing")
    for i, k in missing[:5]:
        print(f"      {DIM}{i}  ->  {k}{RESET}")
    check(not malformed, "no malformed URIs", f"{len(malformed)}")
    for i, why in malformed[:5]:
        print(f"      {DIM}{i}: {why}{RESET}")
    check(not mismatched, "no ownership mismatches", f"{len(mismatched)}")
    for i, k in mismatched[:5]:
        print(f"      {DIM}{i}  ->  {k}{RESET}")
    if orphans:
        print(f"  {DIM}objects with no DB record: {len(orphans)} "
              f"(reported only — nothing deleted){RESET}")
        for k in orphans[:8]:
            print(f"      {DIM}{k}{RESET}")
    else:
        check(True, "no objects without a DB record")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="B3 storage verification")
    ap.add_argument("--reconcile", action="store_true", help="reconciliation only")
    args = ap.parse_args()

    async def go() -> int:
        if not args.reconcile:
            await run_document_test()
        await reconcile()
        return 0

    asyncio.run(go())
    failed = [l for ok, l in _res if not ok]
    print(f"\n  {len(_res) - len(failed)}/{len(_res)} checks passed")
    for l in failed:
        print(f"    {FAIL} {l}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
