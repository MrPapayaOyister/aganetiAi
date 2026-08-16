"""web_search backed by the self-hosted SearXNG instance.

The network hop is stubbed so the suite stays offline and deterministic; the
JSON shape used here is the real one this deployment returns (verified against
http://localhost:5555/search?...&format=json).
"""

import pytest

import backend.tools as tools
from backend.services import websearch


class _Resp:
    def __init__(self, status=200, payload=None, text="{}"):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


SEARX_JSON = {
    "query": "python tutorial",
    "results": [
        {"title": "Python Tutorial - W3Schools", "url": "https://www.w3schools.com/python/",
         "content": "Well organized and easy to understand tutorials.", "engine": "bing"},
        {"title": "Welcome to Python.org", "url": "https://www.python.org/",
         "content": "The official home of Python.", "engine": "duckduckgo"},
    ],
    "unresponsive_engines": [["brave", "Suspended: too many requests"]],
}


def _stub(monkeypatch, **kw):
    import httpx
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"], captured["params"] = url, params
        if kw.get("exc"):
            raise kw["exc"]
        return _Resp(kw.get("status", 200), kw.get("payload", SEARX_JSON))

    monkeypatch.setattr(httpx, "get", fake_get)
    return captured


# ── the service ──────────────────────────────────────────────────────────────

def test_calls_the_configured_instance_with_json_format(monkeypatch):
    cap = _stub(monkeypatch)
    websearch.search("python tutorial", 5)
    assert cap["url"].endswith("/search")
    assert cap["url"].startswith(websearch.SEARXNG_URL)
    assert cap["params"]["format"] == "json", "the JSON API is the whole point"
    assert cap["params"]["q"] == "python tutorial"


def test_normalises_hits_and_honours_max_results(monkeypatch):
    _stub(monkeypatch)
    hits = websearch.search("x", 1)
    assert len(hits) == 1
    assert hits[0] == {"title": "Python Tutorial - W3Schools",
                       "url": "https://www.w3schools.com/python/",
                       "content": "Well organized and easy to understand tutorials.",
                       "engine": "bing"}


def test_unresponsive_engines_are_not_an_error(monkeypatch):
    """SearXNG serves whatever the remaining engines returned; a suspended engine
    must degrade the result set, not fail the search."""
    _stub(monkeypatch)
    assert len(websearch.search("x", 5)) == 2


def test_instance_down_raises_search_unavailable(monkeypatch):
    import httpx
    _stub(monkeypatch, exc=httpx.ConnectError("refused"))
    with pytest.raises(websearch.SearchUnavailable):
        websearch.search("x", 3)


def test_non_200_raises_search_unavailable(monkeypatch):
    """403 is the signature of `json` missing from search.formats in settings.yml."""
    _stub(monkeypatch, status=403)
    with pytest.raises(websearch.SearchUnavailable):
        websearch.search("x", 3)


def test_non_json_body_raises_search_unavailable(monkeypatch):
    _stub(monkeypatch, payload=None)
    with pytest.raises(websearch.SearchUnavailable):
        websearch.search("x", 3)


def test_empty_query_short_circuits(monkeypatch):
    cap = _stub(monkeypatch)
    assert websearch.search("   ", 5) == []
    assert cap == {}, "an empty query must not hit the instance"


# ── the tool ─────────────────────────────────────────────────────────────────

def test_web_search_output_format_is_unchanged(monkeypatch):
    """Downstream consumers read this string; the shape must not drift."""
    _stub(monkeypatch)
    out = tools.dispatch_tool_call("web_search", {"query": "python tutorial"}, "u")
    assert out.startswith("🌐 Web results for *python tutorial*:")
    assert "**Python Tutorial - W3Schools**" in out
    assert "🔗 https://www.w3schools.com/python/" in out


def test_web_search_degrades_cleanly_when_instance_is_down(monkeypatch):
    import httpx
    _stub(monkeypatch, exc=httpx.ConnectError("refused"))
    out = tools.dispatch_tool_call("web_search", {"query": "x"}, "u")
    assert out.startswith("⚠️") and "unavailable" in out
    assert "🔗" not in out, "a result here would mean a fallback backend fired silently"


def test_web_search_has_no_scraper_fallback():
    """A fallback that silently swaps the backend hides an outage: results keep
    arriving, ranked by something else, and nobody learns the instance is down."""
    import inspect
    src = inspect.getsource(tools.dispatch_tool_call)
    block = src.split('if name == "web_search"')[1].split('if name == "schedule_meeting"')[0]
    assert "DDGS" not in block and "ddgs" not in block


def test_no_results_is_not_an_error(monkeypatch):
    _stub(monkeypatch, payload={"results": [], "unresponsive_engines": []})
    out = tools.dispatch_tool_call("web_search", {"query": "zzzz"}, "u")
    assert out == "No web results found for: zzzz"
