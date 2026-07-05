"""One-shot, idempotent, ADDITIVE migration: legacy SQLite (tasks/tasks.db) → Postgres.

Safety properties:
  * Read-only on SQLite. Never writes/deletes the source.
  * Idempotent: UUID-keyed tables use INSERT ... ON CONFLICT (id) DO NOTHING;
    events (int PK) are tagged meta._mig='v1' and delete-then-reinserted.
  * Identity: only the OWNER's legacy ids are mapped to the seeded admin user.
    Every other legacy user_id (test noise / other sessions) is SKIPPED and
    reported — never silently merged into the owner's data.

Does NOT touch the running app. Cutover (pointing the live app at Postgres) is
a separate, gated step.

Run:  .venv/bin/python migrate_sqlite_to_pg.py [--commit]
Without --commit it does a full dry-run inside a rolled-back transaction.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, "/home/matrix/aganetiAi")
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from backend.db.base import SessionLocal  # noqa: E402
from backend.db import models as M, repo  # noqa: E402

SQLITE_PATH = "/home/matrix/aganetiAi/tasks/tasks.db"
OWNER_LEGACY_IDS = {"user_1", "ff3a2d3d-39b8-4709-9cb5-14cf317fe83d"}  # → seeded admin

# Legacy ids are inconsistent: tasks use full UUIDs, schedules/others use 8-char
# short ids. Preserve real UUIDs; derive a STABLE uuid5 for everything else so the
# migration stays idempotent (same legacy id → same PG id on every re-run).
_NS = uuid.UUID("00000000-0000-0000-0000-00000000a9a7")


def as_uuid(table: str, raw) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid5(_NS, f"{table}:{raw}"))


def dt(v):
    if not v:
        return None
    s = str(v).strip()
    for cand in (s, s + "T00:00:00"):
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def jl(v, default=None):
    if v in (None, ""):
        return default if default is not None else {}
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return default if default is not None else {}


def rows(cur, table):
    cur.execute(f"SELECT * FROM {table}")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


async def main(commit: bool):
    sq = sqlite3.connect(SQLITE_PATH)
    sq.row_factory = None
    cur = sq.cursor()

    report: dict[str, dict] = {}
    skipped_ids: dict[str, int] = {}

    async with SessionLocal() as s:
        owner = await repo.resolve_user(s, "ff3a2d3d-39b8-4709-9cb5-14cf317fe83d")
        if not owner:
            print("FATAL: owner user not seeded — run seed first")
            return
        ORG = owner.org_id
        UID = owner.id
        primary = await repo.get_primary_agent(s, UID)
        PRIMARY_AGENT = primary.id if primary else None
        IDMAP = {legacy: UID for legacy in OWNER_LEGACY_IDS}

        def target_uid(legacy):
            if legacy in IDMAP:
                return IDMAP[legacy]
            skipped_ids[str(legacy)] = skipped_ids.get(str(legacy), 0) + 1
            return None

        async def upsert(table, values, conflict=("id",)):
            if not values:
                return 0
            stmt = pg_insert(table).values(values).on_conflict_do_nothing(index_elements=list(conflict))
            await s.execute(stmt)
            return len(values)

        # ── tasks ─────────────────────────────────────────────────────────────
        vals = []
        for r in rows(cur, "tasks"):
            uid = target_uid(r["user_id"])
            if uid is None:
                continue
            vals.append(dict(id=as_uuid("tasks", r["id"]), org_id=ORG, user_id=uid, title=r["title"],
                             source=r.get("source"), status=r.get("status") or "pending",
                             priority=r.get("priority") or "medium", due_date=dt(r.get("due_date")),
                             notes=r.get("notes"), reminder_sent=bool(r.get("reminder_sent")),
                             created_at=dt(r.get("created_at")), updated_at=dt(r.get("updated_at"))))
        report["tasks"] = {"read": cur.rowcount if cur.rowcount > 0 else len(vals), "mapped": len(vals),
                           "inserted": await upsert(M.Task.__table__, vals)}

        # ── delegations ───────────────────────────────────────────────────────
        vals = []
        for r in rows(cur, "delegations"):
            uid = target_uid(r["user_id"])
            if uid is None:
                continue
            frm = PRIMARY_AGENT if (r.get("from_agent") or "").lower() == "aria" else None
            vals.append(dict(id=as_uuid("delegations", r["id"]), org_id=ORG, user_id=uid,
                             from_agent_id=frm, to_agent_id=None,
                             task=r["task"], status=r.get("status") or "pending", result=r.get("result"),
                             error=r.get("error"), created_at=dt(r.get("created_at")),
                             updated_at=dt(r.get("updated_at"))))
        report["delegations"] = {"mapped": len(vals), "inserted": await upsert(M.Delegation.__table__, vals)}

        # ── initiatives ───────────────────────────────────────────────────────
        vals = []
        for r in rows(cur, "initiatives"):
            uid = target_uid(r["user_id"])
            if uid is None:
                continue
            vals.append(dict(id=as_uuid("initiatives", r["id"]), org_id=ORG, user_id=uid,
                             category=r["category"], title=r["title"],
                             body=r["body"], dedup_key=r.get("dedup_key"), status=r.get("status") or "pending",
                             acted_at=dt(r.get("acted_at")), meta=jl(r.get("meta")),
                             created_at=dt(r.get("created_at"))))
        report["initiatives"] = {"mapped": len(vals), "inserted": await upsert(M.Initiative.__table__, vals)}

        # ── schedules ─────────────────────────────────────────────────────────
        vals = []
        for r in rows(cur, "schedules"):
            uid = target_uid(r["user_id"])
            if uid is None:
                continue
            vals.append(dict(id=as_uuid("schedules", r["id"]), org_id=ORG, user_id=uid, label=r["label"],
                             cron_expression=r["cron_expression"], action_type=r["action_type"],
                             action_payload=jl(r.get("action_payload")), is_active=bool(r.get("is_active")),
                             created_at=dt(r.get("created_at"))))
        report["schedules"] = {"mapped": len(vals), "inserted": await upsert(M.Schedule.__table__, vals)}

        # ── contacts (org-level; attributed to owner) ─────────────────────────
        vals = []
        for r in rows(cur, "contacts"):
            vals.append(dict(id=as_uuid("contacts", r["id"]), org_id=ORG, user_id=UID, full_name=r["full_name"],
                             email=r.get("email"), nickname=r.get("nickname"), department=r.get("department"),
                             role=r.get("role"), is_agent=bool(r.get("is_agent")), notes=r.get("notes"),
                             created_at=dt(r.get("created_at"))))
        report["contacts"] = {"mapped": len(vals), "inserted": await upsert(M.Contact.__table__, vals)}

        # ── events (int PK → tag + delete-then-insert for idempotency) ────────
        await s.execute(M.Event.__table__.delete().where(M.Event.meta["_mig"].astext == "v1"))
        vals = []
        for r in rows(cur, "events"):
            uid = target_uid(r["user_id"]) if r.get("user_id") else None
            # keep owner-mapped events; drop test-user events, but keep null-user (org-level) events
            if r.get("user_id") and uid is None:
                continue
            meta = jl(r.get("meta"))
            meta["_mig"] = "v1"
            meta["_legacy_id"] = r["id"]
            vals.append(dict(ts=dt(r.get("ts")), org_id=ORG, user_id=uid, kind=r["kind"], name=r.get("name"),
                             success=(None if r.get("success") is None else bool(r.get("success"))),
                             duration_ms=r.get("duration_ms"), meta=meta))
        if vals:
            await s.execute(pg_insert(M.Event.__table__).values(vals))
        report["events"] = {"mapped": len(vals), "inserted": len(vals)}

        if commit:
            await s.commit()
        else:
            await s.rollback()

    sq.close()
    print("=" * 60)
    print("MIGRATION", "(COMMITTED)" if commit else "(DRY-RUN — rolled back)")
    print("owner user :", UID, "org:", ORG)
    print("-" * 60)
    for t, r in report.items():
        print(f"  {t:14s} {r}")
    print("-" * 60)
    print("SKIPPED (unmapped legacy identities — NOT migrated):")
    for k, v in sorted(skipped_ids.items(), key=lambda x: -x[1]):
        print(f"  {k:44s} {v} rows")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main("--commit" in sys.argv))
