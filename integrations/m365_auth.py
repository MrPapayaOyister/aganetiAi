"""
Microsoft 365 Authentication Module (MSAL Device Flow + Per-User Token Cache)

This is the foundational auth layer for the M365 migration. Every other M365 module
(mail fetch/send, calendar) calls get_access_token(user_id) from here — they never touch
auth directly.

Flow:
  1. One-time per user on the headless VM: run_device_auth_flow(user_id) prints a URL +
     code; the user signs in on any device and the token cache is saved to
     tokens/{user_id}_m365_token.json.
  2. Thereafter: get_access_token(user_id) silently refreshes using the cached refresh
     token — no browser, no prompt.

CLI:
  python3 integrations/m365_auth.py --auth <user_id>   # one-time device flow
  python3 integrations/m365_auth.py --status           # token status for all users
"""

import os
import sys

# When run directly (`python3 integrations/m365_auth.py ...`), the project root is NOT on
# sys.path, so `config` would be unimportable. Add it — same pattern as telegram_bot.py:29.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import msal

from config.settings import (
    TOKENS_DIR, M365_CLIENT_ID, M365_AUTHORITY, M365_SCOPES
)

# MSAL reserves the OIDC scopes openid/profile/offline_access and raises ValueError if
# they are passed explicitly — it requests them automatically for public clients (which is
# what gives us refresh tokens). M365_SCOPES keeps offline_access for documentation/intent;
# strip the reserved set before any MSAL call.
_RESERVED = {"openid", "profile", "offline_access"}
GRAPH_SCOPES = [s for s in M365_SCOPES if s.lower() not in _RESERVED]


def _get_cache(user_id: str) -> msal.SerializableTokenCache:
    """Loads the token cache from disk if it exists. Returns empty cache if first run."""
    cache = msal.SerializableTokenCache()
    token_path = TOKENS_DIR / f"{user_id}_m365_token.json"
    if token_path.exists():
        cache.deserialize(token_path.read_text())
    return cache


def _save_cache(user_id: str, cache: msal.SerializableTokenCache) -> None:
    """Writes cache to disk only if it changed this session. Atomic write via temp file."""
    if not cache.has_state_changed:
        return
    token_path = TOKENS_DIR / f"{user_id}_m365_token.json"
    tmp_path   = token_path.with_suffix(".tmp")
    tmp_path.write_text(cache.serialize())
    tmp_path.replace(token_path)  # atomic replace — no partial writes


def _build_app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        client_id=M365_CLIENT_ID,
        authority=M365_AUTHORITY,
        token_cache=cache
    )


def get_access_token(user_id: str) -> str:
    """
    Returns a valid Microsoft Graph API access token for user_id.
    Silently refreshes using cached refresh token if access token is expired.
    Raises RuntimeError if no token exists — run device flow first.
    Called by: m365_mail.py, m365_calendar.py (Tasks M365-B, M365-C)
    """
    cache = _get_cache(user_id)
    app   = _build_app(cache)

    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError(
            f"[M365] No token found for {user_id}. "
            f"Run: python3 integrations/m365_auth.py --auth {user_id}"
        )

    result = app.acquire_token_silent(scopes=GRAPH_SCOPES, account=accounts[0])

    if not result or "access_token" not in result:
        err = result.get("error_description", "unknown") if result else "no result"
        raise RuntimeError(
            f"[M365] Silent token refresh failed for {user_id}. "
            f"Error: {err}. "
            f"Re-run device flow: python3 integrations/m365_auth.py --auth {user_id}"
        )

    _save_cache(user_id, cache)
    return result["access_token"]


def run_device_auth_flow(user_id: str) -> None:
    """
    One-time setup per user on headless VM.
    Prints device code URL to terminal — user opens on any device to authenticate.
    Saves token to tokens/{user_id}_m365_token.json
    """
    print(f"\n[M365] Starting device auth flow for: {user_id}")

    cache = _get_cache(user_id)
    app   = _build_app(cache)

    flow   = app.initiate_device_flow(scopes=GRAPH_SCOPES)

    if "user_code" not in flow:
        raise RuntimeError(
            f"[M365] Failed to initiate device flow: {flow.get('error_description', flow)}"
        )

    # Print the exact message MSAL provides — contains URL and code
    print(f"\n{'='*60}")
    print(flow["message"])
    print(f"{'='*60}\n")
    print(f"Waiting for authentication... (expires in {flow.get('expires_in', 900)}s)")

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        print(f"\n❌ Authentication failed for {user_id}")
        print(f"   Error: {result.get('error_description', 'unknown')}")
        return

    _save_cache(user_id, cache)
    print(f"\n✅ Token saved successfully for {user_id}")
    print(f"   Token file: {TOKENS_DIR / f'{user_id}_m365_token.json'}")
    print(f"   Refresh token valid for ~90 days (auto-renews on every use)")


def check_token_status(user_id: str) -> dict:
    """Returns token status for user_id without making an API call."""
    token_path = TOKENS_DIR / f"{user_id}_m365_token.json"
    if not token_path.exists():
        return {"user_id": user_id, "status": "no_token", "token_path": str(token_path)}

    cache = _get_cache(user_id)
    app   = _build_app(cache)
    accounts = app.get_accounts()

    if not accounts:
        return {"user_id": user_id, "status": "cache_empty", "token_path": str(token_path)}

    return {
        "user_id":    user_id,
        "status":     "ok",
        "account":    accounts[0].get("username", "unknown"),
        "token_path": str(token_path)
    }


if __name__ == "__main__":
    if "--auth" in sys.argv:
        try:
            uid = sys.argv[sys.argv.index("--auth") + 1]
        except IndexError:
            print("Usage: python3 integrations/m365_auth.py --auth <user_id>")
            print("Example: python3 integrations/m365_auth.py --auth user_1")
            sys.exit(1)
        run_device_auth_flow(uid)

    elif "--status" in sys.argv:
        for uid in ["user_1", "user_2"]:
            status = check_token_status(uid)
            icon = "✅" if status["status"] == "ok" else "❌"
            print(f"{icon} {uid}: {status['status']} "
                  f"({'account: ' + status.get('account','') if status['status'] == 'ok' else status['token_path']})")

    else:
        print("Commands:")
        print("  --auth <user_id>    Run device flow auth for a user")
        print("  --status            Check token status for all users")
        print("")
        print("Examples:")
        print("  python3 integrations/m365_auth.py --auth user_1")
        print("  python3 integrations/m365_auth.py --auth user_2")
        print("  python3 integrations/m365_auth.py --status")
