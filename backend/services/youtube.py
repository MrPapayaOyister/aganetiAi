"""
YouTube playback + metadata for the chat assistant.

Ported from the Open WebUI tool "YouTube Player (No API Key)" v1.6.0
(reference/yt-tool-service/youtube_player.py). The parsing, the oEmbed call and
the player template are the reference's, unchanged where they matter — the
adaptations are structural, to fit this codebase:

  * no ``Tools`` / ``Valves`` pydantic class. Open WebUI stores tool settings in
    its own DB and hands them to the instance; here configuration is env-driven
    module state in ``config.settings``, like every other feature flag.
  * no ``__event_emitter__``. main.py already emits a typed ``thinking`` SSE
    event before each tool runs, so a second per-tool progress channel would
    just duplicate it.
  * the shared per-loop httpx client (``services.http_client``) instead of a
    fresh ``AsyncClient`` per call, so these calls reuse the same connection
    pool as the mail/calendar providers.

``play_youtube_video`` returns ``(HTMLResponse, context)``. That shape is load
bearing, not decoration: ``backend.tool_result.process_tool_result`` splits it
so the player markup travels to the frontend as an ``embeds`` entry while the
model only ever sees ``context``. See that module for why the model must never
be handed the markup itself.

Search is NOT the reference's. That one needs a Data API v3 key and renders a
results list whose items play via a nested iframe — a path we verified renders
black, because a frame nested inside our sandboxed embed inherits the sandbox.
``search_youtube`` below is keyless (yt-dlp) and returns structured results that
React renders; the ordering heuristic is adapted from Hermes.

The transcript pair is still unported — it needs ``youtube-transcript-api``.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import json as _json
import logging
import re
import urllib.parse as _url

from fastapi.responses import HTMLResponse

log = logging.getLogger("aria.youtube")

from config.settings import (
    YOUTUBE_CLICK_TO_PLAY,
    YOUTUBE_EMBED_PLAYER,
    YOUTUBE_NOCOOKIE,
    YOUTUBE_REQUEST_TIMEOUT,
    YOUTUBE_SITE_ORIGIN,
)

OEMBED_URL = "https://www.youtube.com/oembed"

# Accepts: watch?v=, youtu.be/, /embed/, /shorts/, /live/, /v/, or a bare 11-char ID
_ID_PATTERNS = [
    re.compile(
        r"(?:youtube\.com|youtube-nocookie\.com)/watch\?(?:.*&)?v=([A-Za-z0-9_-]{11})"
    ),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/v/([A-Za-z0-9_-]{11})"),
    re.compile(r"^([A-Za-z0-9_-]{11})$"),
]

# t=90 / t=90s / t=1m30s / t=1h2m3s / start=90
# The plain-seconds form must terminate at & or end-of-string, otherwise it
# would match just the "1" in t=1m30s.
_T_SIMPLE = re.compile(r"[?&](?:t|start)=(\d+)s?(?=&|$)")
_T_CLOCK = re.compile(r"[?&]t=(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?=&|$)")


def _js_json(obj) -> str:
    """JSON for embedding inside a <script> block.

    Escapes <, > and & so a value containing "</script>" cannot terminate the
    element and inject markup. Titles come from the YouTube API, i.e. untrusted.
    """
    return (
        _json.dumps(obj)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _extract_video_id(text: str) -> str | None:
    s = (text or "").strip()
    for pat in _ID_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    return None


def _extract_start_seconds(text: str) -> int:
    s = text or ""
    m = _T_SIMPLE.search(s)
    if m:
        return int(m.group(1))
    m = _T_CLOCK.search(s)
    if m and any(m.groups()):
        h, mi, sec = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + sec
    return 0


def _watch_url(video_id: str, start: int = 0) -> str:
    """The canonical watch URL, carrying the requested start time.

    Card mode never renders the embed `src` that normally carries `start=`, so
    this is the only place a requested timestamp survives — it feeds both the
    URL printed on the card and the link React renders outside the iframe.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    return url + (f"&t={start}s" if start > 0 else "")


