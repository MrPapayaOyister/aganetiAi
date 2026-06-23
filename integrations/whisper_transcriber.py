from faster_whisper import WhisperModel

_model = None

def get_model() -> WhisperModel:
    """
    Lazy-loads the Faster-Whisper small model.
    Configured for CPU-only inference with 8-bit integer quantization.
    """
    global _model
    if _model is None:
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    return _model

def transcribe_audio(file_path: str) -> str:
    """
    Transcribes the audio file at file_path using Faster-Whisper.
    Returns the full transcript string or status tokens on empty speech/error.
    """
    try:
        model = get_model()
        # Transcribe with standard beam search settings for English transcription
        segments, info = model.transcribe(file_path, beam_size=5, language="en", vad_filter=True)
        
        # Consume the generator to get segment texts
        segment_list = list(segments)
        transcript = " ".join([s.text.strip() for s in segment_list]).strip()
        
        if not transcript:
            return "[No speech detected]"
            
        return transcript
    except Exception as e:
        print(f"Error during audio transcription: {e}")
        return "[Transcription failed]"
