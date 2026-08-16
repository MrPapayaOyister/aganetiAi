"""Post-processing that every OCR engine needs, implemented once.

Two concerns live here, both discovered while evaluating PaddleOCR and Surya
against the Qwen3-VL path, and both true of ALL THREE engines. Putting them in
the engine adapters would mean three copies drifting apart, so they sit at the
single boundary where OCR output enters the pipeline.

  normalise_visual_order  — OCR reads a page in VISUAL order; Arabic lines come
                            back with their numbers reversed.
  assess               — is this OCR output a document, or noise?
"""

from __future__ import annotations

import re
import statistics

from integrations.libreoffice_converter import looks_like_document_text

# ── bidi ─────────────────────────────────────────────────────────────────────
#
# OCR reports what it SEES, left to right. The Unicode bidi algorithm lays a
# logical string out for display, and inside an RTL paragraph a run of
# `digits SEP digits` is rendered with the SEGMENTS SWAPPED — each segment keeps
# its own digit order, but their positions exchange. So a document whose logical
# text is `رقم الفاتورة 2026-0042` is displayed as `... 0042-2026`, and that is
# what the camera, and therefore the OCR engine, sees.
#
# Measured on the same page across every engine that returns digits at all:
#
#     logical (ground truth)   visual (Qwen3-VL)   visual (Surya)
#     2026-0042                0042-2026           0042-2026
#     2026-09-01               01-09-2026          01-09-2026
#
# Identical, and identical to what the bidi algorithm predicts. The transform is
# therefore not a heuristic to be tuned — it is exactly invertible, and this
# reverses it.
#
# CRITICAL SCOPE NOTE: this must be applied to OCR output ONLY. A PDF text layer
# is stored in LOGICAL order and is already correct; running this over it would
# corrupt every Arabic invoice number in the corpus. That is why this function is
# called at the OCR boundary rather than in extract_file_text's common return
# path, even though "normalise all extracted text in one place" would otherwise
# be the tidier arrangement.

#: Arabic, Hebrew, and their presentation forms. Used only to decide whether a
#: LINE was laid out RTL — not to judge whether text is "foreign", which is the
#: mistake looks_like_document_text explicitly avoids.
_RTL = re.compile(r"[֐-׿؀-ۿ܀-ݏݐ-ݿ"
                  r"ࢠ-ࣿיִ-﷿ﹰ-﻿]")

#: A run of numeric segments joined by neutral separators — the shape the bidi
#: algorithm reorders. Dates, invoice numbers, phone numbers, ratios.
_NUM_RUN = re.compile(r"\d+(?:[-/.]\d+)+")


#: A line carrying Latin letters was laid out left-to-right and its numbers were
#: never reordered. Used to tell a genuine English line from a bare numeric run
#: that came out of an Arabic one.
_LATIN = re.compile(r"[A-Za-z]")


def normalise_visual_order(text: str) -> str:
    """Undo bidi segment reordering in OCR output, line by line.

    A line containing RTL script was laid out right-to-left, so its numeric runs
    were reordered and must be flipped back.

    THE SECOND CASE IS NOT OBVIOUS AND WAS FOUND BY RUNNING THE REAL PIPELINE.
    Arabic pages are read by two passes — the Arabic recogniser gets the script
    and drops the digits, the English one gets the digits and drops the script —
    and the results are concatenated. That puts the numbers on their OWN lines,
    with no Arabic characters left on them, so a purely per-line RTL test stops
    firing and `0042-2026` survives into the corpus as an invoice number:

        وزارة الاقتصاد والسياحة     <- arabic pass
        رقم الفاتورة                <- arabic pass, digits dropped
        0042-2026                   <- english pass: REVERSED, and no longer
                                       recognisable as RTL by itself

    So a bare numeric line on a page that HAS Arabic is treated as having come
    from an Arabic line, because that is the only way it can have got there. A
    line with Latin LETTERS is exempt: on a bilingual page "Invoice No 2026-0042"
    is a genuine LTR line whose numbers are already correct, and flipping it
    would introduce the very error this removes.
    """
    if not text:
        return text

    lines = text.split("\n")
    page_has_rtl = any(_RTL.search(l) for l in lines)

    out = []
    for line in lines:
        rtl_line = bool(_RTL.search(line))
        orphan_number = (page_has_rtl and not rtl_line
                         and not _LATIN.search(line) and _NUM_RUN.search(line))
        if not (rtl_line or orphan_number):
            out.append(line)
            continue

        def _flip(m: re.Match) -> str:
            run = m.group(0)
            seps = re.findall(r"[-/.]", run)
            parts = re.split(r"[-/.]", run)
            parts.reverse()
            # Separators keep their positions; only the segments move, which is
            # precisely what the bidi algorithm did on the way out.
            rebuilt = parts[0]
            for sep, part in zip(seps, parts[1:]):
                rebuilt += sep + part
            return rebuilt

        out.append(_NUM_RUN.sub(_flip, line))
    return "\n".join(out)


