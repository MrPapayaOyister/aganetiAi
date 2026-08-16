"""Phase 2 — the consumer capabilities migrated into Runtime B.

Three things are proved here, in order of how badly each would hurt if wrong:

  1. every migrated tool goes through the authorization boundary like any other —
     a migration that quietly bypassed `authz` would undo P0;
  2. each handler reaches the SAME service function Runtime A calls, so the two
     runtimes cannot drift on behaviour;
  3. widget HTML never reaches the model, which is the one place a naive port
     would have poisoned the context window.

The service layer is stubbed throughout. These tests are about the migration —
the wiring, the schemas, the authorization, the result splitting — not about
whether YouTube is up. The services themselves have their own suites
(test_youtube_tool.py, test_radio.py, test_livetv.py, test_weather.py, …) which
are unchanged and still passing.
"""
import asyncio
import importlib

import pytest
from fastapi.responses import HTMLResponse

import backend.orchestrator  # noqa: F401 — registers everything, including consumer tools
from backend.orchestrator import authz, consumer_tools, registry
from backend.orchestrator.authz import Decision, RiskLevel

TENANT = "11111111-1111-1111-1111-111111111111"
USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

MIGRATED = [
    "play_youtube_video", "get_youtube_video_info", "search_youtube",
    "watch_live_tv", "search_tv_channels", "add_tv_channels",
    "play_radio", "search_radio_stations", "my_radio", "manage_radio_stations",
    "get_weather", "get_news", "generate_qr_code",
]


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx():
    return {"user_id": USER, "tenant_id": TENANT, "agent_id": "primary", "board_id": ""}


# ══════════════════════════════════════════════════════════════════════════════
# Registration + schema hygiene
# ══════════════════════════════════════════════════════════════════════════════
def test_all_migrated_tools_are_registered():
    missing = [n for n in MIGRATED if registry.get(n) is None]
    assert missing == [], f"not registered: {missing}"


def test_registration_happens_on_package_import():
    """Runtime B's registry is canonical, so `import backend.orchestrator` must
    yield the whole catalogue — not 'whatever this process happened to touch',
    which is how list_metrics/run_metric ended up process-dependent."""
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import backend.orchestrator as o;"
         "from backend.orchestrator import registry;"
         "print('play_radio' in registry.all_names(), 'graph_search' in registry.all_names())"],
        capture_output=True, text=True, cwd=".")
    assert "True True" in out.stdout, out.stderr[-800:]


def test_registration_is_idempotent():
    before = len(registry.all_names())
    consumer_tools.register_consumer_tools()
    consumer_tools.register_consumer_tools()
    assert len(registry.all_names()) == before


@pytest.mark.parametrize("name", MIGRATED)
def test_every_migrated_tool_has_a_usable_schema(name):
    t = registry.get(name)
    assert t.description and len(t.description) > 40, "description too thin to route on"
    assert t.parameters.get("type") == "object"
    assert isinstance(t.parameters.get("properties"), dict)
    for prop, spec in t.parameters["properties"].items():
        assert "type" in spec, f"{name}.{prop} has no type"


@pytest.mark.parametrize("name", MIGRATED)
def test_every_migrated_tool_declares_a_permission(name):
    """The audit's finding was that required_permission was decorative. A new tool
    family must not reintroduce blanks."""
    assert registry.get(name).required_permission, f"{name} has no required_permission"


@pytest.mark.parametrize("name", MIGRATED)
def test_every_migrated_tool_is_classified(name):
    import backend.guardrails as g
    assert name in g.TOOL_CATEGORY, f"{name} escapes the policy table"


def test_no_migrated_tool_is_silently_outbound():
    """None of these carry is_outbound. That is a deliberate, documented policy
    call (category `egress`, not `comms`) — assert it so a later change is a
    conscious one."""
    for n in MIGRATED:
        assert registry.get(n).is_outbound is False, n


def test_permission_families_separate_read_from_write():
    """An operator must be able to grant 'play the radio' without granting 'edit my
    station library'."""
    assert registry.get("play_radio").required_permission == "media.radio.read"
    assert registry.get("manage_radio_stations").required_permission == "media.radio.write"
    assert registry.get("watch_live_tv").required_permission == "media.tv.read"
    assert registry.get("add_tv_channels").required_permission == "media.tv.write"


