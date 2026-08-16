"""Arabic radio: the structural filters, and the two card rules that replaced
the original's approach.

The port drops two whole classes of station that the original happily returned,
because neither can play in our sandbox:

  * http:// — mixed content on an HTTPS page, and our CSP is `media-src https:`
  * .m3u8   — needs MSE via hls.js; see radio.HLS_MARKER for the count and how
              to reinstate

and it changes two things about the card itself: station values ride in `data-*`
attributes instead of being interpolated into an inline `onclick`, and the cdnjs
hls.js tag is gone rather than left dead behind a CSP that blocks it.

Network is stubbed throughout — these assert OUR filtering and markup, not Radio
Browser's uptime. The live directory is exercised separately.
"""

import pytest

from backend.services import radio


def _station(name="Test FM", url="https://example.com/stream.mp3", **kw):
    base = {
        "name": name, "url_resolved": url, "url": url,
        "stationuuid": kw.pop("uuid", "u-1"), "countrycode": kw.pop("cc", "AE"),
        "votes": kw.pop("votes", 10), "tags": kw.pop("tags", "news"),
        "codec": "MP3", "bitrate": 128, "language": "arabic", "favicon": "",
    }
    base.update(kw)
    return base


# ── structural filtering ─────────────────────────────────────────────────────

def test_http_stations_are_dropped():
    """40% of the UAE scope. Chromium refuses them outright: 'Media load rejected
    by URL safety check'."""
    pool = radio.playable([
        _station("Secure", "https://ok.example/s.mp3", uuid="a"),
        _station("Insecure", "http://bad.example/s.mp3", uuid="b"),
    ])
    assert [s["name"] for s in pool] == ["Secure"]


def test_hls_stations_are_dropped():
    pool = radio.playable([
        _station("Plain", "https://ok.example/s.mp3", uuid="a"),
        _station("HLS", "https://ok.example/live/index.m3u8", uuid="b"),
    ])
    assert [s["name"] for s in pool] == ["Plain"]


def test_stations_without_a_url_are_dropped():
    assert radio.playable([{"name": "Empty", "url": "", "url_resolved": ""}]) == []


def test_protocol_relative_and_odd_schemes_are_dropped():
    """Anything not explicitly https:// is refused — the check is an allowlist,
    not a blocklist, so a scheme nobody anticipated fails closed."""
    for bad in ("//cdn.example/s.mp3", "rtmp://x/y", "HTTP://up.example/s.mp3", "ftp://x"):
        assert radio.playable([_station("X", bad)]) == [], bad


def test_filtering_runs_before_the_count_the_user_sees():
    """The card and the model both report len(pool). A count that includes
    unplayable stations reads as a promise the player cannot keep."""
    stations = [_station(f"ok{i}", f"https://ok.example/{i}.mp3", uuid=str(i)) for i in range(3)]
    stations += [_station("bad", "http://bad.example/x.mp3", uuid="b")]
    assert len(radio.playable(stations)) == 3


def test_official_uae_stations_rank_first():
    """What makes a bare 'play radio' open Emirates FM rather than whatever
    polled highest this week."""
    pool = radio.playable([
        _station("Random Pop", "https://a.example/1.mp3", uuid="1", votes=9999),
        _station("Emirates FM", "https://b.example/2.mp3", uuid="2", votes=1),
    ])
    assert pool[0]["name"] == "Emirates FM"


# ── country-aware priority ───────────────────────────────────────────────────

def test_an_emirati_place_name_scores_nothing_outside_the_uae():
    """THE inherited bug. The table holds Emirati PLACE names; matching them on
    the bare station name ranked "GD Dubai" — broadcasting from Honduras — above
    Dubai Eye 103.8 in a search for "dubai"."""
    assert radio._priority_score("GD Dubai", "HN") == 0
    assert radio._priority_score("DYNAMIC RADIO DUBAI", "FR") == 0
    assert radio._priority_score("Dubai Eye 103.8", "AE") == 88


def test_the_real_station_outranks_the_foreign_namesake():
    pool = radio.playable([
        _station("GD Dubai", "https://a.example/1.mp3", uuid="1", cc="HN", votes=9999),
        _station("Dubai Eye 103.8", "https://b.example/2.mp3", uuid="2", cc="AE", votes=1),
    ])
    assert [s["name"] for s in pool] == ["Dubai Eye 103.8", "GD Dubai"]


