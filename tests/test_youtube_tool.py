"""Integrated YouTube tool: parsing → dispatch → (llm_result, embeds) split.

Adapted from reference/yt-tool-service/test_smoke.py. The reference proved a
standalone service (load tool source via exec, build a registry, hit two FastAPI
endpoints); none of that exists here, so those assertions are re-pointed at the
real integration path: backend.tools.execute_single_tool → tool_result.

NETWORK: the one live hop (_fetch_oembed) is monkeypatched, exactly as the
reference did, so the suite stays offline and deterministic. Everything else —
ID extraction, timestamp parsing, the player template, the Content-Disposition
split, guardrails, the ACTION/READ classification — is the real code path.
"""

import backend.tools as tools
from backend.services import youtube
from backend.tool_result import ToolResult, process_tool_result

OEMBED = {
    "title": "Never Gonna Give You Up",
    "author_name": "Rick Astley",
    "author_url": "https://www.youtube.com/@RickAstleyYT",
    "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
}


def _stub_oembed(monkeypatch, payload=None, exc=None):
    async def fake(video_id, timeout):
        if exc is not None:
            raise exc
        return payload if payload is not None else OEMBED
    monkeypatch.setattr(youtube, "_fetch_oembed", fake)


# ── ID / timestamp extraction (pure, no network) ─────────────────────────────

def test_extract_video_id_accepts_every_url_shape():
    for text in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                 "https://youtu.be/dQw4w9WgXcQ?t=43",
                 "https://www.youtube.com/shorts/dQw4w9WgXcQ",
                 "https://www.youtube.com/live/dQw4w9WgXcQ",
                 "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
                 "dQw4w9WgXcQ"):
        assert youtube._extract_video_id(text) == "dQw4w9WgXcQ", text


def test_extract_video_id_rejects_non_youtube():
    assert youtube._extract_video_id("not a youtube link at all") is None
    assert youtube._extract_video_id("") is None


def test_extract_start_seconds_handles_both_timestamp_forms():
    assert youtube._extract_start_seconds("https://youtu.be/x?t=43") == 43
    assert youtube._extract_start_seconds("https://youtu.be/x?t=90s") == 90
    # regression: the plain-seconds pattern must not match just the "1" of 1m30s
    assert youtube._extract_start_seconds("https://youtu.be/x?t=1m30s") == 90
    assert youtube._extract_start_seconds("https://youtu.be/x?t=1h2m3s") == 3723
    assert youtube._extract_start_seconds("https://youtu.be/x") == 0


# ── The (llm_result, embeds) split ───────────────────────────────────────────

def test_play_produces_embed_and_llm_context(monkeypatch):
    _stub_oembed(monkeypatch)
    result, is_action, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "https://youtu.be/dQw4w9WgXcQ?t=43"}, "user_1")

    assert embeds, "expected an embed for a valid video"
    assert "dQw4w9WgXcQ" in embeds[0]["html"], "video id should appear in the embed HTML"
    assert isinstance(result, str)
    assert "Now playing" in result and "Rick Astley" in result
    assert "43" in result, "start-time context should be passed to the LLM"
    assert is_action is False, "play_youtube_video is a READ tool (see READ_TOOLS)"


def test_llm_never_receives_the_markup(monkeypatch):
    """The whole point of the split: the model gets prose, not HTML."""
    _stub_oembed(monkeypatch)
    result, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "dQw4w9WgXcQ"}, "user_1")
    assert "<iframe" not in result and "<!DOCTYPE" not in result
    assert "<!DOCTYPE" in embeds[0]["html"]


def test_invalid_input_produces_no_embed(monkeypatch):
    _stub_oembed(monkeypatch)
    result, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "not a youtube link at all"}, "user_1")
    assert embeds == [], "invalid input should not render a player"
    assert isinstance(result, str) and "doesn't look like" in result


