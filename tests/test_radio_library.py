"""The personal station library: kind-discriminated validation, and the
saved-first preference that must not become a ceiling.

Two properties carry most of the risk.

FIRST, `kind` selects the validation path. Applying the TV rules to radio rejects
working stations — a media element loads cross-origin audio with no CORS header
at all, so requiring `Access-Control-Allow-Origin` would refuse most of the
directory. Applying the radio rules to TV would accept a manifest the sandboxed
player cannot actually fetch. Neither failure is visible at add time; both show up
later as "the thing you saved doesn't play".

SECOND, an empty library must leave every one of the five live tools behaving
exactly as before. A library that changes behaviour before you have saved anything
is a regression for everyone who never uses it.
"""

import pytest

from backend.services import media_sources, radio


def _station(name="Test FM", url="https://example.com/s.mp3", **kw):
    base = {"name": name, "url_resolved": url, "url": url,
            "stationuuid": kw.pop("uuid", "u-1"), "countrycode": kw.pop("cc", "AE"),
            "votes": kw.pop("votes", 5), "tags": "news", "codec": "MP3",
            "bitrate": 128, "language": "arabic", "favicon": ""}
    base.update(kw)
    return base


def _saved(name, url="https://saved.example/s.mp3", **kw):
    row = _station(name, url, **kw)
    row["_saved"] = True
    return row


# ── kind discriminates validation ────────────────────────────────────────────

def test_radio_validation_does_not_require_cors(monkeypatch):
    """The rule that would have broken it. hls.js fetches by XHR and needs an
    opaque-origin grant; an <audio> element does not, which was verified in a
    browser before this validator was written."""
    monkeypatch.setattr(radio, "_sounds_like_audio", lambda u: True)
    ok, reason = media_sources.validate_source("https://e.example/s.mp3", "radio")
    assert ok, reason


def test_radio_validation_rejects_http():
    ok, reason = media_sources.validate_source("http://e.example/s.mp3", "radio")
    assert not ok and "https" in reason


def test_radio_validation_rejects_hls():
    ok, reason = media_sources.validate_source("https://e.example/live.m3u8", "radio")
    assert not ok and "m3u8" in reason.lower()


def test_radio_validation_rejects_a_finite_clip(monkeypatch):
    """The 58KB geo-block clip: audio/mpeg, played for 3.4s, then ended."""
    monkeypatch.setattr(radio, "_sounds_like_audio", lambda u: False)
    ok, reason = media_sources.validate_source("https://e.example/s.mp3", "radio")
    assert not ok and "continuous" in reason


def test_an_unknown_kind_gets_the_stricter_check(monkeypatch):
    """Fail toward over-validating: a new kind that forgets to declare itself
    should be refused, not waved through."""
    called = []
    monkeypatch.setattr(media_sources, "validate_hls",
                        lambda u, t=None: (called.append(u), (False, "hls path"))[1])
    ok, _ = media_sources.validate_source("https://e.example/x", "podcast")
    assert not ok and called


def test_batch_validation_uses_the_same_rules_as_add(monkeypatch):
    """A picker validated differently from add offers rows that add then refuses."""
    seen = []
    monkeypatch.setattr(media_sources, "validate_source",
                        lambda u, k="tv", t=None: (seen.append(k), (True, "ok"))[1])
    media_sources.validate_many(["https://a/1.mp3"], kind="radio")
    assert seen == ["radio"]


# ── the saved-first preference ───────────────────────────────────────────────

def test_saved_stations_come_first_and_live_is_appended():
    merged = radio._merge_saved_first(
        [_saved("Mine", "https://mine/1.mp3")],
        radio.playable([_station("Live", "https://live/2.mp3", uuid="2")]))
    assert [s["name"] for s in merged] == ["Mine", "Live"]


def test_the_library_is_a_preference_not_a_ceiling():
    """Live results stay reachable below the saved ones — the point is ordering,
    not restriction."""
    live = radio.playable([_station(f"L{i}", f"https://live/{i}.mp3", uuid=str(i))
                           for i in range(5)])
    merged = radio._merge_saved_first([_saved("Mine")], live)
    assert len(merged) == 6


def test_a_station_saved_and_also_live_appears_once():
    """Deduped by URL, not name: the directory lists the same stream under
    several names."""
    url = "https://same/stream.mp3"
    merged = radio._merge_saved_first(
        [_saved("My Name For It", url)],
        radio.playable([_station("Directory Name", url, uuid="d")]))
    assert len(merged) == 1
    assert merged[0]["name"] == "My Name For It"


def test_saved_stations_are_ranked_not_left_alphabetical():
    """The library returns rows by name; opening on whichever is alphabetically
    first is arbitrary. Same priority table as the live block."""
    merged = radio._merge_saved_first(
        [_saved("Exclusively Pink Floyd", "https://a/1.mp3"),
         _saved("Quran Radio From Sharjah", "https://b/2.mp3")], [])
    assert merged[0]["name"] == "Quran Radio From Sharjah"


def test_an_empty_library_leaves_the_live_pool_untouched():
    live = radio.playable([_station("Live", uuid="1")])
    assert radio._merge_saved_first([], live) == live


