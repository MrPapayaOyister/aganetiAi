"""OCR post-processing shared by every engine.

Two properties, both found by measurement rather than reasoning, and both true
of Qwen3-VL, PaddleOCR and Surya alike — which is why they live in one module
instead of three adapters.
"""

import pytest

from backend.services import ocr_text as O


# ── bidi ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("visual,logical", [
    ("رقم الفاتورة 0042-2026", "رقم الفاتورة 2026-0042"),
    ("تاريخ الاستحقاق 01-09-2026", "تاريخ الاستحقاق 2026-09-01"),
])
def test_rtl_numbers_are_restored_to_logical_order(visual, logical):
    """Exactly the strings Qwen3-VL and Surya both returned for a page whose
    ground truth is `logical`. Not a heuristic — the bidi algorithm's segment
    swap is invertible, and both engines produced the identical swap."""
    assert O.normalise_visual_order(visual) == logical


def test_a_latin_line_is_never_touched():
    """In a bilingual document the English lines were laid out LTR and were
    never reordered. Flipping them would invent the very bug we are fixing."""
    for line in ["Invoice No 2026-0042", "Due 2026-09-01", "PO-77821"]:
        assert O.normalise_visual_order(line) == line


def test_only_the_rtl_lines_of_a_bilingual_page_change():
    src = "وزارة الاقتصاد\nMinistry of Economy\nرقم الفاتورة 0042-2026\nInvoice No 2026-0042"
    out = O.normalise_visual_order(src).split("\n")
    assert out[1] == "Ministry of Economy"
    assert out[3] == "Invoice No 2026-0042"
    assert out[2] == "رقم الفاتورة 2026-0042"


def test_digits_inside_a_segment_keep_their_order():
    """Only the SEGMENTS swap. Reversing digits within one would corrupt every
    number on the page rather than repair it."""
    assert "100394857600003" in O.normalise_visual_order("الرقم الضريبي 100394857600003")


def test_a_lone_number_in_an_rtl_line_is_unchanged():
    assert O.normalise_visual_order("المبلغ 12500 درهم") == "المبلغ 12500 درهم"


def test_the_transform_is_its_own_inverse():
    """Applying it twice returns the original, which is what makes it safe to
    reason about — and what proves it is a reordering, not a rewrite."""
    s = "رقم الفاتورة 0042-2026 تاريخ 01-09-2026"
    assert O.normalise_visual_order(O.normalise_visual_order(s)) == s


def test_empty_input_is_survivable():
    assert O.normalise_visual_order("") == ""


# ── the junk guard ───────────────────────────────────────────────────────────

#: PaddleOCR's actual output on the render of the poisoned garbage.doc.
PADDLE_JUNK = ("酮保磨弧LD悟删操震圈芡3娘型深酮剑查赴：数缚K缩风胶裂割u仍\n"
               "金圈——答罐解题/物腻杖到圈题温数蕊：咬眉型/排踏是慌按码析e衍敦媚凝凉爬县")


def test_the_existing_helper_alone_does_not_catch_ocr_junk():
    """Pinning the reason this module exists. looks_like_document_text hunts for
    UNMAPPED glyphs; Paddle's junk is assigned CJK codepoints, so it sails
    through. If a future change makes the helper catch this on its own, this test
    fails and the confidence gate can be reconsidered — deliberately."""
    from integrations.libreoffice_converter import looks_like_document_text
    ok, _ = looks_like_document_text(PADDLE_JUNK)
    assert ok is True, "helper now catches CJK junk; revisit the confidence gate"


def test_low_confidence_output_is_rejected():
    """98% of the junk page's lines scored below 0.80; real pages score 0.0%."""
    scores = [0.55] * 48 + [0.99]
    ok, why = O.assess(PADDLE_JUNK, scores)
    assert not ok
    assert "low-confidence" in why


def test_a_real_page_is_accepted():
    ok, why = O.assess("Phase 2\nKPI\nSub KPI\nBackend Status", [0.997] * 40)
    assert ok, why


def test_confident_pages_are_not_rejected_for_being_arabic():
    """The helper's own rule: 'looks foreign' is not 'looks broken'."""
    ok, why = O.assess("وزارة الاقتصاد والسياحة\nرقم الفاتورة 2026-0042", [0.96] * 5)
    assert ok, why


