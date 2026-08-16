"""What survives a page reload, and what quietly does not.

`chat_artifacts` splits an embed in two: `spec` is the reproducible descriptor
and `data.html` is the snapshot of what the user saw. Anything absent from the
spec tuple can only ever come back as that snapshot — which for a widget whose
interactive half is rendered by REACT, outside the sandboxed frame, means the
feature disappears while the card still looks fine. That is the failure mode
these tests exist for: not a crash, a picker that stops being a picker.

Three keys were emitted by tools, accepted by the frontend's EmbedItem type, and
dropped in between:

  channels — the multi-select TV picker
  articles — the news links React draws outside the frame
  csp      — the PROFILE NAME a video embed needs; without it a rehydrated
             live_tv player falls back to the strict default and hls.js cannot
             fetch its manifest, which reads as a dead player

Asserted against the real functions rather than a copy of the tuple, so moving
the logic does not quietly move the guarantee.
"""

import inspect

import pytest

import backend.main as main


SPEC_KEYS = ("tool", "link", "video", "results", "query", "qr",
             "channels", "articles", "csp")


def _artifact(spec: dict, html: str = "<!--snapshot-->") -> dict:
    return {"kind": "embed", "spec": spec, "data": {"html": html},
            "meta": {"rendered_at": "2026-08-12T00:00:00+00:00"}}


# ── persistence ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", SPEC_KEYS)
def test_every_structured_key_is_written_to_the_spec(key):
    """A key the writer drops cannot be recovered by any reader."""
    src = inspect.getsource(main._persist_embeds)
    assert f'"{key}"' in src, f"{key} is emitted by a tool but never persisted"


# ── rehydration ──────────────────────────────────────────────────────────────

def test_news_articles_come_back_without_the_snapshot():
    """The links are React's, outside the frame. Losing them leaves a card the
    user can read but not click."""
    arts = [{"title": "A", "url": "https://example.com/a", "category": "world"}]
    out = main._embeds_from_artifacts([_artifact({"articles": arts})])
    assert out[0]["articles"] == arts


def test_tv_channels_come_back_without_the_snapshot():
    chans = [{"id": "x.tv", "name": "X", "url": "https://e/x.m3u8"}]
    out = main._embeds_from_artifacts([_artifact({"channels": chans, "query": "news"})])
    assert out[0]["channels"] == chans
    assert out[0]["query"] == "news"


def test_the_video_csp_profile_survives_reload():
    """The subtlest of the three: the card still renders, the player still draws,
    and the stream silently fails because the default policy forbids the fetches
    hls.js needs."""
    out = main._embeds_from_artifacts([_artifact({"csp": "video"})])
    assert out[0]["csp"] == "video"


def test_structured_data_does_not_depend_on_stored_html():
    """The oversized case, and the reason this matters in production: over
    EMBED_HTML_MAX_BYTES the html is dropped on purpose and only the spec
    remains. The interactive half must still work."""
    out = main._embeds_from_artifacts([
        {"kind": "embed", "meta": {"oversized": True}, "data": {},
         "spec": {"channels": [{"id": "a", "name": "A", "url": "https://e/a.m3u8"}],
                  "csp": "video"}}])
    assert out[0]["html"] == ""
    assert out[0]["oversized"] is True
    assert len(out[0]["channels"]) == 1
    assert out[0]["csp"] == "video"


def test_absent_keys_are_none_not_missing():
    """The frontend reads these fields unconditionally; a missing key and a null
    are the same to it, but only if the key is always present."""
    out = main._embeds_from_artifacts([_artifact({})])
    for key in ("link", "video", "results", "query", "channels", "articles", "csp"):
        assert key in out[0], key


def test_non_embed_artifacts_are_ignored():
    """Charts, tables and PDFs share the table and are a different surface."""
    assert main._embeds_from_artifacts([{"kind": "chart", "spec": {"csp": "video"}}]) == []


def test_a_qr_embed_still_regenerates_rather_than_replays():
    """The one deterministic embed. Guarding it here too so widening the tuple
    cannot quietly turn regeneration back into a snapshot replay."""
    out = main._embeds_from_artifacts([
        _artifact({"qr": {"content": "https://example.com/x"}}, html="<!--stale-->")])
    assert "<!--stale-->" not in out[0]["html"]
    assert "base64," in out[0]["html"]
