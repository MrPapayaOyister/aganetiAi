#!/usr/bin/env python3
"""Re-key the one provider_connection stored under a legacy id.

WHY THIS EXISTS
---------------
`_fetch_connection` used to fall back to "the sole live connection for this
provider, whoever owns it" when a user had none of their own. That leaked one
account's mailbox to every other user, and it has been removed
(tests/test_provider_isolation.py).

Removing it exposed a latent inconsistency: the admin's Google connection is
stored under the literal user id "test", which is neither their internal alias
("user_1") nor their Supabase sub. It resolved only *because* of the leaky
fallback. With strict lookup it becomes a miss, and the admin is told to
reconnect Gmail even though a perfectly good credential is on disk.

This re-keys that row to the admin's Supabase sub, which is the id every other
user's data already uses. After this:
  - today:            "user_1" -> alias lookup -> ff3a2d3d... -> HIT
  - after de-hardcoding: effective_user IS ff3a2d3d...        -> HIT
so it is correct both before and after config/users.py is retired.

SAFE TO RE-RUN. Verifies before it writes, and does nothing if already migrated.

Token encryption is Fernet with a single global key and no per-user binding
(backend/services/token_crypto.py), so changing user_id cannot break decryption.

Usage:  .venv/bin/python3 scripts/fix_miskeyed_connection.py [--apply]
Without --apply it only reports what it would do.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OLD_ID = "test"
NEW_ID = "ff3a2d3d-39b8-4709-9cb5-14cf317fe83d"   # sub of abhinavms20002@gmail.com
PROVIDER = "google"
EXPECT_EMAIL = "abhinavms20002@gmail.com"

FILE_STORE = Path(__file__).resolve().parents[1] / "data_vault" / "provider_connections.json"


async def migrate_pg(apply: bool) -> None:
    from sqlalchemy import text
    from backend.db.base import SessionLocal

    async with SessionLocal() as s:
        # Guard: the target must be a real, active user, and the row we are about
        # to move must be the one we think it is.
        owner = (await s.execute(text(
            "select email, status from users where supabase_uid = :sub"),
            {"sub": NEW_ID})).first()
        if not owner:
            print(f"  ABORT: no users row with supabase_uid={NEW_ID}")
            return
        if owner[0] != EXPECT_EMAIL:
            print(f"  ABORT: {NEW_ID} is {owner[0]}, expected {EXPECT_EMAIL}")
            return

        row = (await s.execute(text(
            "select provider_email from provider_connections "
            "where user_id = :old and provider = :p"),
            {"old": OLD_ID, "p": PROVIDER})).first()
        if not row:
            print(f"  postgres: nothing keyed {OLD_ID!r} — already migrated?")
            return
        if row[0] != EXPECT_EMAIL:
            print(f"  ABORT: row {OLD_ID!r} is {row[0]}, expected {EXPECT_EMAIL}")
            return

        # (user_id, provider) is UNIQUE — a pre-existing target row would collide.
        clash = (await s.execute(text(
            "select 1 from provider_connections where user_id = :new and provider = :p"),
            {"new": NEW_ID, "p": PROVIDER})).first()
        if clash:
            print(f"  ABORT: {NEW_ID} already has a {PROVIDER} row; "
                  f"resolve by hand so no credential is lost")
            return

        print(f"  postgres: {OLD_ID!r} -> {NEW_ID!r}  ({EXPECT_EMAIL}, {PROVIDER})")
        if apply:
            await s.execute(text(
                "update provider_connections set user_id = :new, updated_at = now() "
                "where user_id = :old and provider = :p"),
                {"new": NEW_ID, "old": OLD_ID, "p": PROVIDER})
            await s.commit()
            print("  postgres: committed")


def migrate_file(apply: bool) -> None:
    """The JSON store mirrors Postgres and is the read fallback — migrate both or a
    Postgres outage would silently resurrect the old key."""
    if not FILE_STORE.exists():
        print("  file store: absent, skipping")
        return
    data = json.loads(FILE_STORE.read_text() or "{}")
    old_key, new_key = f"{OLD_ID}::{PROVIDER}", f"{NEW_ID}::{PROVIDER}"
    if old_key not in data:
        print(f"  file store: no {old_key!r} — already migrated?")
        return
    if new_key in data:
        print(f"  ABORT: file store already has {new_key!r}")
        return
    print(f"  file store: {old_key!r} -> {new_key!r}")
    if apply:
        row = data.pop(old_key)
        row["user_id"] = NEW_ID
        data[new_key] = row
        FILE_STORE.write_text(json.dumps(data, indent=2))
        print("  file store: written")


async def verify() -> None:
    """Prove the credential is reachable from the ids the app actually uses."""
    from backend.services import provider_tokens as pt
    for uid in ("user_1", NEW_ID):
        hit = await pt._fetch_connection(uid, PROVIDER)
        print(f"  {uid:42} -> {'HIT' if hit else 'MISS'}")
    stranger = await pt._fetch_connection("nobody-at-all", PROVIDER)
    print(f"  {'nobody-at-all':42} -> {'HIT (LEAK!)' if stranger else 'MISS (correct)'}")


async def main() -> None:
    apply = "--apply" in sys.argv
    print("DRY RUN — pass --apply to write\n" if not apply else "APPLYING\n")
    await migrate_pg(apply)
    migrate_file(apply)
    print("\nreachability after:")
    await verify()


if __name__ == "__main__":
    asyncio.run(main())
