"""
Weather — current conditions and a 5-day outlook, rendered as a chat widget.

Ported from Hermes's tools/weather.py. The Open-Meteo calls, the WMO code table
and the card markup are theirs; the adaptations are the ones our pipeline needs:

  * returns ``(HTMLResponse, context, meta)`` instead of their embed envelope, so
    backend.tool_result splits it the same way every other widget-producing tool
    here does;
  * sync, because our dispatcher already runs in a worker thread — an async
    client plus the loop bridge would add machinery and no concurrency;
  * a null location asks which city rather than erroring. Their version returns
    "location is required", which would be a dead end here: our spec explicitly
    tells the model to leave the parameter null when the user names no place, so
    the null case is the designed path, not a mistake.

Open-Meteo for both geocoding and forecast: no API key, no quota, no account —
the same constraint the YouTube tools work under.

The card carries its own CSP meta tag. Ours is injected FIRST by
lib/embedWidget.ts and therefore wins (first CSP meta wins per spec); the card
needs only `style-src`/`script-src 'unsafe-inline'`, both of which our policy
already grants, so it renders unchanged inside the sandbox.
"""

from __future__ import annotations

import html as _html
import logging
from typing import Any

from fastapi.responses import HTMLResponse

from config.settings import WEATHER_DEFAULT_LOCATION

