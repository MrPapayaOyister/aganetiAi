"""Durable storage for screenshots (requirement 5), and the seam to replace it.

## What the repo already has, and why this file exists anyway

**A production object store exists**: `backend/storage/object_store.py` —
`SeaweedFSStorage`, S3 SigV4 against the authenticated SeaweedFS gateway, with a
`StorageService` Protocol (`put/get/head/delete/exists`), a generated-key scheme
(`build_key`), traversal validation, and four live containers. There is also a
`chat_artifacts` table (`backend/db/models.py`) carrying `org_id`, `user_id`,
`session_id`, `uri`, `byte_size` and `meta` — exactly the durable metadata an
approval payload needs.

**This package may not import it.** The brief forbids importing from `backend/`,
and it is right to: browser_tools must be loadable without dragging the
orchestrator, its database clients, or its settings into the process — which is
also what keeps Phase D's wiring a decision rather than an accident.

So this file defines the *contract* and ships the smallest durable implementation
that satisfies it. `ArtifactStore` is deliberately shaped like the existing
`StorageService` so the Phase D adapter is a thin class, not a redesign:

    class SeaweedArtifactStore:                 # lives in backend/, Phase D
        def put(...): key = build_key(...); get_storage().put(key, png, ...)

**Two properties the worker's in-memory store does not have**, both required here:

1. **Survives a worker restart.** §8.2(c)'s approval payload is reviewed by a human
   minutes to hours after capture; a process-memory reference is gone by then, and
   the reviewer sees a broken image at the moment they are deciding whether to
   submit a form.
2. **Missing is distinguishable from expired.** A reviewer told "not found" cannot
   tell whether the evidence never existed (a bug, or a payload referencing
   something that was never captured) or has aged out (expected, re-capture). Those
   need different responses, so the manifest outlives the bytes.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger("browser_tools.artifacts")

DEFAULT_TTL_S = int(os.getenv("BROWSER_ARTIFACT_TTL_S", "86400"))       # 24h
DEFAULT_ROOT = os.getenv(
    "BROWSER_ARTIFACT_DIR",
    str(Path(__file__).resolve().parent.parent / "temp" / "browser_artifacts"))


class ArtifactState(str, Enum):
    """Why a fetch did not return bytes. The distinction IS the feature."""

    AVAILABLE = "available"
    EXPIRED = "expired"      # we recorded it; the bytes have aged out
    MISSING = "missing"      # we have no record of it ever existing
    DENIED = "denied"        # exists, belongs to someone else


class ArtifactError(Exception):
    def __init__(self, state: ArtifactState, message: str):
        self.state = state
        super().__init__(message)


@dataclass(frozen=True)
class ArtifactRef:
    """What a tool result carries. A reference and provenance — never bytes."""

    artifact_id: str
    kind: str
    content_type: str
    byte_size: int
    sha256: str
    created_at: float
    expires_at: float
    tenant_id: str
    user_id: str
    #: Free-form, redacted. For a screenshot: url, masked field count, page title.
    meta: dict = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def as_dict(self) -> dict:
        d = asdict(self)
        d["expired"] = self.expired
        return d


class ArtifactStore(Protocol):
    """The contract Phase D replaces. Shaped like `backend.storage.StorageService`
    so the adapter is mechanical."""

    def put(self, *, data: bytes, kind: str, content_type: str,
            tenant_id: str, user_id: str, meta: Optional[dict] = None,
            ttl_s: Optional[int] = None) -> ArtifactRef: ...

    def head(self, artifact_id: str, *, tenant_id: str, user_id: str) -> ArtifactRef: ...

    def get(self, artifact_id: str, *, tenant_id: str, user_id: str) -> bytes: ...

    def state(self, artifact_id: str, *, tenant_id: str, user_id: str) -> ArtifactState: ...


class FilesystemArtifactStore:
    """The smallest durable store that satisfies the contract.

    Layout, one directory per artifact:

        <root>/<artifact_id>/manifest.json     ← outlives the bytes, deliberately
        <root>/<artifact_id>/blob             ← deleted on expiry

    **The manifest is never deleted on expiry.** That is the whole mechanism behind
    MISSING-vs-EXPIRED: bytes gone + manifest present = EXPIRED; nothing at all =
    MISSING. A store that deleted the directory wholesale would make an aged-out
    screenshot indistinguishable from one that was never taken, which is precisely
    the ambiguity requirement 5 forbids.

    Not a production store: no replication, no quota, local disk only. It is
    durable in the sense that matters here — it survives a worker or API restart —
    and it exists so the tool layer can be finished and tested before Phase D
    decides where bytes really live.
    """

    def __init__(self, root: Optional[str | Path] = None, *, ttl_s: int = DEFAULT_TTL_S):
        self.root = Path(root or DEFAULT_ROOT)
        self.ttl_s = ttl_s
        self.root.mkdir(parents=True, exist_ok=True)

    # ── paths ─────────────────────────────────────────────────────────────────
    def _dir(self, artifact_id: str) -> Path:
        # The id is generated here and never client-supplied, but validate anyway:
        # a path built from an unchecked identifier is a traversal primitive, and
        # this store will later be handed ids that came back from a database row.
        if not artifact_id or not artifact_id.replace("_", "").replace("-", "").isalnum():
            raise ArtifactError(ArtifactState.MISSING, "malformed artifact id")
        return self.root / artifact_id

    # ── write ─────────────────────────────────────────────────────────────────
    def put(self, *, data: bytes, kind: str, content_type: str,
            tenant_id: str, user_id: str, meta: Optional[dict] = None,
            ttl_s: Optional[int] = None) -> ArtifactRef:
        import hashlib

        if not tenant_id or not user_id:
            # An unowned artifact is one no ownership check can ever fail against.
            raise ArtifactError(ArtifactState.DENIED,
                                "artifacts require tenant_id and user_id")
        now = time.time()
        artifact_id = "art_" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")
        ref = ArtifactRef(
            artifact_id=artifact_id, kind=kind, content_type=content_type,
            byte_size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            created_at=now, expires_at=now + (ttl_s if ttl_s is not None else self.ttl_s),
            tenant_id=tenant_id, user_id=user_id, meta=dict(meta or {}),
        )
        d = self._dir(artifact_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "blob").write_bytes(data)
        # Manifest written LAST: a crash between the two leaves an orphan blob (a
        # reaper problem) rather than a manifest promising bytes that never landed
        # (a broken reference in an approval payload).
        (d / "manifest.json").write_text(json.dumps(ref.as_dict()), encoding="utf-8")
        log.info("artifact stored %s kind=%s bytes=%d", artifact_id, kind, len(data))
        return ref

    # ── read ──────────────────────────────────────────────────────────────────
    def _manifest(self, artifact_id: str) -> Optional[ArtifactRef]:
        try:
            raw = (self._dir(artifact_id) / "manifest.json").read_text(encoding="utf-8")
        except (OSError, ArtifactError):
            return None
        try:
            d = json.loads(raw)
        except ValueError:
            return None
        d.pop("expired", None)
        try:
            return ArtifactRef(**d)
        except TypeError:
            return None

    def state(self, artifact_id: str, *, tenant_id: str, user_id: str) -> ArtifactState:
        ref = self._manifest(artifact_id)
        if ref is None:
            return ArtifactState.MISSING
        if ref.tenant_id != tenant_id or ref.user_id != user_id:
            return ArtifactState.DENIED
        if ref.expired or not (self._dir(artifact_id) / "blob").exists():
            return ArtifactState.EXPIRED
        return ArtifactState.AVAILABLE

    def head(self, artifact_id: str, *, tenant_id: str, user_id: str) -> ArtifactRef:
        st = self.state(artifact_id, tenant_id=tenant_id, user_id=user_id)
        if st is ArtifactState.MISSING:
            raise ArtifactError(st, f"no record of artifact {artifact_id}")
        if st is ArtifactState.DENIED:
            # Same message shape as MISSING would be wrong here: the CALLER of head
            # is the tool layer, not the model, and it needs the distinction to
            # decide whether to log a security event. What reaches the model is
            # flattened by the tool layer.
            raise ArtifactError(st, "artifact belongs to another identity")
        ref = self._manifest(artifact_id)
        assert ref is not None
        return ref

    def get(self, artifact_id: str, *, tenant_id: str, user_id: str) -> bytes:
        st = self.state(artifact_id, tenant_id=tenant_id, user_id=user_id)
        if st is not ArtifactState.AVAILABLE:
            raise ArtifactError(
                st,
                {ArtifactState.MISSING: f"no record of artifact {artifact_id}",
                 ArtifactState.EXPIRED: f"artifact {artifact_id} has expired",
                 ArtifactState.DENIED: "artifact belongs to another identity"}[st])
        return (self._dir(artifact_id) / "blob").read_bytes()

    # ── maintenance ───────────────────────────────────────────────────────────
    def reap(self, now: Optional[float] = None) -> int:
        """Delete expired BLOBS, keep manifests. Returns how many blobs went.

        Keeping the manifest is the point (see the class docstring). A separate,
        much longer sweep would eventually remove manifests too; that policy belongs
        with whoever owns retention, not here.
        """
        now = time.time() if now is None else now
        n = 0
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            ref = self._manifest(d.name)
            blob = d / "blob"
            if ref is not None and now >= ref.expires_at and blob.exists():
                try:
                    blob.unlink()
                    n += 1
                except OSError:
                    log.exception("could not reap %s", d.name)
        return n


_default: Optional[FilesystemArtifactStore] = None


def get_artifact_store() -> ArtifactStore:
    """Process-wide default. Phase D replaces this with `set_artifact_store()`
    rather than editing any tool."""
    global _default
    if _default is None:
        _default = FilesystemArtifactStore()
    return _default


def set_artifact_store(store: ArtifactStore) -> None:
    """The injection point. Phase D calls this once with a SeaweedFS/chat_artifacts
    -backed implementation; no tool changes."""
    global _default
    _default = store  # type: ignore[assignment]
