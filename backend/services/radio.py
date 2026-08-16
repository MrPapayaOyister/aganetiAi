"""Arabic / UAE radio — live streams from the Radio Browser directory.

Ported from FNC Digital Transformation's `arabic_radio` v4.0.0-fnc
(reference/tool-arabic_radio-export-*.json). The station sourcing, the UAE
priority table, the Arabic RTL card and the five tool carve-outs are theirs.

NO CACHED DIRECTORY — BUT A CURATED LIBRARY, YES
------------------------------------------------
Two different things, and only one of them is rejected.

REJECTED: caching Radio Browser locally, the way media_directory caches iptv-org.
That cache exists because IPTV playlists rot — about half of any sample is dead —
so a validated local copy beats the upstream. Radio Browser is already the
maintained directory: it has `hidebroken`, vote ordering and three mirrors. A
copy of it here would only add staleness. Station discovery therefore stays live,
per request, and a station that dies upstream is fixed upstream.

BUILT: a personal station library, in `media_sources` with `kind='radio'`. That
is not a copy of the directory, it is the user's own shortlist — the handful of
stations they actually listen to, validated once at add time and thereafter
theirs. It answers a question the live query cannot: "my stations", as opposed to
"whatever the directory ranks highest today".

The two compose rather than compete. `uae_radio`, the default for a bare "play
radio", opens on the saved list with live UAE results appended below it, so the
library is a preference and never a ceiling. The other four tools — arabic_radio,
quran_radio, radio_by_genre, search_radio — stay purely live, because each of
them is a request to look OUTSIDE what you already have. With an empty library
every one of the five behaves exactly as it did before the library existed.

ADAPTATIONS
-----------
* SYNC, not async. Their v4 rewrote the original's blocking `requests.get` into
  httpx.AsyncClient specifically because Open WebUI calls sync tool methods
  directly on the event loop, where a stalled mirror froze every user for up to
  45s. That hazard does not exist here: both dispatch sites wrap execution in
  `asyncio.to_thread` (backend/main.py), so a blocking call occupies a worker
  thread and never the loop. Sync httpx keeps the loop bridge out of it entirely.
  The timeouts are kept anyway — a worker thread is cheap, not free.

* STRUCTURAL FILTERING the original does not do. Its `_rank` accepts any station
  that merely HAS a url. Two whole classes cannot work in our frame:

    - `http://` streams. The app is served over HTTPS and our embed CSP is
      `media-src https:`, so these are refused twice over. Chromium reports
      "Media load rejected by URL safety check". They are 40% of the UAE scope.
    - `.m3u8` (HLS). A bare <audio> cannot play HLS in any browser but Safari;
      it needs MSE via hls.js. See the note on HLS_EXCLUDED below.

* CLICK-TO-PLAY ONLY. Browsers refuse programmatic playback without a user
  gesture (verified: `NotAllowedError: play() failed because the user didn't
  interact with the document first`). The original attempts autoplay and falls
  back to a "press ▶" message; here the button IS the only path, so the card
  never shows a connecting state it cannot leave.

* NO hls.js SCRIPT TAG. Theirs loads it from cdnjs, which our
  `script-src 'unsafe-inline'` blocks outright — the tag would fail silently and
  read as a bug to the next person who opens the card. Removed along with the
  HLS branch it fed, rather than left dead.

* DATA ATTRIBUTES, NOT INTERPOLATED JS. Their station rows carry
  `onclick="switchStation('{url}','{name}',…)"`. `html.escape(quote=True)` does
  hold that today, but station names are third-party text and needing an escape
  to hold inside a JS string literal inside an HTML attribute is a thinner margin
  than the job requires. Values ride in `data-` attributes and one delegated
  listener reads them, so the text never becomes code.
"""

from __future__ import annotations

import html as _html
import logging
import re
import time
from typing import Any

import httpx
from fastapi.responses import HTMLResponse

log = logging.getLogger("aria.radio")

# Three mirrors, as the original. Sync client, one short timeout each, and an
# overall cap so a bad mirror costs a worker thread briefly and no more.
SERVERS = (
    "https://de1.api.radio-browser.info/json",
    "https://nl1.api.radio-browser.info/json",
    "https://at1.api.radio-browser.info/json",
)
TIMEOUT = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=3.0)
HEADERS = {"User-Agent": "AganetiAI-Radio/1.0", "Accept": "application/json"}

# Overall budget for one tool call, across mirrors AND liveness probes. The
# original had this and the first port dropped it, keeping only the per-request
# timeouts — which bounds each attempt but not their sum. Three mirrors at
# 5s connect + 8s read is ~39s for a single lookup, and quran_radio and
# radio_by_genre issue two lookups, so a slow upstream could hold a turn for over
# a minute and the user would see a reply with no card while it was still going.
TOTAL_BUDGET_SECONDS = 20.0


def _new_deadline() -> float:
    return time.monotonic() + TOTAL_BUDGET_SECONDS


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline

MAX_STATIONS = 80
INLINE = {"Content-Disposition": "inline"}