def test_missing_video_arg_is_handled(monkeypatch):
    _stub_oembed(monkeypatch)
    result, _, embeds = tools.execute_single_tool("play_youtube_video", {}, "user_1")
    assert embeds == []
    assert result.startswith("⚠️")


def test_unavailable_video_returns_text_not_a_player(monkeypatch):
    _stub_oembed(monkeypatch, exc=ValueError("Video not found, private, or embedding "
                                             "is disabled by the uploader."))
    result, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "dQw4w9WgXcQ"}, "user_1")
    assert embeds == []
    assert "embedding is disabled" in result


def test_metadata_failure_still_renders_the_player(monkeypatch):
    """oEmbed is a nicety — a lookup failure must not cost the user the player."""
    _stub_oembed(monkeypatch, exc=RuntimeError("connect timeout"))
    result, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "dQw4w9WgXcQ"}, "user_1")
    assert embeds and "dQw4w9WgXcQ" in embeds[0]["html"]
    assert "metadata lookup failed" in result


def test_video_info_returns_plain_text_and_no_embed(monkeypatch):
    _stub_oembed(monkeypatch)
    result, is_action, embeds = tools.execute_single_tool(
        "get_youtube_video_info", {"video": "dQw4w9WgXcQ"}, "user_1")
    assert embeds == [], "info lookup must not embed a player"
    assert "Never Gonna Give You Up" in result and "Rick Astley" in result
    assert is_action is False


# ── The embedded document's own security properties ──────────────────────────

def test_player_html_escapes_untrusted_metadata(monkeypatch):
    """Titles come from YouTube, i.e. untrusted. They must not break out of the
    markup or terminate the <script> block."""
    _stub_oembed(monkeypatch, payload={
        "title": '</script><img src=x onerror=alert(1)>"',
        "author_name": "<b>evil</b>",
        "thumbnail_url": 'https://i.ytimg.com/vi/x.jpg" onload="alert(1)',
    })
    _, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "dQw4w9WgXcQ"}, "user_1")
    html = embeds[0]["html"]
    assert "<img src=x onerror" not in html
    assert "<b>evil</b>" not in html
    assert 'onload="alert(1)' not in html
    # exactly one <script> element: the height reporter
    assert html.count("<script>") == 1 and html.count("</script>") == 1


def test_player_posts_the_height_handshake(monkeypatch):
    """The frontend clamp/validation is useless if the document never reports."""
    _stub_oembed(monkeypatch)
    _, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "dQw4w9WgXcQ"}, "user_1")
    assert "iframe:height" in embeds[0]["html"]
    assert "parent.postMessage" in embeds[0]["html"]


def test_start_time_shows_on_the_card(monkeypatch):
    """The URL printed on the card carries the timestamp too. It is inside a
    markup document, so it is HTML-escaped there — unlike the React link, which
    gets the raw form (see test_start_time_survives_into_the_react_link)."""
    _stub_oembed(monkeypatch)
    _, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "https://youtu.be/dQw4w9WgXcQ?t=1m30s"}, "user_1")
    assert "&amp;t=90s" in embeds[0]["html"]


def test_card_is_inert_no_iframe_no_link(monkeypatch):
    """Card mode must contain no nested iframe (the black-screen path) and no
    anchor. A link inside the sandbox cannot work: the popup inherits the sandbox
    and youtube.com refuses the opaque origin. The clickable link is rendered by
    React outside the frame instead."""
    _stub_oembed(monkeypatch)
    _, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "dQw4w9WgXcQ"}, "user_1")
    html = embeds[0]["html"]
    assert "youtube.com/embed" not in html
    assert "<iframe" not in html
    assert "<a " not in html and "target=" not in html


# ── process_tool_result contract ─────────────────────────────────────────────

def test_plain_string_results_pass_through_untouched():
    """Every pre-existing tool returns a bare string; nothing may change for them."""
    out = process_tool_result("get_analytics", "You have 3 open tasks.")
    assert isinstance(out, ToolResult)
    assert out.llm_result == "You have 3 open tasks." and out.embeds == []