def test_pan_arab_broadcasters_still_match_anywhere():
    """Al Arabiya, MBC and Rotana broadcast from several countries — tying them to
    one would be the same mistake in the opposite direction."""
    for cc in ("AE", "SA", "LB", ""):
        assert radio._priority_score("Al Arabiya", cc) == 70, cc
        assert radio._priority_score("MBC FM", cc) == 65, cc


def test_a_missing_country_code_does_not_promote_uae_entries():
    """Absent metadata must fail toward NOT promoting: not ranking a real station
    is a smaller error than ranking one from the wrong hemisphere."""
    assert radio._priority_score("Dubai Eye 103.8", "") == 0
    assert radio._priority_score("Dubai Eye 103.8", None) == 0


def test_country_matching_is_case_insensitive():
    assert radio._priority_score("Dubai Eye", "ae") == 88
    assert radio._priority_score("Dubai Eye", " AE ") == 88


def test_the_official_badge_is_country_aware_too():
    """The badge reads "رسمية" (official). A Honduran station wearing it is a
    factual claim, not just a ranking quirk."""
    foreign = radio._render(_station("GD Dubai", cc="HN", uuid="x"),
                            [_station("GD Dubai", cc="HN", uuid="x")], "بحث")
    assert "رسمية" not in foreign
    home = radio._render(_station("Noor Dubai", cc="AE", uuid="y"),
                         [_station("Noor Dubai", cc="AE", uuid="y")], "بحث")
    assert "رسمية" in home


# ── the card ─────────────────────────────────────────────────────────────────

def _card(pool):
    return radio._render(pool[0], pool, "اختبار")


def test_no_cdn_script_tag_survives():
    """The original loaded hls.js from cdnjs. Our script-src blocks it, so the
    tag would fail silently and read as a bug. Removed, not left dead."""
    html = _card(radio.playable([_station()]))
    assert "cdnjs" not in html
    assert "hls" not in html.lower()
    assert "<script src=" not in html, "no external script may be referenced"


def test_station_values_are_data_attributes_not_inline_js():
    html = _card(radio.playable([_station("Noor Dubai")]))
    assert "data-url=" in html and "data-name=" in html
    assert "onclick=" not in html, "inline handlers reintroduce the JS-string escape"
    assert "switchStation(" not in html


def test_a_hostile_station_name_cannot_break_out():
    """Station names are third-party text from a public directory."""
    evil = "\"'></div><script>parent.postMessage({type:'iframe:height',height:99999},'*')</script>"
    html = _card(radio.playable([_station(evil)]))
    assert "<script>parent.postMessage" not in html
    assert "&lt;script&gt;" in html


def test_card_is_click_to_play_with_no_autoplay_attempt():
    """Browsers refuse programmatic playback without a gesture. The card must not
    try — a failed autoplay leaves a 'connecting…' state it never leaves."""
    import re

    html = _card(radio.playable([_station()]))
    # The ATTRIBUTE on the element, not the word anywhere — the card's own
    # comments explain why there is no autoplay, and matching text would fail on
    # the explanation rather than on any behaviour.
    audio_tag = re.search(r"<audio[^>]*>", html).group(0)
    assert "autoplay" not in audio_tag, audio_tag
    assert 'preload="none"' in audio_tag or "preload='none'" in audio_tag

    assert "اضغط ▶ للتشغيل" in html, "the idle state must invite the click"
    # play() is only ever reached from a click handler.
    assert "btn.addEventListener('click'" in html
    # and nothing calls start() at load time
    body_js = html[html.index("<script>"):]
    assert "window.addEventListener('load', start" not in body_js
    assert not re.search(r"^\s*start\(\);", body_js, re.M), "start() is called unconditionally"


def test_card_needs_only_the_default_csp():
    """No external origins means the default profile suffices — media-src https:
    already covers the stream, and nothing here fetches."""
    html = _card(radio.playable([_station()]))
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "<iframe"):
        assert forbidden not in html, forbidden


# ── tool surface ─────────────────────────────────────────────────────────────

def test_every_tool_returns_the_embed_pair(monkeypatch):
    monkeypatch.setattr(radio, "_api_get", lambda *a, **k: [_station()])
    for call in (lambda: radio.uae_radio(), lambda: radio.arabic_radio(),
                 lambda: radio.quran_radio(), lambda: radio.radio_by_genre("news"),
                 lambda: radio.search_radio("dubai")):
        resp, context = call()
        assert resp.headers.get("Content-Disposition") == "inline"
        assert "محطة" in context
        assert "<" not in context, "the model must never receive markup"


def test_an_unreachable_directory_degrades_to_a_message(monkeypatch):
    monkeypatch.setattr(radio, "_api_get", lambda *a, **k: [])
    resp, context = radio.uae_radio()
    assert resp.headers.get("Content-Disposition") == "inline"
    assert "تعذر" in context


