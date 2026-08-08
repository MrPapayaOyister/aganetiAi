"""
Object storage for the platform.

    PostgreSQL  structured record + ownership (authoritative)
    SeaweedFS   the bytes

Import from here rather than reaching into the submodules, so the surface stays
small enough to reason about:

    from backend.storage import get_storage, store_document, fetch_document
"""
from .client import (S3Transport, StorageAuthError, StorageError, StorageNotFound,
                     StorageUnavailable, get_transport, sigv4_headers)
from .documents import (STATUS_FAILED, STATUS_ORPHANED, STATUS_PENDING, STATUS_STORED,
                        DocumentStorageError, StoredDocument, delete_document,
                        fetch_document, store_document)
from .object_store import (SCOPE_ORG, SCOPE_PRIVATE, InvalidObjectKey, ObjectMeta,
                           SeaweedFSStorage, StorageService, build_key, get_storage,
                           key_is_owned_by, sha256, validate_key)

__all__ = [
    "S3Transport", "StorageError", "StorageAuthError", "StorageNotFound",
    "StorageUnavailable", "get_transport", "sigv4_headers",
    "StorageService", "SeaweedFSStorage", "get_storage", "ObjectMeta",
    "build_key", "validate_key", "key_is_owned_by", "InvalidObjectKey",
    "SCOPE_ORG", "SCOPE_PRIVATE", "sha256",
    "store_document", "fetch_document", "delete_document", "StoredDocument",
    "DocumentStorageError", "STATUS_PENDING", "STATUS_STORED", "STATUS_ORPHANED",
    "STATUS_FAILED",
]
