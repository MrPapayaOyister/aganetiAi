"""Provider credentials must never cross between users.

The bug this pins shut: `_fetch_connection` used to end with a third step — if the
caller had no connection of their own, fall back to the sole live connection for that
provider, *whoever owned it*. With one human that read as "stay bound to whichever
account is actually connected". With real accounts it meant every user who had not yet
connected anything silently got the one mailbox that was connected, and the assistant
answered about somebody else's mail, meetings and analytics without any sign that it
had done so.

The guard that existed — refuse when two or more connections are live — is exactly
backwards for the dangerous case: a deployment with ONE connected account leaks it to
everyone, and only stops leaking once a second person connects.

These tests assert the property (a stranger resolves to nothing) rather than the
absence of a particular function, so they still fail if the behaviour returns by
another route.
"""

import asyncio

import pytest

from backend.services import provider_tokens as pt


CONNECTED = "owner-sub-0001"
STRANGER = "stranger-sub-9999"


@pytest.fixture
def one_live_connection(monkeypatch):
    """A deployment with exactly ONE connected account — the leaking shape."""
    row = {
        "user_id": CONNECTED,
        "provider": "microsoft",
        "provider_email": "owner@example.com",
        "access_token": "at",
        "refresh_token": "rt",
    }

    async def fake_pg_fetch(uid, provider):
        return dict(row) if uid == CONNECTED and provider == "microsoft" else None

    monkeypatch.setattr(pt._pg, "fetch", fake_pg_fetch)
    # Neither legacy store may answer for anyone else either.
    monkeypatch.setattr(pt._file, "fetch", lambda uid, p: None)
    monkeypatch.setattr(pt._file, "fetch_all_for_provider", lambda p: [dict(row)])
    monkeypatch.setattr(pt, "get_supabase_admin", lambda: (_ for _ in ()).throw(RuntimeError("no supabase")))
    monkeypatch.setattr(pt.token_crypto, "dec_row", lambda r: r)

    async def no_aliases(uid):
        return []

    monkeypatch.setattr(pt, "_alias_candidates", no_aliases)
    return row


def test_the_owner_still_resolves(one_live_connection):
    """The fix must not cost the connected user their own connection."""
    got = asyncio.run(pt._fetch_connection(CONNECTED, "microsoft"))
    assert got is not None
    assert got["provider_email"] == "owner@example.com"


def test_a_stranger_gets_nothing_when_one_account_is_connected(one_live_connection):
    """THE regression. One live connection is the leaking case, not the safe one."""
    assert asyncio.run(pt._fetch_connection(STRANGER, "microsoft")) is None


def test_connected_providers_is_empty_for_a_stranger(one_live_connection):
    """Drives the UI's connect prompt. Reporting 'microsoft' here told a new user they
    were already linked and blocked them from connecting an account of their own."""
    assert asyncio.run(pt.connected_providers(STRANGER)) == []
    assert asyncio.run(pt.connected_providers(CONNECTED)) == ["microsoft"]


def test_a_stranger_is_told_to_connect_rather_than_served_someone_else(one_live_connection):
    """The miss has to surface as the actionable prompt, not an empty inbox."""
    with pytest.raises(Exception) as e:
        asyncio.run(pt.get_provider_token(STRANGER, "microsoft"))
    detail = getattr(e.value, "detail", None) or {}
    assert detail.get("error") == "microsoft_not_connected", detail
    assert "Settings" in detail.get("message", "")


def test_no_call_site_can_re_enable_the_fallback():
    """Removed outright, not defaulted off — a keyword argument that re-opens a data
    leak is a footgun, and `live_fallback=True` would read as innocuous in review."""
    import inspect

    assert not hasattr(pt, "_sole_live_connection")
    sig = inspect.signature(pt._fetch_connection)
    assert list(sig.parameters) == ["user_id", "provider"], sig
    assert "live_fallback" not in inspect.signature(pt.connected_providers).parameters


def test_fetch_never_scans_across_users():
    """Structural: the read path must not consult the all-rows scan, which is the only
    thing in the module that can see other people's connections."""
    import inspect

    src = inspect.getsource(pt._fetch_connection)
    assert "_all_rows_for_provider" not in src
    assert "fetch_all_for_provider" not in src
