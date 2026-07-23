"""The 5 prompt-to-chart tools, in agenticAi's native Tool-registry format.

Ported from Hermes ``daralber/mcp_server.py``. The SQL-safety and duplicate-
detection logic (``validate_select_only``, ``_normalize_where_placeholder``,
``_chart_signature`` / dedup) is carried over VERBATIM — the whole point is not to
re-introduce bugs that were already fixed once. Only the surface changes: FastMCP
``@mcp.tool()`` functions become ``registry.Tool`` entries with async handlers.

Data queries hit the read-only Azure engine (coreshare_db); chart definitions are
persisted to the local SQLite registry (config_db). These tools are registered here
but deliberately NOT added to templates.PRIMARY_TOOLS, so the primary Assistant can
never call them — the /dashboard endpoint supplies them as its own allow-list.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

from backend.orchestrator.registry import Tool, register
from backend.dashboard import coreshare_db, config_db

config_db.init_dashboard_configs_table()

VALID_CHART_TYPES = {"kpi", "line", "bar", "pie"}


# ── dedup helpers (verbatim from Hermes mcp_server.py) ─────────────────────────
def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.replace("{where}", " ").strip().rstrip(";")).lower()


def _canonical_measure(expr: str) -> str:
    e = re.sub(r"\s+", "", expr.lower())
    m = re.fullmatch(r"sum\((.+)\)", e)
    return m.group(1) if m else e


_TRAILING_CLAUSE_TOKEN_RE = re.compile(
    r"\(|\)|\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b", re.IGNORECASE)


def _first_trailing_clause_index(sql: str) -> int:
    """Index of the first TOP-LEVEL GROUP BY / ORDER BY / HAVING / LIMIT, else len(sql).

    Paren-aware so a GROUP BY inside a subquery/function call doesn't count — the
    {where} placeholder must land in the OUTER query's WHERE position. Ported from
    Hermes db.first_trailing_clause_index."""
    depth = 0
    for m in _TRAILING_CLAUSE_TOKEN_RE.finditer(sql):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            return m.start()
    return len(sql)


def _normalize_where_placeholder(sql: str) -> str:
    """Guarantee the chart SQL has exactly one ``{where}`` in a VALID position.

    Agents omit ``{where}`` (esp. on KPI cards), place it AFTER ``GROUP BY``, or —
    when a chart carries its OWN WHERE (e.g. ``WHERE RequestStatusName='Approved'``)
    — leave it after their WHERE conditions. Fix it deterministically REGARDLESS of
    whether the query has its own WHERE: strip any existing placeholder(s) and
    re-insert exactly one immediately BEFORE the first top-level GROUP BY / ORDER BY
    / HAVING / LIMIT (or at the end), i.e. in the outer query's WHERE position.

    Ported from Hermes's fixed version. The old version bailed (returned the SQL
    untouched) whenever it saw a WHERE — which meant an own-WHERE chart that omitted
    {where} was wrongly rejected by the "must include {where}" guard in save_chart.
    """
    stripped = re.sub(r"\s*\{where\}\s*", " ", sql).strip().rstrip(";")
    idx = _first_trailing_clause_index(stripped)
    head = stripped[:idx].rstrip()
    tail = stripped[idx:].strip()
    out = head + " {where}"
    if tail:
        out += " " + tail
    return out.strip()


def _chart_filter_key(sql: str) -> str:
    """Normalized TOP-LEVEL WHERE conditions of the chart's own SQL (excluding the
    ``{where}`` placeholder), for the dedup signature.

    On the shared aid-request views most charts have the same source view, grouping
    column and measure, so the chart's own WHERE — e.g. ``RequestStatusName='Approved'``
    — is what actually distinguishes them. Without it, "approved requests by category"
    would collapse onto "requests by category". Returns '' when the chart carries no
    own filter. Paren-aware so a WHERE in a subquery is ignored. Ported from Hermes."""
    s = re.sub(r"\{where\}", " ", sql)
    depth = 0
    where_end = -1
    for m in re.finditer(r"\(|\)|\bwhere\b", s, re.IGNORECASE):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            where_end = m.end()
            break
    if where_end < 0:
        return ""
    tail = _first_trailing_clause_index(s)
    conds = s[where_end:tail] if tail > where_end else s[where_end:]
    return re.sub(r"\s+", " ", conds).strip().lower().rstrip(";")


def _chart_signature(chart_type: str, sql: str) -> tuple:
    # Filter (own WHERE conditions) is part of the key: on the shared views the
    # table+grouping+measure are common across charts, so without the filter a
    # filtered chart would wrongly match an unfiltered one. Ported from Hermes.
    s = re.sub(r"\{where\}", " ", sql.lower())
    fm = re.search(r"\bfrom\s+([\w.\[\]]+)", s)
    table = fm.group(1) if fm else ""
    filt = _chart_filter_key(sql)
    if chart_type == "kpi":
        m = re.search(r"select\s+(.+?)\s+as\s+value", s)
        return (chart_type, table, _canonical_measure(m.group(1)) if m else "", "", filt)
    xm = re.search(r"([\w().*]+)\s+as\s+x", s)
    ym = re.search(r"([\w().*]+)\s+as\s+y", s)
    xk = _canonical_measure(xm.group(1)) if xm else ""
    yk = _canonical_measure(ym.group(1)) if ym else ""
    return (chart_type, table, xk, yk, filt)


def _find_duplicate_chart(chart_type: str, sql: str, board_id: str = ""):
    # Dedup is STRICTLY per-board: only charts on the board the user is viewing
    # count. A fresh/empty board therefore never reports a false "already exists"
    # from a chart living on some OTHER board. With no board_id we cannot know which
    # board to scope to, so we do NOT dedup (better a rare duplicate than blocking a
    # legit chart). Ported from Hermes's fixed _find_duplicate_chart.
    if not board_id:
        return None
    new_norm = _normalize_sql(sql)
    new_sig = _chart_signature(chart_type, sql)
    for r in config_db.all_configs_for_dedup(board_id):
        same_type = r["type"] == chart_type
        if (same_type and _normalize_sql(r["sql"]) == new_norm) or _chart_signature(r["type"], r["sql"]) == new_sig:
            return r["id"], r["title"]
    return None


# ── validation / dry-run against the REAL Azure data ──────────────────────────
def _schema_hint() -> str:
    return "\n".join(
        f"  {t}: {', '.join(c['name'] for c in cols)}"
        for t, cols in coreshare_db.get_schema().items()
    )


def _dry_run_chart_sql(chart_type: str, sql: str) -> None:
    """Execute the chart's SQL (unfiltered) before persisting, and confirm it returns
    the columns the renderer needs. On failure, raise with the real schema attached so
    the agent can fix the names and retry. A single unfiltered probe (not the double
    Hermes probe) — {where} is always rendered empty in this port, and each live query
    against the serverless DB is expensive."""
    required = {"value"} if chart_type == "kpi" else {"x", "y"}
    probe = sql.replace("{where}", "")
    coreshare_db.validate_select_only(probe)
    try:
        rows = coreshare_db.run_query(probe)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            f"This chart's query does not run: {exc}\n\n"
            f"Real tables and columns:\n{_schema_hint()}\n\n"
            "Call get_database_schema and use the exact schema-qualified names above."
        ) from None
    cols = set(rows[0].keys()) if rows else set()
    # An empty result set can't confirm aliases; allow it (the query ran).
    if rows:
        missing = required - cols
        if missing:
            raise ValueError(
                f"Chart query must alias its output columns as {sorted(required)}; "
                f"missing {sorted(missing)} (it returned {sorted(cols)}). "
                "Example: SELECT Category AS x, COUNT(*) AS y FROM DataShare.VRequests "
                "{where} GROUP BY Category"
            )


def _audit(event_name: str, ctx: dict, **fields) -> None:
    """Best-effort chart-lifecycle audit through agenticAi's events spine. Never logs
    SQL or chart contents — only id / type / board (metadata only)."""
    try:
        from backend import events
        events.log_event("chart_created" if event_name == "CHART_SAVED" else "chart_deleted",
                          user_id=ctx.get("user_id"), name=fields.get("type"),
                          meta={"event": event_name, **fields})
    except Exception:
        pass


# ── the 5 tools ────────────────────────────────────────────────────────────────
def _sync_get_schema() -> str:
    schema = coreshare_db.get_schema()
    lines = []
    for t, cols in schema.items():
        lines.append(f"{t}: " + ", ".join(f"{c['name']} ({c['type']})" for c in cols))
    return "Database schema (schema-qualified table -> columns):\n" + "\n".join(lines)


async def _get_database_schema(ctx) -> str:
    return await asyncio.to_thread(_sync_get_schema)


async def _query_data(ctx, sql: str) -> str:
    def _run():
        rows = coreshare_db.run_query(sql)
        return json.dumps(rows[:50], default=str)
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        return f"error: query failed: {e}"


async def _save_chart(ctx, title: str, chart_type: str, sql: str, board_id: str = "") -> str:
    # Trim the board_id: an agent occasionally echoes it with stray whitespace
    # (e.g. " abc123"), which would save the chart to a PHANTOM board id the real
    # board can never match — breaking both per-board dedup and display scoping.
    # Normalize once so the whole path uses the clean id. Ported from Hermes.
    board_id = (ctx.get("board_id") or board_id or "").strip()

    def _run():
        if chart_type not in VALID_CHART_TYPES:
            raise ValueError(f"chart_type must be one of {sorted(VALID_CHART_TYPES)}")
        norm_sql = _normalize_where_placeholder(sql)
        if "{where}" not in norm_sql:
            raise ValueError('sql must include the literal placeholder "{where}".')
        coreshare_db.validate_select_only(norm_sql.replace("{where}", ""))
        _dry_run_chart_sql(chart_type, norm_sql)
        dup = _find_duplicate_chart(chart_type, norm_sql, board_id)
        if dup is not None:
            return {"status": "exists", "id": dup[0], "title": dup[1]}
        chart_id = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] + "_" + uuid.uuid4().hex[:6]
        config_db.insert_chart(chart_id, chart_type, title, norm_sql, created_by="agent", board_id=board_id)
        return {"status": "added", "id": chart_id, "title": title}
    try:
        res = await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        return f"error: could not add chart: {e}"
    if res["status"] == "added":
        _audit("CHART_SAVED", ctx, id=res["id"], type=chart_type, board=board_id or "")
        return f'Added the "{res["title"]}" chart to the dashboard.'
    return f'That chart is already on the dashboard ("{res["title"]}") — nothing to add.'


async def _list_charts(ctx) -> str:
    rows = await asyncio.to_thread(config_db.list_configs, None, False)
    if not rows:
        return "No charts on the dashboard yet."
    return "Charts on the dashboard:\n" + "\n".join(f'- [{r["type"]}] {r["title"]} ({r["id"]})' for r in rows)


async def _delete_chart(ctx, chart_id: str) -> str:
    ok = await asyncio.to_thread(config_db.delete_chart, chart_id)
    if ok:
        _audit("CHART_DELETED", ctx, id=chart_id)
        return f"Removed chart {chart_id} from the dashboard."
    return f"No chart with id {chart_id} was found."


# ── registration (kept OUT of templates.PRIMARY_TOOLS) ─────────────────────────
_TOOLS = [
    Tool("get_database_schema",
         "Return every table/view and its columns in the connected aid-request database. "
         "ALWAYS call this before writing any SQL — do not guess table or column names.",
         {"type": "object", "properties": {}},
         _get_database_schema, "charts.read"),
    Tool("query_data",
         "Run a single read-only SELECT against the aid-request database and return the rows. "
         "Use it to explore the data or verify a query before saving it as a chart.",
         {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
         _query_data, "charts.read"),
    Tool("save_chart",
         "Add a chart to the dashboard (it appears on screen instantly). ALWAYS call this when the "
         "user asks for a chart; it checks for duplicates itself by comparing the query (table, "
         "grouping, measure), not the title. For non-kpi charts alias the label column AS x and the "
         "numeric value AS y; for kpi alias the single number AS value. Include the literal text "
         "{where} right before any GROUP BY (or at the end). chart_type is one of kpi/line/bar/pie. "
         "Pass board_id verbatim if the system prompt gives you one.",
         {"type": "object", "properties": {
             "title": {"type": "string"}, "chart_type": {"type": "string", "enum": ["kpi", "line", "bar", "pie"]},
             "sql": {"type": "string"}, "board_id": {"type": "string"}},
          "required": ["title", "chart_type", "sql"]},
         _save_chart, "charts.write"),
    Tool("list_charts",
         "List every chart currently on the dashboard (id, type, title).",
         {"type": "object", "properties": {}},
         _list_charts, "charts.read"),
    Tool("delete_chart",
         "Remove a chart from the dashboard by its id (see list_charts for ids).",
         {"type": "object", "properties": {"chart_id": {"type": "string"}}, "required": ["chart_id"]},
         _delete_chart, "charts.write"),
]

DASHBOARD_TOOL_NAMES = [t.name for t in _TOOLS]

_registered = False


def register_dashboard_tools() -> list[str]:
    """Idempotently register the 5 chart tools into the shared registry. Called at
    dashboard-router import time so a missing Azure driver only breaks the dashboard,
    never the core agent's registry import."""
    global _registered
    if not _registered:
        for t in _TOOLS:
            register(t)
        _registered = True
    return DASHBOARD_TOOL_NAMES
