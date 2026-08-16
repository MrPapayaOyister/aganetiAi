"""QR codes: the two ported fixes, and the determinism the rehydrate path relies on.

The original (reference/tool-qr_code_generator_for_open_webui-export-*.json)
interpolated `content` into the card unescaped and put `str(e)` into its error
page. Both are asserted here against the EMITTED HTML rather than the source, so
they still fail if the escaping is moved, wrapped, or reintroduced elsewhere.

Determinism is not a nicety here: backend/main.py regenerates this widget from
its spec on history load instead of replaying stored HTML. If the same content
ever produced different bytes, a reloaded conversation would show a different
image from the one the user scanned.
"""

import re

import pytest

from backend.services import qr


def _html(resp) -> str:
    return resp.body.decode("utf-8")


# ── fix 1: content is escaped ────────────────────────────────────────────────

def test_script_tag_in_content_does_not_reach_the_document():
    """THE injection case. Sandboxing contains it; emitting it is still a defect."""
    payload = '</div><script>parent.postMessage({type:"iframe:height",height:99999},"*")</script>'
    out = _html(qr.render(payload))
    assert "<script>parent.postMessage" not in out
    assert "&lt;script&gt;" in out


def test_quotes_cannot_break_out_of_the_alt_attribute():
    """The caption is also interpolated into `alt`, which the original left raw —
    a bare quote there closes the attribute and opens a handler."""
    out = _html(qr.render('" onerror="alert(1)'))
    assert 'onerror="alert(1)"' not in out
    assert "&quot;" in out or "&#x27;" in out


def test_ampersand_in_a_url_is_escaped_not_mangled():
    """Real URLs carry &; it must be escaped in the markup while the ENCODED
    payload stays byte-exact — the scan has to match what the user typed."""
    url = "https://example.com/a?x=1&y=2"
    out = _html(qr.render(url))
    assert "&amp;" in out
    # and the code itself encodes the raw string, not the escaped one
    assert qr._png_b64(url) == qr._png_b64("https://example.com/a?x=1&y=2")


# ── fix 2: no exception text in the page ─────────────────────────────────────

def test_encode_failure_does_not_leak_the_exception(monkeypatch):
    def boom(_):
        raise RuntimeError("PIL internals: /home/matrix/secret/path.png missing")

    monkeypatch.setattr(qr, "_png_b64", boom)
    out = _html(qr.render("hello"))
    assert "PIL internals" not in out
    assert "/home/matrix" not in out
    assert "RuntimeError" not in out
    # Phrase chosen without an apostrophe: the message itself goes through
    # html.escape, which turns "couldn't" into "couldn&#x27;t" — matching on the
    # raw wording would fail on the escaping actually working.
    assert "generate that qr code" in out.lower()


def test_oversized_content_is_refused_in_words_not_by_raising():
    """Past QR capacity the library raises DataOverflowError. The original let it
    hit the generic handler and rendered the library's own wording as a 500."""
    out = _html(qr.render("x" * (qr.MAX_CONTENT_BYTES + 50)))
    assert "too long" in out.lower()
    assert "DataOverflow" not in out


def test_empty_content_asks_rather_than_erroring():
    out = _html(qr.render("   "))
    assert "nothing to encode" in out.lower()


# ── determinism: what the regenerate-on-rehydrate branch depends on ──────────

def test_same_content_produces_byte_identical_html():
    a = _html(qr.render("https://example.com/thing"))
    b = _html(qr.render("https://example.com/thing"))
    assert a == b


def test_height_is_reported_on_layout_not_only_on_load():
    """The card rendered as a squashed strip with scrollbars.

    It reported height once, on window.load, when the document had not laid out
    and scrollHeight still read the host's 40px starting viewport. The host
    clamped 40 to 40 and the frame never grew — while the image inside was a
    perfectly correct 250x250. A single late-enough-looking moment is not a
    layout signal; an observer is.
    """
    out = _html(qr.render("https://example.com"))
    assert "ResizeObserver" in out, "no observer — the card can only report once"
    assert "iframe:height" in out
    # And it must not post a zero/absent height, which the host ignores anyway
    # but which would leave the frame stuck at its minimum.
    assert "if (h > 0)" in out


def test_the_card_embeds_no_timestamp():
    """A clock anywhere in the output would make regeneration differ from the
    snapshot on every reload, and the difference would be invisible."""
    out = _html(qr.render("https://example.com"))
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", out), "ISO timestamp in card"
    assert "rendered_at" not in out


def test_different_content_produces_different_images():
    assert qr._png_b64("one") != qr._png_b64("two")


# ── the tool contract ────────────────────────────────────────────────────────

def test_generate_returns_html_context_and_a_reproducible_spec():
    resp, context, meta = qr.generate_qr_code("https://example.com/x")
    assert resp.headers.get("Content-Disposition") == "inline", "must split as an embed"
    assert "https://example.com/x" in context
    assert meta["qr"] == {"content": "https://example.com/x"}


def test_context_does_not_carry_the_markup():
    """The model must never see the HTML — it would summarise or re-emit it."""
    _, context, _ = qr.generate_qr_code("https://example.com")
    assert "<" not in context and "base64" not in context


# ── regenerate_from_spec: the fallback contract ──────────────────────────────

def test_regenerate_reproduces_the_original_card_exactly():
    content = "https://example.com/scan-me"
    live = _html(qr.render(content))
    rebuilt = qr.regenerate_from_spec({"content": content})
    assert rebuilt == live


def test_regenerate_returns_none_for_an_unreadable_spec():
    """None means 'keep the stored html'. A spec from a future version must
    degrade to the snapshot, never blank the widget."""
    assert qr.regenerate_from_spec({}) is None
    assert qr.regenerate_from_spec({"content": None}) is None
    assert qr.regenerate_from_spec(None) is None
    assert qr.regenerate_from_spec({"unknown_future_key": 1}) is None


def test_regenerate_survives_an_internal_failure(monkeypatch):
    def boom(_):
        raise RuntimeError("nope")

    monkeypatch.setattr(qr, "render", boom)
    assert qr.regenerate_from_spec({"content": "x"}) is None


# ── wiring ───────────────────────────────────────────────────────────────────

def test_tool_is_registered_read_and_not_volatile():
    import backend.guardrails as guardrails
    import backend.tools as tools

    assert tools.group_of("generate_qr_code") == "qr"
    assert guardrails.decide("generate_qr_code") == "auto"
    assert "generate_qr_code" in tools.READ_TOOLS
    assert "generate_qr_code" not in tools.ACTION_TOOLS
    # Same input, same output — marking it stale would invite a pointless re-run.
    assert "generate_qr_code" not in tools.VOLATILE_TOOLS


def test_the_spec_key_is_persisted_and_rehydrated():
    """Structural: the spec has to survive the round trip or regeneration can
    never fire, and the failure would look like 'the card just uses stored html'."""
    import inspect

    import backend.main as main

    assert '"qr"' in inspect.getsource(main._persist_embeds), \
        "qr spec is not written to chat_artifacts"
    assert "regenerate_from_spec" in inspect.getsource(main._embeds_from_artifacts), \
        "history load does not regenerate deterministic embeds"
