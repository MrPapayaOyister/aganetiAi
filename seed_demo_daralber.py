"""Seed REMOVABLE demo data for a Dar Al Ber Society walkthrough (charity context).
Everything is tagged so remove_demo_daralber.py can wipe it cleanly:
  tasks.source='demo_seed' | initiatives.dedup_key LIKE 'demoseed:%'
  contacts.notes LIKE '%[demo_seed]%' | agents.config->>'demo_seed'='true'
Idempotent: skips if demo data already present. Run: AGANETI_DATA_BACKEND=postgres .venv/bin/python seed_demo_daralber.py
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")

from backend.db.base import SessionLocal
from backend.db import repo, models as M
from tasks.store import create_task
from backend import initiatives as _initiatives

UID = "user_1"
MARK = "demo_seed"


def _d(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


TASKS = [
    ("Finalize vendor quote for 5,000 Ramadan food parcels", "high", _d(2)),
    ("Complete Q1 Zakat disbursement audit", "high", _d(4)),
    ("Onboard 12 new Ramadan volunteers", "medium", _d(3)),
    ("Send thank-you letters to major donors (Q1)", "medium", _d(6)),
    ("Prepare orphan-sponsorship impact report", "medium", _d(9)),
    ("Renew MoU with Dubai Charity Association", "low", _d(14)),
]
INITIATIVES = [
    ("09:00 — Board of Trustees quarterly review", "Q1 donations, campaign performance, budget approval."),
    ("11:30 — Donor lunch: Al Futtaim Foundation", "Discuss Ramadan campaign partnership & matched giving."),
    ("14:00 — Site visit: Al Quoz labour-camp distribution", "Inspect food-parcel logistics and volunteer readiness."),
    ("16:00 — Ramadan volunteer training", "Safeguarding, distribution SOPs, beneficiary dignity."),
    ("17:30 — Zakat committee sync", "Review disbursement compliance & shariah audit."),
]
CONTACTS = [
    ("Fatima Al Nuaimi", "fatima@daralber.ae", "Campaigns", "Campaigns Director"),
    ("Ahmed Al Suwaidi", "ahmed@daralber.ae", "Finance", "Finance & Zakat Head"),
    ("Mariam Khalifa", "mariam@daralber.ae", "Volunteers", "Volunteers Coordinator"),
    ("Yusuf Rahman", "yusuf@daralber.ae", "Operations", "Logistics Manager"),
    ("Layla Hassan", "layla@daralber.ae", "Development", "Donor Relations Lead"),
    ("Omar Farouk", "omar@affoundation.example", "External", "Al Futtaim Foundation — Partner"),
    ("Sara Ibrahim", "sara@dca.example", "External", "Dubai Charity Association — Liaison"),
    ("Khalid Nasser", "khalid@gulffoods.example", "Vendor", "Gulf Food Supplies — Account Mgr"),
]
AGENTS = [
    ("Donations Analyst", "You analyse donation trends, campaign ROI and donor retention for a charity. Compute from data; cite figures.", ["get_analytics", "search_documents", "predict_deal_outcome"]),
    ("Volunteer Coordinator", "You help coordinate volunteers and their tasks for charity campaigns.", ["list_tasks", "create_task", "get_agenda"]),
]


async def main():
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, UID)
        if not user:
            print("resolve user_1 failed"); return
        from sqlalchemy import select, func
        existing = (await s.execute(select(func.count()).select_from(M.Agent)
                    .where(M.Agent.template_key == "demo_seed", M.Agent.deleted_at.is_(None)))).scalar()
        if existing:
            print(f"demo data already present ({existing} demo agents) — skipping. Run remove_demo_daralber.py first to re-seed.")
            return
        # contacts
        for name, email, dept, role in CONTACTS:
            s.add(M.Contact(org_id=user.org_id, user_id=user.id, full_name=name, email=email,
                            department=dept, role=role, notes="[demo_seed] Dar Al Ber walkthrough"))
        # agents
        for name, prompt, tools in AGENTS:
            ag = await repo.create_agent(s, org_id=user.org_id, user_id=user.id, kind="specialist",
                                         name=name, system_prompt=prompt, template_key="demo_seed",
                                         config={"demo_seed": True, "tools": tools})
            await s.flush()
            known = set()
            try:
                from backend.orchestrator import registry
                known = set(registry.all_names())
            except Exception:
                pass
            await repo.set_permissions(s, org_id=user.org_id, agent_id=ag.id,
                                       permissions=[t for t in tools if not known or t in known], granted_by=user.id)
        await s.commit()
    # tasks (via the store so they read back through /tasks)
    for title, pri, due in TASKS:
        create_task(UID, title=title, source=MARK, priority=pri, due_date=due)
    # initiatives (schedule) — SQLite store, keyed by the "user_1" string
    for i, (title, body) in enumerate(INITIATIVES):
        _initiatives.enqueue(UID, "meeting", title, body, dedup_key=f"demoseed:{i}", meta={"demo_seed": True})
    print(f"seeded: {len(TASKS)} tasks, {len(INITIATIVES)} initiatives, {len(CONTACTS)} contacts, {len(AGENTS)} agents (all tagged '{MARK}')")


asyncio.run(main())
