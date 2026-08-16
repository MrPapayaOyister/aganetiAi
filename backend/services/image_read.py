"""Reading an uploaded image: OCR for what it SAYS, VL for what it IS.

An image is two different things depending on what was photographed. A scan of
an invoice is text — the content IS the words. A photo, a chart or a whiteboard
is not — the content is the subject, and OCR returns nothing useful. Handling
only one case fails the other loudly:

    photo through OCR only   -> "I couldn't read any text out of that file"
    scan through VL only     -> the fabrication this pipeline just removed

So both run, and their outputs are kept in SEPARATE, LABELLED fields.

WHY THE DESCRIPTION PROMPT IS SO CONSTRAINED
--------------------------------------------
Measured on the same four images, an open "describe this image" prompt invents
specifics with total confidence:

    arabic invoice  -> "the Ministry of Interior and Municipalities ... the
                       '12500' project"        (the page says Ministry of
                                                Economy and Tourism)
    KPI table       -> phases "Project Initiation, Design, Development,
                       Deployment"             (none appear on the page)

The same model asked only for the TYPE does not:

    arabic invoice  -> "A scanned document, likely a formal notice or official
                       communication, with Arabic text."
    KPI table       -> "A spreadsheet or table displaying a project plan or task
                       list with columns for tasks, responsible parties, and
                       status."

Both correct, neither naming a value. The rule from the OCR evaluation holds and
is encoded here: trust this model for what a page IS, never for what it SAYS.
The description is also cheaper than the open one — 0.4-0.7s at an 80-token cap.
"""

from __future__ import annotations

import base64
import io
import logging

log = logging.getLogger("aria.image")

#: Same cap as the OCR path, for the same reason: an unresized page render is
#: what killed the shared vision engine. See ingest.OCR_MAX_EDGE_PX.
DESCRIBE_MAX_EDGE_PX = 640

#: One sentence. The cap is part of the safety argument, not just cost — there
#: is no room to start listing values.
DESCRIBE_MAX_TOKENS = 80

DESCRIBE_PROMPT = (
    "In ONE short sentence, say what KIND of image this is (for example: a "
    "scanned invoice, a photograph, a chart, a screenshot, a diagram). "
    "Describe only the type and general subject. Do NOT quote or summarise any "
    "numbers, names, dates or other specific values from it."
)


def describe(path: str) -> str:
    """One line naming what kind of image this is. "" if unavailable.

    Best-effort by design: a missing description degrades an image to its OCR
    text, which is the same place we were before. A failure here must never
    fail the upload.
    """
    try:
        from PIL import Image

        from backend.services import llm as _llm
        from config.settings import LLM_VISION_MODEL as vl_model

        im = Image.open(path).convert("RGB")
        im.thumbnail((DESCRIBE_MAX_EDGE_PX, DESCRIBE_MAX_EDGE_PX), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        out = _llm.complete(
            [{"role": "user", "content": [
                {"type": "text", "text": DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": url}}]}],
            model=vl_model, temperature=0, max_tokens=DESCRIBE_MAX_TOKENS,
            timeout=60.0)
        return " ".join((out or "").split())
    except Exception:  # noqa: BLE001
        log.exception("image description failed for %s", path)
        return ""


def ocr(path: str) -> str:
    """Text in the image, via the same Paddle worker the PDF path uses.

    PyMuPDF opens an image as a one-page document, so the worker needs no image
    branch — and routing through it means images inherit the bounded detection
    resolution, the bilingual two-pass merge and the confidence guard for free,
    rather than growing a second OCR stack that drifts from the first.
    """
    from backend.services import ocr_paddle
    from backend.services.ocr_text import assess, normalise_visual_order

    if not ocr_paddle.available():
        return ""
    for page in ocr_paddle.read_pdf(path, max_pages=1):
        if page.get("error"):
            log.warning("image ocr failed for %s: %s", path, page["error"])
            continue
        text = page.get("text") or ""
        ok, why = assess(text, page.get("scores") or [])
        if not ok:
            log.info("image ocr discarded for %s: %s", path, why)
            return ""
        return normalise_visual_order(text)
    return ""


def read_image(path: str) -> str:
    """The text an image contributes to the corpus and to a chat turn.

    The description is bracketed and prefixed so it can never be read as
    transcription. That separation is the whole safety argument: the OCR text is
    trustworthy and the description is a guess about category, and a reader —
    human or model — has to be able to tell which is which.
    """
    text = ocr(path)
    caption = describe(path)
    parts = []
    if caption:
        parts.append(f"[Image: {caption}]")
    if text.strip():
        parts.append(text.strip())
    elif caption:
        # Say it plainly rather than leaving a caption that implies more was read.
        parts.append("[No readable text was found in this image.]")
    return "\n\n".join(parts)
