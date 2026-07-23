"""Local chart-definition registry (the writable half of the split).

The real Azure data source is READ-ONLY (the reader can't persist anything), so
chart DEFINITIONS live in a small, disposable local SQLite file next to this module.
Only chart configs + board membership live here; all chart DATA is queried live from
CORE-SHARE at render time.

Ported from Hermes ``daralber/db.py`` (the dashboard_configs DDL) and the refined
``daralber/chart_service.py`` board-scoping (strict per-board + a short
include_unclaimed claim window + delete_board — the anti-"ghosting" version).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from sqlalchemy import create_engine, inspect, text

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_configs.db")
engine = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"timeout": 30})

#: How recently an untagged (board_id NULL) agent chart must have been created to be
#: eligible for claiming by a reconcile. Long enough to cover any single chart turn;
#: short enough that a stale orphan from an old session never resurfaces on a later
#: board. Ported from Hermes chart_service._UNCLAIMED_WINDOW_SECONDS.
_UNCLAIMED_WINDOW_SECONDS = 600


def init_dashboard_configs_table() -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS dashboard_configs (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                sql TEXT NOT NULL,
                created_by TEXT DEFAULT 'system',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                board_id TEXT
            )
        """))
    existing = {c["name"] for c in inspect(engine).get_columns("dashboard_configs")}
    if "board_id" not in existing:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE dashboard_configs ADD COLUMN board_id TEXT"))


# ── reads ─────────────────────────────────────────────────────────────────────
def all_configs_for_dedup(board_id: str = "") -> list[dict]:
    """Rows the dedup check compares against — STRICTLY the given board's own charts.

    Ported from Hermes's fix: the old scope (``board_id IS NULL OR board_id = :bid``)
    let a chart with no/other board leak a false duplicate across every board (a
    fresh board wrongly matched a chart living elsewhere). Now a board only ever
    dedups against itself; with no board_id there is nothing to scope to, so return
    nothing (the caller then skips dedup)."""
    if not board_id:
        return []
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(
            text("SELECT id, type, title, sql FROM dashboard_configs WHERE board_id = :bid"),
            {"bid": board_id}).fetchall()]


def list_configs(board_id: str | None = None, include_unclaimed: bool = False) -> list[dict]:
    """Return chart configs (NO data — the caller renders each against CORE-SHARE).

    With *board_id*: STRICTLY that board's charts (a fresh board inherits nothing —
    the anti-ghosting fix). *include_unclaimed* additionally returns agent charts
    still carrying no board, created within the recent window, so a just-saved chart
    the agent forgot to tag is caught and can be claimed. Without a board_id, returns
    every chart (admin/analytics callers).
    """
    if board_id:
        where = "board_id = :bid"
        args: dict = {"bid": board_id}
        if include_unclaimed:
            cutoff = (datetime.utcnow() - timedelta(seconds=_UNCLAIMED_WINDOW_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
            where = ("(board_id = :bid OR (board_id IS NULL AND created_by = 'agent' "
                     "AND created_at >= :cutoff))")
            args["cutoff"] = cutoff
        sql = ("SELECT id, type, title, sql, created_by, board_id FROM dashboard_configs "
               f"WHERE {where} ORDER BY created_at")
    else:
        sql = "SELECT id, type, title, sql, created_by, board_id FROM dashboard_configs ORDER BY created_at"
        args = {}
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), args).fetchall()]


def get_config(chart_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, type, title, sql, created_by, board_id FROM dashboard_configs WHERE id = :id"),
            {"id": chart_id},
        ).fetchone()
    return dict(row._mapping) if row else None


def list_boards() -> list[dict]:
    """Boards that have at least one chart: [{board_id, chart_count, last_created}]."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT board_id, COUNT(*) AS chart_count, MAX(created_at) AS last_created "
            "FROM dashboard_configs WHERE board_id IS NOT NULL GROUP BY board_id "
            "ORDER BY last_created DESC")).fetchall()
    return [dict(r._mapping) for r in rows]


# ── writes ──────────────────────────────────────────────────────────────────--
def insert_chart(chart_id: str, chart_type: str, title: str, sql: str,
                 created_by: str = "agent", board_id: str = "") -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO dashboard_configs (id, type, title, sql, created_by, board_id) "
                 "VALUES (:id, :type, :title, :sql, :cb, :board_id)"),
            {"id": chart_id, "type": chart_type, "title": title, "sql": sql,
             "cb": created_by, "board_id": board_id or None},
        )


def delete_chart(chart_id: str) -> bool:
    with engine.begin() as conn:
        result = conn.execute(text("DELETE FROM dashboard_configs WHERE id = :id"), {"id": chart_id})
    return bool(result.rowcount)


def assign_board(chart_id: str, board_id: str) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            text("UPDATE dashboard_configs SET board_id = :bid WHERE id = :id"),
            {"bid": board_id, "id": chart_id})
    return bool(result.rowcount)


def delete_board(board_id: str) -> int:
    """Delete every chart belonging to *board_id*. Returns how many were removed."""
    if not board_id:
        return 0
    with engine.begin() as conn:
        result = conn.execute(text("DELETE FROM dashboard_configs WHERE board_id = :bid"), {"bid": board_id})
    return int(result.rowcount or 0)
