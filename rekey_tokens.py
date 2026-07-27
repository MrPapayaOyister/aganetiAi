"""Repair a key mismatch: the token file was encrypted with the auto-generated
keyfile key, but the service uses the TOKEN_ENC_KEY from .env. Decrypt each token
with the keyfile key and re-encrypt with the .env key, then remove the keyfile so
.env is the single source of truth.

Run with the .env key exported:
    TOKEN_ENC_KEY=$(grep '^TOKEN_ENC_KEY=' .env | cut -d= -f2-) .venv/bin/python rekey_tokens.py
"""
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

PREFIX = "enc::"
FIELDS = ("access_token", "refresh_token")

env_key = os.getenv("TOKEN_ENC_KEY", "").strip()
assert env_key, "TOKEN_ENC_KEY must be exported (the .env value)"
kf_path = Path("data_vault/.token_enc_key")
assert kf_path.exists(), "keyfile missing — nothing to re-key from"

kf = Fernet(kf_path.read_text(encoding="utf-8").strip().encode())
ek = Fernet(env_key.encode())
store = Path("data_vault/provider_connections.json")
data = json.loads(store.read_text(encoding="utf-8"))

n = 0
for row in data.values():
    for fld in FIELDS:
        v = row.get(fld)
        if isinstance(v, str) and v.startswith(PREFIX):
            pt = kf.decrypt(v[len(PREFIX):].encode()).decode()   # decrypt with keyfile key
            row[fld] = PREFIX + ek.encrypt(pt.encode()).decode()  # re-encrypt with .env key
            n += 1

tmp = store.with_suffix(".tmp")
tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
os.replace(tmp, store)
kf_path.unlink()
print(f"re-keyed {n} token field(s) to the .env key; removed {kf_path}")

# sanity: the .env key can now decrypt the file
d = json.loads(store.read_text(encoding="utf-8"))
first = next(iter(d.values()))
dec = ek.decrypt(first["access_token"][len(PREFIX):].encode()).decode()
print(f"verify: .env-key decrypt OK (access token starts '{dec[:5]}', len={len(dec)})")
