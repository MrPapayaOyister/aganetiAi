"""QR codes — encode a string, render it as a chat widget.

Ported from FNC Digital Transformation's `qr_code_generator_for_open_webui`
(reference/tool-qr_code_generator_for_open_webui-export-*.json). The qrcode
settings and the card layout are theirs; the adaptations are ours.

TWO FIXES TO THE ORIGINAL, both about untrusted text reaching the page:

  1. `content` was interpolated into the card unescaped, in the caption AND the
     image `alt`. The string comes from the model, which is repeating something a
     user typed, so `</div><script>…` lands in the document. Our frames are
     sandboxed with allow-scripts and no allow-same-origin, so injected script
     cannot touch the app's origin or cookies — but it can still forge the
     `iframe:height` postMessage the card uses to size itself, repaint the card
     as something it isn't, and beacon the encoded content out via an image URL.
     Containment is not a reason to emit broken markup; both sites are escaped.

  2. The failure branch put `str(e)` into the returned HTML. Exception text from
     qrcode/PIL carries library internals and, for encoding failures, fragments of
     the input itself. The exception is now logged server-side and the page shows
     a fixed sentence.

HOW THIS DIFFERS FROM weather / news
------------------------------------
Those widgets are SNAPSHOTS: re-running them tomorrow yields a different forecast
or a different top story, so `chat_artifacts.data.html` is the record of what the
user actually saw and rehydration replays it.

A QR code is a pure function of its input. The same string always produces the
same modules, so the spec — one field, `content` — regenerates the card exactly.
That makes stored HTML a FALLBACK here rather than the source of truth, and it is
what `regenerate_from_spec` below exists for. Practical consequence: a QR embed
survives a change to this card's markup (it re-renders in the new style), where a
weather card is frozen in whatever markup shipped the day it was created.

Determinism is a property we depend on, so it is asserted in
tests/test_qr.py rather than assumed: fixed version/box_size/border, no
timestamp in the PNG, no `rendered_at` inside the HTML.
"""

from __future__ import annotations

import base64
import html as _html
import io
import logging

from fastapi.responses import HTMLResponse

log = logging.getLogger("aria.qr")

# Version 40 at error-correction H holds 1273 bytes. Refusing past that with a
# sentence beats letting qrcode raise DataOverflowError into the generic handler,
# which is how the original surfaced it — as a 500 with the library's own wording.
MAX_CONTENT_BYTES = 1200

# The original's settings, kept verbatim: changing them changes every QR this
# tool has ever rendered, because rehydration regenerates from spec.
_BOX_SIZE = 10
_BORDER = 4
_CAPTION_MAX = 120


def _card(body: str) -> str:
    """Full document. Our CSP is injected first by lib/embedWidget.ts and wins;
    this card needs only inline style/script, which the default profile grants,
    so it renders unchanged inside the sandbox with no bespoke profile."""
    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ margin: 0; padding: 20px; display: flex; justify-content: center;
         align-items: center; background: #ffffff;
         font-family: system-ui, -apple-system, sans-serif; }}
  .qr-container {{ text-align: center; }}
  img {{ max-width: 250px; height: auto; border: 4px solid #333;
        border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
  .label {{ margin-top: 10px; color: #666; font-size: 14px; word-break: break-all; }}
  .err {{ color: #dc2626; font-size: 14px; }}
</style>
</head>
<body>
{body}
<script>
  // Height is reported so the host can size the frame, which starts at its
  // 40px minimum and grows to whatever arrives.
  //
  // Reporting ONLY on window.load was a bug: at that instant the document had
  // not laid out to its final size and scrollHeight still read the 40px
  // viewport, so the card posted 40, the host clamped 40 to 40, and the QR
  // rendered as a horizontal strip with scrollbars. The image is a data: URI,
  // which is why it looked like it should already be measurable.
  //
  // So: report whenever the size actually changes, not once at a moment we
  // guessed was late enough. The observer fires on the initial layout too, so
  // it subsumes the load handler rather than merely backing it up.
  function reportHeight() {{
    var h = Math.max(document.documentElement.scrollHeight,
                     document.body ? document.body.scrollHeight : 0);
    if (h > 0) parent.postMessage({{ type: 'iframe:height', height: h }}, '*');
  }}
  window.addEventListener('load', reportHeight);
  if (window.ResizeObserver) {{
    new ResizeObserver(reportHeight).observe(document.documentElement);
  }}
</script>
</body>
</html>"""


def _error_card(message: str) -> HTMLResponse:
    """A failure the user can act on, with nothing from the exception in it."""
    return HTMLResponse(
        content=_card(f'<div class="qr-container"><p class="err">{_html.escape(message)}</p></div>'),
        status_code=200,   # the widget rendered; the ENCODE failed, and says so
        headers={"Content-Disposition": "inline"},
    )


def _png_b64(content: str) -> str:
    import qrcode

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=_BOX_SIZE,
        border=_BORDER,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    # optimize=False keeps the encoder on its default, deterministic path.
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def render(content: str) -> HTMLResponse:
    """The card for `content`. Pure: same input, same bytes, no I/O, no clock."""
    text = content if isinstance(content, str) else str(content or "")
    if not text.strip():
        return _error_card("Nothing to encode — give me a link or some text.")

    size = len(text.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        return _error_card(
            f"That's {size} characters — too long for a QR code "
            f"(the limit is about {MAX_CONTENT_BYTES}). Try a shorter link.")

    try:
        b64 = _png_b64(text)
    except Exception:  # noqa: BLE001
        # Logged with the input length, never the input: the string may be a
        # private URL and this line goes to shared logs.
        log.exception("QR encode failed (%d bytes of content)", size)
        return _error_card("I couldn't generate that QR code.")

    # BOTH escapes are the fix. The caption is truncated for layout, not safety —
    # escaping is what makes it safe, and it happens after truncation so a cut
    # can never land inside an entity.
    caption = text if len(text) <= _CAPTION_MAX else text[:_CAPTION_MAX] + "…"
    safe_caption = _html.escape(caption)
    body = (
        '<div class="qr-container">'
        f'<img src="data:image/png;base64,{b64}" alt="QR code for {safe_caption}" />'
        f'<div class="label">{safe_caption}</div>'
        "</div>"
    )
    return HTMLResponse(content=_card(body), headers={"Content-Disposition": "inline"})


def generate_qr_code(content: str) -> tuple[HTMLResponse, str, dict]:
    """Tool entry point → (html, context for the model, embed meta).

    `qr` in the meta is the reproducible spec. It is what
    backend/main.py persists and hands back to regenerate_from_spec on reload,
    so it must stay small and contain everything render() needs.
    """
    text = content if isinstance(content, str) else str(content or "")
    resp = render(text)
    shown = text if len(text) <= 80 else text[:80] + "…"
    context = (
        f"QR code generated and displayed for: {shown}"
        if text.strip() else
        "No content was supplied, so the card asks the user what to encode."
    )
    return resp, context, {"qr": {"content": text}}


def regenerate_from_spec(spec: dict) -> str | None:
    """Rebuild the card's HTML from a persisted spec, or None if it can't.

    Called on history load. None means "keep whatever HTML was stored" — the
    caller treats this as a fallback rather than a failure, so a spec written by
    a future version that this code cannot read degrades to the snapshot instead
    of blanking the widget.
    """
    if not isinstance(spec, dict):
        return None
    content = spec.get("content")
    if not isinstance(content, str):
        return None
    try:
        return render(content).body.decode("utf-8")
    except Exception:  # noqa: BLE001
        log.exception("QR regenerate failed; falling back to stored html")
        return None
