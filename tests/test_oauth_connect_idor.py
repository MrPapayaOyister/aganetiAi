"""Phase 1.1 — the OAuth account-linking IDOR (audit R2).

The attack this file locks out, end to end:

    1. attacker calls  GET /auth/google/connect?user_id=<victim>      (was anonymous)
    2. attacker completes Google consent with THEIR OWN account
    3. callback reads user_id from the unsigned `state` and files the attacker's
       tokens against the victim's record
    4. the victim's assistant now reads the attacker's mailbox — and the attacker's
       mailbox is now an input channel into the victim's agent

Two independent defences are required and both are tested here. Authenticating
/connect alone would not be enough, because the `state` was unsigned and could be
edited in flight between step 1 and step 3.
"""
import base64
import importlib
import json
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.auth import tenant as T
from backend.auth.enforce import AuthEnforceMiddleware, _PUBLIC_PREFIXES

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
ALICE = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CTX_ALICE = T.TenantContext(user_id=ALICE, tenant_id=TENANT_A,
                            supabase_uid="sub-alice", email="alice@a.test")
VICTIM_SUB = "sub-victim"


@pytest.fixture(autouse=True)
def _state_secret(monkeypatch):
    """A deterministic signing key, so the tests exercise real HMAC verification
    rather than the 'no key configured' branch."""
    monkeypatch.setenv("OAUTH_STATE_SECRET", "test-secret-do-not-use-in-prod")
    yield


@pytest.fixture
def pa(monkeypatch):
    import backend.routes.provider_auth as mod
    # A configured provider, so `connect` reaches the state-minting path.
    monkeypatch.setitem(mod.PROVIDERS["google"], "client_id", "test-client-id")
    return mod


def _app(with_middleware=True):
    from backend.routes.provider_auth import router
    app = FastAPI()
    app.include_router(router)
    if with_middleware:
        app.add_middleware(AuthEnforceMiddleware)
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


def _as_alice(monkeypatch):
    async def _require(request):
        return CTX_ALICE
    monkeypatch.setattr(T, "require", _require)


def _no_other_provider(monkeypatch, pa):
    async def _connected(_uid):
        return []
    monkeypatch.setattr("backend.services.provider_tokens.connected_providers", _connected)


# ══════════════════════════════════════════════════════════════════════════════
# Defence 1 — /connect is authenticated and self-scoped
# ══════════════════════════════════════════════════════════════════════════════
def test_connect_is_no_longer_public():
    """The bare provider prefixes are gone; only the callbacks remain anonymous."""
    assert "/auth/google" not in _PUBLIC_PREFIXES
    assert "/auth/microsoft" not in _PUBLIC_PREFIXES
    assert "/auth/google/callback" in _PUBLIC_PREFIXES
    assert "/auth/microsoft/callback" in _PUBLIC_PREFIXES


def test_anonymous_connect_is_rejected(pa):
    """Step 1 of the attack now fails outright."""
    c = _app()
    r = c.get("/auth/google/connect", params={"user_id": VICTIM_SUB, "redirect_uri": "/settings"})
    assert r.status_code == 401


def test_callback_stays_reachable_without_a_bearer(pa):
    """The provider redirects the user's browser here and cannot attach a token —
    the legitimate OAuth flow must keep working."""
    c = _app()
    r = c.get("/auth/google/callback", params={"error": "access_denied"})
    assert r.status_code != 401


def test_connect_ignores_a_supplied_user_id(monkeypatch, pa):
    """The core of the fix: the subject is the AUTHENTICATED caller, and the victim
    id in the query string has no effect on the issued state."""
    _as_alice(monkeypatch)
    _no_other_provider(monkeypatch, pa)
    c = _app(with_middleware=False)
    r = c.get("/auth/google/connect",
              params={"user_id": VICTIM_SUB, "redirect_uri": "/settings"})
    assert r.status_code == 200
    url = r.json()["authorize_url"]

    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(url).query)["state"][0]
    decoded = pa._decode_state(state)
    assert decoded["user_id"] == "sub-alice"
    assert decoded["user_id"] != VICTIM_SUB
    assert VICTIM_SUB not in url


def test_connect_returns_json_so_the_spa_can_send_a_bearer(monkeypatch, pa):
    """A top-level navigation cannot carry a token, so the consent URL is fetched
    and then navigated to. The provider-facing redirect flow is unchanged."""
    _as_alice(monkeypatch)
    _no_other_provider(monkeypatch, pa)
    c = _app(with_middleware=False)
    body = c.get("/auth/google/connect").json()
    assert body["provider"] == "google"
    assert body["authorize_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")


def test_connect_redirect_mode_still_available(monkeypatch, pa):
    _as_alice(monkeypatch)
    _no_other_provider(monkeypatch, pa)
    c = _app(with_middleware=False)
    r = c.get("/auth/google/connect", params={"mode": "redirect"})
    assert r.status_code == 307
    assert r.headers["location"].startswith("https://accounts.google.com/")


def test_open_redirect_is_refused(monkeypatch, pa):
    """`redirect_uri` is echoed into a Location header after the callback. Without
    a check, a crafted connect link lands the victim on another origin."""
    _as_alice(monkeypatch)
    _no_other_provider(monkeypatch, pa)
    c = _app(with_middleware=False)
    for hostile in ("https://evil.test/x", "//evil.test/x", "/\\evil.test"):
        r = c.get("/auth/google/connect", params={"redirect_uri": hostile})
        from urllib.parse import parse_qs, urlparse
        state = parse_qs(urlparse(r.json()["authorize_url"]).query)["state"][0]
        assert pa._decode_state(state)["redirect_uri"] == "/", hostile