def test_html_response_without_inline_disposition_is_text_not_a_widget():
    from fastapi.responses import HTMLResponse
    out = process_tool_result("some_tool", HTMLResponse(content="<p>hi</p>"))
    assert out.embeds == [] and out.llm_result == "<p>hi</p>"


def test_error_status_html_response_reports_an_error_to_the_llm():
    from fastapi.responses import HTMLResponse
    out = process_tool_result("some_tool", HTMLResponse(
        content="<html><head></head><body>boom</body></html>", status_code=500,
        headers={"content-disposition": "inline"}))
    assert out.embeds and "error 500" in out.llm_result


# ── Registration invariants (mirrors the existing test_tools.py guards) ──────

def test_youtube_tools_are_registered_everywhere_they_must_be():
    import backend.guardrails as g
    names = {t["function"]["name"] for t in tools.TOOL_SCHEMAS}
    for name in ("play_youtube_video", "get_youtube_video_info"):
        assert name in names, f"{name} missing from TOOL_SCHEMAS"
        assert g.decide(name) != "deny", f"{name} would be denied at dispatch"
        assert name in tools.READ_TOOLS and name not in tools.ACTION_TOOLS


# ── The out-of-frame link (rendered by React, not inside the sandbox) ─────────

def test_embed_carries_a_link_for_react_to_render(monkeypatch):
    _stub_oembed(monkeypatch)
    _, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "dQw4w9WgXcQ"}, "user_1")
    link = embeds[0]["link"]
    assert link["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert link["label"] == "Never Gonna Give You Up"


def test_start_time_survives_into_the_react_link(monkeypatch):
    """The card text is not enough — the timestamp has to reach the actual link
    the user clicks, or 'play it from 1:30' opens at 0:00."""
    _stub_oembed(monkeypatch)
    _, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "https://youtu.be/dQw4w9WgXcQ?t=1m30s"}, "user_1")
    assert embeds[0]["link"]["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=90s"


def test_link_label_is_raw_not_html_escaped(monkeypatch):
    """React escapes on render, so the label must arrive RAW. Pre-escaping here
    is double-escaping: the user would literally see 'Rock &amp; Roll'.

    The same title must still be escaped inside the iframe document, where it is
    interpolated into markup rather than rendered by React."""
    raw = """Rock & Roll — Bob's "Best" <Hits>"""
    _stub_oembed(monkeypatch, payload={"title": raw, "author_name": "Bob & Co",
                                       "thumbnail_url": "https://i.ytimg.com/vi/x.jpg"})
    _, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "dQw4w9WgXcQ"}, "user_1")

    label = embeds[0]["link"]["label"]
    assert label == raw, "label must be byte-identical to the oEmbed title"
    for entity in ("&amp;", "&#x27;", "&quot;", "&lt;", "&gt;"):
        assert entity not in label, f"label was HTML-escaped ({entity}) — React will double-escape it"

    # ...but the iframe document interpolates it into markup, so there it IS escaped
    html = embeds[0]["html"]
    assert "&amp;" in html and "<Hits>" not in html


# ── Portability of the Open WebUI 2-tuple contract ───────────────────────────

def test_two_tuple_contract_is_unchanged_and_yields_no_link():
    """The other five exported Open WebUI tools return (HTMLResponse, context)
    with no third element. That path must keep working untouched — link is just
    absent."""
    from fastapi.responses import HTMLResponse
    resp = HTMLResponse(content="<html><head></head><body>hi</body></html>",
                        headers={"content-disposition": "inline"})
    out = process_tool_result("some_ported_tool", (resp, "context for the model"))
    assert out.llm_result == "context for the model"
    assert len(out.embeds) == 1
    assert out.embeds[0]["html"].endswith("</html>")
    assert out.embeds[0]["link"] is None