def test_all_results_unplayable_is_treated_as_empty(monkeypatch):
    """The failure this port exists to prevent: a directory full of http:// URLs
    must produce the 'none found' card, not a player that cannot play."""
    monkeypatch.setattr(radio, "_api_get",
                        lambda *a, **k: [_station("x", "http://bad/1.mp3")])
    resp, context = radio.uae_radio()
    assert "تعذر" in context or "لم يتم العثور" in context


def test_a_bad_country_code_falls_back_to_arabic_language(monkeypatch):
    seen = []
    monkeypatch.setattr(radio, "_api_get",
                        lambda path, params=None, deadline=None: (seen.append(path),
                                                                  [_station()])[1])
    radio.arabic_radio("not-a-code")
    assert seen[0] == "stations/bylanguageexact/arabic"


# ── the overall budget ───────────────────────────────────────────────────────

def test_mirrors_are_abandoned_once_the_budget_is_spent(monkeypatch):
    """Per-request timeouts bound each attempt, not their sum. Three mirrors at
    5s connect + 8s read is ~39s for ONE lookup, and two tools issue two lookups,
    so a slow upstream could hold a turn past a minute — the user sees a reply
    with no card while it is still running."""
    tried = []

    class _Boom:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None):
            tried.append(url)
            raise radio.httpx.ConnectTimeout("slow")

    monkeypatch.setattr(radio.httpx, "Client", lambda **kw: _Boom())
    # An already-expired deadline must stop it before the first mirror.
    assert radio._api_get("stations/x", {}, deadline=radio.time.monotonic() - 1) == []
    assert tried == [], tried


def test_without_a_deadline_every_mirror_is_still_tried(monkeypatch):
    """The budget is a cap, not a new failure mode — callers that pass none keep
    the full fallback chain."""
    tried = []

    class _Boom:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None):
            tried.append(url)
            raise radio.httpx.ConnectTimeout("slow")

    monkeypatch.setattr(radio.httpx, "Client", lambda **kw: _Boom())
    assert radio._api_get("stations/x", {}) == []
    assert len(tried) == len(radio.SERVERS)


def test_probing_stops_when_the_budget_is_spent(monkeypatch):
    """The probe phase is inside the budget too — four stations at 3s each is
    another 12s on top of the lookups."""
    probed = []
    monkeypatch.setattr(radio, "_sounds_like_audio",
                        lambda u: (probed.append(u), False)[1])
    pool = [_station(f"s{i}", f"https://e/{i}.mp3", uuid=str(i)) for i in range(4)]
    out = radio._first_playable(pool, deadline=radio.time.monotonic() - 1)
    assert probed == [], "probed after the budget was spent"
    assert out == pool, "an unprobed pool is still returned, in ranked order"


def test_every_tool_sets_its_own_budget():
    """A deadline created once at module import would expire and never reset."""
    import inspect
    src = inspect.getsource(radio)
    for tool in ("def uae_radio", "def arabic_radio", "def quran_radio",
                 "def radio_by_genre", "def search_radio"):
        body = src[src.index(tool):]
        body = body[:body.index("\n\ndef ")] if "\n\ndef " in body else body
        assert "_new_deadline()" in body, f"{tool} has no budget"


def test_wiring():
    import backend.guardrails as guardrails
    import backend.tools as tools

    for name in ("uae_radio", "arabic_radio", "quran_radio",
                 "radio_by_genre", "search_radio"):
        assert tools.group_of(name) == "radio"
        assert guardrails.decide(name) == "auto"
        assert name in tools.READ_TOOLS and name not in tools.ACTION_TOOLS
        # NOT volatile: the directory and its vote ordering do move, but these
        # tools' replies are pointers to a player rather than data, so they live
        # in WIDGET_TOOLS and mark_stale subtracts them. See the STATUS note on
        # WIDGET_TOOLS in backend/tools.py — the repeat-request bug is open.
        assert name in tools.WIDGET_TOOLS
        assert name not in tools.VOLATILE_TOOLS


def test_the_hls_exclusion_is_documented_where_it_is_made():
    """The decision was taken against a corrected count (47, not 2). Whoever
    revisits it must meet that number, not the estimate."""
    import inspect
    src = inspect.getsource(radio)
    assert "47" in src, "the measured exclusion count must be recorded"
    assert "video" in src, "the route back (the video CSP profile) must be named"


# ── generic queries are not station names ────────────────────────────────────

