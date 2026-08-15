"""`ArtifactStore` over the existing object store + `chat_artifacts` (Phase D).

`browser_tools` ships a filesystem store so it can be built and tested without
`backend/`. This is the adapter that replaces it in the wired path, installed once
by `browser_registration._install_artifact_store()`.

Bytes go to **SeaweedFS** through `backend.storage.object_store` — the same
authenticated S3 path every uploaded document takes. Metadata goes to
**`chat_artifacts`** — the same table charts and embeds use. Nothing new is built;
the point of the adapter is that a browser screenshot is not a special kind of
artifact.

## Why org_id is the whole point

`chat_artifacts.org_id` is **nullable** in the live schema. A screenshot of a filled
application form — name, phone, employer — written with a null org_id is the §5.2
defect in a worse place than the graph: the graph's unstamped nodes are seeded
reference data, this is a picture of one identifiable person's form. So `org_id` is
stamped from the resolved user on every row, and a row that cannot be stamped is not
written at all.

Reads assert **both** `org_id` and `user_id`. Asserting only `user_id` would be a
narrower check that happens to pass today and stops being sufficient the moment a
user id repeats across tenants.

## MISSING vs EXPIRED survives the move

The filesystem store keeps the manifest after the blob expires. Here the row is the
manifest: a row with no readable object is EXPIRED, no row at all is MISSING. Same
distinction, same reason — a reviewer told "not found" cannot tell whether the
evidence never existed or aged out.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from browser_tools.artifacts import ArtifactError, ArtifactRef, ArtifactState

log = logging.getLogger("aganeti.orchestrator.browser_artifacts")

#: Object-key variant for a browser screenshot. Fixed vocabulary — `build_key`
#: validates it, and it is never user input.
VARIANT = "browser-screenshot"

DEFAULT_TTL_S = 86_400   # 24h, matching the filesystem default


class SeaweedArtifactStore:
    """Implements `browser_tools.ArtifactStore`."""

    def __init__(self, ttl_s: int = DEFAULT_TTL_S):
        self.ttl_s = ttl_s
        # Constructed eagerly so a misconfiguration surfaces at registration rather
        # than on the first screenshot — but `get_storage()` opens no socket, so
        # this does not require SeaweedFS to be up at import.
        from backend.storage.object_store import get_storage
        self._storage = get_storage()

    # ── write ─────────────────────────────────────────────────────────────────
    def put(self, *, data: bytes, kind: str, content_type: str,
            tenant_id: str, user_id: str, meta: Optional[dict] = None,
            ttl_s: Optional[int] = None) -> ArtifactRef:
        import hashlib

        if not tenant_id or not user_id:
            raise ArtifactError(ArtifactState.DENIED,
                                "artifacts require tenant_id and user_id")

        from backend.chat import store as chat_store
        from backend.db import models as M
        from backend.db import sync as dbsync
        from backend.storage.object_store import SCOPE_PRIVATE, build_key

        now = time.time()
        expires_at = now + (ttl_s if ttl_s is not None else self.ttl_s)
        digest = hashlib.sha256(data).hexdigest()
        session_key = str((meta or {}).get("session_id") or "")

        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            if user is None:
                raise ArtifactError(ArtifactState.DENIED,
                                    f"no such user {user_id!r}; refusing to store unowned")
            if user.org_id is None:
                # The §5.2 rule, enforced rather than assumed. A user with no org
                # has no isolation boundary, so their screenshot has nowhere safe
                # to live.
                raise ArtifactError(ArtifactState.DENIED,
                                    "user has no org; refusing to write a null-org artifact")
            if str(user.org_id) != str(tenant_id):
                # The caller's claimed tenant must match the user's real one. A
                # mismatch means the ownership plumbing is wrong somewhere upstream,
                # and writing the row would launder it.
                raise ArtifactError(
                    ArtifactState.DENIED,
                    "tenant_id does not match the resolved user's organization")

            document_id = str(uuid.uuid4())
            key = build_key(scope=SCOPE_PRIVATE, owner_id=str(user.id),
                            document_id=document_id, variant=VARIANT)
            # Bytes first: a row pointing at an object that is not there is a
            # broken reference in an approval payload, which is worse than an
            # orphan object nobody reads.
            self._storage.put(key, data, content_type=content_type,
                              metadata={"kind": kind, "sha256": digest})

            sess = chat_store._ensure_session(s, user, session_key or document_id,
                                              source="browser")
            row = M.ChatArtifact(
                org_id=user.org_id,          # ← the point of this class
                user_id=user.id,
                session_id=sess.id,
                kind=kind,
                title=(meta or {}).get("title") or "browser screenshot",
                spec={"variant": VARIANT, "content_type": content_type,
                      "url": (meta or {}).get("url"),
                      "masked_fields": (meta or {}).get("masked_fields")},
                uri=key,
                byte_size=len(data),
                meta={"sha256": digest, "expires_at": expires_at,
                      "created_at": now, "browser": True},
            )
            s.add(row)
            s.commit()
            # Read every value we still need INSIDE the session. After the block
            # the ORM instances are detached and touching an attribute raises
            # DetachedInstanceError — which would turn a successful write into a
            # failed screenshot at the last line.
            artifact_id = str(row.id)
            org_id = str(user.org_id)

        log.info("browser artifact %s stored (%d bytes, org=%s)",
                 artifact_id, len(data), org_id)
        return ArtifactRef(
            artifact_id=artifact_id, kind=kind, content_type=content_type,
            byte_size=len(data), sha256=digest, created_at=now,
            expires_at=expires_at, tenant_id=str(tenant_id), user_id=str(user_id),
            meta=dict(meta or {}),
        )

    # ── read ──────────────────────────────────────────────────────────────────
    def _row(self, artifact_id: str, *, tenant_id: str, user_id: str):
        """Load the row and assert ownership. Returns (row, state)."""
        from backend.db import models as M
        from backend.db import sync as dbsync

        try:
            key = uuid.UUID(str(artifact_id))
        except (ValueError, TypeError):
            return None, ArtifactState.MISSING

        with dbsync.session() as s:
            row = s.get(M.ChatArtifact, key)
            if row is None:
                return None, ArtifactState.MISSING
            user = dbsync.resolve_user(s, user_id)
            # BOTH must match. user_id alone is a narrower check that happens to
            # pass today and stops being sufficient the moment an id repeats.
            if (user is None or row.user_id != user.id
                    or row.org_id is None or str(row.org_id) != str(tenant_id)):
                return None, ArtifactState.DENIED
            snapshot = {"uri": row.uri, "byte_size": row.byte_size,
                        "kind": row.kind, "meta": dict(row.meta or {}),
                        "spec": dict(row.spec or {})}
        return snapshot, ArtifactState.AVAILABLE

    def state(self, artifact_id: str, *, tenant_id: str, user_id: str) -> ArtifactState:
        row, st = self._row(artifact_id, tenant_id=tenant_id, user_id=user_id)
        if st is not ArtifactState.AVAILABLE:
            return st
        meta = row["meta"]
        if float(meta.get("expires_at") or 0) and time.time() >= float(meta["expires_at"]):
            # The ROW is the manifest, so an expired artifact is still
            # distinguishable from one that never existed.
            return ArtifactState.EXPIRED
        if not row["uri"]:
            return ArtifactState.EXPIRED
        try:
            if not self._storage.exists(row["uri"]):
                return ArtifactState.EXPIRED
        except Exception:  # noqa: BLE001 — a storage outage is not "never existed"
            log.warning("could not probe %s in object storage", row["uri"])
            return ArtifactState.EXPIRED
        return ArtifactState.AVAILABLE

    def head(self, artifact_id: str, *, tenant_id: str, user_id: str) -> ArtifactRef:
        row, st = self._row(artifact_id, tenant_id=tenant_id, user_id=user_id)
        if st is not ArtifactState.AVAILABLE:
            raise ArtifactError(st, f"artifact {artifact_id}: {st.value}")
        meta, spec = row["meta"], row["spec"]
        return ArtifactRef(
            artifact_id=str(artifact_id), kind=row["kind"],
            content_type=spec.get("content_type") or "application/octet-stream",
            byte_size=int(row["byte_size"] or 0), sha256=str(meta.get("sha256") or ""),
            created_at=float(meta.get("created_at") or 0),
            expires_at=float(meta.get("expires_at") or 0),
            tenant_id=str(tenant_id), user_id=str(user_id),
            meta={"url": spec.get("url"), "masked_fields": spec.get("masked_fields")},
        )

    def get(self, artifact_id: str, *, tenant_id: str, user_id: str) -> bytes:
        st = self.state(artifact_id, tenant_id=tenant_id, user_id=user_id)
        if st is not ArtifactState.AVAILABLE:
            raise ArtifactError(st, f"artifact {artifact_id}: {st.value}")
        row, _ = self._row(artifact_id, tenant_id=tenant_id, user_id=user_id)
        from backend.storage.object_store import key_is_owned_by, SCOPE_PRIVATE
        from backend.db import sync as dbsync

        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            owner = str(user.id) if user else ""
        # Second check at download time, against the KEY rather than the row: the
        # database is the authority on ownership, and this catches a row whose uri
        # was written for a different owner (object_store.py's own reasoning).
        if not key_is_owned_by(row["uri"], scope=SCOPE_PRIVATE, owner_id=owner):
            raise ArtifactError(ArtifactState.DENIED,
                                "stored object key does not belong to this user")
        return self._storage.get(row["uri"])
