#!/usr/bin/env python3
"""
Migrate provider OAuth connections from the JSON file store into PostgreSQL.

    python scripts/migrate_provider_connections_to_pg.py --dry-run
    python scripts/migrate_provider_connections_to_pg.py
    python scripts/migrate_provider_connections_to_pg.py --verify

Idempotent: rows are upserted on (user_id, provider), so re-running converges
instead of duplicating. The JSON file is READ ONLY — it is never modified or
deleted, because it remains the emergency fallback and is the only rollback path
if Postgres is unreachable.

Tokens move as ciphertext. This script never calls token_crypto, so a plaintext
token cannot appear in a log, a terminal, or a crash trace here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import _token_file_store as _file      # noqa: E402
from backend.services import _token_pg_store as _pg          # noqa: E402

JSON_PATH = ROOT / "data_vault" / "provider_connections.json"


def _mask(value: Any) -> str:
    s = str(value or "")
    return f"{s[:10]}…({len(s)} chars)" if s else "—"


def load_json_rows() -> list[dict]:
    """Every row in the file store, whatever container shape it uses."""
    if not JSON_PATH.exists():
        return []
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        rows = data.get("rows", data)
        return list(rows.values()) if isinstance(rows, dict) else list(rows)
    return []


async def migrate(dry_run: bool = False) -> dict:
    rows = load_json_rows()
    moved, skipped, failed = [], [], []

    for row in rows:
        uid, provider = row.get("user_id"), row.get("provider")
        if not uid or not provider:
            skipped.append(f"{row.get('id')} (missing user_id/provider)")
            continue
        if provider not in ("google", "microsoft"):
            # The CHECK constraint would reject it; report rather than 500.
            skipped.append(f"{uid}/{provider} (provider not in CHECK constraint)")
            continue
        if dry_run:
            moved.append(f"{uid}/{provider}")
            continue
        try:
            await _pg.upsert(uid, provider, row)
            moved.append(f"{uid}/{provider}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"{uid}/{provider}: {type(e).__name__}: {e}")

    return {"json_rows": len(rows), "migrated": moved,
            "skipped": skipped, "failed": failed, "dry_run": dry_run}


async def verify() -> dict:
    """Compare both stores row-by-row on the fields that matter operationally."""
    json_rows = {(r.get("user_id"), r.get("provider")): r for r in load_json_rows()}
    out, mismatches = [], []
    for (uid, provider), jrow in sorted(json_rows.items(), key=lambda kv: str(kv[0])):
        prow = await _pg.fetch(uid, provider)
        if not prow:
            mismatches.append(f"{uid}/{provider}: absent from Postgres")
            continue
        for field in ("access_token", "refresh_token", "provider_email"):
            if (jrow.get(field) or None) != (prow.get(field) or None):
                mismatches.append(f"{uid}/{provider}: {field} differs")
        out.append({"user_id": uid, "provider": provider,
                    "email": prow.get("provider_email"),
                    "access_token": _mask(prow.get("access_token")),
                    "refresh_token": _mask(prow.get("refresh_token")),
                    "token_expiry": prow.get("token_expiry")})
    return {"compared": len(json_rows), "rows": out, "mismatches": mismatches}


async def main_async(args) -> int:
    if args.verify:
        res = await verify()
        print(f"compared {res['compared']} row(s) against Postgres\n")
        for r in res["rows"]:
            print(f"  {r['user_id']:<40} {r['provider']:<10} {r['email'] or '—'}")
            print(f"      access={r['access_token']}  refresh={r['refresh_token']}")
            print(f"      expires={r['token_expiry']}")
        if res["mismatches"]:
            print("\nMISMATCHES:")
            for m in res["mismatches"]:
                print("  ✗", m)
            return 1
        print("\n✓ every JSON row is present in Postgres with identical ciphertext")
        return 0

    res = await migrate(dry_run=args.dry_run)
    tag = "WOULD MIGRATE" if res["dry_run"] else "migrated"
    print(f"{tag}: {len(res['migrated'])} of {res['json_rows']} JSON row(s)")
    for m in res["migrated"]:
        print("  ✓", m)
    for s in res["skipped"]:
        print("  – skipped:", s)
    for f in res["failed"]:
        print("  ✗ FAILED:", f)
    print(f"\nJSON file left untouched at {JSON_PATH} (emergency fallback + rollback path)")
    return 1 if res["failed"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate provider connections JSON → PostgreSQL")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="compare both stores")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
