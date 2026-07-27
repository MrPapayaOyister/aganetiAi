"""Alembic environment — SYNC (psycopg2) against the same Postgres the app uses.

The app runs async (asyncpg); migrations are one-shot DDL, so we use a sync engine
(psycopg2, already installed for backend/db/sync.py) to avoid asyncpg event-loop
bridging. The URL is derived from AGANETI_DATABASE_URL by swapping the driver, exactly
like backend/db/sync.py:_sync_url. target_metadata = Base.metadata via importing the
models module directly (not the db package's async repo).
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Repo root on sys.path (no packaging yet) — mirrors backend/ingest.py's pattern.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config.settings  # noqa: E402,F401  (loads .env-derived settings)
from backend.db.base import Base, database_url  # noqa: E402
import backend.db.models  # noqa: E402,F401  (registers every table on Base.metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    url = database_url() or os.getenv("AGANETI_DATABASE_URL", "")
    return url.replace("+asyncpg", "+psycopg2")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(), target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"},
        compare_type=True, compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata,
            compare_type=True, compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
