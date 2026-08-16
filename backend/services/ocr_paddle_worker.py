"""PaddleOCR in its OWN process. Run as: python -m backend.services.ocr_paddle_worker

WHY A SUBPROCESS, MEASURED
--------------------------
paddleocr pulls in torch (via paddlex -> transformers). torch ships its own
libgomp.so.1, as does ctranslate2, and paddle links the system one. Two OpenMP
runtimes in one process segfault inside AnalysisPredictor::Init:

    import paddle; import paddleocr   -> works
    import torch;  import paddle      -> SIGSEGV

The backend imports torch at startup (Kokoro TTS) and ctranslate2 (faster-whisper),
so the second ordering is exactly what the live process looks like. In-process
PaddleOCR would take the whole API server down with a signal no `except` can
catch. A subprocess is immune to whatever the parent has loaded, and a crash here
costs one document instead of every request in flight.

The model load (~1-2s) is paid once per DOCUMENT, not per page, because this
worker loops over the pages itself.

Protocol: argv = <pdf_path> <max_pages>; stdout = one JSON object.
Anything this prints outside that JSON is noise from paddle's C++ layer, so the
JSON goes out last and the reader takes the last line.
"""
# Import paddle FIRST. Even in a fresh interpreter this keeps the OpenMP that
# paddle expects in front of the one transformers would otherwise pull in.
import paddle  # noqa: F401  isort:skip

import json
import sys
import tempfile

DET_LIMIT = {"en": 960, "ar": 896}
_models: dict = {}


def _model(lang: str):
    m = _models.get(lang)
    if m is None:
        from paddleocr import PaddleOCR
        m = PaddleOCR(lang=lang, text_det_limit_side_len=DET_LIMIT.get(lang, 960),
                      text_det_limit_type="max",
                      use_doc_orientation_classify=False, use_doc_unwarping=False,
                      use_textline_orientation=False, device="cpu")
        _models[lang] = m
    return m


def _read(png: str, lang: str):
    lines, scores = [], []
    for page in _model(lang).predict(png):
        d = page.json.get("res", page.json) if hasattr(page, "json") else page
        lines += list(d.get("rec_texts") or [])
        scores += [float(s) for s in (d.get("rec_scores") or [])]
    return "\n".join(lines), scores


def _has_arabic(t: str) -> bool:
    return any("؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ" for c in t)


def _read_bilingual(png: str):
    """English first, Arabic added only if the page looks bilingual.

    Neither model is sufficient alone on a mixed page: `-en` reads the Latin
    digits and drops the Arabic script, `-ar` reads the script and drops the
    digits. An Arabic invoice through one model loses either its text or its
    numbers.
    """
    text_en, scores_en = _read(png, "en")
    mean_en = (sum(scores_en) / len(scores_en)) if scores_en else 1.0
    # Middling confidence is what an English recogniser produces when it is
    # staring at Arabic — 0.819 on the corpus, against 0.997 for real English.
    if not (_has_arabic(text_en) or mean_en < 0.90):
        return text_en, scores_en
    try:
        text_ar, scores_ar = _read(png, "ar")
    except Exception:
        return text_en, scores_en
    if not text_ar.strip():
        return text_en, scores_en
    return ("\n".join(p for p in (text_ar.strip(), text_en.strip()) if p),
            scores_ar + scores_en)


def main() -> int:
    pdf, max_pages = sys.argv[1], int(sys.argv[2])
    import fitz
    out = []
    try:
        doc = fitz.open(pdf)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"cannot open: {e}", "pages": []}))
        return 0
    with tempfile.TemporaryDirectory(prefix="aganeti-ocrw-") as work:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            try:
                # FULL render resolution. The 640px cap in the VL path exists
                # only because that engine crashed above it; inheriting it here
                # is what made case 4 score 0% instead of 100%.
                png = f"{work}/p{i}.png"
                page.get_pixmap(dpi=150).save(png)
                text, scores = _read_bilingual(png)
                out.append({"page": i, "text": text, "scores": scores})
            except Exception as e:  # noqa: BLE001
                out.append({"page": i, "error": str(e)[:200], "text": "", "scores": []})
    doc.close()
    print(json.dumps({"pages": out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