# WHY .m3u8 IS EXCLUDED — REVISIT HERE.
#
# Playing HLS needs Media Source Extensions driven by hls.js. live_tv already
# vendors hls.js and has a `video` CSP profile that permits the manifest and
# segment fetches (`connect-src https:`); an audio card would need the same
# profile, which means granting radio the network reach of a video player.
#
# The cost of excluding, measured live against Radio Browser rather than
# estimated: 2 stations in the UAE scope, 31 in Arabic-language, 4 in Quran,
# 14 in the news tag — 47 UNIQUE stations across those scopes. An earlier
# estimate of "2" came from sampling the UAE scope alone and was wrong by a
# factor of twenty; the decision to exclude was taken again against 47 and stood.
#
# The sharpest consequence, and the reason to revisit rather than forget:
# "play Abu Dhabi FM" and "play Emarat FM" now answer NOT FOUND. Radio Browser
# returns exactly one match for each, both HLS, so search_radio filters them to
# nothing. Those are the UAE's flagship national broadcasters, and a user who
# names one gets told it does not exist. That is the price of not granting an
# audio card the network reach of a video player — worth paying today, worth
# re-examining the moment anyone asks for either station by name.
#
# To reinstate: drop this filter, set the embed's csp profile to "video", and
# restore the HLS branch in the player (hls.js is already vendored at
# frontend/public/vendor/hls.min.js — do NOT reintroduce the cdnjs tag, our CSP
# blocks it).
HLS_MARKER = ".m3u8"

# Matched against station names to float official broadcasters up — the part that
# makes "play radio" open an Emirati station rather than whatever polls highest
# this week.
#
# COUNTRY-AWARE, which the original was not. Theirs matched the bare name, so any
# station with "dubai" in its title scored 88 wherever on earth it broadcast from:
# a search for "dubai" ranked "GD Dubai" — a HONDURAN station — above Dubai Eye
# 103.8. The place names below are Emirati place names, and a station is only
# Emirati if the directory says it is, so each entry carries the country it
# belongs to and scores nothing outside it.
#
# `None` means pan-Arab: Al Arabiya, MBC and Rotana broadcast from several
# countries and are recognisable in all of them, so they keep matching by name.
PRIORITY_KEYWORDS: list[tuple[str, int, str | None]] = [
    ("emirates fm", 100, "AE"), ("الإمارات", 100, "AE"), ("الامارات", 100, "AE"),
    ("abu dhabi", 95, "AE"), ("أبوظبي", 95, "AE"), ("ابوظبي", 95, "AE"),
    ("abudhabi", 95, "AE"),
    ("quran kareem", 92, "AE"), ("القرآن الكريم", 92, "AE"), ("القران الكريم", 92, "AE"),
    ("noor dubai", 90, "AE"), ("نور دبي", 90, "AE"), ("dubai", 88, "AE"), ("دبي", 88, "AE"),
    ("sharjah", 85, "AE"), ("الشارقة", 85, "AE"), ("ajman", 82, "AE"), ("عجمان", 82, "AE"),
    ("ras al khaimah", 80, "AE"), ("رأس الخيمة", 80, "AE"),
    ("fujairah", 80, "AE"), ("الفجيرة", 80, "AE"),
    ("umm al quwain", 78, "AE"), ("أم القيوين", 78, "AE"),
    ("al arabiya", 70, None), ("العربية", 70, None), ("mbc", 65, None),
    ("rotana", 60, None), ("روتانا", 60, None),
]


def _esc(s: Any) -> str:
    """Everything from the API is third-party text and is escaped before the DOM."""
    return _html.escape(str(s or ""), quote=True)


def _priority_score(name: str, countrycode: str = "") -> int:
    """Editorial rank for a station, 0 if it is not one we promote.

    `countrycode` is REQUIRED in practice even though it defaults to "": an entry
    tied to a country scores only for stations the directory places there. The
    default exists so a caller holding just a name gets pan-Arab matching rather
    than a TypeError, and it deliberately scores nothing for the AE entries —
    failing to promote is a much smaller error than promoting a station from the
    wrong hemisphere.
    """
    n = (name or "").lower()
    cc = (countrycode or "").upper().strip()
    return max((score for kw, score, want_cc in PRIORITY_KEYWORDS
                if kw in n and (want_cc is None or want_cc == cc)), default=0)


def _station_priority(s: dict) -> int:
    return _priority_score(s.get("name", ""), s.get("countrycode", ""))


def _stream_url(s: dict) -> str:
    return s.get("url_resolved") or s.get("url") or ""


def _api_get(path: str, params: dict | None = None, deadline: float | None = None) -> list:
    """Query Radio Browser across mirrors. Never raises — an empty list becomes
    the "couldn't reach the directory" card, which is a better answer than a
    traceback in the chat.

    Stops trying mirrors once the caller's budget is spent, so a fallback chain
    cannot outlive the turn it belongs to."""
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        for server in SERVERS:
            if _expired(deadline):
                log.warning("radio: budget spent before trying %s", server)
                break
            try:
                r = client.get(f"{server}/{path}", params=params or {})
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        return data
            except (httpx.TimeoutException, httpx.RequestError, ValueError) as e:
                log.debug("radio mirror %s failed: %s", server, e)
                continue
    log.warning("all radio mirrors failed for %s", path)
    return []


def playable(stations: list) -> list:
    """Drop what cannot play in our sandbox, then rank.

    This is the structural filter. It runs BEFORE ranking so the counts the card
    and the model report are counts of stations that will actually play — a
    number that includes unplayable entries is worse than no number, because the
    user reads it as a promise.
    """
    out = []
    for s in stations:
        url = _stream_url(s)
        if not url:
            continue
        if not url.startswith("https://"):
            continue                      # mixed content + media-src https:
        if HLS_MARKER in url:
            continue                      # needs MSE; see HLS_MARKER above
        out.append(s)
    out.sort(key=lambda s: (-_station_priority(s), -(s.get("votes") or 0)))
    return out[:MAX_STATIONS]


AUDIO_PROBE_LIMIT = 4
AUDIO_PROBE_TIMEOUT = httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=2.0)

# The probe must ask the way the <audio> element asks. With our API headers
# (`Accept: application/json`, a bot User-Agent) fastcast4u served audio/mpeg,
# while the browser — sending a browser UA and `Accept: */*` — got an HTML
# holding page. The probe passed and playback still failed. Content negotiation
# means a liveness check is only meaningful when it mirrors the real client.
AUDIO_PROBE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "*/*",
}


