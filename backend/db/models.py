"""Enterprise Agentic OS — async SQLAlchemy schema (Postgres).

v1 core: org hierarchy, per-user agents + granular permissions + runs, the hard
approval gate, and the ported work tables (tasks/events/delegations/etc.). Later
phases add meetings/documents/analytics_snapshots/alerts/llm_* per the canonical
spec — same conventions.

Conventions: UUID PK (gen_random_uuid), created_at/updated_at/deleted_at
TIMESTAMPTZ, org_id on user-data tables, JSONB for config/metadata, real FKs.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def fk(target: str, ondelete: str = "CASCADE", nullable: bool = False, index: bool = False):
    return mapped_column(UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable, index=index)


class TS:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"),
                                                 onupdate=text("now()"), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Org hierarchy ─────────────────────────────────────────────────────────────
class Organization(Base, TS):
    __tablename__ = "organizations"
    id = pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Department(Base, TS):
    __tablename__ = "departments"
    id = pk()
    org_id = fk("organizations.id", index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id = fk("departments.id", ondelete="SET NULL", nullable=True)
    head_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_dept_org_name"),)


class Designation(Base, TS):
    __tablename__ = "designations"
    id = pk()
    org_id = fk("organizations.id", index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    scopes: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    __table_args__ = (UniqueConstraint("org_id", "title", name="uq_desig_org_title"),)


class User(Base, TS):
    __tablename__ = "users"
    id = pk()
    org_id = fk("organizations.id", index=True)
    supabase_uid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str | None] = mapped_column(Text)
    department_id = fk("departments.id", ondelete="SET NULL", nullable=True)
    designation_id = fk("designations.id", ondelete="SET NULL", nullable=True)
    role: Mapped[str] = mapped_column(Text, server_default=text("'employee'"))  # employee|manager|admin
    manager_id = fk("users.id", ondelete="SET NULL", nullable=True)
    primary_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    locale: Mapped[str] = mapped_column(Text, server_default=text("'en'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    settings: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # UI/user prefs


class EmployeeProfile(Base, TS):
    __tablename__ = "employee_profiles"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    job_description: Mapped[str | None] = mapped_column(Text)
    responsibilities: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    working_hours: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    seed_prompt: Mapped[str | None] = mapped_column(Text)
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("user_id", name="uq_profile_user"),)


# ── Agents ────────────────────────────────────────────────────────────────────
class Agent(Base, TS):
    __tablename__ = "agents"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # primary|specialist
    template_key: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    persona: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model_key: Mapped[str | None] = mapped_column(Text)
    fallback_models: Mapped[dict] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))  # active|paused|archived
    config: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    department_id = fk("departments.id", ondelete="SET NULL", nullable=True)
    # One active primary agent per user (partial unique index) — backstops the
    # onboarding/create-agent read-then-create race at the DB layer.
    __table_args__ = (
        Index("uq_one_primary_per_user", "user_id", unique=True,
              postgresql_where=text("kind = 'primary' AND deleted_at IS NULL")),
    )


class AgentPermission(Base, TS):
    __tablename__ = "agent_permissions"
    id = pk()
    org_id = fk("organizations.id", index=True)
    agent_id = fk("agents.id", index=True)
    permission: Mapped[str] = mapped_column(Text, nullable=False)  # resource.action
    is_outbound: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    granted_by = fk("users.id", ondelete="SET NULL", nullable=True)
    __table_args__ = (UniqueConstraint("agent_id", "permission", name="uq_agentperm"),)


class AgentRun(Base, TS):
    __tablename__ = "agent_runs"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    agent_id = fk("agents.id", index=True)
    parent_run_id = fk("agent_runs.id", ondelete="SET NULL", nullable=True)
    trigger: Mapped[str | None] = mapped_column(Text)  # chat|delegation|schedule|alert
    goal: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    state: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # messages / checkpoint
    model_key: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_micros: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(Base, TS):
    __tablename__ = "approvals"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    agent_id = fk("agents.id", ondelete="SET NULL", nullable=True)
    run_id = fk("agent_runs.id", ondelete="CASCADE", nullable=True)
    tool_key: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    preview: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    result: Mapped[str | None] = mapped_column(Text)
    decided_by = fk("users.id", ondelete="SET NULL", nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Ported work tables ────────────────────────────────────────────────────────
class Task(Base, TS):
    __tablename__ = "tasks"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    assignee_id = fk("users.id", ondelete="SET NULL", nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    priority: Mapped[str] = mapped_column(Text, server_default=text("'medium'"))
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    agent_id = fk("agents.id", ondelete="SET NULL", nullable=True)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    success: Mapped[bool | None] = mapped_column(Boolean)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    cost_micros: Mapped[int | None] = mapped_column(BigInteger)
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Delegation(Base, TS):
    __tablename__ = "delegations"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    from_agent_id = fk("agents.id", ondelete="SET NULL", nullable=True)
    to_agent_id = fk("agents.id", ondelete="SET NULL", nullable=True)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class AgentMessage(Base, TS):
    __tablename__ = "agent_messages"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", ondelete="SET NULL", nullable=True)
    from_agent: Mapped[str] = mapped_column(Text, nullable=False)
    to_agent: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, server_default=text("'message'"))
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Initiative(Base, TS):
    __tablename__ = "initiatives"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Schedule(Base, TS):
    __tablename__ = "schedules"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    cron_expression: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    action_payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    agent_id = fk("agents.id", ondelete="SET NULL", nullable=True)


class Contact(Base, TS):
    __tablename__ = "contacts"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", ondelete="SET NULL", nullable=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    nickname: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    is_agent: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text)


# ── Predictive-intelligence signal tables ─────────────────────────────────────
class Opportunity(Base, TS):
    """A tracked deal/opportunity — the grounding row for predict_deal_outcome
    (which refuses to forecast a profit without a real value on file)."""
    __tablename__ = "opportunities"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    counterparty: Mapped[str | None] = mapped_column(Text)          # person / company
    value_amount: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))  # currency units
    currency: Mapped[str] = mapped_column(Text, server_default=text("'USD'"))
    stage: Mapped[str] = mapped_column(Text, server_default=text("'prospect'"))
    # prospect|qualified|proposal|negotiation|won|lost
    probability: Mapped[int] = mapped_column(Integer, server_default=text("50"))  # 0-100, manual/base
    expected_close: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))


class Interaction(Base):
    """One touch with a contact (email or meeting), backfilled from Gmail/Calendar.
    Powers relationship-value trends (frequency, recency, direction)."""
    __tablename__ = "interactions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    contact_email: Mapped[str | None] = mapped_column(Text, index=True)
    contact_name: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)              # email|meeting
    direction: Mapped[str | None] = mapped_column(Text)     # inbound|outbound
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), index=True)
    subject: Mapped[str | None] = mapped_column(Text)
    ref_id: Mapped[str | None] = mapped_column(Text)        # gmail msg id / calendar event id (idempotency)
    __table_args__ = (UniqueConstraint("user_id", "channel", "ref_id", name="uq_interaction_ref"),)