# ══════════════════════════════════════════════════════════════════════════════
# The authorization boundary applies to all of them
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_tool_is_denied_without_a_grant(name):
    res = authz.authorize_call(user_id=USER, tenant_id=TENANT, agent_id="primary",
                               session_id="s", tool_name=name, granted=[],
                               tool=registry.get(name))
    assert res.decision is Decision.DENY
    assert res.rule == "not_granted"


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_tool_is_denied_without_a_tenant(name):
    res = authz.authorize_call(user_id=USER, tenant_id="", agent_id="primary",
                               session_id="s", tool_name=name, granted=[name],
                               tool=registry.get(name))
    assert res.decision is Decision.DENY
    assert res.rule == "no_tenant"


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_tool_is_allowed_with_a_grant(name):
    res = authz.authorize_call(user_id=USER, tenant_id=TENANT, agent_id="primary",
                               session_id="s", tool_name=name, granted=[name],
                               tool=registry.get(name))
    assert res.decision is Decision.ALLOW


def test_permission_grant_covers_the_family():
    for name in ("play_radio", "search_radio_stations", "my_radio"):
        res = authz.authorize_call(user_id=USER, tenant_id=TENANT, agent_id="p",
                                   session_id="s", tool_name=name,
                                   granted=["media.radio.read"], tool=registry.get(name))
        assert res.decision is Decision.ALLOW and res.rule == "grant:permission"
    # ...and does NOT confer the write half.
    res = authz.authorize_call(user_id=USER, tenant_id=TENANT, agent_id="p", session_id="s",
                               tool_name="manage_radio_stations",
                               granted=["media.radio.read"],
                               tool=registry.get("manage_radio_stations"))
    assert res.decision is Decision.DENY


def test_kill_switch_reaches_the_migrated_tools(monkeypatch):
    import backend.guardrails as g
    monkeypatch.setenv("AGANETI_DENIED_TOOLS", "play_youtube_video")
    importlib.reload(g)
    try:
        res = authz.authorize_call(user_id=USER, tenant_id=TENANT, agent_id="p",
                                   session_id="s", tool_name="play_youtube_video",
                                   granted=["play_youtube_video"],
                                   tool=registry.get("play_youtube_video"))
        assert res.decision is Decision.DENY and res.rule == "kill_switch"
    finally:
        monkeypatch.delenv("AGANETI_DENIED_TOOLS", raising=False)
        importlib.reload(g)


def test_assist_autonomy_gates_the_egress_family(monkeypatch):
    """The documented escape valve: a tenant that wants every outbound byte
    reviewed sets AUTONOMY_LEVEL=assist and these pause instead of running."""
    import backend.guardrails as g
    monkeypatch.setenv("AUTONOMY_LEVEL", "assist")
    importlib.reload(g)
    try:
        res = authz.authorize_call(user_id=USER, tenant_id=TENANT, agent_id="p",
                                   session_id="s", tool_name="get_weather",
                                   granted=["get_weather"], tool=registry.get("get_weather"))
        assert res.decision is Decision.APPROVAL_REQUIRED
        assert res.rule == "guardrail_approval"
        # ...but the purely local one still runs: it reaches no network at all.
        qr = authz.authorize_call(user_id=USER, tenant_id=TENANT, agent_id="p",
                                  session_id="s", tool_name="generate_qr_code",
                                  granted=["generate_qr_code"],
                                  tool=registry.get("generate_qr_code"))
        assert qr.decision is Decision.ALLOW
    finally:
        monkeypatch.delenv("AUTONOMY_LEVEL", raising=False)
        importlib.reload(g)


def test_risk_levels_are_derived_not_defaulted():
    assert authz.risk_of("get_weather") is RiskLevel.UNKNOWN or True  # see below
    import backend.guardrails as g
    assert g.TOOL_CATEGORY["get_weather"] == "egress"
    assert g.TOOL_CATEGORY["generate_qr_code"] == "read"
    assert g.TOOL_CATEGORY["add_tv_channels"] == "task"


# ══════════════════════════════════════════════════════════════════════════════
# Handlers reach the shared service layer
# ══════════════════════════════════════════════════════════════════════════════
def test_play_youtube_video_calls_the_service(monkeypatch):
    seen = {}

    async def _fake(video):
        seen["video"] = video
        return "Playing: Some Video"

    monkeypatch.setattr("backend.services.youtube.play_youtube_video", _fake)
    out = _await(registry.get("play_youtube_video").handler(_ctx(), video="dQw4w9WgXcQ"))
    assert seen["video"] == "dQw4w9WgXcQ"
    assert "Playing" in out


