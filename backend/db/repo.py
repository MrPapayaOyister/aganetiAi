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
from datetime import datetime, timezone
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
    # MULTI-TENANT: one Organization per email domain. The slug is the FULL domain
    # (injective — no dotted/hyphen collision merging tenants); the name is the first
    # label. Only when the caller didn't pin a slug (config-registry legacy still can).
    if not org_slug or org_slug == DEFAULT_ORG_SLUG:
        domain = (email or "").rsplit("@", 1)[-1].lower().strip()
        org_slug = domain or DEFAULT_ORG_SLUG
    org_name = (org_slug.split(".")[0] if "." in org_slug else org_slug).title()
    org = await get_org_by_slug(s, org_slug)
    if org is None:
        # ON CONFLICT so two concurrent first-logins don't both create the org.
        await s.execute(_pg_insert(M.Organization.__table__)
                        .values(name=org_name, slug=org_slug)
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


# ── Opportunities (deals) — grounding for predict_deal_outcome ─────────────────
async def create_opportunity(s: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, title: str,
                             value_amount: int = 0, counterparty: str | None = None, currency: str = "USD",
                             stage: str = "prospect", probability: int = 50,
                             expected_close: datetime | None = None, notes: str | None = None) -> M.Opportunity:
    op = M.Opportunity(org_id=org_id, user_id=user_id, title=title, value_amount=int(value_amount or 0),
                       counterparty=counterparty, currency=currency, stage=stage,
                       probability=int(probability or 50), expected_close=expected_close, notes=notes)
    s.add(op)
    await s.flush()
    return op


async def list_opportunities(s: AsyncSession, user_id: uuid.UUID, *, status: str = "open",
                             limit: int = 50) -> Sequence[M.Opportunity]:
    q = select(M.Opportunity).where(M.Opportunity.user_id == user_id, M.Opportunity.deleted_at.is_(None))
    if status:
        q = q.where(M.Opportunity.status == status)
    return (await s.execute(q.order_by(M.Opportunity.value_amount.desc()).limit(limit))).scalars().all()


async def find_opportunity(s: AsyncSession, user_id: uuid.UUID, title_like: str) -> Optional[M.Opportunity]:
    q = (select(M.Opportunity).where(M.Opportunity.user_id == user_id, M.Opportunity.deleted_at.is_(None),
                                     M.Opportunity.title.ilike(f"%{title_like}%"))
         .order_by(M.Opportunity.created_at.desc()).limit(1))
    return (await s.execute(q)).scalars().first()


# ── Interactions — relationship signal (backfilled from Gmail/Calendar) ───────
async def record_interaction(s: AsyncSession, *, org_id, user_id, contact_email, channel,
                             ref_id, contact_name=None, direction=None, subject=None, ts=None) -> None:
    from sqlalchemy.dialects.postgresql import insert as _pg
    await s.execute(_pg(M.Interaction.__table__).values(
        org_id=org_id, user_id=user_id, contact_email=(contact_email or "").lower(), contact_name=contact_name,
        channel=channel, direction=direction, subject=subject, ref_id=ref_id, **({"ts": ts} if ts else {}))
        .on_conflict_do_nothing(index_elements=["user_id", "channel", "ref_id"]))


async def interaction_stats(s: AsyncSession, user_id: uuid.UUID, contact_email: str) -> dict:
    rows = (await s.execute(select(M.Interaction).where(
        M.Interaction.user_id == user_id,
        M.Interaction.contact_email == (contact_email or "").lower()))).scalars().all()
    if not rows:
        return {"count": 0, "last": None, "inbound": 0, "outbound": 0}
    return {"count": len(rows), "last": max((r.ts for r in rows if r.ts), default=None),
            "inbound": sum(1 for r in rows if r.direction == "inbound"),
            "outbound": sum(1 for r in rows if r.direction == "outbound")}


# ── Chat sessions — metadata registry for the history dropdown ─────────────────
# Message bodies live in orchestrator/conversation.py JSON (keyed by session_id);
# these rows only make a user's threads enumerable + titled. Every query is
# ownership-scoped by user_id (session ids are client-generated → IDOR guard).
async def upsert_and_touch_chat_session(s: AsyncSession, *, session_id, user_id, org_id,
                                        title: str, count: int) -> None:
    """Lazy-create on first message (ON CONFLICT DO NOTHING preserves the first title),
    then bump message_count + last_message_at on every turn."""
    from sqlalchemy.dialects.postgresql import insert as _pg
    await s.execute(_pg(M.ChatSession.__table__).values(
        id=session_id, user_id=user_id, org_id=org_id, title=title,
        message_count=count, last_message_at=func.now())
        .on_conflict_do_nothing(index_elements=["id"]))
    await s.execute(update(M.ChatSession)
                    .where(M.ChatSession.id == session_id, M.ChatSession.user_id == user_id)
                    .values(message_count=count, last_message_at=func.now()))


async def list_chat_sessions(s: AsyncSession, user_id: uuid.UUID, archived: bool = False,
                             limit: int = 100) -> Sequence[M.ChatSession]:
    return (await s.execute(select(M.ChatSession).where(
        M.ChatSession.user_id == user_id, M.ChatSession.archived == archived,
        M.ChatSession.deleted_at.is_(None))
        .order_by(M.ChatSession.last_message_at.desc()).limit(limit))).scalars().all()


async def search_chat_sessions(s: AsyncSession, user_id: uuid.UUID, *, archived: bool = False,
                               q: str | None = None, date_from=None, date_to=None,
                               limit: int = 200):
    """Sessions for the History page. Pinned first, then most-recent.

    Title search is a plain ILIKE: at ~100 sessions per user it is instant, and a
    pg_trgm index would need CREATE EXTENSION, which this role may not have — a
    migration that fails halfway is a worse outcome than a sequential scan here.
    """
    stmt = select(M.ChatSession).where(
        M.ChatSession.user_id == user_id,
        M.ChatSession.archived.is_(archived),
        M.ChatSession.deleted_at.is_(None),
    )
    if q:
        stmt = stmt.where(M.ChatSession.title.ilike(f"%{q}%"))
    if date_from is not None:
        stmt = stmt.where(M.ChatSession.last_message_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(M.ChatSession.last_message_at <= date_to)
    stmt = stmt.order_by(M.ChatSession.pinned.desc(),
                         M.ChatSession.last_message_at.desc().nullslast()).limit(limit)
    return (await s.execute(stmt)).scalars().all()


async def set_chat_session_pinned(s: AsyncSession, session_id, user_id, pinned: bool) -> bool:
    row = (await s.execute(select(M.ChatSession).where(
        M.ChatSession.id == session_id, M.ChatSession.user_id == user_id))).scalar_one_or_none()
    if row is None:
        return False
    row.pinned = bool(pinned)
    row.pinned_at = datetime.now(timezone.utc) if pinned else None
    return True


async def rename_chat_session(s: AsyncSession, session_id, user_id, title: str) -> bool:
    r = await s.execute(update(M.ChatSession).where(
        M.ChatSession.id == session_id, M.ChatSession.user_id == user_id,
        M.ChatSession.deleted_at.is_(None)).values(title=title, title_source="user"))
    return (r.rowcount or 0) > 0


async def delete_chat_session(s: AsyncSession, session_id, user_id) -> bool:
    r = await s.execute(update(M.ChatSession).where(
        M.ChatSession.id == session_id, M.ChatSession.user_id == user_id)
        .values(deleted_at=func.now()))
    return (r.rowcount or 0) > 0
