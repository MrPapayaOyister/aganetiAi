"""
Native function-calling for the chat assistant.

Replaces the brittle ``[ACTION:{json}]`` tail-tag protocol with OpenAI-style
``tools`` that llama.cpp (run with ``--jinja``) returns as structured
``tool_calls``. The actual side-effects reuse the already-tested
``backend.action_parser.execute_action`` dispatcher, so there is a single place
that talks to the task / email / meeting endpoints.

Toggled by ``NATIVE_TOOLS`` in config.settings; the legacy action-tag path
remains as a fallback.
"""

from __future__ import annotations

import importlib.util as _importlib_util
import json
import logging
import re

from backend.action_parser import execute_action
from backend.service_auth import internal_headers  # Phase 0: auth for internal self-calls

log = logging.getLogger("aria.tools")

# OpenAI tool schema — kept in lock-step with execute_action's action types.
TOOL_SCHEMAS = [
    {
        "group": "tasks",
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a to-do task for the user. Use whenever the user "
                           "asks to add/create/remember a task or action item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short task title"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"],
                                 "description": "Defaults to medium if unstated"},
                    "due": {"type": "string",
                            "description": "Due date as YYYY-MM-DD, or null if none"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "group": "tasks",
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark an existing task as done, matched by partial title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Keywords from the task title"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "group": "email",
        "type": "function",
        "function": {
            "name": "draft_email",
            "description": "Draft an email AND save it to the user's approval queue. "
                           "ALWAYS call this when the user asks to draft/write/send an "
                           "email and a recipient (or a clear topic) is known — do NOT "
                           "just write the email text in chat. Fill the body with your "
                           "best draft (use placeholders for unknown details); the user "
                           "reviews and sends it from the queue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to"],
            },
        },
    },
    {
        "group": "calendar",
        "type": "function",
        "function": {
            "name": "schedule_meeting",
            "description": "Schedule a meeting / calendar event. Requires both who to "
                           "meet with and a time. If the user provides an email address "
                           "directly, use it as-is — do NOT call resolve_contact first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "with": {"type": "string", "description": "Attendee name or email"},
                    "time": {"type": "string", "description": "ISO datetime or natural language"},
                    "title": {"type": "string"},
                },
                "required": ["with", "time"],
            },
        },
    },
    {
        "group": "knowledge",
        "type": "function",
        "function": {
            "name": "get_analytics",
            "description": "Answer a quantitative question about the user's own data "
                           "(task and email stats). Use for 'how many tasks did I finish "
                           "last week', 'what's on my plate', 'who do I email most'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": ["summary", "completed", "created",
                                 "pending_by_priority", "top_contacts", "triage_volume"],
                    },
                    "days": {"type": "integer", "description": "Look-back window (default 7)"},
                },
                "required": ["metric"],
            },
        },
    },
    {
        "group": "contacts",
        "type": "function",
        "function": {
            "name": "resolve_contact",
            "description": "Look up a saved contact by NAME to get their email address. "
                           "Call this ONLY when you have a person's name but NOT their email. "
                           "Do NOT call this when the user has already given an email address. "
                           "Also useful when the user asks 'what is X's email?'.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Person's name or nickname"}},
                "required": ["name"],
            },
        },
    },
    {
        "group": "knowledge",
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Search company policy/handbook documents (the knowledge vault) "
                           "to answer questions about company rules, processes, or facts.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "group": "memory",
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search the user's long-term memory for facts learned in past "
                           "conversations (preferences, people, commitments).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "group": "memory",
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "Save a durable fact to the user's long-term memory when they say "
                           "'remember that ...' or state a lasting preference/commitment.",
            "parameters": {
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
        },
    },
    {
        "group": "tasks",
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a one-time Telegram reminder at a specific future time. "
                           "Use when the user says 'remind me to X at Y' or 'remind me in Z minutes'. "
                           "Do NOT use for recurring schedules — use set_reminder only for one-off alerts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message":   {"type": "string", "description": "What to remind the user about"},
                    "remind_at": {"type": "string",
                                  "description": "When to fire — ISO datetime, or natural language "
                                                 "like '4pm', 'tomorrow at 9am', 'in 30 minutes'"},
                },
                "required": ["message", "remind_at"],
            },
        },
    },
    {
        "group": "web",
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information, news, or facts the model "
                           "may not know. Use for questions about recent events, live data, "
                           "product specs, or any 'what is the latest ...' query. "
                           "Returns top results with titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer",
                                    "description": "Number of results (default 5, max 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "group": "email",
        "type": "function",
        "function": {
            "name": "get_emails",
            "description": "LIST the user's recent inbox messages (subject, sender, time, "
                           "short preview, read/unread). Use when the user asks about their "
                           "email, unread messages, or what's in their inbox. This returns "
                           "only a short preview of each — to read what an email actually "
                           "SAYS, call read_email.",
            "parameters": {
                "type": "object",
                "properties": {"max_results": {"type": "integer", "description": "Default 10"}},
            },
        },
    },
    {
        "group": "email",
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read the FULL text of one email. Use whenever the user wants the "
                           "contents rather than the list — 'read/open my latest email', "
                           "'what does the Jira one say', 'summarise the email from Aisha', "
                           "or any follow-up question about an email's contents. Do NOT answer "
                           "from a get_emails preview; call this to get the real body.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Keywords from the subject or the sender's name "
                                             "to pick which email. Omit to read the most "
                                             "recent one."},
                },
            },
        },
    },
    {
        "group": "calendar",
        "type": "function",
        "function": {
            "name": "get_agenda",
            "description": "Read the user's upcoming Google Calendar events (title, time, "
                           "attendees). Use for 'what's on my calendar', 'my agenda', "
                           "'next meeting'.",
            "parameters": {
                "type": "object",
                "properties": {"days_ahead": {"type": "integer", "description": "Default 1"}},
            },
        },
    },
    {
        "group": "contacts",
        "type": "function",
        "function": {
            "name": "get_contacts",
            "description": "Read or search the user's real Google contacts (name, email, "
                           "phone, company). Pass `query` to search; omit to list.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Optional search text"}},
            },
        },
    },
    {
        "group": "media",
        "type": "function",
        "function": {
            "name": "play_youtube_video",
            "description": "Play a YouTube video inline in the chat using an embedded "
                           "player. Use whenever the user shares a YouTube link or video "
                           "ID and wants to watch it, or asks you to play, show, or embed "
                           "a specific YouTube video. This tool does NOT search YouTube — "
                           "the user must supply the video.",
            "parameters": {
                "type": "object",
                "properties": {
                    "video": {"type": "string",
                              "description": "A YouTube URL (youtube.com/watch, youtu.be, "
                                             "/shorts/, /live/, /embed/) or a bare "
                                             "11-character video ID. Timestamps such as "
                                             "?t=90 or ?t=1m30s are honoured."},
                },
                "required": ["video"],
            },
        },
    },
    {
        "group": "media",
        "type": "function",
        "function": {
            "name": "get_youtube_video_info",
            "description": "Look up the title, channel, and thumbnail of a YouTube video "
                           "WITHOUT embedding a player. Use when the user wants to know "
                           "what a link is, or when you need the title to discuss a video. "
                           "If they want to watch it, call play_youtube_video instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "video": {"type": "string",
                              "description": "A YouTube URL or an 11-character video ID"},
                },
                "required": ["video"],
            },
        },
    },
    {
        "group": "news",
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Latest news headlines, rendered as a readable card in the "
                           "chat. Use when the user asks for news, headlines, what's "
                           "happening, or what's going on in the world/tech/business/"
                           "science. Do NOT use this to look up a specific story or a "
                           "past event — call web_search for that. Do NOT use it for "
                           "the user's own email or calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["world", "tech", "business", "science", "all"],
                        "description": "Which feed. 'all' fetches every category. "
                                       "Defaults to world when the user just says 'news'.",
                    },
                },
            },
        },
    },
    {
        "group": "livetv",
        "type": "function",
        "function": {
            "name": "watch_live_tv",
            "description": "Open the live TV player in the chat, optionally on a named "
                           "channel. Use when the user asks to watch live TV, put on a "
                           "news channel, or watch a specific channel by name (e.g. "
                           "'put on Al Jazeera', 'watch DW', 'show me live TV'). Do NOT "
                           "use this for a YouTube video or a recorded clip — call "
                           "play_youtube_video or search_youtube for those.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string",
                                "description": "Channel name if the user named one. "
                                               "Omit to open on the first channel."},
                },
            },
        },
    },
    {
        "group": "livetv",
        "type": "function",
        "function": {
            "name": "search_tv_channels",
            "description": "Find live TV channels in the public directory by name, "
                           "category or country — no URL needed. Use when the user asks "
                           "to add a channel by name ('add Al Arabiya'), for more "
                           "channels of a kind ('add some sports channels'), or by "
                           "region ('add UAE channels'). Combine filters when they say "
                           "both ('UAE news channels'). Only channels whose stream is "
                           "responding right now are shown. Do NOT use this to play a "
                           "channel already in their list — call watch_live_tv.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Channel name, if they named one"},
                    "category": {"type": "string",
                                 "description": "e.g. news, sports, movies, music, kids"},
                    "country": {"type": "string",
                                "description": "ISO 2-letter country code, e.g. AE, US, GB"},
                },
            },
        },
    },
    {
        "group": "livetv",
        "type": "function",
        "function": {
            "name": "add_tv_channels_bulk",
            "description": "Add channels the user picked from a directory search to "
                           "their library. Only call this with names they explicitly "
                           "chose — the picker is how they choose. Each stream is "
                           "re-checked before it is saved.",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"},
                              "description": "Channel names the user selected"},
                },
                "required": ["names"],
            },
        },
    },
    {
        "group": "livetv",
        "type": "function",
        "function": {
            "name": "add_tv_channel",
            "description": "Add a live TV channel to the user's library. Use when they "
                           "give a channel name AND an HLS stream URL (ending .m3u8). "
                           "The stream is checked before it is saved; a dead or "
                           "non-playable one is rejected with the reason.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "url": {"type": "string", "description": "HLS stream URL (.m3u8)"},
                    "category": {"type": "string", "description": "e.g. news, sport"},
                },
                "required": ["name", "url"],
            },
        },
    },
    # ── Radio (5 tools) ──────────────────────────────────────────────────────
    # The carve-outs between these are the whole design: uae_radio is the DEFAULT
    # for a bare "play radio", and search_radio must NOT fire on a general
    # request. That wording is the original's, kept because it is what the model
    # actually reads, and it is verified against the live model in
    # tests/test_radio_routing.py rather than assumed.
    {
        "group": "radio",
        "type": "function",
        "function": {
            "name": "search_radio_stations",
            "description": "Search the radio directory for stations to SAVE, when the "
                           "user has NOT named specific ones. Use for \"add some Saudi "
                           "stations\", \"I want to save UAE radio stations\", \"add "
                           "radio stations from Egypt\" — anything that asks to add or "
                           "save BY COUNTRY, GENRE, or a partial name. Shows a "
                           "tick-list; it plays nothing and saves nothing by itself. "
                           "If the message already lists exact station names, use "
                           "add_radio_stations_bulk instead. If the user wants to "
                           "LISTEN rather than save, use uae_radio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Station name to look for, Arabic or English."},
                    "country": {"type": "string",
                                "description": "Two-letter ISO code — AE, SA, EG, KW, "
                                               "QA, MA, LB. Use for a country-wise browse."},
                    "genre": {"type": "string",
                              "description": "news, quran, pop, classical, tarab."},
                },
            },
        },
    },
    {
        "group": "radio",
        "type": "function",
        "function": {
            "name": "add_radio_stations_bulk",
            # The FIRST sentence is the exact phrasing the picker sends
            # ("Add these radio stations: X, Y"). Without it the model read that
            # message as a fresh search and the ticked stations were never saved
            # — verified against the live model, which is the only way this class
            # of mistake shows up.
            "description": "Save specific NAMED stations to the user's station list. "
                           "Use whenever the message lists station names to add, "
                           "including the exact form \"Add these radio stations: X, Y\" "
                           "which the picker sends when the user ticks rows. Do NOT "
                           "search first — the names are already chosen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"},
                              "description": "Exact station names as shown in the picker."},
                },
                "required": ["names"],
            },
        },
    },
    {
        "group": "radio",
        "type": "function",
        "function": {
            "name": "remove_radio_station",
            "description": "Remove one station from the user's saved station list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Station name to remove."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "group": "radio",
        "type": "function",
        "function": {
            "name": "my_radio",
            "description": "Play ONLY the user's own saved stations - محطاتي. Use when "
                           "they ask for my stations, my radio list, or my saved "
                           "stations. For a plain \"play radio\" use uae_radio, which "
                           "already puts saved stations first.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "group": "radio",
        "type": "function",
        "function": {
            "name": "uae_radio",
            "description": "Play UAE radio stations - تشغيل الإذاعات الإماراتية. "
                           "USE THIS whenever the user asks to listen to radio, open "
                           "the radio, play a station, or mentions UAE / Emirates / "
                           "الإمارات radio without naming a specific station. This is "
                           "the DEFAULT radio action. Triggers: \"شغل الراديو\", "
                           "\"افتح الإذاعة\", \"play radio\", \"open radio\", "
                           "\"UAE radio\", \"Emirates radio\". Saved stations are "
                           "listed first automatically. Do NOT use when the user "
                           "wants to ADD or SAVE stations rather than listen — that "
                           "is search_radio_stations.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "group": "radio",
        "type": "function",
        "function": {
            "name": "arabic_radio",
            "description": "Play Arabic radio stations from any Arab country - إذاعات "
                           "عربية. USE THIS when the user asks for Arabic radio "
                           "generally, or for a specific Arab country OTHER than the "
                           "UAE. Triggers: \"إذاعات عربية\", \"راديو سعودي\", "
                           "\"Arabic radio\", \"Saudi radio\", \"Egyptian radio\".",
            "parameters": {
                "type": "object",
                "properties": {
                    "country_code": {
                        "type": "string",
                        "description": "ISO code - SA Saudi, EG Egypt, KW Kuwait, "
                                       "QA Qatar, BH Bahrain, OM Oman, JO Jordan, "
                                       "LB Lebanon, MA Morocco, TN Tunisia, DZ Algeria, "
                                       "IQ Iraq, SY Syria, YE Yemen, SD Sudan, LY Libya, "
                                       "PS Palestine. Leave empty for Arabic-language "
                                       "stations across all countries.",
                    },
                },
            },
        },
    },
    {
        "group": "radio",
        "type": "function",
        "function": {
            "name": "quran_radio",
            "description": "Play Holy Quran radio stations - إذاعات القرآن الكريم. "
                           "USE THIS when the user asks for Quran radio, Islamic "
                           "recitation, or religious stations. Triggers: \"إذاعة "
                           "القرآن الكريم\", \"قرآن\", \"تلاوة\", \"Quran radio\", "
                           "\"Islamic radio\", \"recitation\".",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "group": "radio",
        "type": "function",
        "function": {
            "name": "radio_by_genre",
            "description": "Play radio stations by genre or style - إذاعات حسب النوع. "
                           "USE THIS when the user asks for a music style or content "
                           "type. Triggers: \"إذاعة أخبار\", \"موسيقى كلاسيكية\", "
                           "\"طرب\", \"news radio\", \"classical\", \"jazz\", \"pop\".",
            "parameters": {
                "type": "object",
                "properties": {
                    "genre": {
                        "type": "string",
                        "description": "Genre or tag - news, classical, pop, jazz, "
                                       "arabic, khaleeji, tarab, oud, talk, sport.",
                    },
                },
                "required": ["genre"],
            },
        },
    },
    {
        "group": "radio",
        "type": "function",
        "function": {
            "name": "search_radio",
            "description": "Search for a specific radio station BY NAME - البحث عن محطة "
                           "إذاعية. USE THIS ONLY when the user names a specific "
                           "station. Triggers: \"شغل إذاعة أبوظبي\", \"افتح نور دبي\", "
                           "\"Emirates FM\", \"play Dubai Eye\". Do NOT use for "
                           "general or country requests - use uae_radio or "
                           "arabic_radio instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Station name, Arabic or English."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "group": "qr",
        "type": "function",
        "function": {
            "name": "generate_qr_code",
            # The exported description, extended with what the model needs and the
            # original did not say: that the card IS the delivery, so the reply
            # should not also spell the payload out. Without that, the model
            # helpfully pastes the URL underneath, which is the one thing a QR
            # code exists to avoid having to do.
            "description": "Creates a QR code for the given text, URL, or data and "
                           "displays it in the chat as a scannable image. Use when the "
                           "user asks to turn something into a QR code, or to share a "
                           "link so it can be scanned from a phone. The code is shown "
                           "to the user automatically — confirm briefly and do not "
                           "repeat the encoded text back.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The text, link, or data to encode in the QR "
                                       "code. Pass it EXACTLY as the user gave it — "
                                       "do not normalise, shorten, or add a scheme to "
                                       "a URL, because the scanned result must match "
                                       "what they asked for.",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "group": "weather",
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather and a 5-day forecast for a city. Renders a "
                           "weather card in the chat and returns the conditions as "
                           "figures you can answer from. Use for any question about "
                           "weather, temperature, rain or forecast.",
            "parameters": {
                "type": "object",
                "properties": {
                    # Verbatim from the exported tool, minus its final clause. That
                    # clause promised "a default_location will be used instead",
                    # which referenced an Open WebUI valve we do not have — there is
                    # no default location anywhere in this deployment, and deriving
                    # one from the deployment timezone or the request IP would be
                    # precisely the guess this text forbids. Replaced with what
                    # actually happens.
                    "location": {
                        "type": "string",
                        "description": "City explicitly typed by the user, Arabic or "
                                       "English. Only set if the user EXPLICITLY names "
                                       "a place. If the user just says 'weather' with "
                                       "no place, leave this null. Do NOT infer or "
                                       "guess the location from system context, IP, or "
                                       "conversation history — leave it null. The tool "
                                       "uses a configured default city if the deployment "
                                       "has one, and otherwise asks the user which city.",
                    },
                    "units": {"type": "string", "enum": ["metric", "imperial"],
                              "description": "Defaults to metric."},
                },
            },
        },
    },
]

# Search is keyless (yt-dlp) but the dependency is optional. Offering a tool that
# can only fail is worse than not offering it, so the schema is only added when
# the package is importable — the same posture Hermes takes with its check_fn.
if _importlib_util.find_spec("yt_dlp") is not None:
    TOOL_SCHEMAS.append({
        "group": "media",
        "type": "function",
        "function": {
            "name": "search_youtube",
            "description": (
                "Search YouTube by description when the user has NOT given a link. "
                "Two modes, and picking the right one matters:\n"
                "• mode='play' — they want to watch one thing now. Triggers: "
                "'play <song/artist>', 'put on X', 'play some jazz', 'I want to "
                "hear X', 'play the X trailer'. Plays the top result immediately.\n"
                "• mode='browse' — they want to choose. Triggers: 'find me a video "
                "about X', 'search YouTube for X', 'show me videos on X', 'what "
                "videos are there about X', 'find some tutorials on X'. Returns a "
                "list they pick from.\n"
                "When it is genuinely ambiguous, prefer 'play'.\n"
                "Do NOT use this when the user already gave a YouTube URL or video "
                "ID — call play_youtube_video instead. Do NOT use it for general "
                "web questions — call web_search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "What to search for, e.g. 'rick astley "
                                             "never gonna give you up' or 'lo-fi beats'"},
                    "mode": {"type": "string", "enum": ["play", "browse"],
                             "description": "'play' to start the top result now, "
                                            "'browse' to show a pickable list. "
                                            "Defaults to 'play'."},
                    "max_results": {"type": "integer",
                                    "description": "Browse mode only: how many "
                                                   "results (1-10, default 5)"},
                },
                "required": ["query"],
            },
        },
    })

# Tools that produce a user-facing side effect (vs read-only tools whose result the
# model folds into its answer). Used by the chat loop to decide what to surface.
ACTION_TOOLS = {"create_task", "complete_task", "draft_email", "schedule_meeting",
                "remember_fact", "set_reminder",
                # Writes to the user's channel library.
                "add_tv_channel",
                # Adds up to 25 rows to the library in one turn.
                "add_tv_channels_bulk",
                # The radio library's two writes.
                "add_radio_stations_bulk", "remove_radio_station"}
READ_TOOLS = {"get_analytics", "resolve_contact", "search_knowledge", "recall_memory",
              "web_search", "get_emails", "read_email", "get_agenda", "get_contacts",
              # play_youtube_video renders a widget, but it is a READ tool: its
              # result is a context string written AT the model ("do not describe
              # the player; end your reply with this link"). Treating it as an
              # ACTION tool would surface that instruction to the user verbatim
              # and skip the reply that carries the clickable URL.
              "play_youtube_video", "get_youtube_video_info", "search_youtube",
              "get_weather", "get_news", "watch_live_tv",
              "search_tv_channels",
              # Renders a widget and returns a context string — same shape as
              # play_youtube_video, and a read for the same reason.
              "generate_qr_code",
              # Open a player; nothing is mutated.
              "uae_radio", "arabic_radio", "quran_radio",
              "radio_by_genre", "search_radio",
              # Browsing the directory and playing your own list are reads; the
              # two WRITES are in ACTION_TOOLS.
              "search_radio_stations", "my_radio"}


# ── Sync→async bridge for the Google services (dispatch runs in a worker thread) ──
import asyncio as _asyncio

# The primary (uvicorn) event loop, registered at app startup. The shared httpx
# AsyncClient in services/http_client.py is bound to whichever loop first uses
# it — i.e. this one. Tool dispatch runs inside asyncio.to_thread worker
# threads, so we must marshal Google coroutines back ONTO this loop rather than
# spinning up a throwaway loop (a fresh loop can't reuse the bound httpx client,
# which is exactly why the agent's get_emails failed while the REST route worked).
_MAIN_LOOP: "_asyncio.AbstractEventLoop | None" = None


def set_main_loop(loop) -> None:
    """Register the primary event loop (call once from the FastAPI lifespan).
    Delegates to the shared bridge so every sync caller uses one implementation."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop
    from backend.services import async_bridge
    async_bridge.set_main_loop(loop)


def _run_async(coro):
    """Run an async coroutine from the sync tool dispatcher — see async_bridge."""
    from backend.services.async_bridge import run_sync
    return run_sync(coro, timeout=90)


_CONNECT_CTA = ("To use email, calendar, or contacts features, connect your "
                "Microsoft 365 or Google account in Settings → Connected Apps.")

# Errors that mean "the user hasn't linked a mailbox yet", from either provider.
_NOT_CONNECTED_ERRORS = (
    "google_not_connected", "google_token_expired", "google_not_configured",
    "microsoft_not_connected", "microsoft_token_expired", "microsoft_not_configured",
    "no_provider_connected",
)


def _provider_call(coro):
    """Run a provider-service coroutine, mapping a not-connected error to a friendly
    string. Returns (result, error_message)."""
    from fastapi import HTTPException
    try:
        return _run_async(coro), None
    except HTTPException as he:
        detail = he.detail if isinstance(he.detail, dict) else {}
        if str(detail.get("error", "")) in _NOT_CONNECTED_ERRORS:
            return None, _CONNECT_CTA
        return None, "That action isn't available right now."
    except Exception:
        return None, "That action failed — please try again."


# Back-compat alias for the call sites written against the Google-only helper.
_google_call = _provider_call


def _event_when(iso: str | None) -> str:
    """An event timestamp as readable local text for the model.

    Provider layers hand back ISO carrying the local UTC offset, so the wall clock
    is already correct — this only makes it legible. The model must never be shown a
    raw UTC stamp: it quotes whatever it is given, and a bare offset-less ISO is how
    a 09:30 meeting started being read back as 05:30."""
    from backend.services.user_tz import to_aware
    if not iso or "T" not in iso:
        return iso or ""
    dt = to_aware(iso)
    return dt.strftime("%a %d %b %H:%M") if dt else iso


def _lead_in(name: str, args: dict) -> str:
    """A short natural-language confirmation (the model returns empty content
    alongside a tool call, so we synthesize the human-facing sentence)."""
    if name == "create_task":
        title = args.get("title", "task")
        extras = []
        pr = args.get("priority")
        if pr and pr != "medium":
            extras.append(f"{pr} priority")
        due = args.get("due")
        if due and str(due).lower() not in ("null", "none", ""):
            extras.append(f"due {due}")
        suffix = f" ({', '.join(extras)})" if extras else ""
        return f"I've added a task: **{title}**{suffix}."
    if name == "complete_task":
        return f"Marking **{args.get('title', 'that task')}** as done."
    if name == "draft_email":
        to = args.get("to", "")
        subj = args.get("subject")
        return f"I've drafted an email to {to}" + (f" — *{subj}*." if subj else ".")
    if name == "schedule_meeting":
        who = args.get("with", "")
        when = args.get("time", "")
        return f"Setting up a meeting with {who} ({when})."
    if name == "set_reminder":
        msg = args.get("message", "")
        when = args.get("remind_at", "")
        return f"Reminder set: *{msg}* at {when}."
    return ""


def _balanced_json_objects(text: str) -> list:
    """Extract top-level {...} JSON objects from free text (brace-balanced)."""
    objs, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "{":
            depth, j = 0, i
            while j < n:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            objs.append(json.loads(text[i:j + 1]))
                        except Exception:
                            pass
                        i = j
                        break
                j += 1
        i += 1
    return objs


def extract_text_tool_calls(content: str) -> list:
    """
    Fallback: pull tool calls out of RAW CONTENT when the model emits them as text
    instead of structured tool_calls. With the full production prompt this build often
    emits `<tool_call>{...}</tool_call>` or a bare `{"name":..,"arguments":{..}}` in the
    content, which would otherwise be shown verbatim and the action silently skipped.

    Safety rules applied to minimise false positives:
    - <tool_call>…</tool_call> tagged content is preferred and processed first.
    - Bare JSON is only matched as a fallback when NO tagged calls were found.
    - In both paths, the JSON object must have BOTH a "name" key (in our known tool
      set) AND an "arguments" key that is a dict/object — bare `{"name":"…"}` echoes
      from the model's summary text are rejected.
    - Callers (call_llm_tools) further gate this with allow_text_recovery=False in
      follow-up rounds after an action tool has already executed.
    """
    if not content:
        return []
    candidates = []
    fenced = re.findall(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", content, re.DOTALL)
    for frag in fenced:
        try:
            candidates.append(json.loads(frag))
        except Exception:
            pass
    if not candidates:
        candidates = _balanced_json_objects(content)
    calls = []
    for obj in candidates:
        if not (isinstance(obj, dict) and obj.get("name") and
                obj["name"] in (ACTION_TOOLS | READ_TOOLS)):
            continue
        # Require "arguments" to be a dict — bare {"name": "…"} echoes are rejected.
        args = obj.get("arguments")
        if not isinstance(args, dict):
            continue
        calls.append({"id": f"text_{len(calls)}",
                      "function": {"name": obj["name"], "arguments": json.dumps(args)}})
    return calls


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def dispatch_tool_call(name: str, raw_args, user_id: str):
    """Execute one tool call and return its raw result.

    Almost always a user-facing confirmation/answer string. Tools that render a
    widget instead return ``(HTMLResponse, context)``; ``execute_single_tool``
    runs everything through ``tool_result.process_tool_result``, which turns that
    into (llm_result, embeds) and leaves plain strings alone.
    """
    args = _parse_args(raw_args)

    # Guardrail: deny unknown/hallucinated actions outright. (comms actions are still
    # safe-by-design — draft_email queues a draft, it never sends.)
    from backend.guardrails import decide
    if decide(name) == "deny":
        return f"⚠️ I'm not able to perform that action ('{name}')."

    # Belt and braces for tool toggles. The real mechanism is _offered_tools(),
    # which keeps a disabled tool out of the payload entirely — this catches the
    # narrow case where it was still offered earlier in a multi-round turn and
    # switched off mid-conversation. Toggles SUBTRACT ONLY: this can refuse a
    # tool guardrails allowed, never permit one guardrails denied, which is why
    # it runs after decide() and not instead of it.
    if user_id:
        try:
            from backend.services import tool_prefs
            if group_of(name) in set(tool_prefs.get_disabled(user_id)):
                return (f"⚠️ {name} is switched off for this chat. Turn its group "
                        f"back on in the tools menu to use it.")
        except Exception:
            log.exception("tool prefs check failed — allowing the call")

    # Read-only analytics: answer directly, don't go through execute_action.
    if name == "get_analytics":
        from backend.analytics import run_metric
        try:
            days = int(args.get("days", 7))
        except (TypeError, ValueError):
            days = 7
        result = run_metric(args.get("metric", "summary"), user_id, days)
        return result.get("human") or str(result)

    if name == "resolve_contact":
        from backend.services import mailbox
        q = args.get("name", "")
        res, err = _provider_call(mailbox.search_contacts(user_id, q))
        if err:
            return err
        if not res:
            return f"No contact found for '{q}'."
        c = res[0]
        return (f"Contact: {c.get('name','')} | email: {c.get('email') or 'none on file'}"
                f" | phone: {c.get('phone') or '-'} | company: {c.get('company') or '-'}")

    # ── Real mail/calendar/contact reads (Microsoft Graph or Google) ──
    if name == "get_emails":
        from backend.services import mailbox
        try:
            n = int(args.get("max_results", 10))
        except (TypeError, ValueError):
            n = 10
        res, err = _provider_call(mailbox.inbox(user_id, n))
        if err:
            return err
        if not res:
            return "Your inbox is empty."
        # Carry a preview and the arrival time, not just subject+sender. With only a
        # subject line the model has nothing to answer a follow-up with, so it either
        # re-reads the list back or invents a body — which is what "[No preview
        # available]" was. The preview is still a snippet: read_email fetches the rest.
        lines = []
        for m in res[:n]:
            mark = "•" if not m.get("is_read") else " "
            star = "★" if m.get("is_important") else ""
            when = _event_when(m.get("received_at"))
            preview = " ".join((m.get("preview") or "").split())[:160]
            head = f"{mark}{star} {m.get('subject') or '(no subject)'} — {m.get('from_name') or m.get('from_email') or '?'}"
            lines.append(f"{head}{f'  [{when}]' if when else ''}"
                         + (f"\n    {preview}" if preview else ""))
        unread = sum(1 for m in res if not m.get("is_read"))
        return (f"{unread} unread of {len(res)} recent emails "
                f"(previews only — call read_email for the full text):\n" + "\n".join(lines))

    if name == "read_email":
        from backend.services import mailbox
        res, err = _provider_call(mailbox.inbox(user_id, 25))
        if err:
            return err
        if not res:
            return "Your inbox is empty."

        q = (args.get("query") or "").strip().lower()
        if q:
            # Match on subject AND sender: the user names an email either way
            # ("the Jira one", "the email from Aisha").
            hits = [m for m in res
                    if q in " ".join([m.get("subject") or "", m.get("from_name") or "",
                                      m.get("from_email") or ""]).lower()]
            if not hits:
                listed = "\n".join(f"- {m.get('subject')} — {m.get('from_name')}" for m in res[:8])
                return (f"No recent email matches {args.get('query')!r}. The 8 most recent are:\n"
                        f"{listed}")
            msg = hits[0]
        else:
            msg = res[0]

        # Graph ships the body with the listing; Gmail only sends a 150-char snippet,
        # so fall back to a per-message fetch rather than passing the snippet off as
        # the full text.
        body = (msg.get("body_text") or "").strip()
        if not body:
            fetched, ferr = _provider_call(mailbox.message_body(user_id, msg.get("id") or ""))
            if ferr:
                return ferr
            body = (fetched or "").strip()
        body = " ".join(body.split()) if body else ""
        if not body:
            return (f"Subject: {msg.get('subject')}\nFrom: {msg.get('from_name')} "
                    f"<{msg.get('from_email')}>\n\nThis email has no readable text body "
                    f"(it may be an image or attachment only).")
        # Cap the body: a long thread would otherwise crowd out the rest of the prompt.
        clipped = body[:4000]
        if len(body) > 4000:
            clipped += " …[truncated]"

        when = _event_when(msg.get("received_at"))
        header = [f"Subject: {msg.get('subject') or '(no subject)'}",
                  f"From: {msg.get('from_name') or ''} <{msg.get('from_email') or ''}>".strip()]
        if when:
            header.append(f"Received: {when}")
        if msg.get("has_attachments"):
            header.append("Attachments: yes")
        return "\n".join(header) + "\n\n" + clipped

    if name == "get_agenda":
        from backend.services import mailbox
        try:
            days = int(args.get("days_ahead", 1))
        except (TypeError, ValueError):
            days = 1
        res, err = _provider_call(mailbox.agenda(user_id, days))
        if err:
            return err
        if not res:
            return "No upcoming events."
        return "Upcoming events:\n" + "\n".join(
            f"- {e['title']} at {_event_when(e.get('start'))}"
            + (f" ({e['location']})" if e.get('location') else "")
            for e in res)

    if name == "get_contacts":
        from backend.services import mailbox
        q = (args.get("query") or "").strip()
        coro = mailbox.search_contacts(user_id, q) if q else mailbox.list_contacts(user_id)
        res, err = _provider_call(coro)
        if err:
            return err
        if not res:
            return "No contacts found."
        return "Contacts:\n" + "\n".join(
            f"- {c['name']}" + (f" <{c['email']}>" if c.get('email') else "") for c in res[:15])

    if name == "search_knowledge":
        from backend.ingest import search_corporate
        hits = search_corporate(args.get("query", ""), top_k=3)
        if not hits:
            return "No relevant company documents found."
        return "\n\n".join(f"[{h['source']}] {h['text'][:600]}" for h in hits)

    if name == "recall_memory":
        from memory.long_term import search_memory
        mem = search_memory(user_id, args.get("query", ""), top_k=5)
        return mem.strip() if mem and mem.strip() else "No relevant memory found."

    if name == "remember_fact":
        from memory.long_term import upsert_facts
        from datetime import datetime, timezone
        fact = (args.get("fact") or "").strip()
        if not fact:
            return "Nothing to remember."
        try:
            upsert_facts([fact], user_id, datetime.now(timezone.utc).isoformat())
            return f"🧠 Noted: {fact}"
        except Exception as e:
            return f"⚠️ Could not save that to memory ({e})."

    if name == "set_reminder":
        import httpx as _httpx
        message  = (args.get("message") or "").strip()
        remind_at = (args.get("remind_at") or "").strip()
        if not message or not remind_at:
            return "⚠️ Need both a message and a time to set a reminder."
        try:
            r = _httpx.post(
                "http://127.0.0.1:8000/set_reminder",
                json={"user_id": user_id, "message": message, "remind_at": remind_at},
                headers=internal_headers(user_id),
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return f"⏰ Reminder set for *{data.get('remind_at', remind_at)}*: {message}"
        except Exception as e:
            return f"⚠️ Could not set reminder: {e}"

    # YouTube: async services bridged onto the main loop, same as the mail/calendar
    # providers above. play_youtube_video returns (HTMLResponse, context) — that
    # tuple travels out of here untouched and is split by process_tool_result.
    if name in ("play_youtube_video", "get_youtube_video_info"):
        from backend.services import youtube
        video = (args.get("video") or "").strip()
        if not video:
            return "⚠️ Which YouTube video? Give me a link or an 11-character video ID."
        fn = (youtube.play_youtube_video if name == "play_youtube_video"
              else youtube.get_youtube_video_info)
        try:
            return _run_async(fn(video))
        except Exception as e:
            return f"⚠️ Could not load that YouTube video: {e}"

    # Search is sync by design: yt-dlp blocks, and dispatch already runs in a
    # worker thread, so bridging to the event loop and back would buy nothing.
    if name == "watch_live_tv":
        from backend.services import livetv
        return livetv.watch_live_tv(user_id, args.get("channel") or "")

    if name == "search_tv_channels":
        from backend.services import livetv
        return livetv.search_tv_channels(user_id, args.get("name") or "",
                                         args.get("category") or "",
                                         args.get("country") or "")

    if name == "add_tv_channels_bulk":
        from backend.services import livetv
        names = args.get("names")
        return livetv.add_tv_channels_bulk(user_id, names if isinstance(names, list) else [])

    if name == "add_tv_channel":
        from backend.services import livetv
        return livetv.add_tv_channel(user_id, args.get("name") or "",
                                     args.get("url") or "",
                                     args.get("category") or "general")

    if name == "get_news":
        from backend.services import news
        return news.get_news(args.get("category") or "world")

    if name in ("uae_radio", "arabic_radio", "quran_radio", "radio_by_genre",
                "search_radio", "search_radio_stations", "add_radio_stations_bulk",
                "remove_radio_station", "my_radio"):
        from backend.services import radio
        if name == "search_radio_stations":
            return radio.search_radio_stations(user_id, args.get("name") or "",
                                               args.get("country") or "",
                                               args.get("genre") or "")
        if name == "add_radio_stations_bulk":
            return radio.add_radio_stations_bulk(user_id, args.get("names") or [])
        if name == "remove_radio_station":
            return radio.remove_radio_station(user_id, args.get("name") or "")
        if name == "my_radio":
            return radio.my_radio(user_id)
        if name == "uae_radio":
            return radio.uae_radio(user_id)
        if name == "arabic_radio":
            return radio.arabic_radio(args.get("country_code") or "")
        if name == "quran_radio":
            return radio.quran_radio()
        if name == "radio_by_genre":
            return radio.radio_by_genre(args.get("genre") or "")
        return radio.search_radio(args.get("query") or "")

    if name == "generate_qr_code":
        from backend.services import qr
        return qr.generate_qr_code(args.get("content") or "")

    if name == "get_weather":
        from backend.services import weather
        return weather.get_weather(args.get("location") or "",
                                   units=args.get("units") or "metric")

    if name == "search_youtube":
        from backend.services import youtube
        try:
            max_r = int(args.get("max_results") or 5)
        except (TypeError, ValueError):
            max_r = 5
        return youtube.search_youtube(
            args.get("query") or "", mode=args.get("mode") or "play", limit=max_r)

    if name == "web_search":
        query = (args.get("query") or "").strip()
        if not query:
            return "⚠️ No query provided."
        try:
            max_r = min(int(args.get("max_results") or 5), 10)
        except (TypeError, ValueError):
            max_r = 5
        # Self-hosted SearXNG. No scraper fallback on purpose — see
        # backend/services/websearch.py for why a silent backend swap is worse
        # than an honest error.
        from backend.services.websearch import SearchUnavailable, search
        try:
            results = search(query, max_results=max_r)
        except SearchUnavailable as e:
            log.warning("web_search unavailable: %s", e)
            return ("⚠️ Web search is unavailable right now — the search service "
                    "isn't responding. Everything else still works.")
        except Exception as e:
            return f"⚠️ Web search failed: {e}"
        if not results:
            return f"No web results found for: {query}"
        lines = [f"**{r['title']}**\n{r['content'][:300]}\n🔗 {r['url']}" for r in results]
        return f"🌐 Web results for *{query}*:\n\n" + "\n\n".join(lines)

    # Schedule a real calendar event on whichever provider the user connected.
    if name == "schedule_meeting":
        who = (args.get("with") or "").strip()
        when = (args.get("time") or "").strip()
        title = (args.get("title") or (f"Meeting with {who}" if who else "Meeting")).strip()
        if not when:
            return "When should I schedule it?"
        try:
            from backend.services.timeparse import parse_meeting_time
            from backend.services import user_tz
            # The phrase is in the user's wall clock, and create_event sends that same
            # zone to the provider — so both halves agree on what "3pm" meant.
            start, end = parse_meeting_time(when, tz=user_tz.tz())
        except Exception:
            return "I couldn't understand that time — try e.g. 'tomorrow at 3pm'."
        from backend.services import mailbox
        attendees = None
        if who:
            if "@" in who:
                attendees = [who]
            else:
                res, _ = _provider_call(mailbox.search_contacts(user_id, who))
                if res and res[0].get("email"):
                    attendees = [res[0]["email"]]
        created, err = _provider_call(mailbox.create_event(
            user_id, title, start, end, attendees=attendees))
        if err:
            return err
        link = created.get("htmlLink") if isinstance(created, dict) else None
        return f"Scheduled **{title}** for {start}." + (f"\n{link}" if link else "")

    action = {"type": name, **args}
    outcome = execute_action(action, user_id).strip()  # "✅ Task created.", etc.
    lead = _lead_in(name, args)
    if lead and outcome:
        return f"{lead}\n{outcome}"
    return lead or outcome


def execute_single_tool(name: str, raw_args, user_id: str):
    """Run one tool; return (result_string, is_action, embeds). Read tools' results
    are fed back to the model; action tools' results are surfaced to the user.

    `embeds` is a list of HTML documents for the frontend to render as sandboxed
    iframes — empty for every tool that returns plain text, which is all of them
    except the YouTube player. It must never be folded into `result`: the model
    is not meant to see the markup (see backend/tool_result.py).

    Every call is timed + logged to the events table for analytics (P5)."""
    import time as _t
    from backend.tool_result import process_tool_result
    _t0 = _t.monotonic()
    ok = True
    try:
        processed = process_tool_result(name, dispatch_tool_call(name, raw_args, user_id))
        result = processed.llm_result
        # Heuristic success: dispatch returns a ⚠️-prefixed string on failure.
        ok = not (isinstance(result, str) and result.lstrip().startswith("⚠️"))
        return result, (name in ACTION_TOOLS), processed.embeds
    except Exception:
        ok = False
        raise
    finally:
        try:
            from backend import events
            events.log_event("tool_called", user_id=user_id, name=name, success=ok,
                             duration_ms=int((_t.monotonic() - _t0) * 1000))
        except Exception:
            pass


def run_tool_calls(tool_calls: list, user_id: str) -> str:
    """Execute every tool call from an assistant message; join confirmations.

    Text-only: any embeds a tool produced are dropped, since this helper's
    callers have no channel to render them. Use execute_single_tool where the
    widget matters."""
    from backend.tool_result import process_tool_result
    parts = []
    for tc in tool_calls:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        result = process_tool_result(
            name, dispatch_tool_call(name, fn.get("arguments"), user_id)).llm_result
        parts.append(result if isinstance(result, str) else str(result))
    return "\n\n".join(p for p in parts if p)


# ── Tool groups ──────────────────────────────────────────────────────────────
# Groups are how a USER thinks about turning things off, not how the code is
# organised — "I don't want email in this thread", not "these three share a
# service module". 19 tools is already past the point where a flat on/off list
# is usable, and it only grows.
#
# `group` lives on the schema dict (a sibling of "type"/"function") and is
# REQUIRED — a test asserts every tool has one, so a new tool cannot quietly
# become ungroupable and therefore untoggleable. It is stripped in tools_for()
# before the schemas go on the wire: the provider is sent OpenAI's shape and
# nothing else.
TOOL_GROUPS: list[dict] = [
    {"id": "email",     "label": "Email",              "user_visible": True,
     "description": "Read and draft mail"},
    {"id": "calendar",  "label": "Calendar",           "user_visible": True,
     "description": "Agenda and scheduling"},
    # Visible, but LABELLED with its dependency. resolve_contact fires as a
    # sub-step of drafting mail and scheduling, so switching this off makes
    # Email look broken ("I couldn't find Aisha's address") in a group the user
    # did not touch. Saying so in the label is cheaper than the confusion.
    {"id": "contacts",  "label": "Contacts (used by Email & Calendar)",
     "user_visible": True, "description": "Look up people"},
    {"id": "tasks",     "label": "Tasks & reminders",  "user_visible": True,
     "description": "To-dos and alerts"},
    # HIDDEN. Bundles recall_memory (an implicit read) with remember_fact (an
    # ACTION — a write), so one switch conflates "don't save anything from this
    # chat" with "forget everything I have ever told you". A user reaching for
    # the first gets the second, and nothing ever says so: it degrades quietly
    # instead of failing visibly. Privacy-of-memory deserves its own explicit
    # control, not a tool toggle.
    {"id": "memory",    "label": "Memory",             "user_visible": False,
     "description": "Recall and save facts"},
    # HIDDEN because the GROUP is incoherent, not because toggling is
    # meaningless. search_knowledge (company handbook RAG) and get_analytics
    # (the user's own task/email stats) have nothing to do with each other; the
    # label names our implementation, not something a user recognises, so nobody
    # can predict what it turns off. To surface it, SPLIT it — "Company
    # documents" and "My stats" — rather than showing it as-is.
    {"id": "knowledge", "label": "Knowledge & analytics", "user_visible": False,
     "description": "Company documents and your stats"},
    {"id": "web",       "label": "Web search",         "user_visible": True,
     "description": "Search the public web"},
    {"id": "media",     "label": "Media",              "user_visible": True,
     "description": "YouTube search and playback"},
    {"id": "weather",   "label": "Weather",            "user_visible": True,
     "description": "Forecasts and conditions"},
    {"id": "news",      "label": "News",               "user_visible": True,
     "description": "Headlines from public feeds"},
    {"id": "livetv",    "label": "Live TV",            "user_visible": True,
     "description": "Live channel streams"},
    # Its own group rather than a "Utilities" bucket. The rule that hid
    # "Knowledge & analytics" was that a label must name something the user
    # recognises; "Utilities" fails that harder, and a toggle nobody understands
    # costs more than the panel row it saves.
    {"id": "radio",     "label": "Radio",              "user_visible": True,
     "description": "Live Arabic and UAE radio streams"},
    {"id": "qr",        "label": "QR codes",           "user_visible": True,
     "description": "Turn a link or text into a scannable code"},
]

# The rule that decides what the panel renders. PRESENTATION ONLY — tools_for(),
# the dispatch rejection and every test still operate on all of TOOL_GROUPS.
#
# "visible OR currently disabled" is what makes a trapped state impossible: a
# hidden group is only ever absent from the panel while it is ON. The moment it
# is off — however that happened, including a value set through the API or a
# group we hide later — it appears, can be switched back on, and then disappears
# again. No migration, and it covers cases we did not anticipate.
def visible_groups(disabled) -> list[dict]:
    off = set(disabled or [])
    return [g for g in TOOL_GROUPS if g.get("user_visible") or g["id"] in off]


GROUP_IDS = {g["id"] for g in TOOL_GROUPS}

# Tools whose answer goes stale. A turn produced by one of these carries DATA the
# model will otherwise quote back on a repeat instead of re-calling — measured
# for weather, mail and calendar.
#
# Search tools are absent on purpose: their reply is a pointer ("here are some
# results"), not data, and they already re-call.
#
# ACTION tools must NEVER appear here. Marking a "task created" confirmation as
# stale invites the model to re-run it, which is a duplicate side effect rather
# than a wasted lookup. The assertion below makes that structural, not a habit.
VOLATILE_TOOLS = {
    "get_emails", "read_email", "get_agenda", "get_contacts",
    "get_weather", "get_analytics",
    # Headlines change constantly and the reply carries them, so a repeat would
    # otherwise be answered from the transcript.
    "get_news",
    # watch_live_tv and the radio PLAY tools were here. Their results do change
    # — the channel list, what is on air, the vote ordering — so "volatile" was
    # the right word. But this set exists for exactly one purpose, deciding what
    # mark_stale annotates, and annotating a player turn was measured to make the
    # model LESS likely to re-open the player: 2/8 marked vs 7/8 unmarked. They
    # are now in WIDGET_TOOLS instead, which mark_stale subtracts. See the note
    # there and in backend/chat/stale.py before moving any of them back.
    #
    # The PICKERS stay: their replies list what was found, so there is real data
    # to go stale and the marker behaves as designed on them.
    "search_radio_stations",
    # Upstream rots — about half of any directory sample is dead — so a
    # repeat must re-query rather than quote an earlier answer.
    "search_tv_channels",
}

assert not (VOLATILE_TOOLS & ACTION_TOOLS), (
    "an ACTION tool is marked volatile — re-calling it would repeat a side effect"
)

# ── widget tools: an EXCLUSION set for staleness marking ─────────────────────
#
# Tools whose ANSWER IS A PLAYER OR CARD rather than data. Their reply text is a
# pointer — "Opened the live TV player", "the player is active" — and carries
# nothing the model could quote.
#
# This set keeps them out of mark_stale. Nothing else consults it.
#
# STATUS: the exclusion is deliberate but UNVERIFIED, and the open bug it was
# meant to fix is still open. Read this before spending time here.
#
# The bug: ask for live TV (or radio, or a QR code) twice in a row and the second
# request renders no card. The model does not call the tool at all — the turn
# persists `tool_calls: []` — it answers from the transcript instead. Live,
# 3 trials per widget tool: 27/27 cards on the first request, 7/27 on the repeat.
#
# What was tried and did NOT fix it: marking the prior turn stale, marking it
# with a bespoke "the player is no longer on screen" wording, and removing the
# marker entirely (this change). All three measured the same live outcome.
#
# WHAT WE NOW KNOW, and it invalidates the earlier reasoning: the server sends
# `chat_template_kwargs: {"enable_thinking": False}` (backend/services/llm.py,
# build_payload). Replaying one identical captured payload:
#     thinking ON   8/8 re-called
#     thinking OFF  0/8 re-called
# Every measurement that guided the marker and prompt work was taken with
# thinking ON, i.e. against a system we do not run. The suppressor is far more
# likely to be the disabled reasoning than any marker or prompt sentence.
#
# THE OPEN QUESTION: does the repeat request re-call once the model is allowed to
# reason, and is enabling thinking on tool-detection calls affordable? That is
# the next thing to measure, on tests/routing_bench.py, which now sends the flag
# the way the server does.
#
# The exclusion is kept meanwhile because it is harmless and simplifies the
# model: a turn with no data in it has nothing to go stale.
#
# DELIBERATELY EXCLUDED from this set, and why:
#   * the pickers (search_tv_channels, search_radio_stations) — their replies DO
#     list what was found, so there is real data to go stale;
#   * get_news and get_weather — they render a card AND carry headlines/figures,
#     so they stay marked and their data refreshes on a repeat;
#   * every ACTION tool — re-calling repeats a side effect. Asserted below.
WIDGET_TOOLS = {
    "watch_live_tv",
    "uae_radio", "arabic_radio", "quran_radio", "radio_by_genre", "search_radio",
    "my_radio",
    "play_youtube_video",
    # Deterministic rather than volatile, but the symptom is identical: ask for
    # the same QR twice and the second answer has no card.
    "generate_qr_code",
}

assert not (WIDGET_TOOLS & ACTION_TOOLS), (
    "an ACTION tool is marked as a widget — its confirmation must never invite a "
    "re-call, which would repeat the side effect"
)



def group_of(tool_name: str) -> str | None:
    for s in TOOL_SCHEMAS:
        if s["function"]["name"] == tool_name:
            return s.get("group")
    return None


def tools_for(disabled_groups) -> list[dict]:
    """The schemas to offer the model, with `disabled_groups` removed.

    This is the cut point that makes "disabled" honest. Filtering at dispatch
    instead would let the model call a tool the user turned off, fail, and
    apologise — the user asked for it to be unavailable, not for it to break.
    Absent from the payload means the model cannot call it at all.

    An unknown group id is ignored rather than treated as "disable everything":
    a stale preference naming a group we have since renamed must not silently
    strip the user's tools.
    """
    disabled = {g for g in (disabled_groups or []) if g in GROUP_IDS}
    out = []
    for s in TOOL_SCHEMAS:
        if s.get("group") in disabled:
            continue
        # Copy without `group` — it is ours, not part of the provider's schema.
        out.append({k: v for k, v in s.items() if k != "group"})
    return out