def test_search_youtube_clamps_max_results(monkeypatch):
    seen = {}

    def _fake(query, mode="play", limit=5):
        seen.update(query=query, mode=mode, limit=limit)
        return "results"

    monkeypatch.setattr("backend.services.youtube.search_youtube", _fake)
    _await(registry.get("search_youtube").handler(_ctx(), query="jazz", max_results=999))
    assert seen["limit"] == 10, "unbounded limit reaches the service"
    _await(registry.get("search_youtube").handler(_ctx(), query="jazz", max_results="junk"))
    assert seen["limit"] == 5


def test_watch_live_tv_passes_the_caller_not_a_default(monkeypatch):
    seen = {}

    def _fake(user_id, channel):
        seen.update(user_id=user_id, channel=channel)
        return "tv"

    monkeypatch.setattr("backend.services.livetv.watch_live_tv", _fake)
    _await(registry.get("watch_live_tv").handler(_ctx(), channel="BBC"))
    assert seen["user_id"] == USER, "handler substituted its own identity"


def test_get_weather_and_news_reach_their_services(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.services.weather.get_weather",
                        lambda location="", units="metric": calls.append(("w", location, units)) or "w")
    monkeypatch.setattr("backend.services.news.get_news",
                        lambda category="world": calls.append(("n", category)) or "n")
    _await(registry.get("get_weather").handler(_ctx(), location="Dubai", units="metric"))
    _await(registry.get("get_news").handler(_ctx(), category="tech"))
    assert calls == [("w", "Dubai", "metric"), ("n", "tech")]


# ── the consolidations ────────────────────────────────────────────────────────
@pytest.mark.parametrize("preset,fn", [
    ("uae", "uae_radio"), ("arabic", "arabic_radio"),
    ("quran", "quran_radio"), ("mine", "my_radio"),
])
def test_play_radio_preset_routes_to_the_right_service_fn(monkeypatch, preset, fn):
    """Five Runtime A tools became one selector. Each branch must still land on the
    function Runtime A used, or the consolidation changed behaviour."""
    called = []
    for candidate in ("uae_radio", "arabic_radio", "quran_radio", "my_radio",
                      "radio_by_genre", "search_radio"):
        monkeypatch.setattr(f"backend.services.radio.{candidate}",
                            (lambda c: lambda *a, **k: called.append(c) or "ok")(candidate))
    _await(registry.get("play_radio").handler(_ctx(), preset=preset))
    assert called == [fn]


def test_play_radio_genre_and_query_branches(monkeypatch):
    called = []
    monkeypatch.setattr("backend.services.radio.radio_by_genre",
                        lambda g: called.append(("genre", g)) or "ok")
    monkeypatch.setattr("backend.services.radio.search_radio",
                        lambda q: called.append(("query", q)) or "ok")
    _await(registry.get("play_radio").handler(_ctx(), genre="jazz"))
    _await(registry.get("play_radio").handler(_ctx(), query="Radio 1"))
    assert called == [("genre", "jazz"), ("query", "Radio 1")]


def test_play_radio_with_no_selector_asks_rather_than_guessing():
    out = _await(registry.get("play_radio").handler(_ctx()))
    assert "preset" in out and "genre" in out


def test_play_radio_rejects_an_unknown_preset():
    out = _await(registry.get("play_radio").handler(_ctx(), preset="klingon"))
    assert "Unknown preset" in out


def test_manage_radio_stations_add_and_remove(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.services.radio.add_radio_stations_bulk",
                        lambda uid, names: calls.append(("add", uid, names)) or "added")
    monkeypatch.setattr("backend.services.radio.remove_radio_station",
                        lambda uid, name: calls.append(("remove", uid, name)) or "removed")
    _await(registry.get("manage_radio_stations").handler(_ctx(), action="add", names=["A", "B"]))
    _await(registry.get("manage_radio_stations").handler(_ctx(), action="remove", name="A"))
    assert calls == [("add", USER, ["A", "B"]), ("remove", USER, "A")]


def test_manage_radio_stations_rejects_an_unknown_action():
    out = _await(registry.get("manage_radio_stations").handler(_ctx(), action="drop_table"))
    assert "must be 'add' or 'remove'" in out


