"""
Live TV — an HLS player card backed by the user-editable channel library.

Ported from Hermes's tools/live_tv.py. The player shape and channel-switching
are theirs; three things differ, each forced by our sandbox or our storage:

  * hls.js is served from OUR origin, pinned, not `@latest` from a CDN. See
    frontend/public/vendor/README.md for the version and how to update it.
    That keeps the video CSP profile's `script-src` first-party.
  * the card declares the CSP PROFILE NAME "video"; the policy itself lives in
    frontend/src/lib/embedWidget.ts. A tool cannot author its own CSP, or the
    sandbox would be advisory.
  * channels come from the media_sources table (global + per-user), not a JSON
    file, so the library survives a device change like every other preference.

What the video profile grants that the strict default does not: `script-src`
our origin (hls.js), `connect-src https:` (hls.js XHRs the manifest and
segments) and `media-src blob:` (MediaSource). Those are unavoidable for HLS and
are exactly why a news or weather card must not inherit them.
"""

from __future__ import annotations

import html as _html
import json as _json
import logging

from fastapi.responses import HTMLResponse

from config.settings import APP_PUBLIC_ORIGIN

log = logging.getLogger("aria.livetv")

# The vendored player. Absolute on purpose: a srcdoc frame has an opaque origin
# and no base URL, so a relative src cannot resolve.
HLS_JS_SRC = f"{APP_PUBLIC_ORIGIN}/vendor/hls.min.js"


def _js(obj) -> str:
    """JSON for a <script> block — escaped so a channel name containing
    "</script>" cannot terminate the element."""
    return (_json.dumps(obj).replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("&", "\\u0026"))


