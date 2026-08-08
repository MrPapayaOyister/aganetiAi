"""Read-only connection to the Dar Al Ber CORE-SHARE database (the chart DATA source).

This is the client's Azure SQL DB (``dabsproductionodssql``), reached from this host
through the GCP VM socat relay. ``/etc/hosts`` maps the Azure FQDN to the VM's
Tailscale IP so strict-TLS cert validation (Encrypt=yes, TrustServerCertificate=no)
still passes while traffic is relayed. The DB user is read-only at the permission
level (effective grants: CONNECT + SELECT); ``validate_select_only`` is defence in
depth on top of that.

Credentials come from SEPARATE env vars, never a single URL string. The password
contains '@' and '$'; embedded in a URL string, SQLAlchemy's parser reads the
password as just "P" and folds the rest into the hostname. ``URL.create()`` takes
each field as a literal, so no manual %-encoding is ever needed.

Ported from Hermes ``daralber/coreshare_db.py``, plus:
  - query ceiling: an earlier version of this module assumed DABS-CORE-SHARE was a
    SERVERLESS database that auto-pauses, and so allowed 90s per query plus one
    retry on a query timeout, on the theory that the first query was waking paused
    storage. That was wrong twice over: the database is provisioned (S1) and cannot
    auto-pause, and a resume would take seconds rather than ninety in any case -- so
    a 90s QUERY timeout only ever meant the query was genuinely too slow, and the
    retry was guaranteed to fail after burning another 90s. The ceiling is now 30s
    with no retry, which bounds a stuck query at 30s instead of ~182s.
  - ``get_schema`` + ``validate_select_only`` live here (engine-agnostic), so the
    tools never build their own connection.

WARNING: everything that touches CORE-SHARE is SELECT-only. Never write here.
"""
from __future__ import annotations

import os
import re
import time
import threading

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL

# Bounds a genuinely stuck query. Measured: the heaviest legitimate aggregate runs in
# ~2.2s and the worst known plan pathology (the row-goal trap) in 12.8s, so 30s leaves
# ample headroom while capping what a client tester can be made to wait.
_QUERY_TIMEOUT_S = 30
_LOGIN_TIMEOUT_S = 30


def _build_url() -> URL:
    # Env is loaded by config.settings (load_dotenv). Fail loudly if unset — the
    # dashboard cannot function without the real data source.
    return URL.create(
        "mssql+pyodbc",
        username=os.environ["DABS_CORESHARE_USER"],
        password=os.environ["DABS_CORESHARE_PASSWORD"],  # literal; '@' and '$' safe
        host=os.environ["DABS_CORESHARE_HOST"],
        port=int(os.environ.get("DABS_CORESHARE_PORT", "1433")),
        database=os.environ["DABS_CORESHARE_DB"],
        query={
            "driver": os.environ.get("DABS_CORESHARE_DRIVER", "ODBC Driver 18 for SQL Server"),
            "Encrypt": "yes",
            "TrustServerCertificate": "no",
        },
    )


# The relay/VM can drop idle sockets. pool_recycle below already discards connections
# older than 300s, which covers that; pool_pre_ping additionally spent a round-trip
# validating EVERY checkout (~400ms measured on this path), so it is redundant cost.
engine = create_engine(
    _build_url(),
    pool_recycle=300,
    pool_size=8,
    max_overflow=8,
    connect_args={"timeout": _LOGIN_TIMEOUT_S},
)


@event.listens_for(engine, "connect")
def _set_query_timeout(dbapi_conn, _record):  # pragma: no cover - driver hook
    # pyodbc Connection.timeout is the per-query timeout in seconds (0 = infinite).
    try:
        dbapi_conn.timeout = _QUERY_TIMEOUT_S
    except Exception:
        pass


# --- Query safety (ported verbatim from Hermes daralber/db.py) ----------------
# Applied BOTH when an agent saves a new chart AND every time the dashboard re-runs
# a saved chart's SQL. Defence in depth on top of the read-only DB user.
_SELECT_ONLY = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|PRAGMA|CREATE|REPLACE|TRUNCATE|GRANT|EXEC|MERGE)\b",
    re.IGNORECASE,
)


