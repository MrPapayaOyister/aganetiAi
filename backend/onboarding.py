"""Idempotent first-login onboarding.

Provisions a Postgres User and seeds their primary agent + permissions + profile,
exactly once, in a single transaction. Called from the auth seam the first time a
verified Supabase sub with no User row (and an allowed email domain) authenticates.

Additive: touches only the new Postgres tables + (best-effort, backgrounded) the
user's Qdrant memory collection. Never blocks login on memory seeding.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select

from backend.db.base import SessionLocal
from backend.db import models as M, repo
from backend.orchestrator import templates

log = logging.getLogger("aganeti.onboarding")


async def ensure_onboarded(supabase_uid: str, email: str, full_name: str | None = None,
                           org_slug: str = "meerana") -> str:
    """Return the internal User.id (str). Idempotent: safe to call every login."""
    async with SessionLocal() as s:
        user = await repo.get_or_create_user(s, supabase_uid=supabase_uid, email=email,
                                              full_name=full_name, org_slug=org_slug)
        seeded = False

        # Primary agent (idempotency anchor): seed only if the user has none yet.
        agent = await repo.get_primary_agent(s, user.id)
        if agent is None:
            tmpl = templates.primary_template()
            agent = await repo.create_agent(
                s, org_id=user.org_id, user_id=user.id, kind="primary",
                name=tmpl["name"], system_prompt=tmpl["system_prompt"],
                template_key="primary", config={"tools": tmpl["tools"]})
            for t in tmpl["tools"]:
                await repo.grant_permission(s, org_id=user.org_id, agent_id=agent.id,
                                            permission=t, is_outbound=templates.is_outbound(t),
                                            granted_by=user.id)
            user.primary_agent_id = agent.id
            seeded = True

        # EmployeeProfile (marks onboarding complete).
        prof = (await s.execute(
            select(M.EmployeeProfile).where(M.EmployeeProfile.user_id == user.id))).scalar_one_or_none()
        if prof is None:
            s.add(M.EmployeeProfile(org_id=user.org_id, user_id=user.id, onboarded_at=func.now(),
                                    seed_prompt=f"{full_name or email} is a new user of the Enterprise Agentic OS."))
            seeded = True

        await s.commit()
        uid = str(user.id)
        mem_key = user.supabase_uid  # new users' tools key memory on their supabase uid

    if seeded:
        log.info("onboarded user=%s email=%s", uid, email)
        # Seed initial long-term memory OUT of band — never block login on the embedder.
        asyncio.create_task(_seed_memory(mem_key, email, full_name))
    return uid


async def _seed_memory(mem_key: str, email: str, full_name: str | None) -> None:
    try:
        from datetime import datetime, timezone
        from memory.long_term import upsert_facts
        facts = [f"The user's email is {email}."]
        if full_name:
            facts.append(f"The user's name is {full_name}.")
        await asyncio.to_thread(upsert_facts, facts, mem_key, datetime.now(timezone.utc).isoformat())
    except Exception as e:  # noqa: BLE001
        log.info("memory seed skipped for %s: %s", mem_key, e)
