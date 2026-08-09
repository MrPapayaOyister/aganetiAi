"""
Document storage flow — Postgres owns the record, SeaweedFS owns the bytes.

    upload → validate → mint identity → write object → verify → write uri → return

The ordering is the whole design. Two orderings are wrong in ways that matter:

  * Row first, then object — a crash between them leaves a row claiming storage
    that does not exist. Every later reader trusts the row and fails.
  * Object first with no record of intent — a crash leaves an orphan nobody can
    find, because the only pointer to it was the row that never got written.

So: the row is created FIRST in status `pending` with no uri, the object is
written, the object is verified by reading its metadata back, and only then is
the row promoted to `stored` with the uri. Every failure mode leaves a row whose
status says exactly what happened, which is what makes reconciliation possible:

    pending   intent recorded; object not confirmed. Crash here = harmless,
              the row is a to-do item.
    stored    object verified present. The only state a reader will serve.
    orphaned  object was written but the row could not be updated. The bytes
              exist and the key is recorded so they can be reclaimed — the case
              the spec calls out as "do not silently lose track of the object".
    failed    object write failed. No misleading "stored" record exists.

This is deliberately not a distributed transaction. It is a state machine with
an honest intermediate state, which is the right amount of machinery for one
object and one row.

`documents` has no status column, so status lives in the existing `meta` JSONB.
Adding a column would be a migration this phase does not need.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from .object_store import (SCOPE_ORG, SCOPE_PRIVATE, ObjectMeta, StorageError,
                           build_key, get_storage, key_is_owned_by, sha256,
                           validate_key)

log = logging.getLogger("aria.storage.documents")

STATUS_PENDING = "pending"
STATUS_STORED = "stored"
STATUS_ORPHANED = "orphaned"
STATUS_FAILED = "failed"

# scope → the `sensitivity` value the existing column already uses.
# 'internal' is the table default and means org-wide; 'private' is one user's.
SENSITIVITY = {SCOPE_ORG: "internal", SCOPE_PRIVATE: "private"}


class DocumentStorageError(RuntimeError):
    pass


class DuplicateDocument(DocumentStorageError):
    """This user already has a document with identical content.

    Enforced by `uq_document_user_hash UNIQUE (user_id, content_hash)`. Raised
    instead of letting asyncpg's UniqueViolation surface, which leaked SQL and
    the constraint name to the caller. Carries the EXISTING document_id so the
    caller can return something useful rather than an error.

    Uniqueness is per user by design: two people may legitimately hold the same
    file, and each gets their own row and their own object.
    """

    def __init__(self, document_id: str, content_hash: str) -> None:
        super().__init__(f"document with identical content already exists: {document_id}")
        self.document_id = document_id
        self.content_hash = content_hash


@dataclass(slots=True)
class StoredDocument:
    document_id: str
    uri: str
    scope: str
    user_id: str
    org_id: str
    title: str
    content_type: str
    size: int
    content_hash: str
    status: str

    def as_dict(self) -> dict:
        return {"document_id": self.document_id, "uri": self.uri, "scope": self.scope,
                "user_id": self.user_id, "org_id": self.org_id, "title": self.title,
                "content_type": self.content_type, "size": self.size,
                "content_hash": self.content_hash, "status": self.status}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _exec(sql: str, params: dict) -> Any:
    from backend.db.base import engine
    async with engine.begin() as conn:
        return await conn.execute(text(sql), params)


async def store_document(*, data: bytes, filename: str, user_id: str, org_id: str,
                         scope: str = SCOPE_PRIVATE,
                         content_type: str = "application/octet-stream",
                         source_type: str = "file") -> StoredDocument:
    """Store bytes and record them. See the module docstring for the ordering.

    `filename` is used ONLY as a title and as object metadata. It never becomes
    part of the object key — see object_store.build_key.
    """
    if scope not in (SCOPE_ORG, SCOPE_PRIVATE):
        raise DocumentStorageError(f"unknown scope {scope!r}")
    if not data:
        raise DocumentStorageError("refusing to store an empty object")

    document_id = str(uuid.uuid4())
    owner_id = org_id if scope == SCOPE_ORG else user_id
    key = build_key(scope=scope, owner_id=owner_id, document_id=document_id)
    digest = sha256(data)
    # Title is what the user called it; the key is what we call it. Kept apart on
    # purpose so a hostile filename cannot influence addressing.
    title = (filename or "untitled")[:255]

    # Reject an exact re-upload BEFORE writing an object. Checking first avoids
    # storing bytes we would then have to orphan; the try/except below still
    # catches the race where two uploads of the same content arrive together.
    existing = await _find_by_hash(user_id, digest)
    if existing:
        raise DuplicateDocument(existing, digest)

    # 1. record the INTENT first, so a crash mid-write is discoverable.
    try:
        await _exec("""
            INSERT INTO documents (id, org_id, user_id, source_type, source_id, title,
                                   content_hash, sensitivity, meta)
            VALUES (CAST(:id AS uuid), CAST(:org AS uuid), CAST(:usr AS uuid),
                    :stype, :sid, :title, :hash, :sens, CAST(:meta AS jsonb))
        """, {"id": document_id, "org": org_id, "usr": user_id, "stype": source_type,
              "sid": title, "title": title, "hash": digest,
              "sens": SENSITIVITY[scope],
              "meta": _json({"status": STATUS_PENDING, "scope": scope,
                             "object_key": key, "content_type": content_type,
                             "size": len(data), "filename": title,
                             "created_by_phase": "B3", "recorded_at": _now()})})
    except Exception as e:  # noqa: BLE001
        # Race: another upload of identical content won the INSERT between the
        # pre-check and here. Report it as a duplicate, not as a 500.
        if "uq_document_user_hash" in str(e) or "UniqueViolation" in type(e).__name__:
            dup = await _find_by_hash(user_id, digest)
            raise DuplicateDocument(dup or "", digest) from None
        raise

    storage = get_storage()
    meta: Optional[ObjectMeta] = None
    try:
        # 2. write, then 3. VERIFY by reading metadata back. A PUT that returned
        #    200 is not proof the object is retrievable.
        storage.put(key, data, content_type=content_type,
                    metadata={"document-id": document_id, "filename": title,
                              "owner": user_id, "scope": scope})
        meta = storage.head(key)
        if meta.size != len(data):
            raise DocumentStorageError(
                f"verification failed: stored {meta.size} bytes, expected {len(data)}")
    except Exception as e:  # noqa: BLE001
        await _mark(document_id, STATUS_FAILED, error=str(e)[:300])
        raise DocumentStorageError(f"object write failed, document marked failed: {e}") from e

    # 4. promote to stored. If THIS fails the bytes exist but the row does not
    #    point at them — mark it orphaned rather than losing the key.
    uri = f"s3://{storage.bucket}/{key}"
    try:
        await _exec("""
            UPDATE documents
               SET uri = :uri,
                   meta = meta || CAST(:patch AS jsonb),
                   updated_at = now()
             WHERE id = CAST(:id AS uuid)
        """, {"uri": uri, "id": document_id,
              "patch": _json({"status": STATUS_STORED, "etag": meta.etag,
                              "verified_size": meta.size, "stored_at": _now()})})
    except Exception as e:  # noqa: BLE001
        log.error("document %s: object stored at %s but DB update failed — ORPHAN",
                  document_id, key)
        try:
            await _mark(document_id, STATUS_ORPHANED, object_key=key, error=str(e)[:300])
        except Exception:  # noqa: BLE001 — the orphan is already logged with its key
            pass
        raise DocumentStorageError(
            f"object stored at {key} but the record could not be updated; "
            f"document {document_id} is marked orphaned for reconciliation") from e

    return StoredDocument(document_id=document_id, uri=uri, scope=scope,
                          user_id=user_id, org_id=org_id, title=title,
                          content_type=content_type, size=len(data),
                          content_hash=digest, status=STATUS_STORED)


async def fetch_document(document_id: str, *, requester_user_id: str,
                         requester_org_id: Optional[str] = None) -> "tuple[bytes, dict]":
    """Authorize, then resolve the key from the DATABASE and stream the bytes.

    The client supplies a document id, never an object key — so no request can
    address an object outside what the database says it may see.

    Authorization, in order:
      * private  → only the owning user
      * org      → any user in the same organization
    Both are checked against the ROW, not against the key. The key is then
    re-validated against the owner as defence in depth.
    """
    from backend.db.base import engine
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT id, org_id, user_id, title, uri, sensitivity, meta
              FROM documents
             WHERE id = CAST(:id AS uuid) AND deleted_at IS NULL
        """), {"id": document_id})).mappings().first()

    if not row:
        raise DocumentStorageError("document not found")
    meta = dict(row["meta"] or {})
    if meta.get("status") != STATUS_STORED:
        raise DocumentStorageError(
            f"document is not retrievable (status={meta.get('status')!r})")

    scope = meta.get("scope") or (SCOPE_ORG if row["sensitivity"] != "private"
                                  else SCOPE_PRIVATE)
    owner_user = str(row["user_id"])
    owner_org = str(row["org_id"])

    if scope == SCOPE_PRIVATE:
        if str(requester_user_id) != owner_user:
            raise PermissionError("not authorized for this private document")
    else:
        if requester_org_id is not None and str(requester_org_id) != owner_org:
            raise PermissionError("not authorized for this organization's document")

    uri = row["uri"] or ""
    key = uri.split("/", 3)[-1] if uri.startswith("s3://") else ""
    validate_key(key)
    if not key_is_owned_by(key, scope=scope,
                           owner_id=owner_org if scope == SCOPE_ORG else owner_user):
        # The row and the key disagree about who owns this. Refuse rather than
        # serve — reconciliation exists to surface exactly this.
        raise PermissionError("object key does not match the document's owner")

    data = get_storage().get(key)
    return data, {"document_id": str(row["id"]), "title": row["title"],
                  "content_type": meta.get("content_type", "application/octet-stream"),
                  "size": len(data), "scope": scope, "uri": uri}