# ── saved stations skip the probe ────────────────────────────────────────────

def test_a_saved_station_is_not_re_probed(monkeypatch):
    """Validated at add time. Re-probing would make the default request slower
    the more the user curates — the library should earn speed, not cost it."""
    probed = []
    monkeypatch.setattr(radio, "_sounds_like_audio",
                        lambda u: (probed.append(u), True)[1])
    out = radio._first_playable([_saved("Mine"), _station("Live", uuid="2")])
    assert probed == []
    assert out[0]["name"] == "Mine"


def test_live_stations_are_still_probed(monkeypatch):
    probed = []
    monkeypatch.setattr(radio, "_sounds_like_audio",
                        lambda u: (probed.append(u), True)[1])
    radio._first_playable(radio.playable([_station("Live", uuid="2")]))
    assert len(probed) == 1


# ── the write path ───────────────────────────────────────────────────────────

def test_bulk_add_is_capped(monkeypatch):
    """A select-all must not become an unbounded write in one turn."""
    monkeypatch.setattr(radio, "_api_get", lambda *a, **k: [_station("S")])
    calls = []
    monkeypatch.setattr(media_sources, "add_source",
                        lambda *a, **k: (calls.append(a), (True, "ok"))[1])
    radio.add_radio_stations_bulk("u", [f"S{i}" for i in range(60)])
    assert len(calls) == radio.BULK_ADD_CAP


def test_bulk_add_says_what_it_dropped(monkeypatch):
    monkeypatch.setattr(radio, "_api_get", lambda *a, **k: [_station("S")])
    monkeypatch.setattr(media_sources, "add_source", lambda *a, **k: (True, "ok"))
    msg = radio.add_radio_stations_bulk("u", [f"S{i}" for i in range(30)])
    assert str(radio.BULK_ADD_CAP) in msg and "Capped" in msg


def test_bulk_add_revalidates_rather_than_trusting_the_picker(monkeypatch):
    """The picker's probe may be minutes old and NOTHING enters the library
    unvalidated — add_source runs the full check again."""
    monkeypatch.setattr(radio, "_api_get", lambda *a, **k: [_station("S")])
    kinds = []
    monkeypatch.setattr(media_sources, "add_source",
                        lambda uid, n, u, k, **kw: (kinds.append(k), (True, "ok"))[1])
    radio.add_radio_stations_bulk("u", ["S"])
    assert kinds == ["radio"]


def test_codec_and_bitrate_are_carried_to_the_library(monkeypatch):
    """Without them a saved station renders worse than the live result it came
    from — the one thing a library must not do."""
    monkeypatch.setattr(radio, "_api_get",
                        lambda *a, **k: [_station("S", codec="AAC", bitrate=64)])
    got = {}
    monkeypatch.setattr(media_sources, "add_source",
                        lambda uid, n, u, k, **kw: (got.update(kw), (True, "ok"))[1])
    radio.add_radio_stations_bulk("u", ["S"])
    assert got.get("codec") == "AAC" and got.get("bitrate") == 64


def test_search_marks_its_picker_as_radio(monkeypatch):
    """The picker component is shared with Live TV. Without channel_kind a ticked
    station is added as a TV channel and then fails HLS validation."""
    monkeypatch.setattr(radio, "_api_get", lambda *a, **k: [_station("S")])
    monkeypatch.setattr(media_sources, "list_sources", lambda *a, **k: [])
    monkeypatch.setattr(media_sources, "validate_many",
                        lambda urls, **k: {u: True for u in urls})
    _, _, meta = radio.search_radio_stations("u", name="S")
    assert meta["channel_kind"] == "radio"
    assert meta["channels"]


def test_search_hides_what_is_already_saved(monkeypatch):
    monkeypatch.setattr(radio, "_api_get", lambda *a, **k: [_station("S")])
    monkeypatch.setattr(media_sources, "list_sources",
                        lambda *a, **k: [{"url": "https://example.com/s.mp3"}])
    out = radio.search_radio_stations("u", name="S")
    assert isinstance(out, str) and "already" in out


def test_search_needs_something_to_search_for():
    out = radio.search_radio_stations("u")
    assert isinstance(out, str) and "country" in out


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_two_writes_are_actions_and_the_reads_are_not():
    import backend.guardrails as guardrails
    import backend.tools as tools

    for w in ("add_radio_stations_bulk", "remove_radio_station"):
        assert w in tools.ACTION_TOOLS, w
        assert guardrails.decide(w) == "auto"
        assert w not in tools.VOLATILE_TOOLS, "a write must never be marked stale"
    for r in ("search_radio_stations", "my_radio"):
        assert r in tools.READ_TOOLS and r not in tools.ACTION_TOOLS
    # The directory moves under a search; a saved library does not.
    assert "search_radio_stations" in tools.VOLATILE_TOOLS
    assert "my_radio" not in tools.VOLATILE_TOOLS


def test_the_module_note_distinguishes_cache_from_library():
    """The old note read as flatly contradicted by this feature."""
    import inspect
    doc = inspect.getdoc(radio) or ""
    assert "REJECTED" in doc and "BUILT" in doc
    assert "curated" in doc.lower() and "cache" in doc.lower()