def _favicon(raw: str) -> str:
    """A favicon we are willing to put in an <img>, or "".

    Radio Browser returns the literal STRING "null" for some stations. It is
    truthy, so `<img src="null">` reached the DOM, resolved against the app
    origin and was refused by `img-src https: data:` — three CSP violations in
    the console for a card that otherwise had none. http:// icons would be
    blocked the same way. Only https survives.
    """
    f = (raw or "").strip()
    return f if f.startswith("https://") else ""


def _sounds_like_audio(url: str) -> bool:
    """Cheap liveness check: does this URL actually serve audio right now?

    `hidebroken=true` and `lastcheckok` are not enough. The top-ranked UAE
    station resolves to an HTML holding page — HTTP 200, `text/html`, a
    `<!DOCTYPE html>` body — so the card opened on a station that could never
    play and the user's first click failed. This is the same rot that made the
    IPTV directory need validation, arriving through a different door.

    Only the OPENING station is probed (see _player), so a card costs at most a
    few short requests, in a worker thread, and never blocks the event loop.
    """
    try:
        with httpx.Client(timeout=AUDIO_PROBE_TIMEOUT, headers=AUDIO_PROBE_HEADERS,
                          follow_redirects=True) as client:
            with client.stream("GET", url) as r:
                if r.status_code not in (200, 206):
                    return False
                ctype = (r.headers.get("Content-Type") or "").lower()
                # Icecast/Shoutcast answer audio/mpeg, audio/aacp, application/ogg.
                # Anything text/* is a holding page or an error document.
                if not (ctype.startswith("audio/") or "ogg" in ctype or "mpeg" in ctype):
                    return False
                # A BROADCAST is continuous: chunked, no Content-Length. A finite
                # length means a file, and in practice a short one — the top-ranked
                # UAE station served audio/mpeg with Content-Length 58193, which is
                # ~3.4 seconds at 128kbps. It played, ended, and left the card
                # showing "on air" over silence. Content-type alone cannot tell a
                # station from a geo-block clip; the absence of a length can.
                clen = r.headers.get("Content-Length")
                if clen and clen.isdigit() and int(clen) < 1_000_000:
                    log.debug("radio probe: %s is a %s-byte clip, not a stream", url, clen)
                    return False
                return True
    except (httpx.TimeoutException, httpx.RequestError):
        return False


def _first_playable(pool: list, deadline: float | None = None) -> list:
    """Reorder so the card OPENS on a station that actually streams.

    Returns the pool with a verified station first, or unchanged if none of the
    probed candidates answer — a card that opens on a doubtful station still
    beats no card, and every other station stays one click away.
    """
    # Deliberately NOT sorted by codec. Preferring MP3 was tried — headless
    # Chromium has no AAC decoder, so AAC stations look broken here — and it made
    # the product worse: the only MP3 in the UAE window is a dead holding page,
    # and the next MP3s score 0 on the priority table, so "play radio" would have
    # opened on Exclusively Pink Floyd instead of Dubai Eye 103.8. The codec gap
    # is an artefact of the test browser, not of any real one; ranking follows the
    # priority table, and liveness is the only thing probed.
    for i, s in enumerate(pool[:AUDIO_PROBE_LIMIT]):
        # A SAVED station was validated by validate_radio when the user added it.
        # Re-probing it would make the default request slower the more someone
        # curates, which is backwards — the library should earn speed, not cost it.
        if s.get("_saved"):
            return [s] + [x for x in pool if x is not s]
        if _expired(deadline):
            log.info("radio: budget spent after %d probe(s); opening unverified", i)
            break
        if _sounds_like_audio(_stream_url(s)):
            if i:
                log.info("radio: skipped %d unplayable station(s) before %r",
                         i, s.get("name"))
            return [s] + [x for x in pool if x is not s]
    log.warning("radio: no probed station answered with audio; opening on %r",
                pool[0].get("name") if pool else None)
    return pool


# Words that describe the MEDIUM rather than name a station. A query made only of
# these is not a search term, it is the user saying "radio" — and Radio Browser's
# byname endpoint is a substring match, so it answers with whatever happens to
# contain the word. "radio channel" returned NTS Radio Channel 1 and Stegi Radio
# Channel 2 (Greece); "dubai radio channel" returned nothing at all, because no
# station is literally called that.
_FILLER = {"radio", "radios", "channel", "channels", "station", "stations",
           "fm", "am", "live", "stream", "streaming", "online", "music",
           "play", "open", "find", "me", "some", "a", "an", "the", "please",
           "إذاعة", "إذاعات", "راديو", "محطة", "محطات", "قناة"}


def _meaningful(query: str) -> str:
    """The part of a query that could actually name a station, or "".

    Returns "" when every word is filler, which is the caller's signal to stop
    treating the input as a name and fall back to the default scope rather than
    substring-matching the word "radio" across the world.
    """
    words = [w for w in re.split(r"[\s,._-]+", (query or "").lower()) if w]
    kept = [w for w in words if w not in _FILLER]
    return " ".join(kept).strip()


def _flag(code: str) -> str:
    code = (code or "").upper().strip()
    if len(code) != 2 or not code.isalpha():
        return ""
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)


