"""Weather tool — Open-Meteo, keyless, snapshot-is-truth.

Ported from Hermes. Network is stubbed; the card template, the WMO mapping and
the null-location contract are the real code path.
"""

import backend.tools as tools
from backend.services import weather
from backend.tool_result import process_tool_result

GEO = {"results": [{"name": "Dubai", "admin1": "Dubai", "country": "United Arab Emirates",
                    "latitude": 25.07, "longitude": 55.19}]}
FORECAST = {
    "current": {"temperature_2m": 35.2, "apparent_temperature": 38.4,
                "relative_humidity_2m": 43, "precipitation": 0,
                "weather_code": 2, "wind_speed_10m": 12.3},
    "daily": {"time": ["2026-08-09", "2026-08-10"], "weather_code": [2, 0],
              "temperature_2m_max": [39, 40], "temperature_2m_min": [30, 31]},
}


class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


def _stub(monkeypatch, geo=None, forecast=None, exc=None):
    import httpx
    calls = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None):
            calls.append((url, params))
            if exc is not None:
                raise exc
            return _Resp(geo if "geocoding" in url else (forecast or FORECAST))

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return calls


# ── the null-location contract ───────────────────────────────────────────────

def test_null_location_asks_instead_of_guessing(monkeypatch):
    """The spec tells the model to leave location null when the user names no
    place, so null is the designed path — it must not error, and it must not
    invent a location."""
    calls = _stub(monkeypatch, geo=GEO)
    result, _, embeds = tools.execute_single_tool("get_weather", {}, "user_1")
    assert embeds == [], "no place means no card"
    assert "Which city" in result and "do not guess" in result.lower()
    assert calls == [], "must not hit the network without a location"


def test_blank_location_is_the_same_as_null(monkeypatch):
    _stub(monkeypatch, geo=GEO)
    result, _, embeds = tools.execute_single_tool(
        "get_weather", {"location": "   "}, "user_1")
    assert embeds == [] and "Which city" in result


def test_spec_forbids_inferring_a_location():
    """This wording defends against a real slot-filling failure. It must not be
    softened, and it must not promise a fallback we do not have."""
    spec = next(s for s in tools.TOOL_SCHEMAS if s["function"]["name"] == "get_weather")
    desc = spec["function"]["parameters"]["properties"]["location"]["description"]
    assert "Only set if the user EXPLICITLY names a place" in desc
    assert "leave this null" in desc
    assert "Do NOT infer or guess the location from system context, IP, or " \
           "conversation history" in desc
    assert "default_location" not in desc, "we have no default location — do not promise one"


# ── the happy path ───────────────────────────────────────────────────────────

def test_card_and_context(monkeypatch):
    _stub(monkeypatch, geo=GEO)
    result, is_action, embeds = tools.execute_single_tool(
        "get_weather", {"location": "Dubai"}, "user_1")
    assert is_action is False
    assert "Weather for Dubai" in result and "35°C" in result
    assert "partly cloudy" in result
    html = embeds[0]["html"]
    assert "Dubai" in html and "35°C" in html
    assert "iframe:height" in html, "our renderEmbed sizes the frame from this"
    assert embeds[0]["video"] is None and embeds[0]["results"] is None


def test_imperial_units(monkeypatch):
    calls = _stub(monkeypatch, geo=GEO)
    result, _, _ = tools.execute_single_tool(
        "get_weather", {"location": "Dubai", "units": "imperial"}, "user_1")
    assert "°F" in result and "mph" in result
    forecast_params = calls[-1][1]
    assert forecast_params["temperature_unit"] == "fahrenheit"


def test_unknown_place_degrades_to_text(monkeypatch):
    _stub(monkeypatch, geo={"results": []})
    result, _, embeds = tools.execute_single_tool(
        "get_weather", {"location": "Zzzzz"}, "user_1")
    assert embeds == [] and "No location found" in result


def test_network_failure_degrades_to_text(monkeypatch):
    _stub(monkeypatch, geo=GEO, exc=RuntimeError("open-meteo down"))
    result, _, embeds = tools.execute_single_tool(
        "get_weather", {"location": "Dubai"}, "user_1")
    assert embeds == [] and result.startswith("⚠️")


def test_unknown_wmo_code_does_not_crash(monkeypatch):
    """Open-Meteo can add codes; an unmapped one must not take the card down."""
    odd = {**FORECAST, "current": {**FORECAST["current"], "weather_code": 4242}}
    _stub(monkeypatch, geo=GEO, forecast=odd)
    result, _, embeds = tools.execute_single_tool(
        "get_weather", {"location": "Dubai"}, "user_1")
    assert embeds and "unknown" in result.lower()


def test_card_escapes_place_names(monkeypatch):
    """Place names come from a third-party API and land in markup."""
    hostile = {"results": [{**GEO["results"][0], "name": '<img src=x onerror=alert(1)>'}]}
    _stub(monkeypatch, geo=hostile)
    _, _, embeds = tools.execute_single_tool(
        "get_weather", {"location": "x"}, "user_1")
    assert "<img src=x onerror" not in embeds[0]["html"]


def test_registered_everywhere():
    import backend.guardrails as g
    names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert "get_weather" in names
    assert g.decide("get_weather") == "auto"
    assert "get_weather" in tools.READ_TOOLS and "get_weather" not in tools.ACTION_TOOLS


# ── WEATHER_DEFAULT_LOCATION ─────────────────────────────────────────────────

def test_unset_default_still_asks(monkeypatch):
    monkeypatch.setattr(weather, "WEATHER_DEFAULT_LOCATION", "")
    _stub(monkeypatch, geo=GEO)
    result, _, embeds = tools.execute_single_tool("get_weather", {}, "user_1")
    assert embeds == [] and "Which city" in result


def test_configured_default_is_used_and_shown(monkeypatch):
    """A default the user cannot see is a wrong answer they never question, so it
    is marked on the card AND stated in the text the model answers from."""
    monkeypatch.setattr(weather, "WEATHER_DEFAULT_LOCATION", "Dubai")
    _stub(monkeypatch, geo=GEO)
    result, _, embeds = tools.execute_single_tool("get_weather", {}, "user_1")
    assert embeds, "a configured default should produce a card"
    assert "Default location" in embeds[0]["html"]
    assert "the configured default" in result and "Dubai" in result
    assert embeds[0]["link"] is None


def test_explicit_location_is_not_marked_as_default(monkeypatch):
    monkeypatch.setattr(weather, "WEATHER_DEFAULT_LOCATION", "Dubai")
    _stub(monkeypatch, geo=GEO)
    result, _, embeds = tools.execute_single_tool(
        "get_weather", {"location": "Dubai"}, "user_1")
    assert "Default location" not in embeds[0]["html"]
    assert "configured default" not in result


def test_default_is_never_derived_from_timezone_or_ip():
    """APP_TIMEZONE returns the wrong city for a travelling or non-local user, and
    IP inference is exactly what the parameter description forbids."""
    import inspect
    src = inspect.getsource(weather)
    for forbidden in ("APP_TIMEZONE", "timezone.tz", "request.client", "X-Forwarded-For"):
        assert forbidden not in src, f"weather must not derive a location from {forbidden}"
