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

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, Text,
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
    settings: Mapped[dict] = mapped_column(JSONB, nullable=True, server_default=text("'{}'::jsonb"))  # UI/user prefs (nullable in live DB; default fills it)


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


# ── RAG lineage (P1): canonical source/chunk provenance + email/meeting envelopes ──
# documents/document_chunks are the Postgres source-of-truth for the retrieve→cite→act
# layer; Qdrant holds the vectors (1 chunk = 1 point via qdrant_point_id). email_* and
# meeting_* are thin envelope tables giving threads/transcripts real 1:N structure.
class Document(Base, TS):
    """One ingested source: a file upload, an email message, or a meeting transcript."""
    __tablename__ = "documents"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)                    # owner (email/meeting=real user; file=uploader or __org__ curator)
    source_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'file'"))  # file|email|meeting
    source_id: Mapped[str | None] = mapped_column(Text)     # gmail thread/msg id, calendar event id, or filename
    title: Mapped[str | None] = mapped_column(Text)
    uri: Mapped[str | None] = mapped_column(Text)           # storage path / external ref
    content_hash: Mapped[str | None] = mapped_column(Text)  # sha256 of full source (file-level change detection)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    sensitivity: Mapped[str] = mapped_column(Text, server_default=text("'internal'"))
    workspace: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    __table_args__ = (UniqueConstraint("user_id", "content_hash", name="uq_document_user_hash"),
                      Index("ix_document_owner_type", "user_id", "source_type"))


class DocumentChunk(Base, TS):
    """One embedded chunk == one Qdrant point (1:1 via qdrant_point_id)."""
    __tablename__ = "document_chunks"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    document_id = fk("documents.id", index=True)            # ondelete CASCADE (default) — doc delete cleans chunk rows
    source_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'file'"))
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(Text)  # sha256 of NORMALIZED chunk text (per-chunk change detection)
    heading_path: Mapped[dict] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    sensitivity: Mapped[str] = mapped_column(Text, server_default=text("'internal'"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    embed_model: Mapped[str | None] = mapped_column(Text)
    qdrant_point_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # payload timestamp (since-filter)
    __table_args__ = (UniqueConstraint("qdrant_point_id", name="uq_chunk_point"),
                      Index("ix_chunk_owner_type", "user_id", "source_type"))


class EmailThread(Base, TS):
    __tablename__ = "email_threads"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)                    # NON-NULL — private mailbox
    gmail_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    participants: Mapped[dict] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    __table_args__ = (UniqueConstraint("user_id", "gmail_thread_id", name="uq_thread_user_gmail"),)


class EmailMessage(Base, TS):
    __tablename__ = "email_messages"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    thread_id = fk("email_threads.id", index=True)
    document_id = fk("documents.id", ondelete="SET NULL", nullable=True)  # chunks link via documents
    gmail_msg_id: Mapped[str] = mapped_column(Text, nullable=False)
    sender: Mapped[str | None] = mapped_column(Text)
    recipients: Mapped[dict] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    direction: Mapped[str | None] = mapped_column(Text)     # inbound|outbound
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("user_id", "gmail_msg_id", name="uq_msg_user_gmail"),)


class Meeting(Base, TS):
    __tablename__ = "meetings"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)                    # NON-NULL — private
    calendar_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attendees: Mapped[dict] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    __table_args__ = (UniqueConstraint("user_id", "calendar_event_id", name="uq_meeting_user_event"),)


class MeetingSegment(Base, TS):
    __tablename__ = "meeting_segments"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    meeting_id = fk("meetings.id", index=True)
    document_id = fk("documents.id", ondelete="SET NULL", nullable=True)
    speaker: Mapped[str | None] = mapped_column(Text)
    t_start: Mapped[int | None] = mapped_column(Integer)    # seconds offset from meeting start
    t_end: Mapped[int | None] = mapped_column(Integer)


class GmailSyncState(Base, TS):
    """Per-user incremental Gmail sync cursor (history.list startHistoryId)."""
    __tablename__ = "gmail_sync_state"
    id = pk()
    org_id = fk("organizations.id", index=True)
    user_id = fk("users.id", index=True)
    history_id: Mapped[str | None] = mapped_column(Text)
    page_token: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("user_id", name="uq_gmailsync_user"),)


