"""Synchronous Postgres access for the sync code paths.

tasks.store / events / analytics are plain sync functions called from BOTH sync
(scheduler) and async (routes) contexts, so they cannot await the async repo. This
gives them a sync SQLAlchemy engine (psycopg2) over the SAME ORM models, plus a
sync identity resolver mirroring repo.resolve_user (uuid | supabase_uid | email |
config alias 'user_1'). Selected by env AGANETI_DATA_BACKEND (default sqlite).
"""
from __future__ import annotations

import os
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .base import database_url
from . import models as M

BACKEND = os.getenv("AGANETI_DATA_BACKEND", "sqlite")


def _sync_url() -> str:
    u = database_url()
    return u.replace("+asyncpg", "+psycopg2") if u else ""


_engine = None
_Session = None


def _ensure() -> None:
    global _engine, _Session
    if _engine is None:
        _engine = create_engine(_sync_url(), pool_pre_ping=True, pool_size=5,
                                max_overflow=10, pool_recycle=1800, future=True)
        _Session = sessionmaker(bind=_engine, future=True)


def engine():
    _ensure()
    return _engine


def session() -> Session:
    _ensure()
    return _Session()


def resolve_user(s: Session, identity: str):
    if not identity:
        return None
    try:
        row = s.get(M.User, uuid.UUID(str(identity)))
        if row:
            return row
    except (ValueError, TypeError, AttributeError):
        pass
    row = s.execute(select(M.User).where(M.User.supabase_uid == identity)).scalar_one_or_none()
    if row:
        return row
    if "@" in identity:
        row = s.execute(select(M.User).where(M.User.email == identity)).scalar_one_or_none()
        if row:
            return row
    # See repo.resolve_user: the config-alias step is gone with the registry.
    return None


def resolve_ids(identity: str):
    """(user_uuid, org_uuid) or (None, None) — opens its own session."""
    with session() as s:
        u = resolve_user(s, identity)
        return (u.id, u.org_id) if u else (None, None)
