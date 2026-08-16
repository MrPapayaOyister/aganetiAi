"""Part 2 — embeds survive a reload as chat_artifacts rows.

Reopening a thread must show the widgets the user actually saw. The tool is NOT
re-run: that would fire side effects, cost latency, and (for anything
time-varying) show different content from what was on screen. So `data` holds
the rendered snapshot and `spec` holds the reproducible descriptor, following the
doctrine already written into the chat_artifacts table.
"""

import backend.main as main


def _embed(html="<html><head></head><body>x</body></html>", **kw):
    base = {"tool": "search_youtube", "html": html, "link": None, "video": None,
            "results": None, "query": None}
    base.update(kw)
    return base


def _capture(monkeypatch):
    calls = []
    from backend.chat import store as chat_store
    def fake(uid, sid, **kw):
        calls.append({"uid": uid, "sid": sid, **kw})
        return f"art-{len(calls)}"
    monkeypatch.setattr(chat_store, "add_artifact", fake)
    return calls


# ── write ────────────────────────────────────────────────────────────────────

def test_spec_data_split_and_rendered_at(monkeypatch):
    calls = _capture(monkeypatch)
    n = main._persist_embeds("s1", "user_1", "msg-1", [_embed(
        video={"id": "dQw4w9WgXcQ", "start": 90},
        link={"url": "https://youtu.be/dQw4w9WgXcQ", "label": "Rick Astley"})])
    assert n == 1
    c = calls[0]
    assert c["kind"] == "embed" and c["message_id"] == "msg-1"
    assert c["spec"]["video"] == {"id": "dQw4w9WgXcQ", "start": 90}
    assert c["spec"]["tool"] == "search_youtube"
    assert c["data"]["html"].startswith("<html>")
    assert c["meta"]["rendered_at"], "rehydrated embeds need a timestamp for the chip"
    assert "oversized" not in c["meta"]


def test_one_row_per_embed(monkeypatch):
    calls = _capture(monkeypatch)
    assert main._persist_embeds("s1", "u", "m1", [_embed(), _embed()]) == 2
    assert len(calls) == 2


def test_oversized_html_is_dropped_but_the_structured_half_survives(monkeypatch):
    """Over the cap we keep the spec, not the snapshot: a player/picker/link still
    rehydrates. What is lost is the fallback card, not the feature."""
    calls = _capture(monkeypatch)
    huge = "<html><head></head><body>" + ("x" * (main.EMBED_HTML_MAX_BYTES + 10)) + "</body></html>"
    main._persist_embeds("s1", "u", "m1", [_embed(
        html=huge, video={"id": "dQw4w9WgXcQ", "start": 0})])
    c = calls[0]
    assert c["data"] == {}, "oversized html must not be stored"
    assert c["meta"]["oversized"] is True
    assert c["meta"]["html_bytes"] > main.EMBED_HTML_MAX_BYTES
    assert c["spec"]["video"]["id"] == "dQw4w9WgXcQ", "structured half must survive"


def test_cap_is_measured_in_bytes_not_characters(monkeypatch):
    """A multi-byte title must not let an embed slip past the cap."""
    calls = _capture(monkeypatch)
    # Just under the cap in characters, comfortably over it in UTF-8 bytes.
    body = "é" * (main.EMBED_HTML_MAX_BYTES - 100)
    main._persist_embeds("s1", "u", "m1", [_embed(html=body)])
    assert calls[0]["meta"].get("oversized") is True


def test_nothing_written_without_a_message_id(monkeypatch):
    calls = _capture(monkeypatch)
    assert main._persist_embeds("s1", "u", None, [_embed()]) == 0
    assert calls == []


def test_persist_failure_is_logged_not_raised(monkeypatch, caplog):
    from backend.chat import store as chat_store
    monkeypatch.setattr(chat_store, "add_artifact", lambda *a, **k: None)
    with caplog.at_level("WARNING"):
        assert main._persist_embeds("s1", "u", "m1", [_embed()]) == 0
    assert any("NOT persisted" in r.message for r in caplog.records)


# ── read ─────────────────────────────────────────────────────────────────────

