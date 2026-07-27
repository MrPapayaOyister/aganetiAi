"""Idempotent Phase F data migration: rename the single existing tenant's org slug
from the bare 'meerana' to the full email domain 'meerana.ae', so that the new
org-by-domain provisioning maps existing AND new @meerana.ae users to ONE org
(no tenant split). Safe to run repeatedly.

    AGANETI_DATA_BACKEND=postgres .venv/bin/python phaseF_migrate.py
"""
import asyncio
import sys

sys.path.insert(0, ".")
from sqlalchemy import text
from backend.db.base import SessionLocal


async def main():
    async with SessionLocal() as s:
        rows = (await s.execute(
            text("SELECT slug FROM organizations WHERE slug IN ('meerana','meerana.ae')"))).all()
        slugs = {r[0] for r in rows}
        if "meerana.ae" in slugs:
            print("ALREADY_MIGRATED (slug 'meerana.ae' present)")
            return
        if "meerana" in slugs:
            await s.execute(text("UPDATE organizations SET slug='meerana.ae' WHERE slug='meerana'"))
            await s.commit()
            print("RENAMED: organizations.slug 'meerana' -> 'meerana.ae'")
        else:
            print("NO_MEERANA_ORG — nothing to rename (fresh DB will provision by domain)")


asyncio.run(main())
