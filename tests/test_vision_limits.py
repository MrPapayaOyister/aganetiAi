"""Two independent bugs, both about a number that was wrong.

1. The PDF OCR path rendered pages at 150 dpi and sent them to the shared vision
   engine unresized. A4 at 150 dpi is 1240x1755, which Qwen3-VL tokenises to a
   110x78 patch grid = 2,145 vision tokens, and on 2026-08-14 that killed the
   engine:

     gemm_and_bias error: CUBLAS_STATUS_INTERNAL_ERROR ... m 2048 n 2145 k 4608
     EngineCore encountered a fatal error

   The container restarted and every other vision caller failed meanwhile. Worse,
   it failed SILENTLY: llm.complete defaults to raise_on_error=False, so the
   caller saw "" and the document was recorded status="indexed", chunks=0.

2. router.py declared ctx=32768 for all three models. That is the VISION engine's
   limit; the text models serve 40960. Under-claiming by 20% causes premature
   trimming, which is why nothing failed loudly.

These are structural tests — they assert the invariant, not the incident.
"""

import io

import pytest

from backend import ingest


def _png(w: int, h: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def _size(png: bytes) -> tuple[int, int]:
    from PIL import Image
    return Image.open(io.BytesIO(png)).size


# ── the render cap ───────────────────────────────────────────────────────────

def test_an_a4_page_render_is_downscaled_below_the_cap():
    """The exact input that crashed the engine."""
    out = ingest._downscale_png(_png(1240, 1755))
    assert max(_size(out)) <= ingest.OCR_MAX_EDGE_PX


@pytest.mark.parametrize("dims", [(1240, 1755), (2480, 3508), (4032, 3024), (800, 641)])
def test_nothing_above_the_cap_survives_a_resize(dims):
    """Includes a 12MP phone photo — ~5x the activation size that already faulted."""
    assert max(_size(ingest._downscale_png(_png(*dims)))) <= ingest.OCR_MAX_EDGE_PX


def test_aspect_ratio_is_preserved():
    """Squashing a page would corrupt the text the OCR is meant to read."""
    w, h = _size(ingest._downscale_png(_png(1240, 1755)))
    assert abs((w / h) - (1240 / 1755)) < 0.01


def test_a_small_image_is_returned_untouched():
    """No re-encode, no quality loss, no wasted CPU on something already safe."""
    small = _png(320, 240)
    assert ingest._downscale_png(small) is small


def test_the_cap_is_the_measured_safe_value():
    """640 was measured against the live engine (0.66-1.18s per call). Raising it
    means re-measuring, not re-reasoning — 1240x1755 was 'obviously fine' too."""
    assert ingest.OCR_MAX_EDGE_PX == 640


def test_an_unresizable_render_is_detected_rather_than_sent():
    """If Pillow is missing or rejects the render, _downscale_png returns the
    original. The caller must notice and skip the page — sending it is what took
    the engine down."""
    assert ingest._too_large(_png(1240, 1755)) is True
    assert ingest._too_large(_png(400, 400)) is False
    # Undecodable bytes must be treated as unsafe, not waved through.
    assert ingest._too_large(b"not a png") is True


def test_the_ocr_loop_downscales_before_sending():
    """Structural: the resize must sit between the render and the data URL. A
    future edit that reorders them reintroduces the crash silently."""
    import inspect
    src = inspect.getsource(ingest._ocr_pdf_via_vision)
    render = src.index("get_pixmap")
    resize = src.index("_downscale_png")
    encode = src.index("b64encode")
    assert render < resize < encode, "resize must happen between render and encode"


# ── the context window ───────────────────────────────────────────────────────

def test_text_and_vision_models_declare_their_own_context_windows():
    """All three said 32768 — the vision engine's number, pasted onto the text
    models. Verified against the running engines:
        vllm-fast:9002  qwen3-30b-a3b     max_model_len=40960
        vllm-vl:9001    qwen3-vl-30b-a3b  max_model_len=32768
    """
    from backend.orchestrator.router import MODELS
    assert MODELS["gateway"]["caps"]["ctx"] == 40960
    assert MODELS["gateway-fast"]["caps"]["ctx"] == 40960
    assert MODELS["vision-vl"]["caps"]["ctx"] == 32768


def test_the_two_windows_are_not_the_same_constant():
    """They differ for a reason — the vision engine is launched with an explicit
    --max-model-len 32768. Collapsing them again re-creates the bug."""
    from backend.orchestrator.router import MODELS
    assert MODELS["gateway"]["caps"]["ctx"] != MODELS["vision-vl"]["caps"]["ctx"]


def test_only_the_vision_model_claims_vision():
    from backend.orchestrator.router import MODELS
    assert MODELS["vision-vl"]["caps"]["vision"] is True
    assert MODELS["gateway"]["caps"]["vision"] is False
