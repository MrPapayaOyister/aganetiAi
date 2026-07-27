"""AEAD encryption for OAuth tokens at rest (Fernet = AES-128-CBC + HMAC-SHA256).

Encrypts the sensitive provider-token fields so that a read of the token store —
the JSON file on the DGX, or a dump of the Supabase provider_connections row —
yields ciphertext, not live Gmail/Calendar credentials. Idempotent via an
'enc::' prefix, so it is safe to apply at several read/write boundaries without
ever double-encrypting.

Key resolution (first hit wins):
  1. env TOKEN_ENC_KEY   — a urlsafe-base64 32-byte Fernet key (preferred; put in .env)
  2. data_vault/.token_enc_key — auto-generated on first use, chmod 600 (dev fallback)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("aria.token_crypto")

_PREFIX = "enc::"
SENSITIVE = ("access_token", "refresh_token")
_KEYFILE = Path(__file__).resolve().parents[2] / "data_vault" / ".token_enc_key"
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    key = os.getenv("TOKEN_ENC_KEY", "").strip()
    if not key:
        # Manual scripts don't get systemd's EnvironmentFile=.env, so read .env
        # directly before falling back to a generated keyfile — this keeps the
        # service and any one-off migration on the SAME key (no decrypt mismatch).
        _ENVFILE = Path(__file__).resolve().parents[2] / ".env"
        if _ENVFILE.exists():
            for line in _ENVFILE.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("TOKEN_ENC_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        if _KEYFILE.exists():
            key = _KEYFILE.read_text(encoding="utf-8").strip()
        else:
            key = Fernet.generate_key().decode()
            _KEYFILE.parent.mkdir(parents=True, exist_ok=True)
            _KEYFILE.write_text(key, encoding="utf-8")
            try:
                os.chmod(_KEYFILE, 0o600)
            except OSError:
                pass
            log.warning("TOKEN_ENC_KEY not set; generated a key at %s (chmod 600). "
                        "Set TOKEN_ENC_KEY in .env for stable, centralised key management.", _KEYFILE)
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(val):
    """Encrypt a string value; pass through non-strings, empties, already-encrypted."""
    if not isinstance(val, str) or not val or val.startswith(_PREFIX):
        return val
    return _PREFIX + _get_fernet().encrypt(val.encode()).decode()


def decrypt(val):
    """Decrypt an 'enc::' value; pass legacy plaintext / non-strings through unchanged."""
    if not isinstance(val, str) or not val.startswith(_PREFIX):
        return val
    try:
        return _get_fernet().decrypt(val[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        log.error("token decrypt failed (wrong/rotated TOKEN_ENC_KEY?) — returning as-is")
        return val


def enc_row(row):
    """Return a copy of a token dict with SENSITIVE fields encrypted."""
    if not isinstance(row, dict):
        return row
    return {**row, **{f: encrypt(row[f]) for f in SENSITIVE if f in row}}


def dec_row(row):
    """Return a copy of a token dict with SENSITIVE fields decrypted."""
    if not isinstance(row, dict):
        return row
    return {**row, **{f: decrypt(row[f]) for f in SENSITIVE if f in row}}
