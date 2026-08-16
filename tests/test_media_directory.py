"""iptv-org directory cache: structural filtering, refresh safety, search."""

import backend.tools as tools
from backend.services import media_directory as md

CHANNELS = [
    {"id": "good.ae", "name": "Good TV", "alt_names": ["G1"], "country": "AE",
     "categories": ["news"], "closed": False, "is_nsfw": False},
    {"id": "closed.ae", "name": "Closed TV", "country": "AE", "categories": ["news"],
     "closed": True, "is_nsfw": False},
    {"id": "nsfw.ae", "name": "NSFW TV", "country": "AE", "categories": [],
     "closed": False, "is_nsfw": True},
    {"id": "nostream.ae", "name": "No Stream TV", "country": "AE", "categories": [],
     "closed": False, "is_nsfw": False},
]
STREAMS = [
    {"channel": "good.ae", "url": "https://ok/a.m3u8", "quality": "1080p"},
    {"channel": "closed.ae", "url": "https://ok/b.m3u8"},
    {"channel": "nsfw.ae", "url": "https://ok/c.m3u8"},
    {"channel": "http.ae", "url": "http://insecure/d.m3u8"},
    {"channel": "ua.ae", "url": "https://ok/e.m3u8", "user_agent": "VLC/3"},
    {"channel": "ref.ae", "url": "https://ok/f.m3u8", "referrer": "https://x"},
]


# ── structural filtering ─────────────────────────────────────────────────────

def test_only_usable_entries_survive():
    rows = md.build_rows(CHANNELS, STREAMS)
    assert [r["ext_id"] for r in rows] == ["good.ae"]


def test_http_streams_dropped():
    """Mixed content: an http:// stream cannot load on an https page."""
    rows = md.build_rows([{"id": "http.ae", "name": "H", "closed": False}],
                         [{"channel": "http.ae", "url": "http://x/a.m3u8"}])
    assert rows == []


def test_streams_needing_custom_headers_dropped():
    """The subtle one: a browser's fetch cannot set User-Agent or Referrer, so
    these PASS a server-side check and then die in the player — the exact
    'works at add time, fails at play time' shape we refuse to ship."""
    for bad in ({"user_agent": "VLC/3"}, {"referrer": "https://x"}):
        rows = md.build_rows(
            [{"id": "x.ae", "name": "X", "closed": False}],
            [{"channel": "x.ae", "url": "https://ok/a.m3u8", **bad}])
        assert rows == [], f"a stream requiring {list(bad)[0]} must not be offered"


def test_closed_and_nsfw_dropped():
    rows = md.build_rows(CHANNELS, STREAMS)
    names = {r["name"] for r in rows}
    assert "Closed TV" not in names and "NSFW TV" not in names


# ── refresh safety ───────────────────────────────────────────────────────────

def test_truncated_upstream_is_refused(monkeypatch):
    """A refresh yielding implausibly few rows must NOT replace the index: an
    empty directory is worse than a stale one."""
    monkeypatch.setattr(md, "build_rows", lambda c, s: [{"x": 1}] * 10)
    calls = []
    monkeypatch.setattr(md, "_record_failure", lambda m: calls.append(m))
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return type("R", (), {"json": lambda self=None: []})()
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda **k: _C())
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "enabled", lambda: True)
    ok, msg = md.refresh()
    assert not ok and "refusing to replace" in msg
    assert calls and "refusing to replace" in calls[0]


def test_fetch_failure_records_but_does_not_wipe(monkeypatch):
    import httpx
    def boom(**k): raise RuntimeError("network down")
    monkeypatch.setattr(httpx, "Client", boom)
    recorded = []
    monkeypatch.setattr(md, "_record_failure", lambda m: recorded.append(m))
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "enabled", lambda: True)
    ok, msg = md.refresh()
    assert not ok and "fetch failed" in msg and recorded


def test_min_plausible_rows_is_below_the_real_size():
    """8,457 today. The gate must catch a truncation without tripping normally."""
    assert 1000 < md.MIN_PLAUSIBLE_ROWS < 8000


# ── classification ───────────────────────────────────────────────────────────

def test_search_is_read_and_volatile():
    """Upstream rots — about half of any sample is dead — so a repeated search
    must re-query rather than quote an earlier answer."""
    from backend.chat.stale import MARKER, mark_stale
    assert "search_tv_channels" in tools.VOLATILE_TOOLS
    assert "search_tv_channels" not in tools.ACTION_TOOLS
    out = mark_stale([{"role": "assistant", "content": "Found 5.",
                       "tools": ["search_tv_channels"]}])
    assert MARKER in out[0]["content"]


def test_bulk_add_is_an_action_and_never_volatile():
    """Re-calling a bulk add is 25 duplicate rows."""
    from backend.chat.stale import MARKER, mark_stale
    assert "add_tv_channels_bulk" in tools.ACTION_TOOLS
    assert "add_tv_channels_bulk" not in tools.VOLATILE_TOOLS
    out = mark_stale([{"role": "assistant", "content": "Added 5.",
                       "tools": ["add_tv_channels_bulk"]}])
    assert MARKER not in out[0]["content"]


def test_bulk_add_is_capped():
    from backend.services import livetv
    assert livetv.BULK_ADD_CAP == 25
    assert livetv.SHOW_LIMIT <= livetv.PROBE_LIMIT


def test_registered():
    import backend.guardrails as g
    names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert {"search_tv_channels", "add_tv_channels_bulk"} <= names
    for n in ("search_tv_channels", "add_tv_channels_bulk"):
        assert tools.group_of(n) == "livetv" and g.decide(n) != "deny"
