"""Consumer capabilities for Runtime B — YouTube, live TV, radio, weather, news, QR.

These are the 19 real capabilities Runtime A had and Runtime B did not (the other
five "missing" tools are aliases of tools Runtime B already has — see
docs/runtime-tool-reconciliation.md). They are the largest share of live traffic:
`play_youtube_video` alone accounts for 1,975 recorded calls.

THIS IS NOT A COPY OF backend/tools.py. Three things are deliberately different.

1. **The service layer is the shared implementation, not the dispatcher.**
   Every handler below calls `backend/services/{youtube,livetv,radio,weather,news,qr}`
   directly — the same functions Runtime A's dispatcher calls. Nothing is
   reimplemented, so the two runtimes cannot drift on behaviour, and the fixes
   already baked into those services (yt-dlp fallbacks, station dedup, the radio
   directory cache) are inherited rather than re-derived.

2. **Nine radio tools become four, four TV tools become three.**
   Runtime A exposes `uae_radio`, `arabic_radio`, `quran_radio`, `radio_by_genre`
   and `search_radio` as five separate tools that all mean "play a station matching
   X". Handing a model five near-identical schemas measurably degrades selection,
   and it is exactly the kind of thing a migration should fix rather than carry
   forward. They collapse into `play_radio` with a selector. Same reasoning merges
   `add_tv_channel` + `add_tv_channels_bulk`.

3. **Widget HTML never reaches the model.**
   Several of these tools return `(HTMLResponse, context)` so the frontend can
   render a player. Runtime B's executor stringifies whatever a handler returns and
   feeds it to the LLM, which would put hundreds of lines of markup into the
   context window. Each handler therefore runs its result through
   `tool_result.process_tool_result` — the same splitter Runtime A uses — and hands
   the model only the short context string. The HTML travels out of band on a
   per-turn sink (`embeds`), which the SSE layer drains.

Authorization is unchanged and is not re-implemented here: every one of these tools
goes through `orchestrator/authz.authorize_call` in `graph._tools_node` like any
other. This module only declares tools; it never decides whether one may run.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

from .registry import Tool, register

log = logging.getLogger("aganeti.orchestrator.consumer")


# ── per-turn embed sink ───────────────────────────────────────────────────────
# A ContextVar rather than a return value: the executor's handler contract is
# `-> str`, and widening it would touch every tool and every caller for the sake
# of nine media tools. The sink is bound per turn by the SSE layer, so two
# concurrent turns cannot see each other's embeds.
_embeds: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "aganeti_tool_embeds", default=None)


def bind_embed_sink(sink: list | None = None) -> tuple[list, Any]:
    """Start collecting embeds for this turn. Returns (sink, reset_token)."""
    sink = [] if sink is None else sink
    return sink, _embeds.set(sink)


def reset_embed_sink(token) -> None:
    try:
        _embeds.reset(token)
    except (ValueError, LookupError):  # different context — nothing to reset
        pass


def current_embeds() -> list:
    return _embeds.get() or []


def _emit(raw: Any, tool_name: str) -> str:
    """Split a service result into (text for the model, embeds for the browser).

    Returns the text. Embeds are appended to the per-turn sink; when no sink is
    bound (a non-SSE caller, or a test) they are dropped rather than raising — a
    missing player must not fail the turn.
    """
    from backend.tool_result import process_tool_result
    processed = process_tool_result(tool_name, raw)
    if processed.embeds:
        sink = _embeds.get()
        if sink is None:
            log.debug("%s produced %d embed(s) with no sink bound; dropping",
                      tool_name, len(processed.embeds))
        else:
            sink.extend(processed.embeds)
    return str(processed.llm_result)


async def _call(fn, *args, tool_name: str, **kwargs) -> str:
    """Run a service function (sync or async) and normalise its result.

    The sync ones block — yt-dlp shells out, the radio directory does HTTP — so
    they go to a thread. Doing that inside the handler rather than at the call site
    keeps the executor's loop free without every caller having to remember.
    """
    if asyncio.iscoroutinefunction(fn):
        raw = await fn(*args, **kwargs)
    else:
        raw = await asyncio.to_thread(fn, *args, **kwargs)
    return _emit(raw, tool_name)


def _obj(props: dict, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


# ══════════════════════════════════════════════════════════════════════════════
# YouTube
# ══════════════════════════════════════════════════════════════════════════════
async def _play_youtube_video(ctx, video: str) -> str:
    if not (video or "").strip():
        return "Which YouTube video? Give me a link or an 11-character video ID."
    from backend.services import youtube
    return await _call(youtube.play_youtube_video, video.strip(),
                       tool_name="play_youtube_video")


async def _get_youtube_video_info(ctx, video: str) -> str:
    if not (video or "").strip():
        return "Which YouTube video? Give me a link or an 11-character video ID."
    from backend.services import youtube
    return await _call(youtube.get_youtube_video_info, video.strip(),
                       tool_name="get_youtube_video_info")


async def _search_youtube(ctx, query: str, mode: str = "play", max_results: int = 5) -> str:
    from backend.services import youtube
    try:
        limit = max(1, min(int(max_results or 5), 10))
    except (TypeError, ValueError):
        limit = 5
    return await _call(youtube.search_youtube, query, mode=(mode or "play"), limit=limit,
                       tool_name="search_youtube")


# ══════════════════════════════════════════════════════════════════════════════
# Live TV
# ══════════════════════════════════════════════════════════════════════════════
async def _watch_live_tv(ctx, channel: str = "") -> str:
    from backend.services import livetv
    return await _call(livetv.watch_live_tv, ctx["user_id"], channel or "",
                       tool_name="watch_live_tv")


async def _search_tv_channels(ctx, name: str = "", category: str = "", country: str = "") -> str:
    from backend.services import livetv
    return await _call(livetv.search_tv_channels, ctx["user_id"], name or "",
                       category or "", country or "", tool_name="search_tv_channels")


async def _add_tv_channels(ctx, names: list | None = None, name: str = "",
                           url: str = "", category: str = "general") -> str:
    """One tool for both shapes Runtime A split across `add_tv_channel` and
    `add_tv_channels_bulk`: a list of directory names, or a single explicit
    name+url. The model picks by which argument it can fill, not by remembering
    that two tools exist."""
    from backend.services import livetv
    if names:
        picked = [n for n in names if isinstance(n, str) and n.strip()]
        if not picked:
            return "No channel names given."
        return await _call(livetv.add_tv_channels_bulk, ctx["user_id"], picked,
                           tool_name="add_tv_channels")
    if name and url:
        return await _call(livetv.add_tv_channel, ctx["user_id"], name, url,
                           category or "general", tool_name="add_tv_channels")
    return ("Give me either `names` (channels from search_tv_channels) or both "
            "`name` and `url` for a custom channel.")


# ══════════════════════════════════════════════════════════════════════════════
# Radio — five "play something" tools collapsed into one selector
# ══════════════════════════════════════════════════════════════════════════════
_RADIO_PRESETS = ("uae", "arabic", "quran", "mine")


async def _play_radio(ctx, preset: str = "", genre: str = "", query: str = "",
                      country_code: str = "") -> str:
    """Open a radio player. Exactly one selector is used, in the order below.

    Runtime A had `uae_radio`, `arabic_radio`, `quran_radio`, `radio_by_genre` and
    `search_radio` as separate tools. They are one capability with one argument.
    """
    from backend.services import radio
    preset = (preset or "").strip().lower()
    if preset == "uae":
        return await _call(radio.uae_radio, ctx["user_id"], tool_name="play_radio")
    if preset == "arabic":
        return await _call(radio.arabic_radio, country_code or "", tool_name="play_radio")
    if preset == "quran":
        return await _call(radio.quran_radio, tool_name="play_radio")
    if preset == "mine":
        return await _call(radio.my_radio, ctx["user_id"], tool_name="play_radio")
    if preset:
        return f"Unknown preset '{preset}'. Use one of: {', '.join(_RADIO_PRESETS)}."
    if genre:
        return await _call(radio.radio_by_genre, genre, tool_name="play_radio")
    if query:
        return await _call(radio.search_radio, query, tool_name="play_radio")
    return ("Which station? Give a `preset` (uae / arabic / quran / mine), a `genre`, "
            "or a `query` such as a station name.")


async def _search_radio_stations(ctx, name: str = "", country: str = "", genre: str = "") -> str:
    from backend.services import radio
    return await _call(radio.search_radio_stations, ctx["user_id"], name or "",
                       country or "", genre or "", tool_name="search_radio_stations")


async def _my_radio(ctx) -> str:
    from backend.services import radio
    return await _call(radio.my_radio, ctx["user_id"], tool_name="my_radio")


async def _manage_radio_stations(ctx, action: str, names: list | None = None,
                                 name: str = "") -> str:
    """Add or remove stations in the user's own library. One tool, because `add`
    and `remove` differ only by a verb and Runtime A's split gained nothing."""
    from backend.services import radio
    act = (action or "").strip().lower()
    if act == "add":
        picked = [n for n in (names or []) if isinstance(n, str) and n.strip()]
        if not picked:
            return "Which stations should I add? Give `names` from search_radio_stations."
        return await _call(radio.add_radio_stations_bulk, ctx["user_id"], picked,
                           tool_name="manage_radio_stations")
    if act == "remove":
        target = (name or "").strip() or next(
            (n for n in (names or []) if isinstance(n, str) and n.strip()), "")
        if not target:
            return "Which station should I remove? Give its `name`."
        return await _call(radio.remove_radio_station, ctx["user_id"], target,
                           tool_name="manage_radio_stations")
    return "action must be 'add' or 'remove'."


