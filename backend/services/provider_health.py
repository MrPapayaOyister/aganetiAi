"""
Provider connection health — is each stored OAuth credential actually usable?

Surfaces three conditions that were previously silent, because the chat path is
built to degrade rather than complain:

  * **expired**            — token past its expiry with no refresh token. Only a
                             reconnect fixes it; nothing will recover on its own.
  * **refresh_failed**     — a refresh token exists but the provider rejected it
                             (revoked, or a Google "Testing"-mode 7-day expiry).
  * **provider_unreachable** — the token endpoint could not be contacted. This is
                             about the network, not the credential; it may cure
                             itself, so it is reported separately from the two above.

Design rules this file obeys:

  * **Never raises to the caller.** Health is what you consult when things are
    broken; a health check that throws is worse than useless.
  * **Never mutates.** It reads and, for `deep=True`, exercises a refresh through
    the normal path. It never deletes a row and never marks a user disconnected.
  * **Never touches the chat path.** Nothing here is imported by the chat request
    lifecycle — a slow provider endpoint cannot add latency to a user's turn.
  * **Never logs a token.** Only ids, providers, emails and status strings.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("aria.provider.health")

# A token inside this window is "expiring_soon": still usable, but an operator
# wants to know before it lapses rather than after.
_SOON = timedelta(minutes=30)

CHECK_TIMEOUT = 8.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_expiry(raw: Any) -> "datetime | None":
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _classify(row: dict) -> dict:
    """Status from the stored row alone — no network call."""
    expiry = _parse_expiry(row.get("token_expiry"))
    has_refresh = bool(row.get("refresh_token"))
    email = row.get("provider_email") or None

    if expiry and expiry <= _now():
        # Expired WITH a refresh token is recoverable and normal — the next call
        # refreshes it. Expired WITHOUT one is a dead credential.
        if has_refresh:
            return {"status": "ok", "detail": "expired but refreshable", "email": email,
                    "expires_at": expiry.isoformat()}
        return {"status": "expired", "detail": "token expired and no refresh token stored",
                "email": email, "expires_at": expiry.isoformat()}
    if expiry and expiry - _now() <= _SOON and not has_refresh:
        return {"status": "expiring_soon", "detail": "expires within 30 minutes, no refresh token",
                "email": email, "expires_at": expiry.isoformat()}
    return {"status": "ok", "detail": "credential present",
            "email": email, "expires_at": expiry.isoformat() if expiry else None}


async def check_provider(user_id: str, provider: str, *, deep: bool = False) -> dict:
    """Health of one user's connection. Returns a dict; never raises."""
    from backend.services import provider_tokens as pt
    try:
        row = await pt._fetch_connection(user_id, provider)
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "detail": f"store unavailable: {str(e)[:120]}"}
    if not row:
        return {"status": "not_connected", "detail": "no stored connection"}

    result = _classify(row)
    if not deep:
        return result

    # Deep check: exercise a real refresh through the normal code path, so what we
    # report is what a user's next request would actually experience.
    from fastapi import HTTPException
    try:
        async with asyncio.timeout(CHECK_TIMEOUT):
            await pt.get_provider_token(user_id, provider)
        result["status"] = "ok"
        result["detail"] = "refresh verified against the provider"
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {}
        if e.status_code == 401:
            result["status"] = "refresh_failed"
            result["detail"] = detail.get("message") or "provider rejected the refresh token"
        elif e.status_code == 403:
            result["status"] = "not_connected"
            result["detail"] = detail.get("message") or "not connected"
        else:
            result["status"] = "unknown"
            result["detail"] = f"HTTP {e.status_code}"
    except (asyncio.TimeoutError, TimeoutError):
        result["status"] = "provider_unreachable"
        result["detail"] = f"token endpoint did not answer within {CHECK_TIMEOUT:.0f}s"
    except Exception as e:  # noqa: BLE001
        result["status"] = "provider_unreachable"
        result["detail"] = f"{type(e).__name__}: {str(e)[:120]}"
    return result


async def check_all(*, deep: bool = False) -> dict:
    """Every stored connection across every user.

    Reads the store directly rather than iterating users, so a connection stored
    under an alias the user table has forgotten is still reported."""
    from backend.services import provider_tokens as pt

    rows: list[dict] = []
    for provider in pt.PROVIDERS:
        try:
            rows.extend(await pt._all_rows_for_provider(provider))
        except Exception as e:  # noqa: BLE001
            log.debug("provider health: cannot enumerate %s: %s", provider, e)

    providers: dict[str, Any] = {}
    warnings: list[str] = []
    for row in rows:
        uid, provider = row.get("user_id"), row.get("provider")
        if not uid or not provider:
            continue
        key = f"{provider}:{uid}"
        try:
            info = await check_provider(uid, provider, deep=deep)
        except Exception as e:  # noqa: BLE001
            info = {"status": "unknown", "detail": str(e)[:120]}
        providers[key] = info
        if info["status"] in ("expired", "refresh_failed", "provider_unreachable",
                              "expiring_soon"):
            warnings.append(f"{provider} connection for {uid} "
                            f"({info.get('email') or 'unknown account'}): "
                            f"{info['status']} — {info['detail']}")

    # "degraded" describes the CONNECTIONS, not the platform. The caller keeps
    # its own `overall`, so a lapsed calendar token cannot make the API look down.
    status = "ok" if not warnings else "degraded"
    return {"status": status, "checked": len(providers),
            "deep": deep, "providers": providers, "warnings": warnings}
