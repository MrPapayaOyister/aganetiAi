"""One-shot: encrypt any existing plaintext OAuth tokens at rest (file store + Supabase).
Idempotent — safe to re-run (enc:: values are skipped). Run inside the venv from repo root."""
import sys
sys.path.insert(0, ".")

from backend.services import _token_file_store as f
from backend.services import token_crypto


def _count_plain(text: str) -> int:
    # Google access tokens start 'ya29.', refresh tokens '1//'. Count raw (unencrypted) hits.
    return text.count('"ya29.') + text.count('"1//') + text.count(': "ya29.') + text.count(': "1//')


def main():
    # 1) File store: _load() decrypts (no-op on plaintext), _save() re-writes encrypted.
    path = f._STORE_PATH
    if path.exists():
        before = path.read_text(encoding="utf-8")
        data = f._load()
        f._save(data)
        after = path.read_text(encoding="utf-8")
        print(f"file store: {len(data)} row(s) at {path}")
        print(f"  raw ya29./1// tokens  before={_count_plain(before)}  after={_count_plain(after)}")
        print(f"  enc:: ciphertext values after={after.count('enc::')}")
    else:
        print(f"file store: none yet at {path} (nothing to migrate — mechanism still active for future connects)")

    # 2) Supabase best-effort re-encrypt.
    try:
        from backend.auth.supabase_client import get_supabase_admin
        sb = get_supabase_admin()
        rows = (sb.table("provider_connections").select("*").execute()).data or []
        n = 0
        for r in rows:
            e = token_crypto.enc_row(r)
            upd = {fld: e[fld] for fld in ("access_token", "refresh_token")
                   if fld in r and e.get(fld) != r.get(fld)}
            if upd:
                sb.table("provider_connections").update(upd).eq("id", r["id"]).execute()
                n += 1
        print(f"supabase: {len(rows)} row(s), {n} re-encrypted")
    except Exception as ex:
        print(f"supabase: skipped ({type(ex).__name__}: {ex})")

    # 3) round-trip sanity — proves the key works and ciphertext != plaintext.
    tok = "ya29.SANITY-plaintext-value-xyz"
    enc = token_crypto.encrypt(tok)
    assert enc.startswith("enc::") and enc != tok and token_crypto.decrypt(enc) == tok, "round-trip FAILED"
    print("round-trip: OK (encrypt→decrypt identity; ciphertext carries enc:: prefix)")


if __name__ == "__main__":
    main()
