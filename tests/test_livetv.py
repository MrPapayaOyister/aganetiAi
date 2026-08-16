"""Live TV — HLS player, vendored hls.js, video CSP profile, channel library."""

import pathlib

import backend.tools as tools
from backend.services import livetv, media_sources

_ROOT = pathlib.Path(__file__).resolve().parent.parent
CH = [{"name": "NASA TV", "url": "https://x/master.m3u8", "category": "science"},
      {"name": "DW", "url": "https://y/index.m3u8", "category": "news"}]


def _stub_channels(monkeypatch, rows=None):
    monkeypatch.setattr(media_sources, "list_sources",
                        lambda uid, kind="tv", **k: rows if rows is not None else CH)


# ── the vendored player ──────────────────────────────────────────────────────

def test_hls_js_is_vendored_not_a_cdn():
    """Upstream loads hls.js@latest from a CDN. Pinned and first-party here, so
    the video CSP profile's script-src stays our own origin and the player
    cannot change under us."""
    asset = _ROOT / "frontend" / "public" / "vendor" / "hls.min.js"
    assert asset.exists(), "vendored hls.js is missing"
    assert asset.stat().st_size > 100_000, "hls.min.js looks truncated"
    # Assert on what the tool EMITS, not on the module text — the docstring
    # mentions the CDN precisely to explain why we do not use it.
    assert "cdn" not in livetv.HLS_JS_SRC and "@latest" not in livetv.HLS_JS_SRC
    assert livetv.HLS_JS_SRC.endswith("/vendor/hls.min.js")


def test_update_instructions_live_next_to_the_asset():
    """Someone updating hls.js looks in the vendor directory, not in a doc."""
    readme = _ROOT / "frontend" / "public" / "vendor" / "README.md"
    assert readme.exists()
    text = readme.read_text()
    assert "1.5.20" in text, "the pinned version must be recorded"
    assert "sha256" in text.lower() and "CSP" in text
    assert "cdn.jsdelivr.net/npm/hls.js@" in text, "the update command must be there"


def test_player_references_the_asset_absolutely(monkeypatch):
    """A srcdoc frame has an opaque origin and no base URL — a relative src
    cannot resolve there."""
    _stub_channels(monkeypatch)
    _, _, embeds = tools.execute_single_tool("watch_live_tv", {}, "user_1")
    html = embeds[0]["html"]
    assert "/vendor/hls.min.js" in html
    before = html.split("/vendor/hls.min.js")[0]
    assert before.rstrip().endswith(('"http://localhost:3000', '"http://localhost:3000')) or \
        "http" in before[-60:], "the script src must be absolute"


# ── CSP profile ──────────────────────────────────────────────────────────────

def test_live_tv_declares_the_video_profile_by_name(monkeypatch):
    """A NAME, never a policy — a tool that could author its own CSP would make
    the sandbox advisory."""
    _stub_channels(monkeypatch)
    _, _, embeds = tools.execute_single_tool("watch_live_tv", {}, "user_1")
    assert embeds[0]["csp"] == "video"
    src = (_ROOT / "backend" / "services" / "livetv.py").read_text()
    assert "default-src" not in src, "the backend must not author a CSP"


def test_other_tools_get_the_strict_default(monkeypatch):
    """A news or weather card must not inherit a video player's permissions."""
    from backend.services import news, weather

    class _R:
        content = b"""<?xml version="1.0"?><rss version="2.0"><channel><item>
        <title>T</title><link>https://e.com/1</link><description>d</description>
        </item></channel></rss>"""
        text = ""
        def raise_for_status(self): pass
        def json(self): return {"results": [{"name": "Dubai", "latitude": 1, "longitude": 1}]}
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    _, _, news_embeds = tools.execute_single_tool("get_news", {}, "user_1")
    assert news_embeds[0]["csp"] is None, "news must fall back to the strict default"


def test_frontend_owns_the_policies_and_falls_back_closed():
    ts = (_ROOT / "frontend" / "src" / "lib" / "embedWidget.ts").read_text()
    assert "CSP_PROFILES" in ts and "video:" in ts
    assert "connect-src https:" in ts, "the video profile needs connect-src for hls.js"
    assert "|| CSP_PROFILES.default" in ts, "an unknown profile must fail closed"
    # the video grants must NOT be in the shared default
    default_block = ts.split("export const DEFAULT_EMBED_CSP")[1].split("]")[0]
    assert "connect-src" not in default_block
    assert "script-src 'unsafe-inline'\"" in default_block or "script-src 'unsafe-inline'" in default_block


# ── channel library ──────────────────────────────────────────────────────────

def test_named_channel_is_selected(monkeypatch):
    _stub_channels(monkeypatch)
    result, _, embeds = tools.execute_single_tool(
        "watch_live_tv", {"channel": "dw"}, "user_1")
    assert "DW" in result and embeds


