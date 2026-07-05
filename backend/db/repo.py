"""Async data-access layer for the Enterprise Agentic OS (Postgres).

This is the seam the app/orchestrator is cut over onto. Every function takes an
AsyncSession so callers control the transaction boundary. The core is
`resolve_user` / `get_or_create_user`: the string identities the legacy code
passes around (a Supabase uid, or the historical "user_1") become a real
User.id UUID + org_id here, once, so nothing downstream has to guess.

Additive: importing this module has no side effects and does not touch the
running SQLite app. Cutover wires these into the executor + routes.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import models as M


def _alias_to_supabase_uid(alias: str) -> str | None:
    """Legacy config alias ('user_1') → its configured supabase_uid, if any.

    Bridges the old static config.users identity onto the new DB identity so a
    request the auth middleware normalised to 'user_1' still resolves to the real
    User row (and its approvals/tasks land in Postgres, not the SQLite fallback).
    """
    try:
        from config.users import USERS
        return (USERS.get(alias) or {}).get("supabase_uid") or None
    except Exception:
        return None


def _tool_is_outbound(name: str) -> bool:
    """Registry is the authority for whether a tool is approval-gated."""
    try:
        from backend.orchestrator import registry
        t = registry.get(name)
        return bool(t and t.is_outbound)
    except Exception:
        return False

# ── Identity ──────────────────────────────────────────────────────────────────
# The single tenant's slug. Multi-tenant later resolves org from the user row /
# an email domain map; for now every provisioned user lands in this org.
DEFAULT_ORG_SLUG = "meerana"


async def get_org_by_slug(s: AsyncSession, slug: str = DEFAULT_ORG_SLUG) -> Optional[M.Organization]:
    return (await s.execute(select(M.Organization).where(M.Organization.slug == slug))).scalar_one_or_none()


async def resolve_user(s: AsyncSession, identity: str) -> Optional[M.User]:
    """Map an external identity to the internal User row, or None.

    Accepts (in priority order): a User.id UUID string, a Supabase uid, or an
    email. Returns None if unknown — callers decide whether to auto-provision.
    """
    if not identity:
        return None
    # UUID → primary key
    try:
        uid = uuid.UUID(str(identity))
        row = (await s.execute(select(M.User).where(M.User.id == uid))).scalar_one_or_none()
        if row:
            return row
    except (ValueError, AttributeError, TypeError):
        pass
    # Supabase uid
    row = (await s.execute(select(M.User).where(M.User.supabase_uid == identity))).scalar_one_or_none()
    if row:
        return row
    # Email fallback
    if "@" in identity:
        row = (await s.execute(select(M.User).where(M.User.email == identity))).scalar_one_or_none()
        if row:
            return row
    # Legacy config alias ("user_1") → configured supabase_uid → user
    mapped = _alias_to_supabase_uid(identity)
    if mapped and mapped != identity:
        row = (await s.execute(select(M.User).where(M.User.supabase_uid == mapped))).scalar_one_or_none()
        if row:
            return row
    return None


async def get_or_create_user(s: AsyncSession, *, supabase_uid: str, email: str,
                             full_name: str | None = None, org_slug: str = DEFAULT_ORG_SLUG,
                             role: str = "employee") -> M.User:
    """Idempotent login provisioning. Does NOT commit — caller owns the tx.

    Note: the primary-agent seed lives in Phase B onboarding; this only ensures
    the User + its Organization exist so identity resolves on first login.
    """
    from sqlalchemy.dialects.postgresql import insert as _pg_insert
    existing = (await s.execute(
        select(M.User).where(M.User.supabase_uid == supabase_uid))).scalar_one_or_none()
    if existing:
        return existing
    org = await get_org_by_slug(s, org_slug)
    if org is None:
        # ON CONFLICT so two concurrent first-logins don't both create the org.
        await s.execute(_pg_insert(M.Organization.__table__)
                        .values(name=org_slug.title(), slug=org_slug)
                        .on_conflict_do_nothing(index_elements=["slug"]))
        await s.flush()
        org = await get_org_by_slug(s, org_slug)
    # ON CONFLICT (supabase_uid) so a concurrent first-login is a no-op, not a 503:
    # the loser re-selects the winner's row instead of hitting a unique violation.
    await s.execute(_pg_insert(M.User.__table__).values(
        org_id=org.id, supabase_uid=supabase_uid, email=email, full_name=full_name, role=role)
        .on_conflict_do_nothing(index_elements=["supabase_uid"]))
    await s.flush()
    return (await s.execute(
        select(M.User).where(M.User.supabase_uid == supabase_uid))).scalar_one()


# ── Agents + permissions ──────────────────────────────────────────────────────
async def get_primary_agent(s: AsyncSession, user_id: uuid.UUID) -> Optional[M.Agent]:
    # .first() (oldest) rather than scalar_one_or_none so a stray duplicate primary
    # (e.g. a pre-index race) self-heals to one row instead of raising forever.
    return (await s.execute(
        select(M.Agent).where(M.Agent.user_id == user_id, M.Agent.kind == "primary",
                              M.Agent.deleted_at.is_(None))
        .order_by(M.Agent.created_at.asc()).limit(1))).scalars().first()


async def get_agent(s: AsyncSession, agent_id: uuid.UUID) -> Optional[M.Agent]:
    return (await s.execute(
        select(M.Agent).where(M.Agent.id == agent_id, M.Agent.deleted_at.is_(None)))).scalar_one_or_none()


async def list_agents(s: AsyncSession, user_id: uuid.UUID) -> Sequence[M.Agent]:
    return (await s.execute(
        select(M.Agent).where(M.Agent.user_id == user_id, M.Agent.deleted_at.is_(None))
        .order_by(M.Agent.kind.desc(), M.Agent.created_at))).scalars().all()


async def create_agent(s: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, kind: str,
                       name: str, system_prompt: str, template_key: str | None = None,
                       persona: str | None = None, model_key: str | None = None,
                       config: dict | None = None) -> M.Agent:
    agent = M.Agent(org_id=org_id, user_id=user_id, kind=kind, name=name,
                    system_prompt=system_prompt, template_key=template_key, persona=persona,
                    model_key=model_key, config=config or {})
    s.add(agent)
    await s.flush()
    return agent


async def list_permissions(s: AsyncSession, agent_id: uuid.UUID) -> Sequence[M.AgentPermission]:
    return (await s.execute(
        select(M.AgentPermission).where(M.AgentPermission.agent_id == agent_id))).scalars().all()


async def allowed_tools(s: AsyncSession, agent_id: uuid.UUID) -> list[str]:
    """The tool allow-list the executor enforces for this agent."""
    rows = await list_permissions(s, agent_id)
    return [p.permission for p in rows]


async def outbound_tools(s: AsyncSession, agent_id: uuid.UUID) -> set[str]:
    """Tools that always require a human approval, per the non-negotiable gate."""
    rows = await list_permissions(s, agent_id)
    return {p.permission for p in rows if p.is_outbound}


async def grant_permission(s: AsyncSession, *, org_id: uuid.UUID, agent_id: uuid.UUID,
                           permission: str, is_outbound: bool = False,
                           granted_by: uuid.UUID | None = None) -> M.AgentPermission:
    perm = M.AgentPermission(org_id=org_id, agent_id=agent_id, permission=permission,
                             is_outbound=is_outbound, granted_by=granted_by)
    s.add(perm)
    await s.flush()
    return perm


# Agent fields a user/admin is allowed to edit (id/org/user/kind are immutable here).
_EDITABLE_AGENT_FIELDS = {"name", "persona", "system_prompt", "model_key", "fallback_models",
                          "status", "config", "template_key"}


async def update_agent(s: AsyncSession, agent_id: uuid.UUID, **fields) -> None:
    vals = {k: v for k, v in fields.items() if k in _EDITABLE_AGENT_FIELDS and v is not None}
    if vals:
        await s.execute(update(M.Agent).where(M.Agent.id == agent_id).values(**vals))


async def soft_delete_agent(s: AsyncSession, agent_id: uuid.UUID) -> None:
    await s.execute(update(M.Agent).where(M.Agent.id == agent_id)
                    .values(status="archived", deleted_at=func.now()))


async def set_permissions(s: AsyncSession, *, org_id: uuid.UUID, agent_id: uuid.UUID,
                          permissions: list[str], granted_by: uuid.UUID | None = None) -> None:
    """Declaratively set an agent's tool allow-list: grant the missing, revoke the extra.

    Outbound flags are derived from the registry (the executor's authority), so a
    customization UI can never mark an inherently-outbound tool as non-approval-gated.
    """
    existing = {p.permission for p in await list_permissions(s, agent_id)}
    want = set(permissions)
    for perm in sorted(want - existing):
        s.add(M.AgentPermission(org_id=org_id, agent_id=agent_id, permission=perm,
                                is_outbound=_tool_is_outbound(perm), granted_by=granted_by))
    extra = existing - want
    if extra:
        await s.execute(delete(M.AgentPermission).where(
            M.AgentPermission.agent_id == agent_id, M.AgentPermission.permission.in_(extra)))
    await s.flush()


async def revoke_permission(s: AsyncSession, agent_id: uuid.UUID, permission: str) -> None:
    await s.execute(delete(M.AgentPermission).where(
        M.AgentPermission.agent_id == agent_id, M.AgentPermission.permission == permission))


# ── Tasks ─────────────────────────────────────────────────────────────────────
async def list_tasks(s: AsyncSession, user_id: uuid.UUID, *, status: str | None = None,
                     limit: int = 100) -> Sequence[M.Task]:
    q = select(M.Task).where(M.Task.user_id == user_id, M.Task.deleted_at.is_(None))
    if status:
        q = q.where(M.Task.status == status)
    q = q.order_by(M.Task.due_date.asc().nullslast(), M.Task.created_at.desc()).limit(limit)
    return (await s.execute(q)).scalars().all()


async def create_task(s: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, title: str,
                      status: str = "pending", priority: str = "medium", source: str | None = None,
                      due_date: datetime | None = None, notes: str | None = None,
                      agent_id: uuid.UUID | None = None) -> M.Task:
    task = M.Task(org_id=org_id, user_id=user_id, title=title, status=status, priority=priority,
                  source=source, due_date=due_date, notes=notes, agent_id=agent_id)
    s.add(task)
    await s.flush()
    return task


async def set_task_status(s: AsyncSession, task_id: uuid.UUID, status: str) -> None:
    await s.execute(update(M.Task).where(M.Task.id == task_id).values(status=status))


# ── Runs ──────────────────────────────────────────────────────────────────────
async def create_run(s: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, agent_id: uuid.UUID,
                     goal: str | None = None, trigger: str = "chat",
                     parent_run_id: uuid.UUID | None = None, model_key: str | None = None) -> M.AgentRun:
    run = M.AgentRun(org_id=org_id, user_id=user_id, agent_id=agent_id, goal=goal, trigger=trigger,
                     parent_run_id=parent_run_id, model_key=model_key, status="running",
                     started_at=func.now())
    s.add(run)
    await s.flush()
    return run


async def save_run_state(s: AsyncSession, run_id: uuid.UUID, state: dict) -> None:
    await s.execute(update(M.AgentRun).where(M.AgentRun.id == run_id).values(state=state))


async def finish_run(s: AsyncSession, run_id: uuid.UUID, *, status: str = "completed",
                     tokens_in: int = 0, tokens_out: int = 0, cost_micros: int = 0,
                     error: str | None = None) -> None:
    await s.execute(update(M.AgentRun).where(M.AgentRun.id == run_id).values(
        status=status, tokens_in=tokens_in, tokens_out=tokens_out, cost_micros=cost_micros,
        error=error, finished_at=func.now()))


# ── Approvals (the hard gate) ─────────────────────────────────────────────────
async def create_approval(s: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, tool_key: str,
                          payload: dict, preview: str | None = None, action_type: str | None = None,
                          agent_id: uuid.UUID | None = None,
                          run_id: uuid.UUID | None = None) -> M.Approval:
    ap = M.Approval(org_id=org_id, user_id=user_id, tool_key=tool_key, payload=payload,
                    preview=preview, action_type=action_type, agent_id=agent_id, run_id=run_id,
                    status="pending")
    s.add(ap)
    await s.flush()
    return ap


async def get_approval(s: AsyncSession, approval_id: uuid.UUID) -> Optional[M.Approval]:
    return (await s.execute(select(M.Approval).where(M.Approval.id == approval_id))).scalar_one_or_none()


async def list_approvals(s: AsyncSession, user_id: uuid.UUID, *, status: str = "pending",
                         limit: int = 100) -> Sequence[M.Approval]:
    q = select(M.Approval).where(M.Approval.user_id == user_id)
    if status:
        q = q.where(M.Approval.status == status)
    q = q.order_by(M.Approval.created_at.desc()).limit(limit)
    return (await s.execute(q)).scalars().all()


async def decide_approval(s: AsyncSession, approval_id: uuid.UUID, *, status: str,
                          decided_by: uuid.UUID | None = None, result: str | None = None) -> None:
    """status ∈ {approved, rejected}. Guarded to pending → decided only."""
    await s.execute(update(M.Approval)
                    .where(M.Approval.id == approval_id, M.Approval.status == "pending")
                    .values(status=status, decided_by=decided_by, result=result,
                            decided_at=func.now()))


# ── Events (telemetry / analytics source of truth) ────────────────────────────
async def log_event(s: AsyncSession, *, kind: str, name: str | None = None,
                    org_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None,
                    agent_id: uuid.UUID | None = None, run_id: uuid.UUID | None = None,
                    success: bool | None = None, duration_ms: int | None = None,
                    cost_micros: int | None = None, meta: dict[str, Any] | None = None) -> None:
    s.add(M.Event(kind=kind, name=name, org_id=org_id, user_id=user_id, agent_id=agent_id,
                  run_id=run_id, success=success, duration_ms=duration_ms, cost_micros=cost_micros,
                  meta=meta or {}))
