"""Async SQLAlchemy engine + session for the Enterprise Agentic OS (Postgres).

Additive during Phase A: the app keeps running on SQLite while these Postgres
models + engine are built and the data is migrated. Cutover (swapping the data
layer + the executor checkpointer to Postgres) is a separate, gated step.
"""
from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


def database_url() -> str:
    u = os.getenv("AGANETI_DATABASE_URL")
    if u:
        return u
    try:
        for line in open(os.path.expanduser("~/aganetiAi/.env")):
            if line.startswith("AGANETI_DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


DATABASE_URL = database_url()

engine = create_async_engine(DATABASE_URL, pool_size=10, max_overflow=20,
                             pool_pre_ping=True, pool_recycle=1800)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
