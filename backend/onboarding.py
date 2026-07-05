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
from sqlalchemy.exc import IntegrityError

from backend.db.base import SessionLocal
from backend.db import models as M, repo
from backend.orchestrator import templates

log = logging.getLogger("aganeti.onboarding")

# Retain background tasks so the event loop can't GC them mid-flight.
_bg_tasks: set = set()


async def ensure_onboarded(supabase_uid: str, email: str, full_name: str | None = None,
                           org_slug: str = "meerana") -> str:
    """Return the internal User.id (str). Idempotent + concurrency-safe: two
    simultaneous first-logins converge on one User + one primary agent."""
    seeded = False
    mem_key = None
    try:
        async with SessionLocal() as s:
            user = await repo.get_or_create_user(s, supabase_uid=supabase_uid, email=email,
                                                 full_name=full_name, org_slug=org_slug)
            # Primary agent (idempotency anchor): seed only if the user has none. A
            # partial unique index (one primary per user) turns a concurrent duplicate
            # into an IntegrityError we handle below.
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

            prof = (await s.execute(
                select(M.EmployeeProfile).where(M.EmployeeProfile.user_id == user.id))).scalar_one_or_none()
            if prof is None:
                s.add(M.EmployeeProfile(org_id=user.org_id, user_id=user.id, onboarded_at=func.now(),
                                        seed_prompt=f"{full_name or email} is a new user of the Enterprise Agentic OS."))
                seeded = True

            await s.commit()
            uid = str(user.id)
            mem_key = user.supabase_uid
    except IntegrityError:
        # A concurrent onboarding won the race (duplicate user/primary agent). Re-resolve
        # the winner's row and return it — the user is fully onboarded either way.
        log.info("onboarding race for %s — converging on existing row", email)
        async with SessionLocal() as s2:
            user = await repo.resolve_user(s2, supabase_uid)
            if user is None:
                raise
            return str(user.id)

    if seeded:
        log.info("onboarded user=%s email=%s", uid, email)
        task = asyncio.create_task(_seed_memory(mem_key, email, full_name))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
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
        log.warning("memory seed failed for %s: %s", mem_key, e)