log = logging.getLogger("aria.weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 20.0

# WMO weather interpretation codes → (label, emoji). Open-Meteo returns the raw
# code; without this the card would show a bare integer.
WMO = {
    0: ("Clear sky", "☀️"), 1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"), 45: ("Fog", "🌫️"), 48: ("Rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Dense drizzle", "🌧️"),
    61: ("Light rain", "🌦️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"),
    71: ("Light snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"), 80: ("Rain showers", "🌦️"),
    81: ("Heavy showers", "🌧️"), 82: ("Violent showers", "⛈️"),
    85: ("Snow showers", "🌨️"), 86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm, hail", "⛈️"),
    99: ("Thunderstorm, heavy hail", "⛈️"),
}


def _describe(code: Any) -> tuple:
    try:
        return WMO.get(int(code), ("Unknown", "🌡️"))
    except (TypeError, ValueError):
        return ("Unknown", "🌡️")


def _card(geo: dict, cur: dict, daily: dict, units: str,
          used_default: bool = False) -> str:
    """The widget. Self-contained: no external CSS, fonts or scripts.

    The postMessage height handshake is the one our renderEmbed listens for
    (`type: 'iframe:height'`), and the reported value is clamped on our side.
    """
    esc = _html.escape
    label, icon = _describe(cur.get("weather_code"))
    deg = "°C" if units == "metric" else "°F"
    speed = "km/h" if units == "metric" else "mph"

    place = ", ".join(
        p for p in (geo.get("name"), geo.get("admin1"), geo.get("country")) if p
    )
    # Visible, not silent: a default the user cannot see is a wrong answer they
    # have no reason to question.
    default_note = ('<div class="dflt">Default location — name a city to change it</div>'
                    if used_default else "")

    days = []
    times = (daily or {}).get("time") or []
    for i in range(min(5, len(times))):
        d_label, d_icon = _describe((daily.get("weather_code") or [None] * 5)[i])
        hi = (daily.get("temperature_2m_max") or [None] * 5)[i]
        lo = (daily.get("temperature_2m_min") or [None] * 5)[i]
        days.append(
            f'<div class="d"><div class="dn">{esc(str(times[i])[5:])}</div>'
            f'<div class="di" title="{esc(d_label)}">{d_icon}</div>'
            f'<div class="dt"><b>{"—" if hi is None else round(hi)}°</b>'
            f'<span>{"—" if lo is None else round(lo)}°</span></div></div>'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:16px; font:14px/1.45 ui-sans-serif,system-ui,-apple-system,
         "Segoe UI",Roboto,sans-serif; color:#e8eaed; background:transparent; }}
  @media (prefers-color-scheme: light) {{ body {{ color:#1f2328; }} }}
  .card {{ border:1px solid rgba(128,128,128,.25); border-radius:14px; padding:16px;
           background:linear-gradient(160deg,rgba(80,140,255,.10),rgba(80,140,255,.02)); }}
  .top {{ display:flex; align-items:center; gap:14px; }}
  .ico {{ font-size:44px; line-height:1; }}
  .now {{ font-size:34px; font-weight:600; letter-spacing:-.02em; }}
  .place {{ font-size:13px; opacity:.75; margin-top:2px; }}
  .cond {{ font-size:13px; opacity:.9; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(84px,1fr));
           gap:8px; margin-top:14px; }}
  .m {{ border:1px solid rgba(128,128,128,.2); border-radius:10px; padding:8px 10px; }}
  .ml {{ font-size:11px; opacity:.65; }}
  .mv {{ font-size:15px; font-weight:600; margin-top:2px; }}
  .days {{ display:flex; gap:8px; margin-top:14px; overflow-x:auto; }}
  .d {{ flex:1 0 62px; text-align:center; border:1px solid rgba(128,128,128,.2);
        border-radius:10px; padding:8px 4px; }}
  .dn {{ font-size:11px; opacity:.65; }} .di {{ font-size:20px; margin:4px 0; }}
  .dt b {{ font-size:13px; }} .dt span {{ font-size:12px; opacity:.6; margin-left:4px; }}
  .dflt {{ margin-top:10px; font-size:11px; opacity:.6; border-top:1px solid
           rgba(128,128,128,.2); padding-top:8px; }}
</style></head>
<body>
  <div class="card">
    <div class="top">
      <div class="ico">{icon}</div>
      <div style="min-width:0">
        <div class="now">{round(cur.get("temperature_2m", 0))}{deg}</div>
        <div class="cond">{esc(label)}</div>
        <div class="place">{esc(place)}</div>
      </div>
    </div>
    <div class="grid">
      <div class="m"><div class="ml">Feels like</div>
        <div class="mv">{round(cur.get("apparent_temperature", 0))}{deg}</div></div>
      <div class="m"><div class="ml">Humidity</div>
        <div class="mv">{cur.get("relative_humidity_2m", "—")}%</div></div>
      <div class="m"><div class="ml">Wind</div>
        <div class="mv">{round(cur.get("wind_speed_10m", 0))} {speed}</div></div>
      <div class="m"><div class="ml">Precip.</div>
        <div class="mv">{cur.get("precipitation", 0)} mm</div></div>
    </div>
    <div class="days">{"".join(days)}</div>
    {default_note}
  </div>
<script>
  (function () {{
    function send() {{
      try {{
        parent.postMessage(
          {{ type: 'iframe:height', height: document.body.scrollHeight }}, '*');
      }} catch (e) {{}}
    }}
    window.addEventListener('load', send);
    window.addEventListener('resize', send);
    // Added on top of Hermes's version: it sends on `load` plus a 50ms timer,
    // and that early one fires before layout with scrollHeight 0. The host
    // ignores non-positive heights, so a zero is harmless — but a card that
    // only ever reported once would stay at its initial size if that report
    // were the zero. Re-measuring on any body resize makes it self-correcting.
    if (window.ResizeObserver) {{ new ResizeObserver(send).observe(document.body); }}
    [50, 300, 900].forEach(function (d) {{ setTimeout(send, d); }});
  }})();
</script>
</body></html>"""


def get_weather(location: str = "", units: str = "metric"):
    """Current conditions plus a 5-day outlook.

    Returns ``(HTMLResponse, context, meta)`` on success, or a plain string when
    there is nothing to render — including the null-location case, which asks
    the user rather than guessing.

    Sync on purpose: the tool dispatcher already runs in a worker thread.
    """
    place_q = (location or "").strip()
    used_default = False
    if not place_q:
        # The spec tells the model to leave this null when the user named no
        # place, so this is the designed path — not a mistake to error on.
        #
        # The fallback is an EXPLICIT, configured city and nothing else. It is
        # never derived from the deployment timezone or the request IP: those
        # are precisely the system-context inferences the parameter description
        # forbids, and they return the wrong city, silently, for anyone
        # travelling or simply not in the configured place.
        if not WEATHER_DEFAULT_LOCATION:
            return ("Which city should I check the weather for? "
                    "(Ask the user — do not guess a location.)")
        place_q = WEATHER_DEFAULT_LOCATION
        used_default = True

    units = "imperial" if str(units).lower().startswith("imp") else "metric"

    try:
        import httpx
        with httpx.Client(timeout=TIMEOUT) as c:
            # `language` affects the RESULT labels, not the query — an Arabic
            # place name still matches.
            g = c.get(GEOCODE_URL, params={"name": place_q, "count": 1,
                                           "language": "en", "format": "json"})
            g.raise_for_status()
            results = (g.json() or {}).get("results") or []
            if not results:
                return f"No location found matching “{place_q}”."
            geo = results[0]

            params = {
                "latitude": geo["latitude"], "longitude": geo["longitude"],
                "current": ("temperature_2m,apparent_temperature,relative_humidity_2m,"
                            "precipitation,weather_code,wind_speed_10m"),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "timezone": "auto", "forecast_days": 5,
            }
            if units == "imperial":
                params["temperature_unit"] = "fahrenheit"
                params["wind_speed_unit"] = "mph"
            f = c.get(FORECAST_URL, params=params)
            f.raise_for_status()
            data = f.json() or {}
    except Exception as exc:
        log.warning("weather lookup failed for %r: %s", place_q, exc)
        return f"⚠️ Weather lookup failed: {type(exc).__name__}: {exc}"

    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    label, _ = _describe(cur.get("weather_code"))
    deg = "°C" if units == "metric" else "°F"
    place = ", ".join(p for p in (geo.get("name"), geo.get("country")) if p)

    # The model gets facts it can reason about and quote; the card is for the
    # human. Deliberately NOT a description of the widget.
    context = (
        f"Weather for {place}: {round(cur.get('temperature_2m', 0))}{deg}, {label.lower()}, "
        f"feels like {round(cur.get('apparent_temperature', 0))}{deg}, "
        f"humidity {cur.get('relative_humidity_2m', '—')}%, "
        f"wind {round(cur.get('wind_speed_10m', 0))} "
        f"{'km/h' if units == 'metric' else 'mph'}. The card is already visible to "
        f"the user — answer their question from these figures, do not describe it."
    )
    if used_default:
        # Say the assumption out loud in the text too, not just on the card: a
        # default the user does not notice is a wrong answer they never question.
        context += (f" NOTE: the user named no city, so the configured default "
                    f"({WEATHER_DEFAULT_LOCATION}) was used — tell them which city "
                    f"this is for so they can correct it.")
    return (
        HTMLResponse(content=_card(geo, cur, daily, units, used_default),
                     media_type="text/html",
                     headers={"content-disposition": "inline"}),
        context,
        {"place": place, "units": units, "used_default": used_default},
    )