def _station_rows(pool: list, active_uuid: str) -> str:
    rows = []
    for s in pool:
        name = _esc(s.get("name") or "غير معروف")
        url = _esc(_stream_url(s))
        fav = _esc(_favicon(s.get("favicon")))
        flag = _flag(s.get("countrycode") or "")
        uuid = _esc(s.get("stationuuid") or "")
        tags = s.get("tags") or ""
        genre = _esc(next((t.strip().title() for t in tags.split(",") if t.strip()), "إذاعة"))
        active = " s-active" if s.get("stationuuid") == active_uuid else ""
        official = ("<span class='s-badge'>رسمية</span>"
                    if _station_priority(s) >= 85 else "")
        icon = (f"<img src=\"{fav}\" class='s-fav' alt=''>" if fav
                else "<span class='s-fav s-fav-ph'>📻</span>")
        # Values live in data-* and are read by the delegated listener below.
        # Nothing here is interpolated into executable JS.
        rows.append(
            f"<div class='s-item{active}' data-uuid='{uuid}' data-url='{url}' "
            f"data-name='{name}' data-fav='{fav}' data-flag='{_esc(flag)}'>"
            f"{icon}<div class='s-info'><div class='s-name'>{_esc(flag)} {name} {official}</div>"
            f"<div class='s-meta'>{genre}</div></div><div class='s-arrow'>▶</div></div>"
        )
    return "".join(rows)


def _render(station: dict, pool: list, heading: str = "") -> str:
    name = _esc(station.get("name") or "غير معروف")
    stream = _esc(_stream_url(station))
    favicon = _esc(_favicon(station.get("favicon")))
    cc = (station.get("countrycode") or "").upper().strip()
    tags = station.get("tags") or ""
    bitrate = station.get("bitrate") or 0
    codec = _esc(station.get("codec") or "")
    language = _esc((station.get("language") or "").title())
    votes = station.get("votes") or 0
    uuid = station.get("stationuuid") or ""
    flag = _esc(_flag(cc))
    genres = [t.strip().title() for t in tags.split(",") if t.strip()][:3]
    genre_str = _esc(" · ".join(genres) if genres else "إذاعة")

    badges = ""
    if bitrate:
        badges += f"<span class='badge badge-green'>{int(bitrate)}kbps {codec}</span>"
    elif codec:
        badges += f"<span class='badge badge-green'>{codec}</span>"
    if language:
        badges += f"<span class='badge badge-blue'>{language}</span>"
    if votes:
        badges += f"<span class='badge badge-amber'>♥ {int(votes):,}</span>"

    fav_html = (f"<img id='r-fav' src='{favicon}' class='main-fav' alt=''>" if favicon else "")
    bars = "".join(
        f"<div class='wb' style='height:{h}px;animation-delay:{i * 0.06:.2f}s'></div>"
        for i, h in enumerate([10, 22, 16, 30, 8, 26, 18, 34, 12, 28, 20, 14,
                               32, 8, 24, 16, 30, 10, 26, 18, 34, 12, 22, 8, 28, 14, 20, 8])
    )

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:transparent;font-family:'Segoe UI','Noto Sans Arabic',Tahoma,
  -apple-system,sans-serif}}
