"""PaddleOCR: the transcription path.

Chosen over the Qwen3-VL path by measurement, not preference. On the four real
pages in the evaluation corpus the VL model scored 81-92% numeric recall and, on
the landscape KPI table, invented an entire HR register that the confabulation
guard caught only 1 time in 6. PaddleOCR reads the same pages at 100% and cannot
fabricate: it transcribes detected glyphs, so its failure mode is obvious junk
rather than a plausible different document. On Arabic the VL model named a
DIFFERENT REAL UAE MINISTRY on each run; the Arabic recogniser here gets it
right.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not describe images or answer questions about them. That is a different
job and the VL model is better at it — asked *about* the KPI table it correctly
called it a project status tracker, the same page it fabricates when told to
transcribe. VL remains the fallback and the describer; see backend/ingest.py.

RESOLUTION AND MEMORY
---------------------
Detection is bounded by `text_det_limit_side_len`; recognition still crops from
the FULL-resolution page, which is what makes the bound cheap. Measured peaks on
a 1240x1755 page:

    det limit   english        arabic
    unbounded   2,161 MB       11,697 MB      <- does not fit beside two vLLM engines
    1280        6,335 MB       -
    960         1,170 MB       4,113 MB
    896         -              3,680 MB       <- chosen for ar
    736         1,021 MB       2,726 MB       <- case-4 accuracy collapses (18% / 75%)

Steady-state RSS is 550-780 MB; the peak is a transient inside one detection
call. 960/896 keep both models under 4 GB with no accuracy given up — the Arabic
model is in fact better bounded than unbounded (100% vs 80% on the numeric
control). Do not raise these without re-measuring: this box has ONE 121 GB
unified pool shared with the GPU, and an uncapped probe during the evaluation
triggered 19 OOM kills.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

log = logging.getLogger("aria.ocr.paddle")

#: Longest edge of the DETECTION image, per language. Kept in sync with the
#: worker, which is the process that actually applies them.
DET_LIMIT = {"en": 960, "ar": 896}

#: Wall-clock ceiling for one document. Measured 21-32s per page at these
#: bounds, so six pages is comfortably inside this; a run that exceeds it has
#: gone wrong and should not hold an upload open indefinitely.
TIMEOUT_SECONDS = 600


def available() -> bool:
    try:
        import paddleocr  # noqa: F401
        return True
    except Exception:
        return False


def read_pdf(pdf_path: str, max_pages: int = 6) -> list[dict]:
    """OCR a PDF in a SEPARATE PROCESS. Returns [{page, text, scores}].

    Not an implementation detail — see ocr_paddle_worker for the measurement.
    paddleocr pulls in torch, torch and ctranslate2 each ship their own
    libgomp, and paddle links the system one; with torch already imported (as
    it is in this backend, for Kokoro) paddle segfaults inside
    AnalysisPredictor::Init. A SIGSEGV cannot be caught, so in-process OCR would
    take the API server down. Here it costs one document.
    """
    cmd = [sys.executable, "-m", "backend.services.ocr_paddle_worker",
           str(pdf_path), str(max_pages)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log.error("paddle worker timed out after %ss on %s", TIMEOUT_SECONDS, pdf_path)
        return []
    if proc.returncode != 0:
        # Includes the signal case: a segfault shows up as a negative returncode
        # and must be visible, not swallowed into "no text found".
        log.error("paddle worker exited %s on %s: %s", proc.returncode, pdf_path,
                  proc.stderr.decode("utf-8", "replace")[-400:])
        return []
    # Paddle's C++ layer writes to stdout too, so the JSON is the LAST line.
    tail = (proc.stdout.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
    try:
        payload = json.loads(tail)
    except json.JSONDecodeError:
        log.error("paddle worker produced no JSON for %s (last line: %.200r)",
                  pdf_path, tail)
        return []
    if payload.get("error"):
        log.error("paddle worker: %s", payload["error"])
    return payload.get("pages", [])
