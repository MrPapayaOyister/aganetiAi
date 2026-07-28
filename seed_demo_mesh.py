"""Seed REMOVABLE dummy agents into the Agent Matrix mesh (enterprise theme).

The mesh graph is built from agent-inbox MESSAGES grouped by sender, so this
seeds messages TO the user's primary agent (agent_1) FROM several specialist
agents, each with a rich payload (agent_name / title / domain). It also registers
the agents in the DB so they appear in the "registered agents" list.

Everything is tagged for one-command removal:
  agent_messages.from_agent IN (the demo agent ids)
  agents.config->>'demo_seed' = 'mesh'

Seed:   AGANETI_DATA_BACKEND=postgres .venv/bin/python seed_demo_mesh.py
Remove: AGANETI_DATA_BACKEND=postgres .venv/bin/python seed_demo_mesh.py remove
"""
import asyncio
import sys
sys.path.insert(0, ".")

UID = "user_1"
TO_AGENT = "agent_1"   # USERS['user_1'].agent_id

# (from_agent id, display name, domain, [ (title, summary, priority) ... ])
AGENTS = [
    ("agent_legal", "Legal Agent", "Contracts & Compliance", [
        ("Completed audit of NDA_v3.pdf — 2 clauses flagged", "Limitation of liability (§7.3) and data residency (§12.1) need review.", "high"),
        ("Reviewing Phase 2 vendor infrastructure agreement", "Cross-checking indemnity and termination clauses before signature.", "medium"),
    ]),
    ("agent_finance", "Finance Agent", "Budgets & Procurement", [
        ("Cross-referenced Q3 budget framework vs allocation request", "Allocation is within the approved envelope; flagged one overage line.", "medium"),
        ("Reconciled vendor payment schedule", "Q3 procurement disbursements reconciled against POs.", "normal"),
    ]),
    ("agent_research", "Research Agent", "Policy & Knowledge", [
        ("Retrieved 3 policy documents for the Q3 research request", "Data-sovereignty framework, procurement policy, and cloud SLA.", "normal"),
        ("Compiled competitor benchmark summary", "Five providers compared on price, latency, and residency guarantees.", "low"),
    ]),
    ("agent_itsec", "IT Security Agent", "Security & Risk", [
        ("Blocked an external connection attempt — flagged for review", "Unusual inbound from an unrecognized source; connection dropped.", "urgent"),
        ("Monthly security scan complete — all 14 checks passed", "No critical findings; two low-severity items auto-remediated.", "normal"),
    ]),
    ("agent_hr", "HR Compliance", "Employment & Records", [
        ("Filed the Q3 employment compliance report", "Submitted to central records; acknowledgement pending.", "medium"),
        ("Reviewed 12 onboarding records", "All complete; two awaiting signed policy acknowledgements.", "low"),
    ]),
]
FROM_IDS = [a[0] for a in AGENTS]


def seed_messages():
    from integrations.agent_inbox import send_message, get_pending_messages
    existing = {m.get("from_agent") for m in get_pending_messages(TO_AGENT, limit=200)}
    if any(fid in existing for fid in FROM_IDS):
        print("mesh demo messages already present — skipping (run 'remove' first to re-seed).")
        return 0
    n = 0
    for fid, name, domain, msgs in AGENTS:
        for title, summary, priority in msgs:
            send_message(fid, TO_AGENT, "task_update", {
                "agent_name": name, "title": title, "summary": summary,
                "domain": domain, "category": domain, "priority": priority,
                "status": "pending", "demo_seed": "mesh",
            })
            n += 1
    print(f"seeded {n} mesh messages from {len(AGENTS)} agents")
    return n


def remove_messages():
    from integrations.agent_inbox import get_conn
    with get_conn() as conn:
        q = ",".join("?" * len(FROM_IDS))
        cur = conn.execute(f"DELETE FROM agent_messages WHERE from_agent IN ({q})", FROM_IDS)
        conn.commit()
        print(f"removed mesh messages: {cur.rowcount}")


async def seed_agents():
    from backend.db.base import SessionLocal
    from backend.db import repo, models as M
    from sqlalchemy import select, func
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, UID)
        if not user:
            print("resolve user_1 failed"); return
        have = (await s.execute(select(func.count()).select_from(M.Agent)
                .where(M.Agent.template_key == "demo_mesh", M.Agent.deleted_at.is_(None)))).scalar()
        if have:
            print(f"mesh demo agents already present ({have}) — skipping."); return
        tools_by = {
            "Legal Agent": ["search_documents", "list_tasks", "draft_email"],
            "Finance Agent": ["get_analytics", "list_tasks", "predict_deal_outcome"],
            "Research Agent": ["search_documents", "web_search", "search_memory"],
            "IT Security Agent": ["list_tasks", "search_documents"],
            "HR Compliance": ["list_tasks", "search_documents"],
        }
        known = set()
        try:
            from backend.orchestrator import registry
            known = set(registry.all_names())
        except Exception:
            pass
        for _fid, name, domain, _msgs in AGENTS:
            ag = await repo.create_agent(s, org_id=user.org_id, user_id=user.id, kind="specialist",
                                         name=name, system_prompt=f"You are the {name} for the executive office ({domain}).",
                                         template_key="demo_mesh", config={"demo_seed": "mesh", "domain": domain})
            await s.flush()
            perms = [t for t in tools_by.get(name, []) if not known or t in known]
            await repo.set_permissions(s, org_id=user.org_id, agent_id=ag.id, permissions=perms, granted_by=user.id)
        await s.commit()
        print(f"registered {len(AGENTS)} mesh agents")


async def remove_agents():
    from backend.db.base import SessionLocal
    from sqlalchemy import text
    async with SessionLocal() as s:
        r1 = await s.execute(text("DELETE FROM agent_permissions WHERE agent_id IN "
                                  "(SELECT id FROM agents WHERE config->>'demo_seed'='mesh')"))
        r2 = await s.execute(text("DELETE FROM agents WHERE config->>'demo_seed'='mesh'"))
        await s.commit()
        print(f"removed mesh agents: {r2.rowcount} (perms {r1.rowcount})")


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "remove":
        remove_messages()
        await remove_agents()
        print("mesh demo removed.")
    else:
        seed_messages()
        await seed_agents()
        print("mesh demo seeded.")


asyncio.run(main())