def test_bare_html_response_still_embeds_with_no_link():
    from fastapi.responses import HTMLResponse
    out = process_tool_result("t", HTMLResponse(
        content="<html><head></head><body>x</body></html>",
        headers={"content-disposition": "inline"}))
    assert out.embeds[0]["link"] is None
    assert "embedded UI result" in out.llm_result


# ── Inline player payload (rendered by React at YouTube's own origin) ─────────

def test_embed_carries_video_id_and_start(monkeypatch):
    _stub_oembed(monkeypatch)
    _, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "https://youtu.be/dQw4w9WgXcQ?t=1m30s"}, "user_1")
    assert embeds[0]["video"] == {"id": "dQw4w9WgXcQ", "start": 90}


def test_video_id_is_always_exactly_eleven_chars(monkeypatch):
    """The frontend refuses any id that isn't 11 chars before building an iframe
    src, so the backend must never emit anything else."""
    import re
    _stub_oembed(monkeypatch)
    for url in ("dQw4w9WgXcQ", "https://www.youtube.com/shorts/dQw4w9WgXcQ",
                "https://youtu.be/dQw4w9WgXcQ?t=43"):
        _, _, embeds = tools.execute_single_tool(
            "play_youtube_video", {"video": url}, "user_1")
        assert re.fullmatch(r"[A-Za-z0-9_-]{11}", embeds[0]["video"]["id"]), url


def test_card_html_is_still_sent_as_the_fallback(monkeypatch):
    """A client that doesn't understand `video` must still get something."""
    _stub_oembed(monkeypatch)
    _, _, embeds = tools.execute_single_tool(
        "play_youtube_video", {"video": "dQw4w9WgXcQ"}, "user_1")
    assert embeds[0]["html"].startswith("<!DOCTYPE html>")
    assert embeds[0]["link"]["url"].endswith("v=dQw4w9WgXcQ")


def test_tools_without_video_get_none():
    from fastapi.responses import HTMLResponse
    out = process_tool_result("weatherish_tool", (
        HTMLResponse(content="<html><head></head><body>x</body></html>",
                     headers={"content-disposition": "inline"}), "ctx"))
    assert out.embeds[0]["video"] is None and out.embeds[0]["link"] is None


# ── Search (yt-dlp, keyless). Network is stubbed; ordering/shape are real. ────

def _stub_search(monkeypatch, entries=None, exc=None):
    """Replace the one network hop (_search) exactly as _fetch_oembed is stubbed."""
    def fake(query, limit):
        if exc is not None:
            raise exc
        rows = entries if entries is not None else [
            {"id": "dQw4w9WgXcQ", "title": "Never Gonna Give You Up",
             "channel": "Rick Astley", "thumb": "https://i.ytimg.com/vi/dQw4w9WgXcQ/mq.jpg",
             "duration": 213},
            {"id": "aaaaaaaaaaa", "title": "A Cover", "channel": "Someone",
             "thumb": "https://i.ytimg.com/vi/aaaaaaaaaaa/mq.jpg", "duration": 200},
        ]
        return rows[:limit]
    monkeypatch.setattr(youtube, "_search", fake)


def test_wants_recent_picks_ordering_per_query():
    """Neither ordering is a safe default: relevance answers 'man city highlights'
    with a match from years ago; date answers a classic with an hour-old cover."""
    assert youtube._wants_recent("latest man city highlights") is True
    assert youtube._wants_recent("man city highlights") is True      # 'highlights'
    assert youtube._wants_recent("breaking news today") is True
    assert youtube._wants_recent("never gonna give you up") is False
    assert youtube._wants_recent("1994 world cup final") is False    # old year
    import datetime
    this_year = datetime.date.today().year
    assert youtube._wants_recent(f"{this_year} season review") is True


def test_search_target_switches_between_date_and_relevance():
    assert youtube._search_target("never gonna give you up", 5).startswith("ytsearch5:")
    recent = youtube._search_target("latest highlights", 5)
    assert recent.startswith("https://www.youtube.com/results?") and "sp=CAI" in recent


