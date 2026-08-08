"""
S3 transport for SeaweedFS — signing and raw requests, nothing higher-level.

This is the ONLY place in the project that computes an AWS SigV4 signature, so
there is one implementation to review rather than several that drift. The
observability probe and the verification scripts import from here.

Why hand-rolled instead of boto3: the project has no S3 SDK installed, and the
whole of SeaweedFS's S3 surface that this platform needs is PUT/GET/HEAD/DELETE
plus a bucket listing. SigV4 is ~40 lines of stdlib hmac. Adding boto3 (and
botocore, and its transitive tree) to get four verbs is a poor trade on a host
where model weights already dominate the disk.

Credentials come from config.settings, which reads .env. Nothing in this module
logs, prints, returns or embeds a key — `_redact` exists to keep it that way when
errors are surfaced.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("aria.storage.client")


class StorageError(RuntimeError):
    """Base for every storage failure."""


class StorageUnavailable(StorageError):
    """The endpoint did not answer. Retrying later may succeed."""


class StorageAuthError(StorageError):
    """The gateway rejected a SIGNED request — credentials or bucket ACL are wrong.
    Distinct from StorageUnavailable because retrying will not help."""


class StorageNotFound(StorageError):
    """The object does not exist."""


def _redact(text: str, *secrets: str) -> str:
    """Strip credential material out of anything about to be logged or raised."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(method: str, endpoint: str, path: str, *, access_key: str,
                  secret_key: str, region: str = "us-east-1", service: str = "s3",
                  payload: bytes = b"", query: str = "",
                  extra: Optional[dict] = None) -> dict:
    """AWS Signature Version 4 for one S3 request.

    `path` must already be an application-generated object key — see
    object_store.build_key. Anything derived from user input has to be validated
    BEFORE it reaches here; signing does not sanitize.
    """
    host = endpoint.split("://", 1)[-1].rstrip("/")
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    # Custom x-amz-meta-* headers must be part of the signature, or SeaweedFS
    # rejects the request — so they are folded into the canonical form here
    # rather than added to the request afterwards.
    signed_extra = {k.lower(): v for k, v in (extra or {}).items()
                    if k.lower().startswith("x-amz-meta-")}
    header_map = {"host": host, "x-amz-content-sha256": payload_hash,
                  "x-amz-date": amz_date, **signed_extra}
    canonical_headers = "".join(f"{k}:{header_map[k]}\n" for k in sorted(header_map))
    signed_headers = ";".join(sorted(header_map))

    canonical_request = (f"{method}\n{quote(path, safe='/')}\n{query}\n"
                         f"{canonical_headers}\n{signed_headers}\n{payload_hash}")
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    to_sign = (f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
               f"{hashlib.sha256(canonical_request.encode()).hexdigest()}")

    k = _sign(f"AWS4{secret_key}".encode(), date_stamp)
    for part in (region, service, "aws4_request"):
        k = _sign(k, part)
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()

    return {"Authorization": (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                              f"SignedHeaders={signed_headers}, Signature={signature}"),
            "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash,
            **(extra or {})}


class S3Transport:
    """Signed request execution against the SeaweedFS S3 gateway.

    Sync and async variants exist because the callers differ: FastAPI handlers
    want async, while verification scripts and health probes run synchronously.
    Both share one signing path.
    """

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 timeout: float = 5.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._ak = access_key
        self._sk = secret_key
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._endpoint and self._ak and self._sk)

    def _prepare(self, method: str, path: str, payload: bytes,
                 query: str, extra: Optional[dict]) -> "tuple[str, dict]":
        headers = sigv4_headers(method, self._endpoint, path, access_key=self._ak,
                                secret_key=self._sk, payload=payload, query=query,
                                extra=extra)
        url = f"{self._endpoint}{path}" + (f"?{query}" if query else "")
        return url, headers

    def _classify(self, r: httpx.Response, path: str) -> None:
        if r.status_code in (401, 403):
            raise StorageAuthError(
                f"gateway rejected a signed request for {path} (HTTP {r.status_code}) — "
                "credentials or bucket ACL are wrong")
        if r.status_code == 404:
            raise StorageNotFound(f"no object at {path}")
        if r.status_code >= 500:
            raise StorageUnavailable(f"gateway error {r.status_code} for {path}")

    def request(self, method: str, path: str, *, payload: bytes = b"",
                query: str = "", extra: Optional[dict] = None,
                raise_for_status: bool = True) -> httpx.Response:
        url, headers = self._prepare(method, path, payload, query, extra)
        try:
            r = httpx.request(method, url, headers=headers,
                              content=payload or None, timeout=self._timeout)
        except Exception as e:  # noqa: BLE001
            raise StorageUnavailable(_redact(str(e)[:200], self._sk)) from e
        if raise_for_status:
            self._classify(r, path)
        return r

    async def arequest(self, method: str, path: str, *, payload: bytes = b"",
                       query: str = "", extra: Optional[dict] = None,
                       raise_for_status: bool = True) -> httpx.Response:
        url, headers = self._prepare(method, path, payload, query, extra)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                r = await c.request(method, url, headers=headers,
                                    content=payload or None)
        except Exception as e:  # noqa: BLE001
            raise StorageUnavailable(_redact(str(e)[:200], self._sk)) from e
        if raise_for_status:
            self._classify(r, path)
        return r


_transport: Optional[S3Transport] = None


def get_transport() -> S3Transport:
    """Process-wide transport built from settings. Constructing it opens no socket."""
    global _transport
    if _transport is None:
        from config.settings import (SEAWEEDFS_ACCESS_KEY, SEAWEEDFS_S3_URL,
                                     SEAWEEDFS_SECRET_KEY, SEAWEEDFS_TIMEOUT)
        _transport = S3Transport(SEAWEEDFS_S3_URL, SEAWEEDFS_ACCESS_KEY,
                                 SEAWEEDFS_SECRET_KEY, SEAWEEDFS_TIMEOUT)
    return _transport