async def delete_document(document_id: str, *, requester_user_id: str,
                          hard: bool = False) -> dict:
    """Soft-delete the row and remove the object. Owner only."""
    from backend.db.base import engine
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT user_id, uri, meta FROM documents WHERE id = CAST(:id AS uuid)"),
            {"id": document_id})).mappings().first()
    if not row:
        raise DocumentStorageError("document not found")
    if str(row["user_id"]) != str(requester_user_id):
        raise PermissionError("only the owner may delete this document")

    uri = row["uri"] or ""
    removed = False
    if uri.startswith("s3://"):
        key = uri.split("/", 3)[-1]
        removed = get_storage().delete(validate_key(key))

    if hard:
        await _exec("DELETE FROM documents WHERE id = CAST(:id AS uuid)",
                    {"id": document_id})
    else:
        await _exec("""UPDATE documents SET deleted_at = now(),
                          meta = meta || CAST(:patch AS jsonb) WHERE id = CAST(:id AS uuid)""",
                    {"id": document_id,
                     "patch": _json({"status": "deleted", "deleted_at": _now()})})
    return {"document_id": document_id, "object_removed": removed, "hard": hard}


async def _mark(document_id: str, status: str, **extra: Any) -> None:
    await _exec("""UPDATE documents SET meta = meta || CAST(:patch AS jsonb),
                      updated_at = now() WHERE id = CAST(:id AS uuid)""",
                {"id": document_id, "patch": _json({"status": status, **extra})})


def _json(obj: dict) -> str:
    import json
    return json.dumps(obj)


async def _find_by_hash(user_id: str, content_hash: str) -> Optional[str]:
    """The user's existing document with this exact content, if any."""
    from backend.db.base import engine
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT id FROM documents WHERE user_id = CAST(:u AS uuid) "
            "AND content_hash = :h AND deleted_at IS NULL LIMIT 1"),
            {"u": str(user_id), "h": content_hash})).first()
    return str(row[0]) if row else None