def validate_select_only(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if not _SELECT_ONLY.match(stripped):
        raise ValueError("Only SELECT queries are allowed.")
    if _FORBIDDEN.search(stripped):
        raise ValueError("Query contains a forbidden keyword.")
    _toks = stripped[:80].upper().split()
    _i = 1
    if _i < len(_toks) and _toks[_i] == "DISTINCT":
        _i += 1
    if _i < len(_toks) and _toks[_i] == "TOP":
        _i += 2 if (_i + 1 < len(_toks) and _toks[_i + 1].isdigit()) else 1
    if _i < len(_toks) and _toks[_i].startswith("*"):
        raise ValueError("SELECT * is not allowed - select specific, non-personal columns.")
    if ";" in stripped:
        raise ValueError("Multiple statements are not allowed.")


# --- Beneficiary-PII hard block --------------------------------------------------
# Individual identifiers + sensitive personal finance. Hidden from get_schema AND
# rejected if a query names one. Demographics (Gender/Nationality/Religion/
# MaritalStatus/NumberOfFamilyMembers) stay queryable for aggregate analytics.
_PII_COLUMNS = {
    "arabicfullname", "englishfullname", "idnumber", "phonenumber", "email",
    "debitedfromaccount", "currentsalary", "netmonthlyincome",
    "totalsourcesofincome", "employer", "jobtitle",
}


def validate_no_pii(sql: str) -> None:
    """Reject any query naming a beneficiary-PII column. Aggregate analytics only."""
    hit = {t.lower() for t in re.findall("[A-Za-z_]+", sql)} & _PII_COLUMNS
    if hit:
        raise ValueError(
            f"'{sorted(hit)[0]}' is a restricted personal-data column and cannot be "
            "queried. Only aggregate, non-identifying analytics are allowed.")


def run_query(sql: str) -> list[dict]:
    """Validate then execute a SELECT-only query against CORE-SHARE. Returns row dicts.

    No retry on a query timeout. It used to retry once, on the theory that the first
    query was waking paused serverless storage — but this database is provisioned and
    cannot pause, and a resume would take seconds rather than the full 30s ceiling
    anyway. A timeout here means the query is genuinely too slow, so the retry could
    only fail a second time while doubling what the user waits.
    """
    validate_select_only(sql)
    validate_no_pii(sql)
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        return [dict(row._mapping) for row in result]


# --- Live result cache (stale-while-revalidate) ---------------------------------
# Chart DATA is re-queried on every board open and on every incremental chart-build
# re-fetch. Against remote serverless Azure SQL (each round-trip ~0.5-0.7s, some
# aggregations multi-second) that made a 9-chart board cost ~24s on EVERY open. Cache
# each query's rows by SQL text: fresh (<TTL) -> return instantly; stale -> return the
# cached rows now AND refresh in the background; cold -> query live once, then cache.
# A saved board thus queries live only the very first time; every re-open is instant.
import hashlib

_result_cache: dict = {}                       # sqlhash -> {"data": list, "ts": float, "refreshing": bool}
_result_lock = threading.Lock()
_RESULT_TTL_S = float(os.getenv("CORESHARE_RESULT_TTL_S", "180"))       # "fresh" window
_RESULT_STALE_S = float(os.getenv("CORESHARE_RESULT_STALE_S", "3600"))  # serve stale up to here while revalidating
_RESULT_MAX = 800                              # soft cap on distinct cached queries


def _cache_put(key: str, data: list) -> None:
    with _result_lock:
        _result_cache[key] = {"data": data, "ts": time.time(), "refreshing": False}
        if len(_result_cache) > _RESULT_MAX:   # evict oldest ~20%
            for k in sorted(_result_cache, key=lambda k: _result_cache[k]["ts"])[: _RESULT_MAX // 5]:
                _result_cache.pop(k, None)


def _refresh_async(key: str, sql: str) -> None:
    with _result_lock:
        ent = _result_cache.get(key)
        if ent is None or ent.get("refreshing"):
            return
        ent["refreshing"] = True

    def _job() -> None:
        try:
            _cache_put(key, run_query(sql))
        except Exception:
            with _result_lock:
                e = _result_cache.get(key)
                if e:
                    e["refreshing"] = False

    threading.Thread(target=_job, daemon=True, name="coreshare-refresh").start()


def run_query_cached(sql: str, ttl: float | None = None,
                    stale: float | None = None) -> list:
    """SELECT with stale-while-revalidate caching (keyed by SQL text).

    fresh (<ttl): cached rows. stale (<stale window): cached rows now + background
    refresh. cold: query live, then cache. For read-only idempotent chart/KPI queries
    where <ttl staleness is fine (the underlying aid data changes slowly).

    `stale` caps how old a row set may be and still be served. Pass stale=ttl to
    refuse stale rows entirely — which is what the CHAT path does, because a board
    stamped "as of HH:MM" can honestly show hour-old numbers and a chat answer that
    states a figure as current cannot.
    """
    ttl = _RESULT_TTL_S if ttl is None else ttl
    stale_window = _RESULT_STALE_S if stale is None else stale
    key = hashlib.sha1(sql.encode("utf-8")).hexdigest()
    now = time.time()
    with _result_lock:
        ent = _result_cache.get(key)
        snap = None if ent is None else (ent["data"], now - ent["ts"])
    if snap is not None:
        data, age = snap
        if age < ttl:
            return data
        if age < stale_window:
            _refresh_async(key, sql)
            return data
    data = run_query(sql)
    _cache_put(key, data)
    return data


def warm_query(sql: str, ttl: float | None = None) -> None:
    """Seed/refresh the cache for a query, ignoring the result (used by the pre-warmer)."""
    try:
        run_query_cached(sql, ttl=ttl)
    except Exception:
        pass



# System/derived schemas that never hold chartable client data.
_SKIP_SCHEMAS = {"sys", "INFORMATION_SCHEMA", "guest"}

_schema_cache: dict = {"data": None, "ts": 0.0}
_SCHEMA_TTL_S = 1800  # schema is effectively static; cache 30 min


def get_schema() -> dict:
    """Table/column map of the REAL database, schema-qualified.

    Reads INFORMATION_SCHEMA.COLUMNS directly (the reader can read it even without
    VIEW DEFINITION perms, and SQLAlchemy's default-schema inspector would miss the
    DataShare/Analytics schemas where the real data lives). Keys are
    ``schema.table`` so the agent can query them verbatim (e.g. DataShare.VRequests).
    """
    now = time.monotonic()
    if _schema_cache["data"] is not None and (now - _schema_cache["ts"]) < _SCHEMA_TTL_S:
        return _schema_cache["data"]
    rows = run_query(
        "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE "
        "FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
    )
    out: dict[str, list] = {}
    for r in rows:
        if r["TABLE_SCHEMA"] in _SKIP_SCHEMAS:
            continue
        if r["COLUMN_NAME"].lower() in _PII_COLUMNS:
            continue
        key = f'{r["TABLE_SCHEMA"]}.{r["TABLE_NAME"]}'
        out.setdefault(key, []).append({"name": r["COLUMN_NAME"], "type": r["DATA_TYPE"]})
    _schema_cache["data"] = out; _schema_cache["ts"] = now
    return out


_fc_cache: dict = {"data": None, "ts": 0.0}


def get_forecast_context() -> dict:
    """Live figures forecasts must respect: approval_rate (%), data span (months),
    avg approved grant (AED). Cached 30 min; safe defaults on any error."""
    now = time.monotonic()
    if _fc_cache["data"] is not None and (now - _fc_cache["ts"]) < _SCHEMA_TTL_S:
        return _fc_cache["data"]
    out = {"approval_rate": 14.1, "span_months": 19, "avg_grant": 14878}
    try:
        r = run_query(
            "SELECT (SELECT COUNT(*) FROM DataShare.VRequestsApproved) AS appr, "
            "(SELECT COUNT(*) FROM DataShare.VRequests) AS total, "
            "(SELECT DATEDIFF(month, MIN(SubmitDate), MAX(SubmitDate)) FROM DataShare.VRequests) AS span")[0]
        appr, total, span = r["appr"], r["total"], r["span"]
        avg = run_query(
            "SELECT AVG(CAST(a.SuggestedAssistanceAmount AS float)) AS g "
            "FROM DataShare.VRequestAttributes a JOIN DataShare.VRequestsApproved r "
            "ON r.RequestID = a.RequestId WHERE a.SuggestedAssistanceAmount > 0")[0]["g"]
        if total:
            out = {"approval_rate": round(100.0 * appr / total, 1),
                   "span_months": int(span or 0), "avg_grant": int(avg or 0)}
    except Exception:
        pass
    _fc_cache["data"] = out
    _fc_cache["ts"] = now
    return out


# --- Keep-warm ------------------------------------------------------------------
# This used to be described as preventing a serverless auto-pause, with a note about
# the database staying "billing-active". Neither applies: CORE-SHARE is provisioned, so
# it never pauses and it bills the same whether we ping it or not. The ping is kept
# because what it ACTUALLY buys is a warm socket through the relay and a warm connection
# pool, so the first real query after a quiet spell does not pay setup. Disable with
# CORESHARE_KEEPWARM=0.
def _keepwarm_loop() -> None:  # pragma: no cover
    while True:
        time.sleep(240)
        try:
            run_query("SELECT 1")
        except Exception:
            pass


if os.getenv("CORESHARE_KEEPWARM", "1") != "0":
    threading.Thread(target=_keepwarm_loop, daemon=True, name="coreshare-keepwarm").start()