def test_artifacts_map_back_to_the_client_embed_shape():
    embeds = main._embeds_from_artifacts([{
        "kind": "embed",
        "spec": {"tool": "search_youtube", "query": "rust",
                 "results": [{"id": "dQw4w9WgXcQ", "title": "T"}],
                 "link": {"url": "u", "label": "l"}},
        "data": {"html": "<html></html>"},
        "meta": {"rendered_at": "2026-08-09T10:00:00+00:00"},
    }])
    assert len(embeds) == 1
    e = embeds[0]
    assert e["html"] == "<html></html>" and e["query"] == "rust"
    assert e["results"][0]["id"] == "dQw4w9WgXcQ"
    assert e["link"] == {"url": "u", "label": "l"}
    assert e["renderedAt"] == "2026-08-09T10:00:00+00:00"
    assert e["oversized"] is False


def test_non_embed_artifacts_are_ignored():
    """chat_artifacts is shared with charts/tables/PDFs from the analytics agent."""
    assert main._embeds_from_artifacts([
        {"kind": "chart", "spec": {}, "data": {"rows": []}, "meta": {}},
        {"kind": "pdf", "spec": {}, "data": None, "meta": {}},
    ]) == []


def test_oversized_row_rehydrates_without_html():
    e = main._embeds_from_artifacts([{
        "kind": "embed", "spec": {"video": {"id": "dQw4w9WgXcQ", "start": 0}},
        "data": {}, "meta": {"rendered_at": "2026-08-09T10:00:00+00:00", "oversized": True},
    }])[0]
    assert e["html"] == "" and e["oversized"] is True
    assert e["video"]["id"] == "dQw4w9WgXcQ", "the player still rehydrates"


def test_live_embeds_have_no_rendered_at():
    """The chip must appear only on replayed widgets. A live embed comes straight
    off the SSE wire and never through _embeds_from_artifacts."""
    assert main._embeds_from_artifacts([]) == []


# ── replay safety ────────────────────────────────────────────────────────────
# Stored HTML is attacker-influenced (titles and thumbnails come from YouTube)
# and is replayed verbatim on reload. It must go back through exactly the same
# sandboxed path a live embed takes — never interpolated into the page. There is
# no JS test runner in this project, so these guard the source; the live proof is
# the browser run that reloads a picker and inspects the rendered iframe.

import pathlib

_FE = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "src"


def test_no_raw_html_injection_anywhere_in_the_frontend():
    hits = [p for p in _FE.rglob("*.tsx") if "dangerouslySetInnerHTML" in p.read_text()]
    hits += [p for p in _FE.rglob("*.ts") if "dangerouslySetInnerHTML" in p.read_text()]
    assert hits == [], f"raw HTML injection found in {hits} — replayed embeds must use an iframe"


def test_replayed_html_goes_through_render_embed():
    src = (_FE / "components" / "ToolEmbeds.tsx").read_text()
    # Prefix match, not the exact call: the options argument grew a CSP profile
    # and will grow again. The property under test is that the frame is built by
    # lib/embedWidget, not that the call has a particular arity.
    assert "renderEmbed(host, html" in src, \
        "the srcdoc card must be created by lib/embedWidget, not inline"
    # The word appears in the comments explaining why; what must not appear is an
    # ASSIGNMENT — this component may never construct a srcdoc frame itself, and
    # so can never be the place the CSP or the sandbox flags get forgotten.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith(("*", "/*", "//")))
    assert ".srcdoc" not in code and "srcdoc=" not in code, \
        "ToolEmbeds must not assign srcdoc itself — route it through renderEmbed"


def test_sandbox_stays_allow_scripts_only_for_srcdoc():
    src = (_FE / "lib" / "embedWidget.ts").read_text()
    for flag in ("allowSameOrigin", "allowPopups", "allowForms", "allowDownloads"):
        assert f"{flag} = false" in src, f"{flag} must default to false for srcdoc embeds"
    assert "'allow-scripts'," in src


def test_csp_is_injected_into_every_srcdoc():
    src = (_FE / "lib" / "embedWidget.ts").read_text()
    assert "iframe.srcdoc = injectCsp(html, csp)" in src, \
        "srcdoc must never be assigned without the CSP"
    # first-in-head is what makes it override any policy the tool's HTML set
    assert "html.indexOf('<head>')" in src


def test_zero_height_reports_are_ignored_by_the_clamp():
    """A card firing before layout posts height 0. Clamping that to minHeight
    collapses the frame, and if the real measurement is delayed or dropped it
    stays collapsed — observed live as a weather card stuck at 40px."""
    src = (_FE / "lib" / "embedWidget.ts").read_text()
    assert "data.height > 0" in src, \
        "non-positive heights must be ignored, not clamped up to minHeight"