# ══════════════════════════════════════════════════════════════════════════════
# Weather / news / QR
# ══════════════════════════════════════════════════════════════════════════════
async def _get_weather(ctx, location: str = "", units: str = "metric") -> str:
    from backend.services import weather
    return await _call(weather.get_weather, location or "",
                       units=(units or "metric"), tool_name="get_weather")


async def _get_news(ctx, category: str = "world") -> str:
    from backend.services import news
    return await _call(news.get_news, category or "world", tool_name="get_news")


async def _generate_qr_code(ctx, content: str) -> str:
    if not (content or "").strip():
        return "What should the QR code contain? Give text, a URL, or contact details."
    from backend.services import qr
    return await _call(qr.generate_qr_code, content, tool_name="generate_qr_code")


# ══════════════════════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════════════════════
# Permission vocabulary. Read and write are separated per family so an operator can
# grant "play the radio" without granting "edit my station library" — the audit's
# complaint about `required_permission` was that it was decorative, so a new family
# of tools had better use it properly.
_registered = False


def register_consumer_tools() -> list[str]:
    """Idempotently register the consumer tools. Returns the names registered.

    Called from backend/orchestrator/__init__.py so any process that imports the
    orchestrator has them, which is what makes Runtime B's registry canonical.
    """
    global _registered
    if _registered:
        return _NAMES
    for t in _TOOLS:
        register(t)
    _registered = True
    return _NAMES