# ── is this output a document, or noise? ─────────────────────────────────────
#
# looks_like_document_text was the obvious candidate and it DOES NOT COVER THIS.
# Measured: on the render of the poisoned garbage.doc, PaddleOCR emits 1,638
# characters of CJK soup —
#
#     酮保磨弧LD悟删操震圈芡3娘型深酮剑查赴：数缚K缩风胶裂割u仍
#
# — and looks_like_document_text ACCEPTS it. That is not a bug in the helper. It
# looks for UNMAPPED glyphs (private-use area, U+FFFD, `(cid:N)`), which is what
# a broken font mapping produces, and it deliberately refuses to judge by script
# so that Arabic, Chinese and Korean documents survive. Paddle's junk is made of
# assigned CJK codepoints — the recogniser's best guess from a Chinese-capable
# charset — so there is nothing unmapped for it to find.
#
# OCR exposes a signal LibreOffice never had: a CONFIDENCE PER LINE. Measured
# across the corpus at det_limit=960:
#
#     page                 lines   mean conf   frac < 0.80
#     real_dense             110       0.997         0.000
#     real_mixed              36       0.997         0.000
#     real_small             105       0.998         0.000
#     case4_table            248       0.997         0.000
#     control_numeric          5       1.000         0.000
#     garbage_doc             49       0.658         0.980
#     arabic (read by -en)     5       0.819         0.400
#
# Real pages and junk are separated by a chasm, not a threshold. The Arabic page
# read by the WRONG language model sits deliberately in between: that is the
# signature of "try the other model", not "discard".
#
# Both checks run. Confidence catches confident-looking nonsense; the existing
# helper still catches unmapped glyphs and empty output, and keeping it means the
# LibreOffice and OCR paths continue to answer the same question the same way:
# judge the OUTPUT, never the fact that the call returned.

#: Below this, a recognised line is not trustworthy. Real pages put 0.0% of their
#: lines here; the junk page puts 98%.
LINE_CONFIDENCE_FLOOR = 0.80

#: Share of lines allowed below the floor before the page is rejected outright.
#: 0.5 sits an order of magnitude clear of both populations.
MAX_LOW_CONFIDENCE_SHARE = 0.5

#: Below this mean, re-reading with a different language model is worth a try
#: before giving up. The Arabic-page-through-the-English-model case.
RETRY_MEAN_CONFIDENCE = 0.90


#: Fewer non-whitespace characters than this is not a transcription, whatever
#: the confidence says.
#:
#: Measured: a photograph containing NO text at all produced the single
#: character "O" — the sun in the image — at 0.87 confidence, comfortably above
#: LINE_CONFIDENCE_FLOOR. A detector asked to find text in a picture of shapes
#: will eventually find a shape that looks like a letter, and it will be sure
#: about it, so confidence cannot be the only gate.
#:
#: The trade is deliberate and slightly lossy: a genuine two-character image
#: (a door number, "42") is discarded too. That costs little, because an image
#: still gets its one-line description either way, and it buys not attaching a
#: stray glyph to a photo and calling it the photo's text.
MIN_TEXT_CHARS = 3


def assess(text: str, scores: list[float] | None = None) -> tuple[bool, str]:
    """Is this OCR output usable? Returns (ok, reason).

    `scores` is the engine's per-line confidence. It is optional because the VL
    path has no equivalent — for that caller only the glyph, length and
    emptiness checks apply, which is exactly the coverage it has today and no
    less.
    """
    if len("".join((text or "").split())) < MIN_TEXT_CHARS:
        return False, "no readable text was found"
    if scores:
        low = sum(1 for s in scores if s < LINE_CONFIDENCE_FLOOR) / len(scores)
        if low > MAX_LOW_CONFIDENCE_SHARE:
            return False, (f"{low:.0%} of recognised lines were low-confidence — "
                           f"this page does not appear to contain readable text")

    ok, why = looks_like_document_text(text)
    if not ok:
        return False, why
    return True, "ok"


def should_retry_other_language(scores: list[float] | None) -> bool:
    """Middling confidence means the right model probably was not used.

    Distinct from `assess` returning False: that page is junk, this page is
    probably fine and was read by the wrong recogniser. Arabic through the
    English model scored 0.819 mean where genuine English pages score 0.997.
    """
    if not scores:
        return False
    return statistics.mean(scores) < RETRY_MEAN_CONFIDENCE