# ══════════════════════════════════════════════════════════════════════════════
# Defence 2 — the `state` is signed, so it cannot be edited in flight
# ══════════════════════════════════════════════════════════════════════════════
def test_state_round_trips(pa):
    s = pa._encode_state("sub-alice", "/settings")
    d = pa._decode_state(s)
    assert d["user_id"] == "sub-alice"
    assert d["redirect_uri"] == "/settings"


def test_state_is_not_plain_base64_json(pa):
    """Regression guard: the old format was decodable (and therefore editable) by
    anyone. It must no longer parse as bare JSON."""
    s = pa._encode_state("sub-alice", "/")
    with pytest.raises(Exception):
        json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


def test_tampered_subject_is_rejected(pa):
    """THE attack. Take a legitimate state, swap the user id, re-encode."""
    good = pa._encode_state("sub-alice", "/settings")
    body_b64, sig = good.rsplit(".", 1)
    body = json.loads(pa._b64d(body_b64))
    body["user_id"] = VICTIM_SUB
    forged_body = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    forged = base64.urlsafe_b64encode(forged_body).decode().rstrip("=") + "." + sig
    assert pa._decode_state(forged) == {}


def test_forged_signature_is_rejected(pa):
    good = pa._encode_state("sub-alice", "/")
    body_b64, _ = good.rsplit(".", 1)
    assert pa._decode_state(f"{body_b64}.aaaaaaaaaaaaaaaaaaaaaaaaaaaa") == {}


def test_state_signed_with_another_key_is_rejected(monkeypatch, pa):
    good = pa._encode_state("sub-alice", "/")
    monkeypatch.setenv("OAUTH_STATE_SECRET", "a-different-secret")
    assert pa._decode_state(good) == {}


def test_expired_state_is_rejected(monkeypatch, pa):
    s = pa._encode_state("sub-alice", "/")
    # A stub module, not a patch of time.time itself: pa.time IS the stdlib module,
    # and monkeypatching it globally makes logging (which calls time.time) recurse.
    future = time.time() + pa.STATE_TTL_SECONDS + 60

    class _FrozenClock:
        @staticmethod
        def time():
            return future

    monkeypatch.setattr(pa, "time", _FrozenClock)
    assert pa._decode_state(s) == {}


def test_state_from_the_future_is_rejected(monkeypatch, pa):
    """Clock skew is tolerated to 60s; beyond that a state we could not yet have
    issued is refused. Encoded on the real clock, then decoded an hour EARLIER."""
    s = pa._encode_state("sub-alice", "/")          # iat = now
    earlier = time.time() - 3600

    class _FrozenClock:
        @staticmethod
        def time():
            return earlier

    monkeypatch.setattr(pa, "time", _FrozenClock)   # now = iat - 3600 -> age = -3600
    assert pa._decode_state(s) == {}


@pytest.mark.parametrize("junk", ["", "not-a-state", "a.b.c", "....", "ZZZZ.ZZZZ"])
def test_malformed_state_is_rejected(pa, junk):
    assert pa._decode_state(junk) == {}


def test_unsigned_deployment_refuses_to_issue_a_state(monkeypatch, pa):
    """With no signing key the signature would be worthless. Refuse to start the
    flow rather than issue a state that only looks protected."""
    monkeypatch.delenv("OAUTH_STATE_SECRET", raising=False)
    monkeypatch.setenv("INTERNAL_API_TOKEN", "")
    with pytest.raises(pa.StateUnsigned):
        pa._encode_state("sub-alice", "/")


# ══════════════════════════════════════════════════════════════════════════════
# The callback refuses to link on a bad state — no token store is touched
# ══════════════════════════════════════════════════════════════════════════════
def test_callback_with_a_tampered_state_links_nothing(monkeypatch, pa):
    """Even if an attacker somehow reaches the callback with a forged state and a
    real code, nothing is written: the subject is empty so we bounce before the
    token exchange."""
    exchanged = []

    async def _api_request(*a, **kw):
        exchanged.append(a)
        raise AssertionError("token exchange must not run for an invalid state")

    monkeypatch.setattr(pa, "api_request", _api_request)
    c = _app(with_middleware=False)
    r = c.get("/auth/google/callback",
              params={"code": "real-looking-code", "state": "forged.state"})
    assert r.status_code in (307, 302)
    assert "connect_error=google" in r.headers["location"]
    assert exchanged == []


def test_callback_with_no_state_links_nothing(monkeypatch, pa):
    async def _api_request(*a, **kw):
        raise AssertionError("token exchange must not run without a state")
    monkeypatch.setattr(pa, "api_request", _api_request)
    c = _app(with_middleware=False)
    r = c.get("/auth/google/callback", params={"code": "abc"})
    assert r.status_code in (307, 302)
    assert "connect_error" in r.headers["location"]


def test_callback_redirect_target_is_constrained(monkeypatch, pa):
    """A signed state cannot carry a hostile redirect either — _safe_redirect runs
    on the way out as well as the way in."""
    monkeypatch.setattr(pa, "_decode_state",
                        lambda s: {"user_id": "", "redirect_uri": "https://evil.test/x"})
    c = _app(with_middleware=False)
    r = c.get("/auth/google/callback", params={"code": "x", "state": "whatever"})
    assert "evil.test" not in r.headers["location"]
