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
  - cold-start tolerance: DABS-CORE-SHARE is a SERVERLESS Azure SQL DB that
    auto-pauses when idle. After idle, login + server-scalar queries return
    instantly, but the FIRST storage/catalog query blocks ~15-60s while the DB
    resumes. We set a generous per-query timeout (90s) so that first query waits
    for the resume and succeeds instead of failing, and retry once on a query
    timeout.
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

# The reader auto-resumes serverless storage on the first query after idle; give it
# room. Warm queries return in <5s; the ceiling only bounds a pathological hang.
_QUERY_TIMEOUT_S = 90
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


# pool_pre_ping mirrors the resilience need: the relay/VM can drop idle sockets, so
# validate a pooled connection before handing it out rather than failing a query.
engine = create_engine(
    _build_url(),
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=8,
    max_overflow=8,
    connect_args={"timeout": _LOGIN_TIMEOUT_S},
)


@event.listens_for(engine, "connect")
def _set_query_timeout(dbapi_conn, _record):  # pragma: no cover - driver hook
    # pyodbc Connection.timeout is the per-query timeout in seconds (0 = infinite).
    # A generous bound lets the serverless auto-resume complete on a cold first
    # query while still capping a genuinely stuck query.
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


def _is_cold_start_timeout(exc: Exception) -> bool:
    s = str(exc)
    return "HYT00" in s or "Query timeout expired" in s or "HYT01" in s


def run_query(sql: str) -> list[dict]:
    """Validate then execute a SELECT-only query against CORE-SHARE. Returns row dicts.

    Retries once on a query-timeout, which on this serverless DB almost always means
    the first query is still resuming paused storage — the retry then lands warm.
    """
    validate_select_only(sql)
    validate_no_pii(sql)
    last: Exception | None = None
    for attempt in range(2):
        try:
            with engine.connect() as conn:
                result = conn.execute(text(sql))
                return [dict(row._mapping) for row in result]
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 0 and _is_cold_start_timeout(exc):
                time.sleep(2)  # let the resume finish, then retry warm
                continue
            raise
    raise last  # unreachable, keeps type-checkers happy


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


def run_query_cached(sql: str, ttl: float | None = None) -> list:
    """SELECT with stale-while-revalidate caching (keyed by SQL text).

    fresh (<ttl): cached rows. stale (<stale window): cached rows now + background
    refresh. cold: query live, then cache. For read-only idempotent chart/KPI queries
    where <ttl staleness is fine (the underlying aid data changes slowly)."""
    ttl = _RESULT_TTL_S if ttl is None else ttl
    key = hashlib.sha1(sql.encode("utf-8")).hexdigest()
    now = time.time()
    with _result_lock:
        ent = _result_cache.get(key)
        snap = None if ent is None else (ent["data"], now - ent["ts"])
    if snap is not None:
        data, age = snap
        if age < ttl:
            return data
        if age < _RESULT_STALE_S:
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
# Serverless CORE-SHARE auto-pauses when idle; the first query then takes ~60s to
# resume ("temporary issue connecting to the data" + stuck dashboard skeletons). A
# tiny background ping keeps it warm so the dashboard stays snappy. Trade-off: the
# serverless DB stays billing-active during quiet periods — disable with
# CORESHARE_KEEPWARM=0 if cost matters more than latency.
def _keepwarm_loop() -> None:  # pragma: no cover
    while True:
        time.sleep(240)
        try:
            run_query("SELECT 1")
        except Exception:
            pass


if os.getenv("CORESHARE_KEEPWARM", "1") != "0":
    threading.Thread(target=_keepwarm_loop, daemon=True, name="coreshare-keepwarm").start()
