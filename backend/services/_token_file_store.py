"""
File-based fallback for provider OAuth tokens.

Used when the Supabase `provider_connections` table is unavailable (e.g.
the migration hasn't been run yet). Mirrors the same shape as the Supabase
row so callers can read/write through the same interface.

Storage path: data_vault/provider_connections.json
Layout:
  {
    "<user_id>::<provider>": {
        "id": "<uuid>", "user_id": "...", "provider": "google",
        "provider_email": "...", "access_token": "...",
        "refresh_token": "...", "token_expiry": "ISO",
        "scopes": [...], "raw_profile": {}, "updated_at": "ISO"
    }
  }
"""
from __future__ import annotations

import json
import os
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.services import token_crypto

_STORE_PATH = Path(__file__).resolve().parents[2] / "data_vault" / "provider_connections.json"
_LOCK = threading.Lock()


def _ensure_dir() -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load() -> dict[str, dict]:
    # Tokens are stored encrypted at rest; decrypt on load so callers see plaintext.
    if not _STORE_PATH.exists():
        return {}
    try:
        with _STORE_PATH.open("r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: token_crypto.dec_row(row) for k, row in raw.items()}


def _save(data: dict[str, dict]) -> None:
    # Encrypt the sensitive token fields before they ever touch disk.
    _ensure_dir()
    enc = {k: token_crypto.enc_row(row) for k, row in data.items()}
    tmp = _STORE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(enc, f, indent=2)
    os.replace(tmp, _STORE_PATH)


def _key(user_id: str, provider: str) -> str:
    return f"{user_id}::{provider}"


def upsert(user_id: str, provider: str, payload: dict[str, Any]) -> dict:
    """Insert-or-update a provider connection. Returns the stored row."""
    with _LOCK:
        data = _load()
        k = _key(user_id, provider)
        row = data.get(k, {}).copy()
        row.update(payload)
        row["user_id"] = user_id
        row["provider"] = provider
        if "id" not in row:
            row["id"] = str(uuid.uuid4())
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        if "created_at" not in row:
            row["created_at"] = row["updated_at"]
        data[k] = row
        _save(data)
        return row


def fetch(user_id: str, provider: str) -> dict | None:
    with _LOCK:
        data = _load()
        return data.get(_key(user_id, provider))


def fetch_all(user_id: str) -> list[dict]:
    """All provider rows for a user."""
    with _LOCK:
        data = _load()
        return [row for k, row in data.items() if k.startswith(f"{user_id}::")]


def fetch_all_for_provider(provider: str) -> list[dict]:
    """Every stored row for a provider, across all user ids."""
    with _LOCK:
        data = _load()
        return [row for k, row in data.items() if k.endswith(f"::{provider}")]


def update(row_id: str, patch: dict[str, Any]) -> None:
    with _LOCK:
        data = _load()
        for k, row in data.items():
            if row.get("id") == row_id:
                row.update(patch)
                row["updated_at"] = datetime.now(timezone.utc).isoformat()
                data[k] = row
                _save(data)
                return


def delete(row_id: str) -> None:
    with _LOCK:
        data = _load()
        for k in list(data.keys()):
            if data[k].get("id") == row_id:
                del data[k]
                _save(data)
                return


def delete_by_user_provider(user_id: str, provider: str) -> bool:
    with _LOCK:
        data = _load()
        k = _key(user_id, provider)
        if k in data:
            del data[k]
            _save(data)
            return True
        return False