async def _fetch_oembed(video_id: str, timeout: int) -> dict:
    """Public oEmbed endpoint. No key, no quota.

    Raises ValueError for the one case the caller can explain to the user (404 =
    private / deleted / embedding disabled); any other failure propagates so the
    caller can decide whether metadata is essential.
    """
    from backend.services import http_client

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    r = await http_client.get_client().get(
        OEMBED_URL,
        params={"url": watch_url, "format": "json"},
        timeout=timeout,
        follow_redirects=True,
    )
    if r.status_code == 404:
        raise ValueError(
            "Video not found, private, or embedding is disabled by the uploader."
        )
    r.raise_for_status()
    return r.json() or {}


def _build_player(
    video_id: str,
    meta: dict,
    nocookie: bool,
    click_to_play: bool,
    start: int,
    origin: str = "",
    embed_player: bool = False,
) -> str:
    """The self-contained player document rendered inside the sandboxed iframe.

    Every untrusted value (title, author, thumbnail URL — all from YouTube)
    is HTML-escaped here, or JSON-escaped by _js_json when it crosses into the
    <script> block.
    """
    domain = "www.youtube-nocookie.com" if nocookie else "www.youtube.com"

    title = _html.escape(meta.get("title") or "YouTube video")
    author = _html.escape(meta.get("author_name") or "")
    thumb = _html.escape(
        meta.get("thumbnail_url") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    )
    watch_url = _html.escape(_watch_url(video_id, start))

    params = ["rel=0"]
    if origin:
        params.append(f"origin={_url.quote(origin, safe=':/')}")
    if start > 0:
        params.append(f"start={start}")
    src = f"https://{domain}/embed/{video_id}?" + "&".join(params)

    allow = (
        "accelerometer; autoplay; clipboard-write; encrypted-media; "
        "gyroscope; picture-in-picture; web-share"
    )
    REFPOL = "strict-origin-when-cross-origin"

    play_button = (
        '<span class="yt-play" aria-hidden="true">'
        '<svg viewBox="0 0 68 48" width="68" height="48">'
        '<path class="yt-play-bg" d="M66.5 7.7a8.6 8.6 0 0 0-6-6C55.2 0 34 0 34 0'
        "S12.8 0 7.5 1.7a8.6 8.6 0 0 0-6 6A90 90 0 0 0 0 24a90 90 0 0 0 1.5 16.3"
        "8.6 8.6 0 0 0 6 6C12.8 48 34 48 34 48s21.2 0 26.5-1.7a8.6 8.6 0 0 0 6-6"
        'A90 90 0 0 0 68 24a90 90 0 0 0-1.5-16.3z"/>'
        '<path d="M45 24 27 14v20z" fill="#fff"/></svg></span>'
    )

    facade_js = ""
    if not embed_player:
        # Card mode (default): no nested iframe and no link — the card is inert,
        # exactly as upstream intended.
        #
        # A link cannot live in here. Anything opened from inside this sandboxed
        # frame inherits its sandbox: with allow-popups the new tab gets an opaque
        # origin and youtube.com refuses it (ERR_BLOCKED_BY_RESPONSE), and the
        # flag that would fix that (allow-popups-to-escape-sandbox) makes every
        # embed able to open fully-unsandboxed tabs. Both verified in-browser.
        # The clickable affordance is therefore rendered by React OUTSIDE the
        # iframe, where it is an ordinary same-origin app link — see
        # ToolEmbeds.tsx. The watch URL travels there via the embed payload.
        stage = (
            f'<div class="yt-facade yt-static">'
            f'<img class="yt-thumb" src="{thumb}" alt="" loading="lazy">'
            f"{play_button}</div>"
        )
    elif click_to_play:
        stage = (
            f'<button class="yt-facade" id="yt-facade" type="button" '
            f'aria-label="Play video">'
            f'<img class="yt-thumb" src="{thumb}" alt="" loading="lazy">'
            f"{play_button}</button>"
        )
        facade_js = """
  var facade = document.getElementById('yt-facade');
  if (facade) {
    facade.addEventListener('click', function () {
      var f = document.createElement('iframe');
      f.className = 'yt-frame';
      f.src = %s;
      f.allow = %s;
      f.referrerPolicy = %s;
      f.allowFullscreen = true;
      facade.replaceWith(f);
      setTimeout(reportHeight, 60);
    });
  }
""" % (
            _js_json(src + "&autoplay=1"),
            _js_json(allow),
            _js_json(REFPOL),
        )
    else:
        stage = (
            f'<iframe class="yt-frame" src="{src}" allow="{allow}" '
            f'referrerpolicy="{REFPOL}" allowfullscreen loading="lazy"></iframe>'
        )

    # No <a target="_blank"> anywhere: the embed sandbox omits allow-popups, so
    # such a link navigates the frame instead of opening a tab, and
    # youtube.com/watch refuses to be framed (ERR_BLOCKED_BY_RESPONSE). The
    # clickable link is surfaced in the chat message text instead — which is why
    # the LLM context below insists the model append it.
    byline = f"<span>{author}</span>" if author else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="{REFPOL}">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:transparent;padding:4px;overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans Arabic',sans-serif;
  color:#E6EBF5}}
