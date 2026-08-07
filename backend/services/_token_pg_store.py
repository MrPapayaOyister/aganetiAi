"""
PostgreSQL store for provider OAuth connections — the durable primary.

Replaces `data_vault/provider_connections.json` as the system of record. The JSON
store stays behind it as an emergency fallback (see provider_tokens._fetch_connection),
because a token store that is unavailable means every connected user silently
appears disconnected.

Why the app's own Postgres rather than Supabase: the Supabase service key in this
deployment is the new `sb_secret_…` format and PostgREST rejects it with
401 "Invalid API key". That is fixable only by regenerating the key in the
Supabase dashboard, which is outside the backend's control — whereas the app
already owns a Postgres with Alembic migrations, connection pooling and backups.
Supabase remains supported: provider_tokens tries it when it is configured AND
working, and falls through to here otherwise.

API is deliberately identical to `_token_file_store` (upsert / fetch / fetch_all /
fetch_all_for_provider / update / delete / delete_by_user_provider) so the two are
drop-in interchangeable and the caller needs no branching.

**Tokens are stored exactly as handed in.** Encryption happens one layer up in
`token_crypto` — this module must never see a plaintext token, and never decrypts.
`user_id` is TEXT, not UUID: internal aliases like "user_1" and "test" are valid
ids here and would not survive a UUID column.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

log = logging.getLogger("aria.provider.pgstore")

# Every column the file store round-trips, so a row converted from JSON keeps
# every field it had. `raw_profile`/`scopes` are JSONB/array in the DDL but are
# handed to us as Python objects and adapted by the driver.
_COLUMNS = ("id", "user_id", "provider", "provider_email", "access_token",
            "refresh_token", "token_expiry", "scopes", "raw_profile",
            "created_at", "updated_at")


class ProviderStoreUnavailable(RuntimeError):
    """Postgres could not serve the request. Caller should fall back, not crash."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _engine():
    from backend.db.base import engine
    return engine


def _as_dt(value: Any) -> Optional[datetime]:
    """ISO string → aware datetime. The file store keeps ISO text; the column is
    timestamptz, so the conversion has to happen somewhere and here is the seam."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _row_to_dict(row: Any) -> dict:
    """DB row → the dict shape every existing caller already expects.

    Timestamps go back out as ISO strings because `get_provider_token` parses
    `token_expiry` with `datetime.fromisoformat` — handing it a datetime would
    work by luck today and break the moment that parse is reused elsewhere.
    """
    d = dict(row._mapping)
    for key in ("token_expiry", "created_at", "updated_at"):
        if isinstance(d.get(key), datetime):
            d[key] = d[key].isoformat()
    d["id"] = str(d["id"])
    return d


async def _exec(sql: str, params: dict) -> list:
    try:
        async with _engine().begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.fetchall()) if result.returns_rows else []
    except Exception as e:  # noqa: BLE001 — surfaced as a typed failure to the caller
        raise ProviderStoreUnavailable(str(e)) from e


# ── writes ────────────────────────────────────────────────────────────────────

async def upsert(user_id: str, provider: str, payload: dict[str, Any]) -> dict:
    """Insert-or-update one connection, keyed on (user_id, provider).

    ON CONFLICT rather than select-then-write: two concurrent OAuth callbacks for
    the same user would otherwise race and leave a duplicate that the UNIQUE
    constraint then rejects at the worst possible moment — mid-login.
    """
    row = {
        "id": str(payload.get("id") or uuid.uuid4()),
        "user_id": str(user_id),
        "provider": provider,
        "provider_email": payload.get("provider_email"),
        "access_token": payload.get("access_token") or "",
        "refresh_token": payload.get("refresh_token"),
        "token_expiry": _as_dt(payload.get("token_expiry")),
        "scopes": list(payload.get("scopes") or []) or None,
        "raw_profile": payload.get("raw_profile"),
    }
    import json as _json
    if row["raw_profile"] is not None and not isinstance(row["raw_profile"], str):
        row["raw_profile"] = _json.dumps(row["raw_profile"])
    rows = await _exec("""
        INSERT INTO provider_connections
            (id, user_id, provider, provider_email, access_token, refresh_token,
             token_expiry, scopes, raw_profile)
        VALUES
            (:id, :user_id, :provider, :provider_email, :access_token, :refresh_token,
             :token_expiry, :scopes, CAST(:raw_profile AS jsonb))
        ON CONFLICT (user_id, provider) DO UPDATE SET
            provider_email = EXCLUDED.provider_email,
            access_token   = EXCLUDED.access_token,
            -- A refresh response often omits refresh_token; COALESCE keeps the
            -- stored one instead of nulling the only way back from an expiry.
            refresh_token  = COALESCE(EXCLUDED.refresh_token, provider_connections.refresh_token),
            token_expiry   = EXCLUDED.token_expiry,
            scopes         = COALESCE(EXCLUDED.scopes, provider_connections.scopes),
            raw_profile    = COALESCE(EXCLUDED.raw_profile, provider_connections.raw_profile),
            updated_at     = now()
        RETURNING *
    """, row)
    return _row_to_dict(rows[0])


async def update(row_id: str, patch: dict[str, Any]) -> None:
    """Patch selected columns of one row by id (the token-refresh path)."""
    allowed = {k: v for k, v in patch.items()
               if k in ("access_token", "refresh_token", "token_expiry",
                        "provider_email", "scopes")}
    if not allowed:
        return
    if "token_expiry" in allowed:
        allowed["token_expiry"] = _as_dt(allowed["token_expiry"])
    sets = ", ".join(f"{k} = :{k}" for k in allowed)
    await _exec(f"UPDATE provider_connections SET {sets}, updated_at = now() "
                f"WHERE id = CAST(:row_id AS uuid)", {**allowed, "row_id": str(row_id)})


async def delete(row_id: str) -> None:
    await _exec("DELETE FROM provider_connections WHERE id = CAST(:row_id AS uuid)",
                {"row_id": str(row_id)})


async def delete_by_user_provider(user_id: str, provider: str) -> bool:
    rows = await _exec("DELETE FROM provider_connections "
                       "WHERE user_id = :user_id AND provider = :provider RETURNING id",
                       {"user_id": str(user_id), "provider": provider})
    return bool(rows)


# ── reads ─────────────────────────────────────────────────────────────────────

async def fetch(user_id: str, provider: str) -> dict | None:
    rows = await _exec("SELECT * FROM provider_connections "
                       "WHERE user_id = :user_id AND provider = :provider LIMIT 1",
                       {"user_id": str(user_id), "provider": provider})
    return _row_to_dict(rows[0]) if rows else None


async def fetch_all(user_id: str) -> list[dict]:
    rows = await _exec("SELECT * FROM provider_connections WHERE user_id = :user_id "
                       "ORDER BY provider", {"user_id": str(user_id)})
    return [_row_to_dict(r) for r in rows]


async def fetch_all_for_provider(provider: str) -> list[dict]:
    rows = await _exec("SELECT * FROM provider_connections WHERE provider = :provider "
                       "ORDER BY user_id", {"provider": provider})
    return [_row_to_dict(r) for r in rows]


async def is_available() -> bool:
    """True when the table can be queried. Used by health checks, never by the
    read path — the read path just falls back."""
    try:
        await _exec("SELECT 1 FROM provider_connections LIMIT 1", {})
        return True
    except ProviderStoreUnavailable:
        return False
