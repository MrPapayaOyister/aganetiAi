"""Remove ALL demo data seeded by seed_demo_daralber.py (by their tags). Idempotent.
Run: AGANETI_DATA_BACKEND=postgres .venv/bin/python remove_demo_daralber.py
"""
import asyncio
import sys
sys.path.insert(0, ".")
from sqlalchemy import text
from backend.db.base import SessionLocal

STMTS = [
    ("agent_permissions (demo)", "DELETE FROM agent_permissions WHERE agent_id IN "
                                 "(SELECT id FROM agents WHERE config->>'demo_seed'='true')"),
    ("agents", "DELETE FROM agents WHERE config->>'demo_seed'='true'"),
    ("initiatives", "DELETE FROM initiatives WHERE dedup_key LIKE 'demoseed:%'"),
    ("contacts", "DELETE FROM contacts WHERE notes LIKE '%[demo_seed]%'"),
    ("tasks", "DELETE FROM tasks WHERE source='demo_seed'"),
]


async def main():
    async with SessionLocal() as s:
        for label, sql in STMTS:
            r = await s.execute(text(sql))
            print(f"removed {label}: {r.rowcount}")
        await s.commit()
    # initiatives live in a SQLite store (not Postgres) — clean those too.
    try:
        from backend import initiatives
        initiatives.init()
        with initiatives._conn() as c:
            cur = c.execute("DELETE FROM initiatives WHERE dedup_key LIKE 'demoseed:%'")
            print(f"removed initiatives (sqlite): {cur.rowcount}")
    except Exception as e:
        print(f"sqlite initiatives cleanup skipped: {e}")
    print("demo data removed.")


asyncio.run(main())