# ── Chat session registry (metadata only) ─────────────────────────────────────
# Message BODIES stay in the JSON store (orchestrator/conversation.py, keyed by
# this session id). This row just makes a user's chat threads enumerable + titled
# for the history dropdown. id is the CLIENT-supplied uuid (lazy upsert on 1st msg).
class ChatSession(Base, TS):
    __tablename__ = "chat_sessions"
    id = pk()
    org_id = fk("organizations.id", ondelete="SET NULL", nullable=True, index=True)
    user_id = fk("users.id", index=True)
    title: Mapped[str | None] = mapped_column(Text)
    title_source: Mapped[str] = mapped_column(Text, server_default=text("'auto'"))  # auto|user
    message_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # --- product-freeze additions -------------------------------------------------
    # pinned/kind/board_id let the History page pin threads and tell an analytics
    # thread (which owns a chart board) from a plain conversation. board_id mirrors
    # the 32-hex id /dashboard/board-session mints, so save_chart needs no changes.
    pinned: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(Text, server_default=text("'assistant'"), nullable=False)
    board_id: Mapped[str | None] = mapped_column(Text)
    last_message_preview: Mapped[str | None] = mapped_column(Text)
    artifact_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    __table_args__ = (
        Index("ix_chat_sessions_user_active", "user_id", "archived", "last_message_at"),
        Index("ix_chat_sessions_user_pinned", "user_id", "pinned", "last_message_at",
              postgresql_where=text("deleted_at IS NULL")),
    )


# --- Chat messages (the durable thread) ---------------------------------------
# One row per turn. Replaces the 24-turn flat-JSON store: nothing is ever silently
# dropped, and `pipeline` keeps the per-stage trace of a multi-agent answer so a
# reopened thread can show how it was produced.
class ChatMessage(Base, TS):
    __tablename__ = "chat_messages"
    id = pk()
    org_id = fk("organizations.id", ondelete="SET NULL", nullable=True, index=True)
    user_id = fk("users.id", index=True)
    session_id = fk("chat_sessions.id", index=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)      # 1-based, per session
    role: Mapped[str] = mapped_column(Text, nullable=False)           # user|assistant|tool|system
    content: Mapped[str | None] = mapped_column(Text)                 # markdown / plain text
    status: Mapped[str] = mapped_column(Text, server_default=text("'complete'"), nullable=False)
    agent: Mapped[str | None] = mapped_column(Text)                   # primary|analytics|planner|query|...
    model_key: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[dict] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(Text)
    pipeline: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    cost_micros: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_chat_messages_session_seq"),
        Index("ix_chat_messages_session_seq", "session_id", "seq"),
        Index("ix_chat_messages_user_created", "user_id", "created_at"),
    )


# --- Chat artifacts (charts / tables / PDFs rendered in a turn) ----------------
# `spec` says how to reproduce it; `data` is a SNAPSHOT of what the user actually
# saw. Both matter: re-querying live would show different numbers on reload, and
# permanent history must not depend on the disposable dashboard_configs.db.
class ChatArtifact(Base, TS):
    __tablename__ = "chat_artifacts"
    id = pk()
    org_id = fk("organizations.id", ondelete="SET NULL", nullable=True, index=True)
    user_id = fk("users.id", index=True)
    session_id = fk("chat_sessions.id", index=True)
    message_id = fk("chat_messages.id", nullable=True, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)           # chart|table|pdf|card|image|sql
    title: Mapped[str | None] = mapped_column(Text)
    spec: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    data: Mapped[dict | None] = mapped_column(JSONB)
    uri: Mapped[str | None] = mapped_column(Text)
    byte_size: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    __table_args__ = (Index("ix_chat_artifacts_session", "session_id", "created_at"),)