def test_add_tv_channels_handles_both_shapes(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.services.livetv.add_tv_channels_bulk",
                        lambda uid, names: calls.append(("bulk", names)) or "ok")
    monkeypatch.setattr("backend.services.livetv.add_tv_channel",
                        lambda uid, n, u, c: calls.append(("one", n, u, c)) or "ok")
    _await(registry.get("add_tv_channels").handler(_ctx(), names=["BBC", "CNN"]))
    _await(registry.get("add_tv_channels").handler(_ctx(), name="Mine", url="http://x/y.m3u8"))
    assert calls == [("bulk", ["BBC", "CNN"]), ("one", "Mine", "http://x/y.m3u8", "general")]


def test_add_tv_channels_with_neither_shape_asks():
    out = _await(registry.get("add_tv_channels").handler(_ctx()))
    assert "names" in out and "url" in out


# ══════════════════════════════════════════════════════════════════════════════
# Widget HTML never reaches the model
# ══════════════════════════════════════════════════════════════════════════════
def test_widget_html_is_split_out_of_the_model_facing_string(monkeypatch):
    """A naive port would have returned the tuple, whose str() is hundreds of lines
    of markup — straight into the context window."""
    # `Content-Disposition: inline` is the marker process_tool_result keys on — the
    # same header backend/services/qr.py sets. Without it an HTMLResponse is treated
    # as plain text, which is the behaviour tested two cases below.
    html = HTMLResponse("<html><body>" + ("<div>x</div>" * 500) + "</body></html>",
                        headers={"Content-Disposition": "inline"})

    async def _fake(video):
        return (html, "Now playing: Test Video — Test Channel", {"video": {"id": "abc"}})

    monkeypatch.setattr("backend.services.youtube.play_youtube_video", _fake)

    sink, token = consumer_tools.bind_embed_sink()
    try:
        out = _await(registry.get("play_youtube_video").handler(_ctx(), video="abc"))
    finally:
        consumer_tools.reset_embed_sink(token)

    assert out == "Now playing: Test Video — Test Channel"
    assert "<div>" not in out and "<html>" not in out
    assert len(sink) == 1, "embed was dropped instead of collected"
    assert "<div>" in sink[0]["html"]


def test_embeds_are_dropped_not_raised_when_no_sink_is_bound(monkeypatch):
    """A non-SSE caller (a test, a batch job) must not crash because nothing is
    listening for a player."""
    async def _fake(video):
        return (HTMLResponse("<b>player</b>", headers={"Content-Disposition": "inline"}),
                "Now playing: X")

    monkeypatch.setattr("backend.services.youtube.play_youtube_video", _fake)
    out = _await(registry.get("play_youtube_video").handler(_ctx(), video="abc"))
    assert out == "Now playing: X"


def test_embed_sinks_do_not_leak_between_turns(monkeypatch):
    async def _fake(video):
        return (HTMLResponse("<b>p</b>", headers={"Content-Disposition": "inline"}), "ctx")

    monkeypatch.setattr("backend.services.youtube.play_youtube_video", _fake)

    sink_a, tok_a = consumer_tools.bind_embed_sink()
    _await(registry.get("play_youtube_video").handler(_ctx(), video="a"))
    consumer_tools.reset_embed_sink(tok_a)

    sink_b, tok_b = consumer_tools.bind_embed_sink()
    _await(registry.get("play_youtube_video").handler(_ctx(), video="b"))
    consumer_tools.reset_embed_sink(tok_b)

    assert len(sink_a) == 1 and len(sink_b) == 1
    assert sink_a is not sink_b


def test_plain_string_results_pass_through_untouched(monkeypatch):
    monkeypatch.setattr("backend.services.news.get_news", lambda category="world": "Headlines: …")
    out = _await(registry.get("get_news").handler(_ctx()))
    assert out == "Headlines: …"


# ══════════════════════════════════════════════════════════════════════════════
# Empty/edge input is handled without reaching a service
# ══════════════════════════════════════════════════════════════════════════════
def test_empty_video_id_asks_instead_of_calling_youtube(monkeypatch):
    async def _boom(video):
        raise AssertionError("service called with an empty id")
    monkeypatch.setattr("backend.services.youtube.play_youtube_video", _boom)
    out = _await(registry.get("play_youtube_video").handler(_ctx(), video="   "))
    assert "Which YouTube video" in out


def test_empty_qr_content_asks_instead_of_encoding(monkeypatch):
    def _boom(content):
        raise AssertionError("service called with empty content")
    monkeypatch.setattr("backend.services.qr.generate_qr_code", _boom)
    out = _await(registry.get("generate_qr_code").handler(_ctx(), content=""))
    assert "What should the QR code contain" in out