def test_play_mode_returns_one_ready_player(monkeypatch):
    _stub_search(monkeypatch)
    result, is_action, embeds = tools.execute_single_tool(
        "search_youtube", {"query": "never gonna give you up", "mode": "play"}, "user_1")
    assert embeds[0]["video"] == {"id": "dQw4w9WgXcQ", "start": 0}
    assert embeds[0]["results"] is None, "play mode must not also send a picker"
    assert "Found and now playing" in result and is_action is False


def test_browse_mode_returns_structured_results_not_a_player(monkeypatch):
    _stub_search(monkeypatch)
    result, _, embeds = tools.execute_single_tool(
        "search_youtube", {"query": "python tutorial", "mode": "browse",
                           "max_results": 2}, "user_1")
    e = embeds[0]
    assert e["video"] is None, "browse mode must let the user pick"
    assert [r["id"] for r in e["results"]] == ["dQw4w9WgXcQ", "aaaaaaaaaaa"]
    assert set(e["results"][0]) >= {"id", "title", "channel", "thumb", "duration"}
    assert e["query"] == "python tutorial"
    assert "Showed 2 YouTube results" in result


def test_browse_fallback_card_has_no_iframe_and_no_link(monkeypatch):
    """The reference's results list played items via a nested iframe, which
    renders black inside our sandbox. Ours must contain neither that nor a link."""
    _stub_search(monkeypatch)
    _, _, embeds = tools.execute_single_tool(
        "search_youtube", {"query": "x", "mode": "browse"}, "user_1")
    html = embeds[0]["html"]
    assert "<iframe" not in html and "youtube.com/embed" not in html
    assert "<a " not in html and "target=" not in html


def test_search_defaults_to_play_mode(monkeypatch):
    _stub_search(monkeypatch)
    _, _, embeds = tools.execute_single_tool("search_youtube", {"query": "x"}, "user_1")
    assert embeds[0]["video"] is not None and embeds[0]["results"] is None


def test_search_rejects_malformed_ids(monkeypatch):
    """Ids reach an iframe src, so anything not exactly 11 chars is dropped."""
    _stub_search(monkeypatch, entries=[
        {"id": "short", "title": "bad", "channel": "", "thumb": "", "duration": 0},
        {"id": "dQw4w9WgXcQ", "title": "good", "channel": "", "thumb": "", "duration": 0},
    ])
    _, _, embeds = tools.execute_single_tool(
        "search_youtube", {"query": "x", "mode": "browse"}, "user_1")
    assert [r["id"] for r in embeds[0]["results"]] == ["dQw4w9WgXcQ"]


def test_empty_and_failed_searches_degrade_to_text(monkeypatch):
    _stub_search(monkeypatch, entries=[])
    result, _, embeds = tools.execute_single_tool(
        "search_youtube", {"query": "zzz", "mode": "browse"}, "user_1")
    assert embeds == [] and "No YouTube results" in result

    _stub_search(monkeypatch, exc=RuntimeError("network down"))
    result, _, embeds = tools.execute_single_tool("search_youtube", {"query": "x"}, "user_1")
    assert embeds == [] and result.startswith("⚠️")


def test_search_tool_is_hidden_when_yt_dlp_is_absent(monkeypatch):
    """Offering a tool that can only fail is worse than not offering it."""
    import importlib
    import importlib.util as util
    real = util.find_spec
    monkeypatch.setattr(util, "find_spec",
                        lambda n, *a, **k: None if n == "yt_dlp" else real(n, *a, **k))
    reloaded = importlib.reload(tools)
    try:
        names = {s["function"]["name"] for s in reloaded.TOOL_SCHEMAS}
        assert "search_youtube" not in names
        assert "play_youtube_video" in names, "only search depends on yt-dlp"
    finally:
        monkeypatch.undo()
        importlib.reload(tools)