def _build_player(channels: list[dict], initial: int) -> str:
    esc = _html.escape
    rows = "".join(
        f'<button class="ch{" on" if i == initial else ""}" type="button" data-i="{i}">'
        f'<span class="nm">{esc(c["name"])}</span>'
        f'<span class="cat">{esc(c.get("category") or "")}</span></button>'
        for i, c in enumerate(channels)
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:8px; background:transparent; color:#E6EBF5;
         font:13px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
  @media (prefers-color-scheme: light) {{ body {{ color:#1f2328; }} }}
  .stage {{ position:relative; width:100%; aspect-ratio:16/9; background:#000;
            border-radius:12px; overflow:hidden; }}
  video {{ position:absolute; inset:0; width:100%; height:100%; }}
  .msg {{ position:absolute; inset:0; display:flex; align-items:center;
          justify-content:center; text-align:center; padding:12px; font-size:12px;
          opacity:.85; }}
  .now {{ margin:8px 2px 6px; font-size:12px; font-weight:600; }}
  .list {{ display:flex; flex-wrap:wrap; gap:6px; }}
  .ch {{ display:flex; flex-direction:column; align-items:flex-start; gap:2px;
         background:rgba(127,127,127,.10); border:1px solid rgba(128,128,128,.18);
         border-radius:9px; padding:6px 9px; color:inherit; font:inherit;
         cursor:pointer; }}
  .ch:hover {{ background:rgba(127,127,127,.20); }}
  .ch.on {{ background:rgba(56,219,255,.16); border-color:rgba(56,219,255,.35); }}
  .nm {{ font-size:12px; font-weight:600; }}
  .cat {{ font-size:10px; opacity:.6; text-transform:uppercase; }}
</style></head>
<body>
  <div class="stage"><video id="v" controls playsinline></video>
    <div class="msg" id="msg">Loading…</div></div>
  <div class="now" id="now"></div>
  <div class="list">{rows}</div>
<script src="{HLS_JS_SRC}"></script>
<script>
(function () {{
  var CH = {_js(channels)};
  var v = document.getElementById('v');
  var msg = document.getElementById('msg');
  var now = document.getElementById('now');
  var hls = null;

  function say(t) {{ msg.textContent = t || ''; msg.style.display = t ? 'flex' : 'none'; }}

  function play(i) {{
    var c = CH[i];
    if (!c) return;
    var btns = document.querySelectorAll('.ch');
    for (var n = 0; n < btns.length; n++) btns[n].classList.toggle('on', n === i);
    now.textContent = c.name;
    say('Loading ' + c.name + '…');
    if (hls) {{ try {{ hls.destroy(); }} catch (e) {{}} hls = null; }}

    if (window.Hls && window.Hls.isSupported()) {{
      hls = new Hls({{ enableWorker: false }});
      hls.loadSource(c.url);
      hls.attachMedia(v);
      hls.on(Hls.Events.MANIFEST_PARSED, function () {{
        say(''); report('manifest', c.name); v.play().catch(function () {{}});
      }});
      hls.on(Hls.Events.ERROR, function (_e, d) {{
        if (d && d.fatal) {{ say('Could not play ' + c.name + ' (' + d.details + ')');
                             report('error', d.details); }}
      }});
    }} else if (v.canPlayType('application/vnd.apple.mpegurl')) {{
      v.src = c.url; say(''); report('native', c.name); v.play().catch(function () {{}});
    }} else {{
      say('This browser cannot play HLS.'); report('unsupported', '');
    }}
    setTimeout(reportHeight, 60);
  }}

  // Status is posted out for diagnostics; the host ignores anything it does not
  // recognise, so this is safe to send unconditionally.
  function report(stage, detail) {{
    try {{ parent.postMessage({{ type: 'livetv', stage: stage, detail: detail }}, '*'); }}
    catch (e) {{}}
  }}

  var btns = document.querySelectorAll('.ch');
  for (var i = 0; i < btns.length; i++) {{
    (function (idx) {{
      btns[idx].addEventListener('click', function () {{ play(idx); }});
    }})(i);
  }}

  function reportHeight() {{
    try {{
      parent.postMessage({{ type: 'iframe:height',
        height: Math.ceil(document.documentElement.getBoundingClientRect().height) }}, '*');
    }} catch (e) {{}}
  }}
  window.addEventListener('load', reportHeight);
  window.addEventListener('resize', reportHeight);
  if (window.ResizeObserver) {{ new ResizeObserver(reportHeight).observe(document.body); }}
  [50, 300, 900].forEach(function (d) {{ setTimeout(reportHeight, d); }});

  play({initial});
}})();
</script>
</body></html>"""


def _match(channels: list[dict], name: str) -> int:
    q = (name or "").strip().lower()
    if not q:
        return 0
    for i, c in enumerate(channels):
        if c["name"].lower() == q:
            return i
    for i, c in enumerate(channels):
        if q in c["name"].lower():
            return i
    return -1


def watch_live_tv(user_id: str, channel: str = ""):
    """Open the player, on `channel` if named. Sync: dispatch runs off the loop."""
    from backend.services import media_sources

    channels = media_sources.list_sources(user_id, "tv")
    if not channels:
        # Seed on first use rather than at startup: nothing should depend on a
        # migration having run before the first question.
        media_sources.seed_defaults()
        channels = media_sources.list_sources(user_id, "tv")
    if not channels:
        return ("No TV channels are configured yet, and the channel library is "
                "unavailable. Ask an administrator to check the database.")

    idx = _match(channels, channel)
    if idx < 0:
        names = ", ".join(c["name"] for c in channels)
        return (f"I don't have a channel called “{channel}”. Available: {names}. "
                f"You can add one by giving me its name and .m3u8 URL.")

    slim = [{"name": c["name"], "url": c["url"], "category": c.get("category") or ""}
            for c in channels]
    context = (
        f"Opened the live TV player on {channels[idx]['name']}. The player and the "
        f"full channel list ({', '.join(c['name'] for c in channels)}) are already "
        f"visible and the user can switch channels by tapping one — do not list them "
        f"again and do not paste URLs. Just say it is playing."
    )
    return (
        HTMLResponse(content=_build_player(slim, idx), media_type="text/html",
                     headers={"content-disposition": "inline"}),
        context,
        # NAME only. The policy lives in frontend/src/lib/embedWidget.ts.
        {"csp": "video", "channel": channels[idx]["name"], "count": len(channels)},
    )


def add_tv_channel(user_id: str, name: str, url: str, category: str = "general") -> str:
    from backend.services import media_sources
    ok, message = media_sources.add_source(user_id, name, url, "tv", category)
    return message if ok else f"⚠️ {message}"


def remove_tv_channel(user_id: str, name: str) -> str:
    from backend.services import media_sources
    ok, message = media_sources.remove_source(user_id, name, "tv")
    return message if ok else f"⚠️ {message}"


# ── Directory search & bulk add ──────────────────────────────────────────────

# Shown per search. `sports` alone matches hundreds, so the cap is what keeps a
# search to one validation wave. Over-fetch is larger because roughly half of
# any directory sample is dead.
SHOW_LIMIT = 12
PROBE_LIMIT = 20

# A select-all on a large category must not add hundreds of rows in one turn.
BULK_ADD_CAP = 25


def search_tv_channels(user_id: str, name: str = "", category: str = "",
                       country: str = ""):
    """Find addable channels in the directory. Only LIVE ones are shown.

    Validation happens before the results are rendered, not after you pick: a
    directory sample is about half dead, so an unvalidated list would mostly
    offer channels that cannot play.
    """
    from backend.services import media_directory, media_sources

    if not any((name, category, country)):
        return ("Tell me what to look for — a channel name, a category "
                "(news, sports, movies, music), or a country.")

    candidates = media_directory.search(name=name, category=category,
                                        country=country, limit=PROBE_LIMIT)
    if not candidates:
        return (f"Nothing in the directory matches that. Try a broader term, or "
                f"give me a channel name and its .m3u8 URL directly.")

    # Skip anything already in the library — offering a duplicate wastes a slot.
    have = {c["url"] for c in media_sources.list_sources(user_id, "tv")}
    candidates = [c for c in candidates if c["url"] not in have]
    if not candidates:
        return "Everything matching that is already in your channel list."

    live = media_sources.validate_many([c["url"] for c in candidates])
    results = [c for c in candidates if live.get(c["url"])][:SHOW_LIMIT]
    st = media_directory.status()

    if not results:
        # The staleness note belongs here MORE than on the success path: if the
        # index is old, "nothing is responding" may say more about the index
        # than about the channels.
        return (f"Found {len(candidates)} matching channel(s) in the directory, but "
                f"none of their streams are responding right now. Directory entries "
                f"go dead often — try another search or a different term."
                + _staleness_note(st))
    rows = [{"id": c["ext_id"], "name": c["name"], "url": c["url"],
             "country": c.get("country") or "",
             "category": (c.get("categories") or [""])[0],
             "quality": c.get("quality") or ""} for c in results]

    listing = "; ".join(f"{r['name']} ({r['country']})" for r in rows)
    context = (
        f"Found {len(rows)} live channel(s) matching that: {listing}. The list is "
        f"already visible with checkboxes — tell the user to tick the ones they want "
        f"and press Add. Do not list them again and do not paste URLs. Every one "
        f"shown has been checked and is responding right now."
    )
    # Reaches the model so it can say so, rather than presenting a possibly
    # months-old index as current.
    context += _staleness_note(st)

    return (
        HTMLResponse(content=_build_channel_list(rows), media_type="text/html",
                     headers={"content-disposition": "inline"}),
        context,
        {"channels": rows, "query": name or category or country},
    )


def _staleness_note(st: dict) -> str:
    """The line that tells the model the directory may be out of date.

    Appended to EVERY search outcome, not just the successful one — see the
    no-results branch for why.
    """
    if not st.get("stale"):
        return ""
    age = st.get("age_days")
    return (f" NOTE: the channel directory was last refreshed "
            f"{age if age is not None else 'an unknown number of'} days ago and may be "
            f"out of date — mention this to the user.")


def _build_channel_list(rows: list[dict]) -> str:
    """Inert fallback for a client that cannot render the React picker.

    No checkboxes and no links: selection happens in React, outside the sandbox,
    because acting on a choice made inside the frame would need a channel back
    out of it that we deliberately do not have.
    """
    esc = _html.escape
    items = "".join(
        f'<div class="r"><span class="n">{esc(r["name"])}</span>'
        f'<span class="m">{esc(r["country"])} · {esc(r["category"])}</span></div>'
        for r in rows
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; padding:8px; background:transparent; color:#E6EBF5;
         font:13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
  @media (prefers-color-scheme: light) {{ body {{ color:#1f2328; }} }}
  .r {{ display:flex; justify-content:space-between; gap:10px; padding:6px 8px;
        border-radius:8px; background:rgba(127,127,127,.10); margin-bottom:5px; }}
  .n {{ font-weight:600; }} .m {{ opacity:.6; font-size:11px; }}
</style></head><body>{items}
<script>
(function () {{
  function h() {{ try {{ parent.postMessage({{type:'iframe:height',
    height: Math.ceil(document.documentElement.getBoundingClientRect().height)}}, '*'); }} catch(e) {{}} }}
  window.addEventListener('load', h); window.addEventListener('resize', h);
  if (window.ResizeObserver) {{ new ResizeObserver(h).observe(document.body); }}
  [50,300,900].forEach(function(d){{ setTimeout(h,d); }});
}})();
</script></body></html>"""


def add_tv_channels_bulk(user_id: str, names: list[str]) -> str:
    """Add several directory channels at once, re-validating each.

    The picker is the confirmation — the user selected these rows — so there is
    no second approval gate. The cap is what stops a select-all on a large
    category becoming hundreds of rows in one turn.
    """
    from backend.services import media_directory, media_sources

    wanted = [n.strip() for n in (names or []) if n and n.strip()][:BULK_ADD_CAP]
    if not wanted:
        return "⚠️ Tell me which channels to add."

    added, failed = [], []
    for n in wanted:
        hits = media_directory.search(name=n, limit=1)
        if not hits:
            failed.append(f"{n} (not in the directory)")
            continue
        c = hits[0]
        # Full-timeout re-validation: the search probe may be minutes old, and
        # NOTHING enters the library unvalidated.
        ok, message = media_sources.add_source(
            user_id, c["name"], c["url"], "tv",
            (c.get("categories") or ["general"])[0])
        (added if ok else failed).append(c["name"] if ok else f"{c['name']} ({message})")

    parts = []
    if added:
        parts.append(f"Added {len(added)}: {', '.join(added)}.")
    if failed:
        parts.append(f"Could not add {len(failed)}: {'; '.join(failed)}.")
    if len(names or []) > BULK_ADD_CAP:
        parts.append(f"(Capped at {BULK_ADD_CAP} per request.)")
    return " ".join(parts) or "Nothing was added."