def test_a_query_of_pure_filler_is_not_searched_literally():
    """"find me radio channel" is the user saying "radio", not naming a station.

    Radio Browser's byname is a substring match, so searching it literally
    returned NTS Radio Channel 1 and Stegi Radio Channel 2 — Greek stations that
    happened to contain the word. That reads as a broken search.
    """
    assert radio._meaningful("radio channel") == ""
    assert radio._meaningful("find me a radio station") == ""
    assert radio._meaningful("إذاعة") == ""


def test_filler_is_stripped_but_real_words_survive():
    assert radio._meaningful("dubai radio channel") == "dubai"
    assert radio._meaningful("dubai eye") == "dubai eye"


def test_a_name_containing_filler_is_still_searched_whole(monkeypatch):
    """THE regression guard. "Radio Mirchi Dubai" is a real station name and the
    word "radio" is part of it. byname is a substring match, so searching the
    stripped "mirchi dubai" would find nothing — the full string must be tried
    FIRST and the stripped form only as a fallback."""
    seen = []

    def fake(path, params=None, deadline=None):
        seen.append((params or {}).get("name"))
        return [_station("Radio Mirchi Dubai")]

    monkeypatch.setattr(radio, "_api_get", fake)
    monkeypatch.setattr(radio, "_sounds_like_audio", lambda u: True)
    radio.search_radio("Radio Mirchi Dubai")
    assert seen[0] == "Radio Mirchi Dubai", "the full name must be tried first"
    assert "mirchi dubai" not in seen, "no fallback should fire when the first hit works"


def test_a_failed_literal_search_retries_on_the_meaningful_part(monkeypatch):
    """"dubai radio channel" matched nothing and the user was told the station
    does not exist — for a request we serve perfectly."""
    seen = []

    def fake(path, params=None, deadline=None):
        name = (params or {}).get("name")
        seen.append(name)
        return [_station("DJ Radio Dubai")] if name == "dubai" else []

    monkeypatch.setattr(radio, "_api_get", fake)
    monkeypatch.setattr(radio, "_sounds_like_audio", lambda u: True)
    radio.search_radio("dubai radio channel")
    assert seen == ["dubai radio channel", "dubai"]


def test_an_all_filler_play_request_falls_back_to_the_default_scope(monkeypatch):
    """They asked for "radio". That is uae_radio's job, not a name search."""
    seen = []

    def fake(path, params=None, deadline=None):
        seen.append(path)
        return [_station("Emirates FM")]

    monkeypatch.setattr(radio, "_api_get", fake)
    monkeypatch.setattr(radio, "_sounds_like_audio", lambda u: True)
    radio.search_radio("radio")
    assert seen == ["stations/bycountrycodeexact/AE"]
    assert not any("search" in p for p in seen), "no literal name search should happen"


def test_an_all_filler_add_request_defaults_to_the_uae(monkeypatch):
    seen = []

    def fake(path, params=None, deadline=None):
        seen.append(path)
        return [_station("Emirates FM")]

    from backend.services import media_sources
    monkeypatch.setattr(radio, "_api_get", fake)
    monkeypatch.setattr(media_sources, "list_sources", lambda *a, **k: [])
    monkeypatch.setattr(media_sources, "validate_many", lambda urls, **k: {u: True for u in urls})
    radio.search_radio_stations("u", name="radio channel")
    assert seen == ["stations/bycountrycodeexact/AE"]


# ── notice cards report their height ─────────────────────────────────────────

def test_every_notice_card_reports_its_height():
    """_fail returned a bare <div> with no script, so the frame never posted a
    height, the host left it at its 40px minimum, and the message rendered as a
    clipped strip with a scrollbar."""
    for html in (radio._fail("تعذر")[0].body.decode(),
                 radio._empty_state("nothing yet")[0].body.decode(),
                 radio._card_shell("<p>x</p>")):
        assert "iframe:height" in html
        assert "ResizeObserver" in html, "load alone measures before layout settles"


def test_an_empty_library_is_not_styled_as_an_error():
    """Red on white is this app's palette for things that went wrong. Nothing had:
    the user simply had not saved a station yet."""
    import re as _re
    html = radio._empty_state("No saved stations yet.")[0].body.decode()
    assert _re.search(r"<div class='notice info'", html)
    assert "notice warn" not in _re.search(r"<body>.*?</body>", html, _re.S).group(0)


def test_a_real_failure_still_looks_like_one():
    import re as _re
    html = radio._fail("تعذر جلب الإذاعات")[0].body.decode()
    assert _re.search(r"<div class='notice warn'", html)
