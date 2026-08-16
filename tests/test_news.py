"""News reader — public RSS, no key, snapshot-is-truth.

Ported from Hermes. Network is stubbed; the parsing, the card template and the
sandbox-driven divergences are the real code path.
"""

import backend.tools as tools
from backend.services import news

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>First story</title><link>https://example.com/1</link>
      <description>&lt;p&gt;Summary   one&lt;/p&gt;</description></item>
<item><title>Second story</title><link>https://example.com/2</link>
      <description>Summary two</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom story</title><link href="https://example.com/a"/>
       <summary>Atom summary</summary></entry></feed>"""


class _Resp:
    def __init__(self, content): self.content = content
    def raise_for_status(self): pass


def _stub(monkeypatch, content=RSS, exc=None, fail_for=()):
    import httpx
    def fake_get(url, **kw):
        for frag in fail_for:
            if frag in url:
                raise RuntimeError("feed down")
        if exc is not None:
            raise exc
        return _Resp(content)
    monkeypatch.setattr(httpx, "get", fake_get)


# ── parsing ──────────────────────────────────────────────────────────────────

def test_rss_is_parsed_and_summaries_cleaned(monkeypatch):
    _stub(monkeypatch)
    result, is_action, embeds = tools.execute_single_tool(
        "get_news", {"category": "tech"}, "user_1")
    assert is_action is False and embeds
    assert "First story" in result and "https://example.com/1" in result
    # tags stripped, entities unescaped, whitespace collapsed
    assert "Summary one" in embeds[0]["html"]
    assert "<p>" not in embeds[0]["html"]


def test_atom_feeds_also_parse(monkeypatch):
    """A feed swapping RSS for Atom must not silently return nothing."""
    _stub(monkeypatch, content=ATOM)
    result, _, embeds = tools.execute_single_tool("get_news", {}, "user_1")
    assert "Atom story" in result and "https://example.com/a" in result


def test_all_fetches_every_category(monkeypatch):
    _stub(monkeypatch)
    _, _, embeds = tools.execute_single_tool("get_news", {"category": "all"}, "user_1")
    # 4 feeds x 2 items
    assert embeds[0]["html"].count("First story") == len(news.FEEDS)


def test_unknown_category_falls_back_to_world(monkeypatch):
    _stub(monkeypatch)
    _, _, embeds = tools.execute_single_tool(
        "get_news", {"category": "sports"}, "user_1")
    assert "world" in embeds[0]["html"]


def test_partial_failure_is_reported_not_swallowed(monkeypatch):
    """A silently missing category looks identical to 'there is no news', which
    is never true."""
    _stub(monkeypatch, fail_for=("sciencedaily",))
    result, _, embeds = tools.execute_single_tool(
        "get_news", {"category": "all"}, "user_1")
    assert embeds, "the reachable feeds should still render"
    assert "unreachable" in result and "science" in result


def test_total_failure_degrades_to_text(monkeypatch):
    _stub(monkeypatch, exc=RuntimeError("network down"))
    result, _, embeds = tools.execute_single_tool("get_news", {}, "user_1")
    assert embeds == [] and result.startswith("⚠️")


# ── the card's sandbox-driven divergences from Hermes ────────────────────────

def test_card_has_no_anchors(monkeypatch):
    """Hermes has a 'Read Full Article' link. Ours cannot: the frame is sandboxed
    to allow-scripts only, so a target=_blank popup gets an opaque origin and the
    browser refuses it — the same failure that broke the YouTube card. The URL is
    shown as selectable text and the model cites it as markdown instead."""
    import re
    _stub(monkeypatch)
    _, _, embeds = tools.execute_single_tool("get_news", {}, "user_1")
    html = embeds[0]["html"]
    assert re.findall(r"<a\s+[^>]*href=", html) == []
    assert "createElement('a')" not in html


def test_model_is_told_to_cite_the_links(monkeypatch):
    _stub(monkeypatch)
    result, _, _ = tools.execute_single_tool("get_news", {}, "user_1")
    assert "markdown link" in result


def test_feed_content_never_becomes_markup(monkeypatch):
    """Feed content is third-party and arbitrary."""
    hostile = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>&lt;img src=x onerror=alert(1)&gt;</title>
    <link>javascript:alert(1)</link><description>&lt;script&gt;bad()&lt;/script&gt;</description>
    </item></channel></rss>"""
    _stub(monkeypatch, content=hostile)
    _, _, embeds = tools.execute_single_tool("get_news", {}, "user_1")
    html = embeds[0]["html"]
    assert "<img src=x onerror" not in html
    # exactly one <script> element: the card's own
    assert html.count("<script>") == 1 and html.count("</script>") == 1


