"""Verify the token store decrypts through the real app path (the way the running
service does): _token_file_store._load() -> plaintext usable Google tokens."""
import sys
sys.path.insert(0, ".")
from backend.services import _token_file_store as f

rows = f._load()
print(f"rows: {len(rows)}")
ok = 0
for k, r in rows.items():
    at = r.get("access_token", "") or ""
    rt = r.get("refresh_token", "") or ""
    at_plain = at.startswith("ya29.")
    rt_plain = (not rt) or rt.startswith("1//")
    enc_leak = at.startswith("enc::") or rt.startswith("enc::")
    print(f"  {k[:24]}…  access_plain={at_plain}(len{len(at)})  refresh_plain={rt_plain}  enc_leak={enc_leak}")
    if at_plain and rt_plain and not enc_leak:
        ok += 1
print(f"decrypted-usable rows: {ok}/{len(rows)}")
sys.exit(0 if ok == len(rows) and rows else 1)