# --- Media sources (live TV channels, news feeds) ------------------------------
# A LIBRARY, not a per-session preference. Tool toggles answer "may this chat use
# Live TV at all"; this answers "which channels exist". A curated list belongs to
# the person, not the thread, so there is deliberately no session scope.
#
# user_id NULL = a global/seeded row everyone sees; set = one person's addition.
# That gives a curated default list plus personal additions without a second
# mechanism.
#
# last_status/last_checked_at exist because a dead stream must fail VISIBLY at
# add time, not silently at play time — and rot later on. This is why it is a
# table rather than a JSONB blob on the user: we sort and filter on status.
class MediaSource(Base, TS):
    __tablename__ = "media_sources"
    id = pk()
    org_id = fk("organizations.id", ondelete="SET NULL", nullable=True, index=True)
    user_id = fk("users.id", nullable=True, index=True)   # NULL = global/seeded
    # tv | radio | news. ALSO selects the validation path — see
    # media_sources.validate_source: tv means HLS (needs #EXTM3U and an
    # opaque-origin CORS grant), radio means a continuous audio stream (no CORS
    # requirement, because a media element does not need one).
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    # Radio only: rendered as the card's badges, so a saved station looks like
    # the live result it was saved from.
    codec: Mapped[str | None] = mapped_column(Text)
    bitrate: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)
    seeded: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(Text)          # ok | <reason>
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    __table_args__ = (
        Index("ix_media_sources_kind_user", "kind", "user_id", "enabled"),
    )


# --- Directory cache (iptv-org) -----------------------------------------------
# A CACHE OF UPSTREAM, not a place user channels live — those stay in
# media_sources. Refreshed daily; rows are replaced wholesale in one transaction
# so a failed refresh can never leave a partially-populated directory.
#
# Only structurally usable entries are stored: https, no custom User-Agent or
# Referrer (a browser cannot set either, so such a stream passes a server-side
# check and then dies in the player), not closed, not NSFW. Filtering on the way
# IN means every read is already safe.
class MediaDirectory(Base, TS):
    __tablename__ = "media_directory"
    id = pk()
    source: Mapped[str] = mapped_column(Text, nullable=False)      # iptv-org
    ext_id: Mapped[str] = mapped_column(Text, nullable=False)      # upstream channel id
    name: Mapped[str] = mapped_column(Text, nullable=False)
    alt_names: Mapped[dict] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    country: Mapped[str | None] = mapped_column(Text, index=True)
    categories: Mapped[dict] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    quality: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("ix_media_directory_name", "name"),
        Index("ix_media_directory_source", "source"),
    )


# Refresh bookkeeping, one row per source. Kept separate from the rows it
# describes so a failed refresh can record WHY without touching the data it
# failed to replace.
class MediaDirectoryState(Base, TS):
    __tablename__ = "media_directory_state"
    id = pk()
    source: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)


# --- Long-term memory (cross-thread facts) ------------------------------------
# Postgres is the system of record (stable ordering, pagination, dedup constraint,
# soft-delete, cascade with the user); Qdrant user_memory_{ext_uid} stays the
# retrieval index. vector_id links them; meta.ext_uid carries the EXTERNAL identity
# the Qdrant collection is named after, since user_id here is the internal uuid.
class MemoryItem(Base, TS):
    __tablename__ = "memory_items"
    id = pk()
    org_id = fk("organizations.id", ondelete="SET NULL", nullable=True, index=True)
    user_id = fk("users.id", index=True)
    fact: Mapped[str] = mapped_column(Text, nullable=False)
    fact_norm: Mapped[str] = mapped_column(Text, nullable=False)      # lowercased/collapsed, for dedup
    kind: Mapped[str] = mapped_column(Text, server_default=text("'fact'"), nullable=False)
    source: Mapped[str] = mapped_column(Text, server_default=text("'auto'"), nullable=False)
    source_session_id = fk("chat_sessions.id", ondelete="SET NULL", nullable=True)
    confidence: Mapped[float] = mapped_column(Float, server_default=text("0.6"), nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"), nullable=False)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="SET NULL"), nullable=True)
    vector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    use_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    __table_args__ = (
        Index("uq_memory_user_factnorm", "user_id", "fact_norm", unique=True,
              postgresql_where=text("status = 'active' AND deleted_at IS NULL")),
        Index("ix_memory_user_status", "user_id", "status", "updated_at"),
    )
