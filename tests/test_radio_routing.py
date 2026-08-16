"""Does the LIVE model pick the right radio tool?

Five tools with deliberately overlapping surfaces, so their descriptions carry
explicit carve-outs — `uae_radio` declares itself THE DEFAULT for a bare "play
radio", and `search_radio` says "Do NOT use for general or country requests".
Those sentences only work if the model actually honours them, which no offline
assertion can establish: schema tests prove the tools are offered, not that they
are chosen correctly.

Tools are offered but NEVER executed. The assertion reads `tool_calls` off the
first response, so nothing contacts Radio Browser and no station is fetched.

Skipped when the gateway is unreachable — this exercises a running model, so a
laptop with no LiteLLM should not see a red suite. That does mean it can be
skipped into uselessness; the skip reason names the gateway so it is obvious
why, rather than silently passing.
"""

import json
import urllib.error
import urllib.request

import pytest

from config.settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

URL = LLM_BASE_URL.rstrip("/") + "/chat/completions"
HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"}


def _gateway_up() -> bool:
    try:
        body = {"model": LLM_MODEL, "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1}
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


needs_model = pytest.mark.skipif(
    not _gateway_up(), reason=f"LLM gateway not reachable at {LLM_BASE_URL}")


def _picked(prompt: str) -> str:
    import backend.tools as tools

    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": "You are a helpful assistant with tools."},
                     {"role": "user", "content": prompt}],
        "tools": tools.tools_for([]),
        "tool_choice": "auto",
        "temperature": 0,
    }
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.load(r)
    calls = (data["choices"][0]["message"] or {}).get("tool_calls") or []
    return calls[0]["function"]["name"] if calls else "(none)"


# ── the default ──────────────────────────────────────────────────────────────

@needs_model
@pytest.mark.parametrize("prompt", ["play radio", "open the radio", "شغل الراديو",
                                    "play UAE radio"])
def test_a_bare_request_goes_to_the_declared_default(prompt):
    """uae_radio calls itself "the DEFAULT radio action". If a bare "play radio"
    lands anywhere else, that sentence is decoration."""
    assert _picked(prompt) == "uae_radio"


# ── the carve-outs ───────────────────────────────────────────────────────────

@needs_model
@pytest.mark.parametrize("prompt", ["play some Arabic radio", "I want Saudi radio",
                                    "إذاعة مصرية"])
def test_general_and_other_country_requests_go_to_arabic_radio(prompt):
    assert _picked(prompt) == "arabic_radio"


@needs_model
@pytest.mark.parametrize("prompt", ["play Quran radio", "إذاعة القرآن الكريم"])
def test_quran_has_its_own_route(prompt):
    assert _picked(prompt) == "quran_radio"


@needs_model
@pytest.mark.parametrize("prompt", ["put on a news radio station",
                                    "I want to listen to classical music radio"])
def test_a_style_request_goes_to_genre(prompt):
    assert _picked(prompt) == "radio_by_genre"


@needs_model
@pytest.mark.parametrize("prompt", ["play Dubai Eye", "play Emirates FM",
                                    "شغل إذاعة نور دبي"])
def test_a_named_station_goes_to_search(prompt):
    assert _picked(prompt) == "search_radio"


@needs_model
@pytest.mark.parametrize("prompt", ["play radio", "open the radio",
                                    "play some Arabic radio", "شغل الراديو"])
def test_search_radio_never_fires_on_a_general_request(prompt):
    """The explicit negative in search_radio's description. Stated separately from
    the positive cases because this is the failure that would look harmless: a
    search for the literal word "radio" returns stations, so the card still
    renders and nobody notices the wrong tool ran."""
    assert _picked(prompt) != "search_radio"


# ── the library carve-outs ───────────────────────────────────────────────────

@needs_model
@pytest.mark.parametrize("prompt", ["play my saved stations", "play my stations"])
def test_my_stations_plays_the_library(prompt):
    assert _picked(prompt) == "my_radio"


@needs_model
@pytest.mark.parametrize("prompt", ["add some Saudi radio stations",
                                    "I want to save UAE radio stations",
                                    "add radio stations from Egypt"])
def test_adding_by_country_or_genre_opens_the_picker(prompt):
    """ADD/SAVE must not be read as "listen". Both are about stations and a
    country, and uae_radio claims the default — so this is the pair most likely
    to collide."""
    assert _picked(prompt) == "search_radio_stations"


@needs_model
def test_the_pickers_own_message_reaches_the_bulk_add():
    """THE regression. This is the exact string ChannelPicker sends when the user
    ticks rows and presses Add. It first routed to search_radio_stations, so
    ticking stations silently re-searched instead of saving them — the picker
    appeared to do nothing."""
    assert _picked("Add these radio stations: Dubai Eye 103.8, Noor Dubai") \
        == "add_radio_stations_bulk"


@needs_model
def test_removing_names_the_remove_tool():
    assert _picked("remove Dubai Eye from my stations") == "remove_radio_station"


@needs_model
@pytest.mark.parametrize("prompt", ["play radio", "شغل الراديو", "play Quran radio"])
def test_listening_never_reaches_the_library_writers(prompt):
    """A play request must never save anything."""
    assert _picked(prompt) not in ("add_radio_stations_bulk", "remove_radio_station",
                                   "search_radio_stations")