def test_unknown_channel_lists_what_exists(monkeypatch):
    _stub_channels(monkeypatch)
    result, _, embeds = tools.execute_single_tool(
        "watch_live_tv", {"channel": "BBC One"}, "user_1")
    assert embeds == [] and "NASA TV" in result and "DW" in result


def test_channel_names_cannot_break_out_of_the_script(monkeypatch):
    _stub_channels(monkeypatch, [{"name": "</script><img src=x onerror=alert(1)>",
                                  "url": "https://x/a.m3u8", "category": "n"}])
    _, _, embeds = tools.execute_single_tool("watch_live_tv", {}, "user_1")
    html = embeds[0]["html"]
    assert "<img src=x onerror" not in html
    assert html.count("<script") == 2, "only the hls.js tag and the card's own"


# ── add-time validation ──────────────────────────────────────────────────────

def test_non_http_url_rejected():
    ok, msg = media_sources.validate_hls("ftp://host/x.m3u8")
    assert not ok and "http" in msg


def test_cors_failure_is_rejected_with_the_reason(monkeypatch):
    """THE important one: a stream that plays in a normal tab but not inside our
    sandbox would look like our bug and reproduce nowhere else."""
    class _R:
        status_code = 200
        headers = {}                      # no Access-Control-Allow-Origin
        text = "#EXTM3U\n"
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    ok, msg = media_sources.validate_hls("https://host/x.m3u8")
    assert not ok
    assert "cross-origin" in msg and "sandboxed" in msg


def test_non_playlist_body_rejected(monkeypatch):
    """A login page returning 200 with HTML must not be stored as a channel."""
    class _R:
        status_code = 200
        headers = {"access-control-allow-origin": "*"}
        text = "<html>login</html>"
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    ok, msg = media_sources.validate_hls("https://host/x.m3u8")
    assert not ok and "EXTM3U" in msg


def test_valid_stream_accepted(monkeypatch):
    class _R:
        status_code = 200
        headers = {"access-control-allow-origin": "*"}
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow.m3u8\n"
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    assert media_sources.validate_hls("https://host/x.m3u8") == (True, "ok")


def test_seed_list_excludes_the_known_bad_channel():
    """Hermes dropped Red Bull TV: rotating endpoint, stream failing. Carrying
    that knowledge over beats rediscovering it."""
    names = {c["name"] for c in media_sources.SEED_CHANNELS}
    assert "NASA TV" in names and len(names) == 5
    assert not any("red bull" in n.lower() for n in names)


# ── registration ─────────────────────────────────────────────────────────────

def test_registered_everywhere():
    import backend.guardrails as g
    names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert {"watch_live_tv", "add_tv_channel"} <= names
    assert tools.group_of("watch_live_tv") == "livetv"
    assert tools.group_of("add_tv_channel") == "livetv"
    assert g.decide("watch_live_tv") == "auto"
    assert "livetv" in {gr["id"] for gr in tools.TOOL_GROUPS}


def test_adding_a_channel_is_an_action_not_a_read():
    """It writes to the user's library, so it must never be marked stale and
    re-called — that would re-add a channel."""
    from backend.chat.stale import MARKER, mark_stale
    assert "add_tv_channel" in tools.ACTION_TOOLS
    assert "add_tv_channel" not in tools.VOLATILE_TOOLS
    out = mark_stale([{"role": "assistant", "content": "Added.",
                       "tools": ["add_tv_channel"]}])
    assert MARKER not in out[0]["content"]


def test_watching_is_deliberately_never_marked():
    """watch_live_tv is NOT marked stale, and that is not an oversight.

    It looks like one: the tool's result plainly does change — the channel list,
    what is on air — so "volatile" is the honest description, and this test
    originally asserted the marker WAS applied. Do not put it back without
    reading this.

    VOLATILE_TOOLS exists for one purpose: deciding what mark_stale annotates,
    and a player turn's reply ("Playing NASA TV") carries no data for a staleness
    note to be about. So it lives in WIDGET_TOOLS, which mark_stale subtracts.

    Do NOT read this as "marking was proven harmful". Numbers that appeared here
    earlier came from a bench running with model thinking ENABLED, while the
    server disables it — see the STATUS note on WIDGET_TOOLS in backend/tools.py.
    The repeat-request bug is still open and this exclusion is not its fix.
    """
    from backend.chat.stale import MARKER, mark_stale
    assert "watch_live_tv" in tools.WIDGET_TOOLS
    assert "watch_live_tv" not in tools.VOLATILE_TOOLS, \
        "putting it back here re-marks the turn and re-breaks the repeat request"
    out = mark_stale([{"role": "assistant", "content": "Playing NASA TV.",
                       "tools": ["watch_live_tv"]}])[0]["content"]
    assert MARKER not in out