_TOOLS = [
    # ── YouTube ───────────────────────────────────────────────────────────────
    Tool("play_youtube_video",
         "Open a YouTube video in a player for the user. Accepts a YouTube URL or an "
         "11-character video ID. Use when the user asks to watch or play a specific video.",
         _obj({"video": {"type": "string",
                         "description": "YouTube URL or 11-character video ID"}}, ["video"]),
         _play_youtube_video, "media.youtube.read"),
    Tool("get_youtube_video_info",
         "Get the title, channel and duration of a YouTube video WITHOUT opening a player.",
         _obj({"video": {"type": "string"}}, ["video"]),
         _get_youtube_video_info, "media.youtube.read"),
    Tool("search_youtube",
         "Search YouTube. mode='play' opens the best match in a player; mode='list' "
         "returns titles and links for the user to choose from.",
         _obj({"query": {"type": "string"},
               "mode": {"type": "string", "enum": ["play", "list"]},
               "max_results": {"type": "integer"}}, ["query"]),
         _search_youtube, "media.youtube.read"),

    # ── Live TV ───────────────────────────────────────────────────────────────
    Tool("watch_live_tv",
         "Open a live TV channel in a player. Omit `channel` to open the user's default. "
         "Use search_tv_channels first if the channel is not already in their library.",
         _obj({"channel": {"type": "string"}}),
         _watch_live_tv, "media.tv.read"),
    Tool("search_tv_channels",
         "Search the live-TV directory for channels by name, category or country. "
         "Returns a list; nothing is saved and no player opens.",
         _obj({"name": {"type": "string"}, "category": {"type": "string"},
               "country": {"type": "string"}}),
         _search_tv_channels, "media.tv.read"),
    Tool("add_tv_channels",
         "Add channels to the user's TV library. Either `names` (from search_tv_channels) "
         "or a single custom channel via `name` + `url`.",
         _obj({"names": {"type": "array", "items": {"type": "string"}},
               "name": {"type": "string"}, "url": {"type": "string"},
               "category": {"type": "string"}}),
         _add_tv_channels, "media.tv.write"),

    # ── Radio ─────────────────────────────────────────────────────────────────
    Tool("play_radio",
         "Open a radio station in a player. Use `preset` for uae / arabic / quran / mine "
         "(the user's saved list), or `genre` (e.g. jazz), or `query` (a station name). "
         "Exactly one selector is needed.",
         _obj({"preset": {"type": "string", "enum": list(_RADIO_PRESETS)},
               "genre": {"type": "string"}, "query": {"type": "string"},
               "country_code": {"type": "string",
                                "description": "ISO-2 code, only with preset='arabic'"}}),
         _play_radio, "media.radio.read"),
    Tool("search_radio_stations",
         "Browse the radio directory by name, country or genre. Returns a list for the "
         "user to pick from; nothing is saved and no player opens.",
         _obj({"name": {"type": "string"}, "country": {"type": "string"},
               "genre": {"type": "string"}}),
         _search_radio_stations, "media.radio.read"),
    Tool("my_radio",
         "Open the user's own saved radio stations in a player.",
         _obj({}), _my_radio, "media.radio.read"),
    Tool("manage_radio_stations",
         "Add stations to, or remove one from, the user's saved radio list. "
         "action='add' takes `names` (from search_radio_stations); action='remove' takes `name`.",
         _obj({"action": {"type": "string", "enum": ["add", "remove"]},
               "names": {"type": "array", "items": {"type": "string"}},
               "name": {"type": "string"}}, ["action"]),
         _manage_radio_stations, "media.radio.write"),

    # ── Weather / news / QR ───────────────────────────────────────────────────
    Tool("get_weather",
         "Current weather and a short forecast for a place. Omit `location` for the "
         "user's own city.",
         _obj({"location": {"type": "string"},
               "units": {"type": "string", "enum": ["metric", "imperial"]}}),
         _get_weather, "web.weather"),
    Tool("get_news",
         "Recent headlines from public RSS feeds for a category "
         "(world, tech, business, sport, …).",
         _obj({"category": {"type": "string"}}),
         _get_news, "web.news"),
    Tool("generate_qr_code",
         "Turn text, a URL or contact details into a QR code image for the user. "
         "Runs entirely locally — nothing leaves the system.",
         _obj({"content": {"type": "string"}}, ["content"]),
         _generate_qr_code, "utility.qr"),
]

_NAMES = [t.name for t in _TOOLS]