def test_the_glyph_check_still_runs_without_scores():
    """The VL path has no per-line confidence. It must lose nothing it has today."""
    ok, _ = O.assess(" ��", None)
    assert not ok
    assert O.assess("", None)[0] is False


def test_wrong_language_model_is_a_retry_not_a_rejection():
    """Arabic read by the English recogniser scored 0.819 mean against 0.997 for
    genuine English. That page is readable — by the other model."""
    arabic_via_en = [0.72, 0.75, 0.88, 0.91, 0.83]
    assert O.should_retry_other_language(arabic_via_en) is True
    assert O.should_retry_other_language([0.997] * 20) is False
    # A retry signal must not also be a rejection: the page is fine.
    assert O.assess("رقم 2026-0042", arabic_via_en)[0] is True


# ── the seam ─────────────────────────────────────────────────────────────────

def test_ocr_output_reaches_the_pipeline_only_through_one_normalising_call():
    """Structural. Adding PaddleOCR must not mean remembering to normalise it —
    extract_file_text calls the dispatcher, and the dispatcher normalises. A
    future edit that reaches past it to an engine directly fails here."""
    import inspect

    from backend import ingest
    src = inspect.getsource(ingest.extract_file_text)
    assert "_ocr_pdf(" in src
    assert "_ocr_pdf_via_vision" not in src, \
        "extract_file_text must go through the dispatcher, not an engine"
    assert "normalise_visual_order" in inspect.getsource(ingest._ocr_pdf)


def test_the_pdf_text_layer_is_not_bidi_normalised():
    """The correction applies to OCR only. A text layer is already logical order;
    'fixing' it would reverse every Arabic invoice number that arrives intact."""
    import inspect

    from backend import ingest
    src = inspect.getsource(ingest.extract_file_text)
    # The early return for a usable text layer must not pass through normalisation.
    i_return_text = src.index("return text")
    i_ocr = src.index("_ocr_pdf(")
    assert i_return_text < i_ocr


# ── the two-pass merge puts numbers on their own lines ───────────────────────

def test_a_bare_numeric_line_on_an_arabic_page_is_still_flipped():
    """Found by running the wired pipeline, not by review.

    Arabic pages need two passes — `-ar` gets the script and drops the digits,
    `-en` gets the digits and drops the script — and the results are
    concatenated. The numbers therefore land on lines with no Arabic left on
    them, so a per-line RTL test stops firing and `0042-2026` reaches the corpus
    as the invoice number.
    """
    page = "وزارة الاقتصاد والسياحة\nرقمالفاتورة\n0042-2026\n01-09-2026"
    out = O.normalise_visual_order(page).split("\n")
    assert out[2] == "2026-0042"
    assert out[3] == "2026-09-01"


def test_a_latin_line_on_an_arabic_page_is_still_exempt():
    """The exemption that keeps the fix from becoming the bug. On a bilingual
    page "Invoice No 2026-0042" was laid out LTR and is already correct."""
    page = "رقم الفاتورة 0042-2026\nInvoice No 2026-0042"
    out = O.normalise_visual_order(page).split("\n")
    assert out[0] == "رقم الفاتورة 2026-0042"
    assert out[1] == "Invoice No 2026-0042"


def test_a_bare_numeric_line_on_a_page_with_no_arabic_is_untouched():
    """The page-level RTL flag is what licenses the flip. Without Arabic
    anywhere, a lone number is just a number."""
    page = "Invoice\n2026-0042\nDue 2026-09-01"
    assert O.normalise_visual_order(page) == page


# ── a shape is not a letter ──────────────────────────────────────────────────

def test_a_single_character_is_not_a_transcription():
    """A photograph with no text produced "O" — the sun — at 0.87 confidence,
    above the floor. Confidence cannot be the only gate: a detector asked to
    find text in a picture of shapes will find one, and be sure about it."""
    ok, why = O.assess("O", [0.87])
    assert not ok and "no readable text" in why


def test_the_minimum_applies_even_with_perfect_confidence():
    ok, _ = O.assess("8", [1.0])
    assert not ok


def test_a_short_but_real_reading_survives():
    ok, why = O.assess("EXIT", [0.98])
    assert ok, why
    assert O.assess("Ingest\nEmbed\nQdrant", [1.0, 1.0, 1.0])[0]
