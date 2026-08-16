"""Uploaded images: OCR for what they say, VL for what they are.

An image is two different things depending on what was photographed, and
handling only one case fails the other loudly — a photo through OCR alone
answers "I couldn't read any text" for something the user can see plainly, and a
scan through VL alone reinstates the fabrication the OCR change removed.
"""

import pytest

from backend.services import attachments as att
from backend.services import image_read as ir


# ── what we now accept ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name,data", [
    ("a.png", b"\x89PNG\r\n\x1a\n" + b"x" * 80),
    ("b.jpg", b"\xff\xd8\xff\xe0" + b"x" * 80),
    ("c.jpeg", b"\xff\xd8\xff\xe1" + b"x" * 80),
    ("d.webp", b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"x" * 80),
])
def test_the_image_formats_we_can_read_are_accepted(name, data):
    assert att.check_upload(data, name) == "." + name.split(".")[-1]


def test_a_wav_renamed_to_webp_is_refused():
    """WEBP is the one format here whose magic is not a prefix: RIFF, a length,
    then WEBP. Checking only "RIFF" would accept a WAV or an AVI."""
    wav = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"x" * 80
    with pytest.raises(att.AttachmentRejected):
        att.check_upload(wav, "x.webp")


def test_images_are_no_longer_advertised_as_not_yet():
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        assert ext not in att.NOT_YET
        assert ext in att.ACCEPTED


def test_a_format_we_still_cannot_read_says_what_to_use_instead():
    """The message has to stay true as formats graduate. "I can't read images
    yet" became false the moment PNG worked, and a wrong reason sends someone
    looking in the wrong place."""
    with pytest.raises(att.AttachmentRejected) as e:
        att.check_upload(b"GIF89a" + b"x" * 80, "x.gif")
    assert "PNG" in e.value.reason and "yet" in e.value.reason


def test_ingest_accepts_the_same_image_set_as_upload():
    """Two entry points, one answer. A format the composer accepts and the
    corpus walker ignores is a file that uploads and then does nothing."""
    from backend.ingest import IMAGE_EXTENSIONS, SUPPORTED
    assert IMAGE_EXTENSIONS <= SUPPORTED
    assert IMAGE_EXTENSIONS == {e for e in att.ACCEPTED
                                if e in {".png", ".jpg", ".jpeg", ".webp"}}


# ── the division of labour ───────────────────────────────────────────────────

def test_the_description_is_delimited_from_the_transcription(monkeypatch):
    """The whole safety argument. OCR text is trustworthy, the description is a
    guess about category, and a reader has to be able to tell which is which."""
    monkeypatch.setattr(ir, "ocr", lambda p: "Total: AED 12,500.00")
    monkeypatch.setattr(ir, "describe", lambda p: "A scanned invoice.")
    out = ir.read_image("x.png")
    assert out.startswith("[Image: A scanned invoice.]")
    assert "Total: AED 12,500.00" in out
    assert "AED" not in out.split("\n")[0], "a value must never sit in the caption line"


def test_an_image_with_no_text_says_so_rather_than_looking_empty(monkeypatch):
    """A caption with nothing after it implies more was read than was."""
    monkeypatch.setattr(ir, "ocr", lambda p: "")
    monkeypatch.setattr(ir, "describe", lambda p: "A photograph of a landscape.")
    out = ir.read_image("x.png")
    assert "No readable text" in out


def test_a_failed_description_degrades_to_the_text(monkeypatch):
    """Best-effort by design: losing the caption leaves us where we were before
    it existed. It must never fail the upload."""
    monkeypatch.setattr(ir, "ocr", lambda p: "Invoice 2026-0042")
    monkeypatch.setattr(ir, "describe", lambda p: "")
    assert ir.read_image("x.png") == "Invoice 2026-0042"


def test_the_describe_prompt_forbids_specifics():
    """Measured: asked openly, this model called a Ministry of Economy invoice
    'the Ministry of Interior and Municipalities' and invented a '12500
    project'. Constrained to the TYPE it named nothing. The constraint IS the
    feature — if this prompt loosens, the fabrication comes back."""
    p = ir.DESCRIBE_PROMPT.lower()
    assert "kind" in p
    assert "do not" in p
    for forbidden in ("number", "name", "date"):
        assert forbidden in p, f"the prompt must forbid quoting {forbidden}s"
    assert ir.DESCRIBE_MAX_TOKENS <= 100, "no room to start listing values"


def test_the_description_render_is_capped_like_every_other_vision_call():
    """An unresized render is what killed the shared vision engine."""
    from backend import ingest
    assert ir.DESCRIBE_MAX_EDGE_PX == ingest.OCR_MAX_EDGE_PX


def test_images_route_through_the_shared_ocr_worker():
    """Structural. Images must inherit the bounded detection resolution, the
    Arabic two-pass merge and the confidence guard — not grow a second OCR
    stack that drifts from the first."""
    import inspect
    src = inspect.getsource(ir.ocr)
    assert "ocr_paddle" in src and "assess" in src
    assert "normalise_visual_order" in src


def test_extract_file_text_sends_images_to_read_image():
    import inspect

    from backend import ingest
    src = inspect.getsource(ingest.extract_file_text)
    assert "IMAGE_EXTENSIONS" in src and "read_image" in src
