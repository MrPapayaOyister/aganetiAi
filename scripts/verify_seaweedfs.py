#!/usr/bin/env python3
"""
Verify the SeaweedFS deployment: connectivity, authentication and the security
boundary. Read-only except for one clearly-namespaced test object.

    python scripts/verify_seaweedfs.py            # full check
    python scripts/verify_seaweedfs.py --no-write # skip the write/delete probe

Why this signs requests by hand instead of using boto3: boto3 is not installed,
and this phase is security/connectivity verification, not the storage
implementation. Adding an S3 SDK to prove credentials work would be a dependency
change smuggled in under a security fix. AWS SigV4 is ~40 lines of stdlib hmac,
so the check costs nothing and the script stays runnable on a bare venv.

What it does NOT do: touch production objects. The only mutation is a PUT and
DELETE of `b2-test/<utc-timestamp>/healthcheck.txt`, and the DELETE is in a
finally block so an assertion failure still cleans up.

Credentials come from config.settings (which reads .env). Nothing here prints,
logs or returns a key — only whether authentication succeeded.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
PASS, FAIL, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((ok, label))
    print(f"  {PASS if ok else FAIL} {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    return ok


def note(label: str, detail: str = "") -> None:
    print(f"  {WARN} {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


# ── AWS SigV4, minimal and S3-specific ───────────────────────────────────────

# SigV4 lives in backend/storage/client.py — the one implementation in the
# project. This script imports it rather than keeping a second copy that
# could silently drift from what production actually signs with.
from backend.storage.client import sigv4_headers  # noqa: E402


def s3_request(method: str, path: str, *, endpoint: str, access_key: str,
               secret_key: str, payload: bytes = b"", query: str = "",
               timeout: float = 10.0, signed: bool = True) -> httpx.Response:
    url = f"{endpoint.rstrip('/')}{path}" + (f"?{query}" if query else "")
    headers = (sigv4_headers(method, endpoint, path, access_key=access_key,
                             secret_key=secret_key, payload=payload, query=query)
               if signed else {})
    return httpx.request(method, url, headers=headers, content=payload or None,
                         timeout=timeout)


# ── checks ───────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Verify SeaweedFS connectivity and auth")
    ap.add_argument("--no-write", action="store_true",
                    help="skip the test-object write/delete probe")
    args = ap.parse_args()

    from config.settings import (SEAWEEDFS_ACCESS_KEY, SEAWEEDFS_BUCKET_NAME,
                                 SEAWEEDFS_ENABLED, SEAWEEDFS_FILER_URL,
                                 SEAWEEDFS_MASTER_URL, SEAWEEDFS_S3_URL,
                                 SEAWEEDFS_SECRET_KEY, SEAWEEDFS_TIMEOUT)

    print(f"\n{DIM}config{RESET}")
    print(f"  enabled={SEAWEEDFS_ENABLED} bucket={SEAWEEDFS_BUCKET_NAME}")
    print(f"  master={SEAWEEDFS_MASTER_URL}  filer={SEAWEEDFS_FILER_URL}  s3={SEAWEEDFS_S3_URL}")
    # Presence only — a key is never printed.
    print(f"  credentials: access_key={'set' if SEAWEEDFS_ACCESS_KEY else 'MISSING'} "
          f"secret_key={'set' if SEAWEEDFS_SECRET_KEY else 'MISSING'}")

    if not SEAWEEDFS_ENABLED:
        note("SEAWEEDFS_ENABLED=false — nothing to verify")
        return 0
    if not (SEAWEEDFS_ACCESS_KEY and SEAWEEDFS_SECRET_KEY):
        check(False, "credentials configured",
              "set SEAWEEDFS_ACCESS_KEY / SEAWEEDFS_SECRET_KEY in .env")
        return 1

    # ── 1. master → filer → s3 chain ─────────────────────────────────────────
    print(f"\n{DIM}chain: master → filer → s3{RESET}")
    for label, url, path in (("master", SEAWEEDFS_MASTER_URL, "/cluster/status"),
                             ("filer", SEAWEEDFS_FILER_URL, "/"),
                             ("s3", SEAWEEDFS_S3_URL, "/")):
        t0 = time.monotonic()
        try:
            r = httpx.get(f"{url.rstrip('/')}{path}", timeout=SEAWEEDFS_TIMEOUT)
            ms = round((time.monotonic() - t0) * 1000, 1)
            # s3 answering 403 to an anonymous GET is the CORRECT answer.
            ok = r.status_code < 500
            check(ok, f"{label} reachable", f"http {r.status_code} in {ms}ms")
        except Exception as e:  # noqa: BLE001
            check(False, f"{label} reachable", str(e)[:70])

    # ── 2. security boundary ─────────────────────────────────────────────────
    print(f"\n{DIM}security boundary{RESET}")
    anon = s3_request("GET", "/", endpoint=SEAWEEDFS_S3_URL, access_key="",
                      secret_key="", signed=False, timeout=SEAWEEDFS_TIMEOUT)
    check(anon.status_code in (401, 403),
          "S3 denies unauthenticated access", f"http {anon.status_code}")

    anon_bucket = s3_request("GET", f"/{SEAWEEDFS_BUCKET_NAME}/", endpoint=SEAWEEDFS_S3_URL,
                             access_key="", secret_key="", signed=False,
                             timeout=SEAWEEDFS_TIMEOUT)
    check(anon_bucket.status_code in (401, 403),
          "S3 denies unauthenticated bucket listing", f"http {anon_bucket.status_code}")

    # The Filer has no authentication of its own, so the ONLY thing protecting it
    # is the network boundary. Verify it is not answering on a non-loopback address.
    import socket
    lan_ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); lan_ip = s.getsockname()[0]; s.close()
    except Exception:  # noqa: BLE001
        pass
    if lan_ip and not lan_ip.startswith("127."):
        try:
            r = httpx.get(f"http://{lan_ip}:8888/", timeout=3.0)
            check(False, "Filer NOT reachable off-loopback",
                  f"http {r.status_code} on {lan_ip}:8888 — bucket namespace is exposed")
        except Exception:
            check(True, "Filer NOT reachable off-loopback", f"refused on {lan_ip}:8888")
    else:
        note("Filer off-loopback check skipped", "could not determine a LAN address")

    # ── 3. authenticated S3 ──────────────────────────────────────────────────
    print(f"\n{DIM}authenticated S3{RESET}")
    kw = dict(endpoint=SEAWEEDFS_S3_URL, access_key=SEAWEEDFS_ACCESS_KEY,
              secret_key=SEAWEEDFS_SECRET_KEY, timeout=SEAWEEDFS_TIMEOUT)

    t0 = time.monotonic()
    listing = s3_request("GET", f"/{SEAWEEDFS_BUCKET_NAME}/", query="list-type=2", **kw)
    ms = round((time.monotonic() - t0) * 1000, 1)
    if listing.status_code == 403:
        check(False, "authenticated bucket listing",
              "403 — credentials rejected. STOPPING rather than weakening security.")
        return 1
    ok_list = check(listing.status_code == 200, "authenticated bucket listing",
                    f"http {listing.status_code} in {ms}ms")
    if ok_list:
        keys = listing.text.count("<Key>")
        print(f"    {DIM}bucket '{SEAWEEDFS_BUCKET_NAME}' contains {keys} object(s){RESET}")

    # ── 4. test object round trip ────────────────────────────────────────────
    if args.no_write:
        note("write/delete probe skipped", "--no-write")
    else:
        print(f"\n{DIM}test object round trip{RESET}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        key = f"/b2-test/{stamp}/healthcheck.txt"
        body = f"seaweedfs b2 verification {stamp}\n".encode()
        path = f"/{SEAWEEDFS_BUCKET_NAME}{key}"
        wrote = False
        try:
            put = s3_request("PUT", path, payload=body, **kw)
            wrote = put.status_code in (200, 201)
            check(wrote, "write test object", f"http {put.status_code} → {key}")

            if wrote:
                got = s3_request("GET", path, **kw)
                check(got.status_code == 200 and got.content == body,
                      "read test object back", f"http {got.status_code}, "
                      f"{len(got.content)} bytes, content matches")

                head = s3_request("HEAD", path, **kw)
                check(head.status_code == 200, "object metadata (HEAD)",
                      f"http {head.status_code}, "
                      f"length={head.headers.get('content-length')}")
        finally:
            # Always clean up, even if an assertion above failed.
            if wrote:
                dele = s3_request("DELETE", path, **kw)
                check(dele.status_code in (200, 204), "delete test object",
                      f"http {dele.status_code}")
                gone = s3_request("GET", path, **kw)
                check(gone.status_code == 404, "test object is gone",
                      f"http {gone.status_code}")

    # ── summary ──────────────────────────────────────────────────────────────
    failed = [l for ok, l in _results if not ok]
    print(f"\n  {len(_results) - len(failed)}/{len(_results)} checks passed")
    for l in failed:
        print(f"    {FAIL} {l}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
