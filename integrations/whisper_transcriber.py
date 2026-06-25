"""
Faster-Whisper speech-to-text.

On the DGX Spark (GB10) the CUDA backend transcribes a minute of audio in well
under a second. We try CUDA first (controlled by WHISPER_DEVICE / WHISPER_COMPUTE
in config.settings) and fall back to CPU int8 automatically if the CUDA build of
ctranslate2 isn't available on this platform — so the module always loads.
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from faster_whisper import WhisperModel
from config.settings import WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE

_model = None
_active = None  # (device, compute) actually in use, for logging/diagnostics


def _candidate_configs():
    """Ordered (device, compute) pairs to attempt, based on settings."""
    dev = (WHISPER_DEVICE or "auto").lower()
    comp = (WHISPER_COMPUTE or "auto").lower()

    if dev == "cpu":
        return [("cpu", comp if comp != "auto" else "int8")]
    if dev == "cuda":
        return [("cuda", comp if comp != "auto" else "float16"),
                ("cpu", "int8")]  # safety net even if cuda explicitly requested
    # auto: prefer GPU, fall back to CPU
    return [("cuda", comp if comp != "auto" else "float16"),
            ("cpu", "int8")]


def get_model() -> WhisperModel:
    """Lazy-load the Whisper model, trying GPU first then CPU."""
    global _model, _active
    if _model is not None:
        return _model

    last_err = None
    for device, compute in _candidate_configs():
        try:
            _model = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute)
            _active = (device, compute)
            print(f"[whisper] loaded '{WHISPER_MODEL}' on {device} ({compute})")
            return _model
        except Exception as e:  # noqa: BLE001 - any backend/load failure → try next
            last_err = e
            print(f"[whisper] {device}/{compute} unavailable: {e}")

    raise RuntimeError(f"Could not initialize any Whisper backend: {last_err}")


def transcribe_audio(file_path: str) -> str:
    """
    Transcribe the audio file. Returns the transcript, or a status token on
    empty speech / error.
    """
    try:
        model = get_model()
        segments, info = model.transcribe(
            file_path, beam_size=5, language="en", vad_filter=True
        )
        transcript = " ".join(s.text.strip() for s in segments).strip()
        return transcript if transcript else "[No speech detected]"
    except Exception as e:
        print(f"Error during audio transcription: {e}")
        return "[Transcription failed]"