.yt-card{{max-width:860px;margin:0 auto;border-radius:14px;overflow:hidden;
  background:rgba(127,127,127,0.10);box-shadow:0 4px 18px rgba(0,0,0,0.22)}}
.yt-stage{{position:relative;width:100%;aspect-ratio:16/9;background:#000}}
.yt-frame,.yt-facade{{position:absolute;inset:0;width:100%;height:100%;border:0;padding:0;display:block}}
.yt-facade{{cursor:pointer;background:#000}}
.yt-static{{cursor:default}}
.yt-thumb{{width:100%;height:100%;object-fit:cover;display:block}}
.yt-play{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%)}}
.yt-play-bg{{fill:#212121;fill-opacity:.82;transition:fill-opacity .15s ease}}
.yt-facade:hover .yt-play-bg{{fill:#f00;fill-opacity:1}}
.yt-meta{{padding:11px 14px 13px}}
.yt-title{{font-size:.95rem;font-weight:700;line-height:1.35;margin-bottom:5px}}
.yt-sub{{font-size:.75rem;opacity:.72;display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
.yt-url{{font:11px/1.4 ui-monospace,Menlo,monospace;opacity:.5;margin-top:6px;
  user-select:all;word-break:break-all}}
</style>
</head>
<body>
<div class="yt-card">
  <div class="yt-stage">{stage}</div>
  <div class="yt-meta">
    <div class="yt-title">{title}</div>
    <div class="yt-sub">
      {byline}
    </div>
    <div class="yt-url">{watch_url}</div>
  </div>
</div>
<script>
(function () {{
  function reportHeight() {{
    try {{
      parent.postMessage({{ type: 'iframe:height',
        height: Math.ceil(document.documentElement.getBoundingClientRect().height) }}, '*');
    }} catch (e) {{}}
  }}
  window.addEventListener('load', reportHeight);
  window.addEventListener('resize', reportHeight);
  if (window.ResizeObserver) {{ new ResizeObserver(reportHeight).observe(document.body); }}
  [80, 300, 900].forEach(function (d) {{ setTimeout(reportHeight, d); }});
{facade_js}
}})();
</script>
</body>
</html>"""


async def play_youtube_video(video: str) -> str | tuple[HTMLResponse, str, dict]:
    """Build the video card for `video`.

    Returns ``(HTMLResponse, context, link)`` on success: the HTML goes to the
    frontend, the context string to the model, and `link` is rendered by React
    *outside* the sandboxed iframe (a link inside it cannot work — see the card
    branch of _build_player). Returns a plain string when there is nothing to
    render, which process_tool_result passes straight through to the model.

    `link["label"]` is the RAW oEmbed title, deliberately not HTML-escaped:
    React escapes on render, so escaping here would show `&amp;` to the user.
    """
    video_id = _extract_video_id(video)
    if not video_id:
        return (
            "That doesn't look like a YouTube video. Provide a full YouTube URL "
            "(youtube.com/watch?v=..., youtu.be/..., /shorts/...) or an "
            "11-character video ID."
        )

    start = _extract_start_seconds(video)

    meta, warning = {}, ""
    try:
        meta = await _fetch_oembed(video_id, YOUTUBE_REQUEST_TIMEOUT)
    except ValueError as ve:
        return f"❌ {ve}"
    except Exception as exc:
        # Metadata is a nicety; the player still works without it.
        warning = f" (metadata lookup failed: {type(exc).__name__}: {exc})"

    player = _build_player(
        video_id,
        meta,
        nocookie=YOUTUBE_NOCOOKIE,
        click_to_play=YOUTUBE_CLICK_TO_PLAY,
        start=start,
        origin=(YOUTUBE_SITE_ORIGIN or "").strip().rstrip("/"),
        embed_player=YOUTUBE_EMBED_PLAYER,
    )

    title = meta.get("title") or video_id
    author = meta.get("author_name")

    context = f'Now playing "{title}"'
    if author:
        context += f" by {author}"
    if start:
        context += f", starting at {start}s"
    context += (
        f". The player is already embedded and visible to the user - do not "
        f"describe it. IMPORTANT: end your reply with this markdown link on "
        f"its own line, exactly as written, so the user has a clickable way "
        f"to open it (links inside the embed cannot open new tabs): "
        f"[{title}](https://www.youtube.com/watch?v={video_id}){warning}"
    )

    return (
        HTMLResponse(
            content=player,
            media_type="text/html",
            headers={"content-disposition": "inline"},
        ),
        context,
        {
            # Rendered by React as a real player iframe pointed at YouTube's own
            # origin. The card HTML above is still sent as the fallback for a
            # client that doesn't understand `video`.
            "video": {"id": video_id, "start": start},
            "link": {"url": _watch_url(video_id, start), "label": title},
        },
    )


async def get_youtube_video_info(video: str) -> str:
    """Title / channel / thumbnail as plain text, with no player embedded."""
    video_id = _extract_video_id(video)
    if not video_id:
        return "That doesn't look like a YouTube video URL or ID."

    try:
        meta = await _fetch_oembed(video_id, YOUTUBE_REQUEST_TIMEOUT)
    except ValueError as ve:
        return f"❌ {ve}"
    except Exception as exc:
        return f"❌ Could not reach YouTube: {type(exc).__name__}: {exc}"

    return (
        f"Title: {meta.get('title', 'Unknown')}\n"
        f"Channel: {meta.get('author_name', 'Unknown')}\n"
        f"Channel URL: {meta.get('author_url', '')}\n"
        f"Thumbnail: {meta.get('thumbnail_url', '')}\n"
        f"Video ID: {video_id}\n"
        f"URL: https://www.youtube.com/watch?v={video_id}"
    )


# ── Search (yt-dlp, keyless) ─────────────────────────────────────────────────
#
# yt-dlp rather than YouTube Data API v3: no key to provision and no quota
# (search.list costs 100 units against a 10,000/day default — about 100 searches
# a day for the whole instance). The trade is that this is a scraper: it can
# break when YouTube changes, and datacentre IPs are sometimes throttled. Hence
# `yt_dlp_available()` — the tool is hidden when the dependency is missing
# instead of being offered and failing.
#
# Ordering logic is adapted from Hermes's tools/youtube_search.py. The insight
# is that neither ordering is a safe default: relevance answers "man city
# highlights" with a famous match from years ago, while date answers "never
# gonna give you up" with whatever cover was uploaded an hour ago. The query
# itself has to choose.

SEARCH_TIMEOUT = 25

_RECENCY_WORDS = frozenset({
    "latest", "recent", "recently", "newest", "new", "today", "tonight",
    "yesterday", "tomorrow", "current", "live", "now", "breaking", "update",
    "updates", "highlights", "recap", "this week", "this month", "last night",
    "just released", "premiere",
})

# A bare year means "recent" only when it is at or near the present: "2026
# season" does, "1994 world cup" does not. Evaluated per call rather than at
# import so a long-running process doesn't stay anchored to its start year.
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# YouTube's own "Sort by: upload date" filter as it appears in the results URL.
_SORT_BY_DATE = "CAI%3D"
_RESULTS_URL = "https://www.youtube.com/results?search_query={q}&sp=" + _SORT_BY_DATE


def yt_dlp_available() -> bool:
    """Whether the optional search dependency is installed."""
    import importlib.util
    return importlib.util.find_spec("yt_dlp") is not None


def _wants_recent(query: str, today: _dt.date | None = None) -> bool:
    """True when the query asks for something new rather than something famous."""
    q = (query or "").lower()
    if any(w in q for w in _RECENCY_WORDS):
        return True
    year = (today or _dt.date.today()).year
    recent = {str(year), str(year - 1)}
    return any(m.group(0) in recent for m in _YEAR_RE.finditer(q))


def _search_target(query: str, limit: int) -> str:
    """The yt-dlp target — date-ordered for recency queries, else relevance."""
    if _wants_recent(query):
        # yt-dlp no longer ships a `ytsearchdate` extractor, so the ordering is
        # requested from YouTube directly via the results URL.
        return _RESULTS_URL.format(q=_url.quote(query))
    return f"ytsearch{max(1, limit)}:{query}"


def _entry_thumb(entry: dict, video_id: str) -> str:
    thumbs = entry.get("thumbnails") or []
    for t in reversed(thumbs):
        if isinstance(t, dict) and t.get("url"):
            return t["url"]
    return entry.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def _search(query: str, limit: int) -> list[dict]:
    """Blocking yt-dlp search. Returns [{id, title, channel, thumb, duration}].

    Called from the sync tool dispatcher, which already runs in a worker thread,
    so there is no async bridge here on purpose.
    """
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Metadata only. Resolving formats triples the request count and nothing
        # is ever downloaded here.
        "extract_flat": True,
        "socket_timeout": SEARCH_TIMEOUT,
        "noplaylist": True,
        "playlistend": max(1, limit),
        # yt-dlp writes progress to stdout by default; in-process that would
        # interleave with our structured logs.
        "logger": log,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_search_target(query, limit), download=False)

    entries = info.get("entries") if isinstance(info, dict) else None
    if entries is None:
        # A direct URL rather than a search — the payload IS the video.
        entries = [info] if isinstance(info, dict) and info.get("id") else []

    out: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        vid = entry.get("id") or ""
        # Guard the same 11-char rule the frontend enforces before an iframe src.
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            continue
        out.append({
            "id": vid,
            "title": (entry.get("title") or "").strip() or "Untitled",
            "channel": (entry.get("channel") or entry.get("uploader") or "").strip(),
            "thumb": _entry_thumb(entry, vid),
            "duration": int(entry.get("duration") or 0),
        })
        if len(out) >= limit:
            break
    return out


def _build_results_card(results: list[dict], query: str) -> str:
    """Inert fallback list for a client that can't render the React picker.

    No iframes and no links, for the same reasons the video card has none: a
    nested player inherits the sandbox and renders black, and a link opened from
    it gets an opaque origin that YouTube refuses.
    """
    rows = []
    for i, v in enumerate(results, 1):
        rows.append(
            f'<div class="r"><span class="n">{i}</span>'
            f'<img src="{_html.escape(v["thumb"])}" alt="" loading="lazy">'
            f'<span class="t"><span class="tt">{_html.escape(v["title"])}</span>'
            f'<span class="tc">{_html.escape(v["channel"])}</span></span></div>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:transparent;padding:4px;overflow:hidden;color:#E6EBF5;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
.wrap{{max-width:860px;margin:0 auto}}
.q{{font-size:.72rem;opacity:.6;margin:0 2px 8px}}
.r{{display:flex;gap:10px;align-items:center;width:100%;background:rgba(127,127,127,.10);
  border-radius:10px;padding:7px;margin-bottom:6px}}
.n{{width:18px;text-align:center;font-size:.72rem;opacity:.5;flex-shrink:0}}
.r img{{width:112px;height:63px;object-fit:cover;border-radius:6px;flex-shrink:0;background:#000}}
.t{{display:flex;flex-direction:column;gap:3px;min-width:0}}
.tt{{font-size:.83rem;font-weight:600;line-height:1.3}}
.tc{{font-size:.7rem;opacity:.62}}
</style>
</head>
<body>
<div class="wrap">
  <div class="q">Results for &ldquo;{_html.escape(query)}&rdquo;</div>
  {''.join(rows)}
</div>
<script>
(function () {{
  function reportHeight() {{
    try {{
      parent.postMessage({{ type: 'iframe:height',
        height: Math.ceil(document.documentElement.getBoundingClientRect().height) }}, '*');
    }} catch (e) {{}}
  }}
  window.addEventListener('load', reportHeight);
  window.addEventListener('resize', reportHeight);
  if (window.ResizeObserver) {{ new ResizeObserver(reportHeight).observe(document.body); }}
  [80, 300, 900].forEach(function (d) {{ setTimeout(reportHeight, d); }});
}})();
</script>
</body>
</html>"""


def search_youtube(query: str, mode: str = "play", limit: int = 5):
    """Search YouTube. `mode` decides the shape of the answer.

    play   → the top hit, rendered as a ready player (one turn, no picking).
    browse → up to `limit` hits as structured results for the React picker.

    Sync on purpose: yt-dlp blocks, and the tool dispatcher already runs in a
    worker thread.
    """
    q = (query or "").strip()
    if not q:
        return "What should I search YouTube for?"
    if not yt_dlp_available():
        return ("⚠️ YouTube search isn't available — the yt-dlp package is not "
                "installed on this server.")

    browse = (mode or "play").strip().lower() == "browse"
    want = max(1, min(int(limit or 5), 10)) if browse else 1
    try:
        results = _search(q, want)
    except Exception as exc:
        log.warning("youtube search failed for %r: %s", q, exc)
        return f"⚠️ YouTube search failed: {type(exc).__name__}: {exc}"

    # Re-check ids here, not only where they are parsed: these end up in an
    # iframe src, and the guard has to hold whatever produced the list.
    results = [r for r in results
               if re.fullmatch(r"[A-Za-z0-9_-]{11}", str(r.get("id") or ""))]
    if not results:
        return f"No YouTube results found for “{q}”."

    if not browse:
        top = results[0]
        meta = {"title": top["title"], "author_name": top["channel"],
                "thumbnail_url": top["thumb"]}
        player = _build_player(
            top["id"], meta,
            nocookie=YOUTUBE_NOCOOKIE, click_to_play=YOUTUBE_CLICK_TO_PLAY,
            start=0, origin=(YOUTUBE_SITE_ORIGIN or "").strip().rstrip("/"),
            embed_player=YOUTUBE_EMBED_PLAYER,
        )
        context = (
            f'Found and now playing "{top["title"]}"'
            + (f' by {top["channel"]}' if top["channel"] else "")
            + f'. The player is already embedded and visible to the user - do not '
              f'describe it and do not list other results. IMPORTANT: end your '
              f'reply with this markdown link on its own line, exactly as '
              f'written: [{top["title"]}]({_watch_url(top["id"])})'
        )
        return (
            HTMLResponse(content=player, media_type="text/html",
                         headers={"content-disposition": "inline"}),
            context,
            {"video": {"id": top["id"], "start": 0},
             "link": {"url": _watch_url(top["id"]), "label": top["title"]}},
        )

    listing = "; ".join(
        f'{i}. {v["title"]}' + (f' ({v["channel"]})' if v["channel"] else "")
        for i, v in enumerate(results, 1)
    )
    context = (
        f'Showed {len(results)} YouTube results for "{q}": {listing}. The list is '
        f'already visible and each item plays inline when the user taps it. '
        f'Briefly tell them to pick one. Do not list the results again and do '
        f'not paste URLs.'
    )
    return (
        HTMLResponse(content=_build_results_card(results, q), media_type="text/html",
                     headers={"content-disposition": "inline"}),
        context,
        {"results": results, "query": q},
    )