:root{{--bg1:#0b0e14;--bg2:#1e293b;--line:rgba(255,255,255,.06);--text:#e5e7eb;
  --text2:#9ca3af;--text3:#6b7280;--accent:#c8a44d;--green:#34d399;--amber:#fbbf24;
  --red:#f87171;--blue:#60a5fa}}
.card{{max-width:640px;margin:0 auto;background:linear-gradient(160deg,var(--bg1),var(--bg2));
  border:1px solid var(--line);border-radius:16px;overflow:hidden;color:var(--text)}}
.head{{padding:14px 16px;border-bottom:1px solid var(--line);font-size:13px;color:var(--text2)}}
.now{{display:flex;gap:12px;align-items:center;padding:16px}}
.main-fav,.ico{{width:56px;height:56px;border-radius:12px;object-fit:cover;flex:0 0 auto}}
.ico{{display:flex;align-items:center;justify-content:center;font-size:26px;
  background:rgba(255,255,255,.05)}}
.meta{{min-width:0;flex:1}}
.r-name{{font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}}
.r-genre{{font-size:12px;color:var(--text2);margin-top:2px}}
.wave{{display:flex;align-items:flex-end;gap:2px;height:36px;padding:0 16px}}
.wb{{width:3px;background:var(--accent);opacity:.25;border-radius:2px}}
.wb.on{{opacity:1;animation:p .9s ease-in-out infinite alternate}}
@keyframes p{{from{{transform:scaleY(.35)}}to{{transform:scaleY(1)}}}}
.controls{{display:flex;gap:12px;align-items:center;padding:12px 16px}}
.play-btn{{width:46px;height:46px;border-radius:50%;border:0;cursor:pointer;
  background:var(--accent);color:#111;font-size:17px}}
.vol-wrap{{flex:1}}
.vol-bar{{width:100%}}
.status{{font-size:12px;color:var(--text3);text-align:center;margin-top:2px}}
.badges{{display:flex;gap:6px;flex-wrap:wrap;padding:0 16px 12px}}
.badge{{font-size:11px;padding:2px 8px;border-radius:999px}}
.badge-green{{background:rgba(52,211,153,.12);color:var(--green)}}
.badge-blue{{background:rgba(96,165,250,.12);color:var(--blue)}}
.badge-amber{{background:rgba(251,191,36,.12);color:var(--amber)}}
.search-bar{{display:flex;gap:8px;align-items:center;padding:10px 16px;
  border-top:1px solid var(--line)}}
.search-bar input{{flex:1;background:rgba(255,255,255,.04);border:1px solid var(--line);
  border-radius:8px;color:var(--text);padding:7px 10px;font-size:13px}}
.count{{font-size:11px;color:var(--text3)}}
.stations{{max-height:260px;overflow-y:auto}}
.s-item{{display:flex;gap:10px;align-items:center;padding:9px 16px;cursor:pointer;
  border-top:1px solid var(--line)}}
.s-item:hover{{background:rgba(255,255,255,.04)}}
.s-item.s-active{{background:rgba(200,164,77,.10)}}
.s-fav{{width:30px;height:30px;border-radius:7px;object-fit:cover;flex:0 0 auto}}
.s-fav-ph{{display:flex;align-items:center;justify-content:center;
  background:rgba(255,255,255,.05);font-size:15px}}
.s-info{{min-width:0;flex:1}}
.s-name{{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.s-meta{{font-size:11px;color:var(--text3)}}
.s-badge{{font-size:10px;color:var(--accent);border:1px solid var(--accent);
  border-radius:4px;padding:0 4px;margin-inline-start:4px}}
.s-arrow{{color:var(--text3);font-size:11px}}
</style>
</head>
<body>
<div class="card">
  <div class="head">{_esc(heading)}</div>
  <div class="now">
    {fav_html}<div class="ico" id="r-ico" {'style="display:none"' if favicon else ''}>📻</div>
    <div class="meta">
      <div class="r-name" id="r-name">{flag} {name}</div>
      <div class="r-genre" id="r-genre">{genre_str}</div>
    </div>
  </div>
  <div class="wave">{bars}</div>
  <audio id="r-audio" preload="none"></audio>
  <div class="controls">
    <button id="r-btn" class="play-btn" type="button" aria-label="تشغيل">▶</button>
    <div class="vol-wrap">
      <input type="range" id="r-vol" class="vol-bar" min="0" max="1" step="0.01" value="0.8">
      <div id="r-status" class="status">اضغط ▶ للتشغيل</div>
    </div>
  </div>
  <div class="badges">{badges}</div>
  <div class="search-bar">
    <span>🔍</span>
    <input type="text" id="s-search" placeholder="ابحث في المحطات…">
    <span class="count" id="s-count">{len(pool)} محطة</span>
  </div>
  <div class="stations" id="s-list">{_station_rows(pool, uuid)}</div>
</div>
<script>
(function(){{
  var audio=document.getElementById('r-audio'), btn=document.getElementById('r-btn'),
      stat=document.getElementById('r-status'), vol=document.getElementById('r-vol'),
      bars=document.querySelectorAll('.wb'), playing=false, url={stream!r};

  function wave(on){{ for(var i=0;i<bars.length;i++){{
    on?bars[i].classList.add('on'):bars[i].classList.remove('on'); }} }}
  function say(t,c){{ stat.textContent=t; stat.style.color=c||'var(--text3)'; }}
  function setPlaying(v){{
    playing=v; btn.textContent=v?'⏸':'▶';
    v?say('● على الهواء','var(--accent)'):say('اضغط ▶ للتشغيل'); wave(v);
  }}
  function stop(){{
    audio.pause(); audio.removeAttribute('src'); audio.load(); setPlaying(false);
  }}
  // CLICK-TO-PLAY ONLY. Every call to play() below descends from a real click,
  // which is the gesture browsers require; there is no autoplay attempt to fail.
  function start(){{
    audio.src=url; audio.load(); audio.volume=parseFloat(vol.value);
    say('جارٍ الاتصال…','var(--text2)');
    audio.play().then(function(){{ setPlaying(true); }})
      .catch(function(){{ stop(); say('تعذر تشغيل البث','var(--red)'); }});
  }}
  audio.addEventListener('error', function(){{
    stop(); say('تعذر تشغيل البث','var(--red)');
  }});

  btn.addEventListener('click', function(){{ playing?stop():start(); }});
  vol.addEventListener('input', function(){{ audio.volume=parseFloat(vol.value); }});

  // One delegated listener. Station values are READ from data-* attributes —
  // they are never interpolated into this script.
  document.getElementById('s-list').addEventListener('click', function(ev){{
    var row=ev.target.closest('.s-item'); if(!row) return;
    var was=playing; if(playing) stop();
    url=row.getAttribute('data-url');
    document.getElementById('r-name').textContent=
      (row.getAttribute('data-flag')||'')+' '+(row.getAttribute('data-name')||'');
    var items=document.querySelectorAll('.s-item');
    for(var i=0;i<items.length;i++) items[i].classList.remove('s-active');
    row.classList.add('s-active');
    // Switching while playing continues playing: the click IS the gesture.
    if(was) start(); else say('اضغط ▶ للتشغيل');
  }});

  document.getElementById('s-search').addEventListener('input', function(e){{
    var q=e.target.value.toLowerCase(), items=document.querySelectorAll('.s-item'), vis=0;
    for(var i=0;i<items.length;i++){{
      var n=(items[i].getAttribute('data-name')||'').toLowerCase();
      var hit=n.indexOf(q)!==-1; items[i].style.display=hit?'':'none'; if(hit) vis++;
    }}
    document.getElementById('s-count').textContent=vis+' محطة';
  }});

  // Broken favicons. _favicon() rejects anything that is not https, but a
  // well-formed https URL still 404s constantly — 34 of 49 in a live UAE card —
  // and each one drew the browser's broken-image glyph. The original hid them
  // with an inline onerror; that handler carried no interpolated data, but it
  // went out with the rest of the inline JS, and nothing replaced it. Error
  // events do not bubble, so each image is watched individually, and images that
  // already failed before this script ran are swept by the naturalWidth check.
  function replaceBroken(img){{
    if(img.id === 'r-fav'){{
      img.style.display='none';
      var ico=document.getElementById('r-ico'); if(ico) ico.style.display='flex';
      return;
    }}
    var ph=document.createElement('span');
    ph.className='s-fav s-fav-ph'; ph.textContent='📻';
    if(img.parentNode) img.parentNode.replaceChild(ph, img);
  }}
  function sweepImages(){{
    var imgs=document.querySelectorAll('img');
    for(var i=0;i<imgs.length;i++){{
      (function(im){{
        if(im.complete && im.naturalWidth===0){{ replaceBroken(im); return; }}
        im.addEventListener('error', function(){{ replaceBroken(im); }});
      }})(imgs[i]);
    }}
  }}
  sweepImages();
  window.addEventListener('load', sweepImages);

  function reportHeight(){{
    parent.postMessage({{type:'iframe:height',
      height:document.documentElement.scrollHeight}},'*');
  }}
  window.addEventListener('load', reportHeight);
  new ResizeObserver(reportHeight).observe(document.body);
}})();
</script>
</body>
</html>"""


def _notice_shell(body: str) -> str:
    """A minimal document that STILL REPORTS ITS HEIGHT.

    The failure this fixes: _fail returned a bare <div> with no script at all, so
    the frame never posted a height, the host left it at its 40px minimum, and a
    two-line message rendered as a clipped strip with a scrollbar. Every other
    card in this file reports; this one was the exception because it looked too
    small to need it.

    ResizeObserver as well as load, for the same reason the QR card needed it:
    at `load` the document has not always reached its final height, and a single
    early measurement is how you end up reporting the viewport back to the host.
    """
    return f"""<!DOCTYPE html>
<html dir="rtl"><head><meta charset="utf-8"><style>
  html,body {{ margin:0; padding:0; background:transparent; }}
  .notice {{ padding:14px 16px; border-radius:12px; font-size:13px; line-height:1.55;
            font-family:'Segoe UI','Noto Sans Arabic',Tahoma,-apple-system,sans-serif; }}
  .notice.info {{ background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.08);
                 color:#c9d3e2; }}
  .notice.warn {{ background:#fdecea; border:1px solid #f5c6c0; color:#8a1c12; }}
</style></head><body>{body}
<script>
  function send() {{
    var h = Math.max(document.documentElement.scrollHeight,
                     document.body ? document.body.scrollHeight : 0);
    if (h > 0) parent.postMessage({{type:'iframe:height', height:h}}, '*');
  }}
  window.addEventListener('load', send);
  if (window.ResizeObserver) {{ new ResizeObserver(send).observe(document.documentElement); }}
</script></body></html>"""


def _fail(message_ar: str) -> tuple[HTMLResponse, str]:
    """A genuine failure — upstream unreachable, nothing matched. Warn styling."""
    return (HTMLResponse(content=_notice_shell(
                f"<div class='notice warn'>⚠️ {_esc(message_ar)}</div>"),
            headers=INLINE),
            message_ar)


def _empty_state(message: str, rtl: bool = False) -> tuple[HTMLResponse, str]:
    """Nothing to show YET — which is not an error and must not look like one.

    An empty library rendered through _fail: red text on white, the palette this
    app uses for things that went wrong. Nothing had gone wrong; the user simply
    had not saved a station. Same neutral surface as every other card, and the
    message says what to do next.
    """
    align = "right" if rtl else "left"
    return (HTMLResponse(content=_notice_shell(
                f"<div class='notice info' style='text-align:{align}' "
                f"dir='{'rtl' if rtl else 'ltr'}'>📻 {_esc(message)}</div>"),
            headers=INLINE),
            message)


def _player(pool: list, heading: str, empty_msg: str,
            deadline: float | None = None,
            empty_is_error: bool = True) -> tuple[HTMLResponse, str]:
    if not pool:
        # An empty LIBRARY is an empty state; an empty SEARCH is a failure.
        return _fail(empty_msg) if empty_is_error else _empty_state(empty_msg)
    pool = _first_playable(pool, deadline)
    first = pool[0]
    return (
        HTMLResponse(content=_render(first, pool, heading), headers=INLINE),
        f"تم فتح مشغل {heading} ({len(pool)} محطة). "
        f"المحطة الحالية: {first.get('name', '')}. "
        "The player is visible to the user — do not list the stations, "
        "and do not print the stream URL.",
    )


_QUERY = {"limit": 200, "hidebroken": "true", "order": "votes", "reverse": "true"}


# ── the five tools ───────────────────────────────────────────────────────────

def uae_radio(user_id: str = "") -> tuple[HTMLResponse, str]:
    """The default for a bare "play radio": saved stations first, live appended.

    A preference, never a ceiling — the live UAE list is still there, below the
    user's own. With an empty library this is exactly the pre-library behaviour.
    """
    dl = _new_deadline()
    saved = _saved_stations(user_id) if user_id else []
    live = playable(_api_get("stations/bycountrycodeexact/AE", _QUERY, dl))
    pool = _merge_saved_first(saved, live)
    heading = "محطاتي + الإذاعات الإماراتية" if saved else "الإذاعات الإماراتية"
    return _player(pool, heading,
                   "تعذر جلب الإذاعات الإماراتية حالياً. قد تكون الخدمة غير متاحة.", dl)


def arabic_radio(country_code: str = "") -> tuple[HTMLResponse, str]:
    dl = _new_deadline()
    cc = (country_code or "").upper().strip()
    if cc.isalpha() and len(cc) == 2:
        pool = playable(_api_get(f"stations/bycountrycodeexact/{cc}", _QUERY, dl))
        heading = f"إذاعات {cc}"
    else:
        pool = playable(_api_get("stations/bylanguageexact/arabic", _QUERY, dl))
        heading = "الإذاعات العربية"
    return _player(pool, heading,
                   f"لم يتم العثور على محطات{(' للرمز ' + cc) if cc else ''}.", dl)


def quran_radio() -> tuple[HTMLResponse, str]:
    dl = _new_deadline()
    stations = _api_get("stations/bytag/quran", _QUERY, dl)
    if not stations:
        stations = _api_get("stations/search", {**_QUERY, "name": "quran"}, dl)
    return _player(playable(stations), "إذاعات القرآن الكريم",
                   "تعذر جلب إذاعات القرآن الكريم حالياً.", dl)


def radio_by_genre(genre: str) -> tuple[HTMLResponse, str]:
    dl = _new_deadline()
    tag = (genre or "").lower().replace(" ", "")
    if not tag:
        return _fail("يرجى تحديد نوع الإذاعة.")
    stations = _api_get(f"stations/bytag/{tag}", _QUERY, dl)
    if not stations:
        stations = _api_get("stations/search", {**_QUERY, "tag": genre}, dl)
    return _player(playable(stations), f"إذاعات: {genre}",
                   f"لم يتم العثور على محطات من نوع «{genre}».", dl)


def search_radio(query: str) -> tuple[HTMLResponse, str]:
    dl = _new_deadline()
    if not (query or "").strip():
        return _fail("يرجى تحديد اسم المحطة.")

    # "open dubai radio channel" is not a station name. Radio Browser's byname is
    # a substring match, so the literal string found nothing and the user was
    # told the station does not exist — for a request we can serve perfectly.
    core = _meaningful(query)
    if not core:
        # Every word was filler: they asked for "radio", not for a station.
        # That is the default action, so do it instead of failing.
        pool = playable(_api_get("stations/bycountrycodeexact/AE", _QUERY, dl))
        return _player(pool, "الإذاعات الإماراتية",
                       "تعذر جلب الإذاعات الإماراتية حالياً.", dl)

    stations = _api_get("stations/search", {**_QUERY, "name": query}, dl)
    if not playable(stations) and core != (query or "").strip().lower():
        # Retry on the meaningful part only. Tried SECOND, never first: the full
        # string is what matches "Radio Mirchi Dubai", and stripping "radio" out
        # of it would break a name that legitimately contains the word.
        log.info("radio: %r found nothing, retrying as %r", query, core)
        stations = _api_get("stations/search", {**_QUERY, "name": core}, dl)

    return _player(playable(stations), f"نتائج البحث: {query}",
                   f"لم يتم العثور على محطة باسم «{query}». "
                   "جرّب اسماً آخر أو اطلب الإذاعات الإماراتية.", dl)


# ── personal station library ─────────────────────────────────────────────────
#
# See the module docstring: this is a CURATED LIBRARY, not a cache of the
# directory. The distinction is the whole design.

LIBRARY_KIND = "radio"
BULK_ADD_CAP = 25
SEARCH_SHOW_LIMIT = 12
SEARCH_PROBE_LIMIT = 30


def _saved_stations(user_id: str) -> list[dict]:
    """This user's library, shaped like a Radio Browser station.

    Returned in the same dict shape the live path uses so ranking, rendering and
    the player treat a saved station identically to a fetched one — the card
    should not be able to tell where a station came from.

    `_saved` marks them so the liveness probe can be skipped: these were checked
    by validate_radio at ADD time, and re-probing a curated list would make the
    default request slower the more the user saves, which is backwards.
    """
    try:
        from backend.services import media_sources
        rows = media_sources.list_sources(user_id, LIBRARY_KIND)
    except Exception:  # noqa: BLE001 — a library outage must not break live radio
        log.exception("radio: could not read the saved library")
        return []
    out = []
    for r in rows:
        out.append({
            "name": r["name"], "url_resolved": r["url"], "url": r["url"],
            "stationuuid": f"saved:{r['id']}",
            "countrycode": (r.get("category") or "")[:2].upper()
                           if len(r.get("category") or "") == 2 else "AE",
            "votes": 0, "tags": r.get("category") or "",
            "codec": r.get("codec") or "", "bitrate": r.get("bitrate") or 0,
            "language": "", "favicon": "",
            "_saved": True,
        })
    return out


def _merge_saved_first(saved: list[dict], live: list[dict]) -> list[dict]:
    """Saved stations on top, live results appended, no duplicates.

    Deduped by URL rather than name: the same station is listed under several
    names across the directory, and the URL is what actually plays.
    """
    # Saved stations are RANKED like live ones rather than left in the library's
    # alphabetical order, which is arbitrary from the listener's point of view —
    # it opened a two-station library on "Exclusively Pink Floyd" instead of
    # "Quran Radio From Sharjah". Same table, so the saved block and the live
    # block below it are ordered on the same principle.
    ranked = sorted(saved, key=lambda s: (-_station_priority(s), s.get("name") or ""))
    have = {_stream_url(s) for s in ranked}
    return ranked + [s for s in live if _stream_url(s) not in have]


def search_radio_stations(user_id: str, name: str = "", country: str = "",
                          genre: str = "") -> tuple[HTMLResponse, str, dict] | str:
    """Find stations to ADD, as a picker. Read-only — nothing is saved here.

    Every row shown has been probed with the same check the add path applies, so
    what the picker offers is exactly what add will accept.
    """
    from backend.services import media_sources

    dl = _new_deadline()
    cc = (country or "").upper().strip()
    # A name made only of filler ("radio channel") is not a search term. Matching
    # it literally returned arbitrary global stations that happened to contain the
    # word — NTS Radio Channel 1, Stegi Radio Channel 2 — which reads as a broken
    # search. With no country or genre either, fall back to the deployment's
    # default scope rather than guessing.
    core = _meaningful(name) if name.strip() else ""
    if name.strip() and not core and not cc and not genre.strip():
        cc = "AE"
        name = ""
    if name.strip():
        rows = _api_get("stations/search", {**_QUERY, "name": name.strip()}, dl)
        if not playable(rows) and core and core != name.strip().lower():
            log.info("radio library: %r found nothing, retrying as %r", name, core)
            rows = _api_get("stations/search", {**_QUERY, "name": core}, dl)
        label = name.strip()
    elif cc.isalpha() and len(cc) == 2:
        rows = _api_get(f"stations/bycountrycodeexact/{cc}", _QUERY, dl)
        label = cc
    elif genre.strip():
        rows = _api_get(f"stations/bytag/{genre.lower().replace(' ', '')}", _QUERY, dl)
        label = genre.strip()
    else:
        return ("Tell me what to look for — a station name, a two-letter country "
                "code (AE, SA, EG), or a genre (news, quran, pop).")

    candidates = playable(rows)
    if not candidates:
        return (f"Nothing playable matched “{label}”. Many stations are http-only or "
                f"HLS, which this player cannot use.")

    have = {s["url"] for s in media_sources.list_sources(user_id, LIBRARY_KIND)}
    candidates = [c for c in candidates if _stream_url(c) not in have]
    if not candidates:
        return "Everything matching that is already in your station list."

    # Same validator as add, so the picker cannot offer a row that add refuses.
    urls = [_stream_url(c) for c in candidates[:SEARCH_PROBE_LIMIT]]
    live = media_sources.validate_many(urls, kind=LIBRARY_KIND)
    results = [c for c in candidates if live.get(_stream_url(c))][:SEARCH_SHOW_LIMIT]
    if not results:
        return (f"Found {len(candidates)} station(s) matching “{label}”, but none are "
                f"broadcasting right now. Try another term.")

    picker_rows = [{"id": c.get("stationuuid") or _stream_url(c),
                    "name": c.get("name") or "?",
                    "url": _stream_url(c),
                    "country": c.get("countrycode") or "",
                    "category": (c.get("tags") or "").split(",")[0].strip() or "radio",
                    "quality": f"{int(c.get('bitrate') or 0)}kbps {c.get('codec') or ''}".strip()}
                   for c in results]

    listing = "; ".join(f"{r['name']} ({r['country']})" for r in picker_rows)
    context = (
        f"Found {len(picker_rows)} live station(s) matching “{label}”: {listing}. The "
        f"list is already visible with checkboxes — tell the user to tick the ones "
        f"they want and press Add. Do not list them again and do not paste URLs. "
        f"Every one shown is broadcasting right now."
    )
    return (
        HTMLResponse(content=_build_station_list(picker_rows), media_type="text/html",
                     headers={"content-disposition": "inline"}),
        context,
        # channel_kind is what tells the picker to commit as RADIO — without it
        # the shared component would phrase the follow-up as a TV add.
        {"channels": picker_rows, "query": label, "channel_kind": LIBRARY_KIND},
    )


def _build_station_list(rows: list[dict]) -> str:
    """A plain fallback listing. The real UI is the React picker outside the
    frame; this renders if the structured half is ever missing."""
    items = "".join(
        f"<li>{_esc(r['name'])} — {_esc(r['country'])} {_esc(r['quality'])}</li>"
        for r in rows)
    return _card_shell(f"<ul style='margin:0;padding-inline-start:18px'>{items}</ul>")


def _card_shell(body: str) -> str:
    """Plain listing fallback. Uses the reporting shell so it cannot be clipped."""
    return _notice_shell(f"<div class='notice info'>{body}</div>")


def add_radio_stations_bulk(user_id: str, names: list[str]) -> str:
    """Save several stations at once, re-validating each.

    The picker is the confirmation — the user ticked these — so there is no
    second approval gate. The cap is what stops a select-all becoming an
    unbounded write in one turn.
    """
    from backend.services import media_sources

    wanted = [n.strip() for n in (names or []) if n and n.strip()][:BULK_ADD_CAP]
    if not wanted:
        return "⚠️ Tell me which stations to add."

    dl = _new_deadline()
    added, failed = [], []
    for n in wanted:
        hits = playable(_api_get("stations/search", {**_QUERY, "name": n}, dl))
        match = next((h for h in hits if (h.get("name") or "").strip().lower()
                      == n.lower()), hits[0] if hits else None)
        if not match:
            failed.append(f"{n} (not found)")
            continue
        ok, message = media_sources.add_source(
            user_id, match.get("name") or n, _stream_url(match), LIBRARY_KIND,
            category=(match.get("countrycode") or "").upper() or "radio",
            codec=match.get("codec") or None, bitrate=match.get("bitrate") or None)
        (added if ok else failed).append(
            (match.get("name") or n) if ok else f"{match.get('name') or n} ({message})")

    parts = []
    if added:
        parts.append(f"Added {len(added)}: {', '.join(added)}.")
    if failed:
        parts.append(f"Could not add {len(failed)}: {'; '.join(failed)}.")
    if len(names or []) > BULK_ADD_CAP:
        parts.append(f"(Capped at {BULK_ADD_CAP} per request.)")
    return " ".join(parts) or "Nothing was added."


def remove_radio_station(user_id: str, name: str) -> str:
    from backend.services import media_sources
    if not (name or "").strip():
        return "⚠️ Tell me which station to remove."
    ok, message = media_sources.remove_source(user_id, name.strip(), LIBRARY_KIND)
    return message if ok else f"⚠️ {message}"


def my_radio(user_id: str) -> tuple[HTMLResponse, str]:
    """Play the user's own library, and nothing else."""
    dl = _new_deadline()
    saved = _saved_stations(user_id)
    return _player(saved, "محطاتي المحفوظة",
                   "No saved stations yet. Ask me to “add some UAE radio stations” and "
                   "tick the ones you want — or just say “play radio” for the live "
                   "UAE list.", dl, empty_is_error=False)