def test_javascript_urls_are_not_shown_as_links(monkeypatch):
    """The card only renders http(s) URLs; a javascript: link must not appear."""
    hostile = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>T</title><link>javascript:alert(1)</link><description>d</description>
    </item></channel></rss>"""
    _stub(monkeypatch, content=hostile)
    _, _, embeds = tools.execute_single_tool("get_news", {}, "user_1")
    assert "/^https?:" in embeds[0]["html"], "the http(s) guard must be present"


def test_card_posts_the_height_handshake(monkeypatch):
    _stub(monkeypatch)
    _, _, embeds = tools.execute_single_tool("get_news", {}, "user_1")
    assert "iframe:height" in embeds[0]["html"]


# ── registration ─────────────────────────────────────────────────────────────

def test_registered_everywhere():
    import backend.guardrails as g
    names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert "get_news" in names
    assert g.decide("get_news") == "auto"
    assert "get_news" in tools.READ_TOOLS and "get_news" not in tools.ACTION_TOOLS
    assert tools.group_of("get_news") == "news"
    assert "news" in {gr["id"] for gr in tools.TOOL_GROUPS}


def test_news_is_volatile_and_marks_stale():
    """Headlines change constantly and the reply carries them, so a repeat would
    otherwise be answered from the transcript."""
    from backend.chat.stale import MARKER, mark_stale
    assert "get_news" in tools.VOLATILE_TOOLS
    out = mark_stale([{"role": "assistant", "content": "Top story: X.",
                       "tools": ["get_news"]}])
    assert MARKER in out[0]["content"]


def test_disabling_the_news_group_removes_only_that_tool():
    offered = {s["function"]["name"] for s in tools.tools_for(["news"])}
    assert "get_news" not in offered
    assert "get_weather" in offered


# ── structured article links (rendered by React, outside the frame) ──────────

def test_articles_are_in_the_payload(monkeypatch):
    """The card cannot carry a working anchor, and the model-cited markdown
    fallback is unreliable — it produced zero links when asked to summarise a
    single story (2/2 trials). The link has to come from the payload."""
    _stub(monkeypatch)
    _, _, embeds = tools.execute_single_tool("get_news", {"category": "tech"}, "user_1")
    arts = embeds[0]["articles"]
    assert len(arts) == 2
    assert arts[0] == {"title": "First story", "url": "https://example.com/1",
                       "category": "tech"}


def test_every_article_with_a_usable_link_is_offered(monkeypatch):
    """All stories get a link, not just the one selected in the card — selection
    lives inside the iframe and reading it would mean trusting a new inbound
    message type from tool HTML."""
    _stub(monkeypatch)
    _, _, embeds = tools.execute_single_tool("get_news", {"category": "all"}, "user_1")
    assert len(embeds[0]["articles"]) == 2 * len(news.FEEDS)


def test_non_http_links_are_excluded_from_the_payload(monkeypatch):
    hostile = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Bad</title><link>javascript:alert(1)</link><description>d</description></item>
    <item><title>Good</title><link>https://ok.example/1</link><description>d</description></item>
    </channel></rss>"""
    _stub(monkeypatch, content=hostile)
    _, _, embeds = tools.execute_single_tool("get_news", {}, "user_1")
    urls = [a["url"] for a in embeds[0]["articles"]]
    assert urls == ["https://ok.example/1"], "javascript: must never reach an href"


def test_card_still_has_no_anchors_of_its_own(monkeypatch):
    """The links moved OUT; nothing crept back in."""
    import re
    _stub(monkeypatch)
    _, _, embeds = tools.execute_single_tool("get_news", {}, "user_1")
    assert re.findall(r"<a\s+[^>]*href=", embeds[0]["html"]) == []


def test_news_keeps_the_default_csp_profile(monkeypatch):
    _stub(monkeypatch)
    _, _, embeds = tools.execute_single_tool("get_news", {}, "user_1")
    assert embeds[0]["csp"] is None, "news must not inherit the video profile"


def test_react_links_open_safely():
    """target=_blank without noopener hands the opened page a window.opener
    handle back into ours."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "frontend" / "src"
           / "components" / "ToolEmbeds.tsx").read_text()
    block = src.split("function ArticleLinks")[1].split("function ")[0]
    assert 'target="_blank"' in block and 'rel="noopener noreferrer"' in block
