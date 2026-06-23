from kokoro import KPipeline
import soundfile as sf
import numpy as np
import os
import re

_pipeline = None

def get_pipeline() -> KPipeline:
    """
    Lazy-loads the Kokoro KPipeline for American English ('a').
    """
    global _pipeline
    if _pipeline is None:
        _pipeline = KPipeline(lang_code='a')
    return _pipeline

def clean_for_tts(text: str) -> str:
    """
    Sanitizes LLM response text for speech synthesis:
    - Removes markdown formatting (asterisks, underscores, code quotes, hashes).
    - Removes markdown links.
    - Removes raw URLs.
    - Filters out non-ASCII characters (e.g. emojis/emoticons).
    - Normalizes multiple whitespace characters.
    - Hard-truncates length to 500 characters.
    """
    text = re.sub(r'[*_`#~]', '', text)           # markdown symbols
    text = re.sub(r'\[.*?\]\(.*?\)', '', text)     # markdown links
    text = re.sub(r'http\S+', '', text)            # raw URLs
    text = re.sub(r'[^\x00-\x7F]+', '', text)     # non-ASCII (removes emoji)
    text = re.sub(r'\s+', ' ', text).strip()       # normalize whitespace
    return text[:500]                               # hard truncation

def synthesize_speech(text: str, output_path: str) -> bool:
    """
    Synthesizes the given text to a 24kHz WAV file using Kokoro TTS 'af_bella' voice.
    Catches all exceptions internally to guarantee resilient fallback behavior.
    """
    try:
        pipeline = get_pipeline()
        cleaned_text = clean_for_tts(text)
        
        if not cleaned_text:
            return False
            
        generator = pipeline(cleaned_text, voice='af_bella', speed=1.0)
        audio_chunks = [
            chunk.numpy() if hasattr(chunk, 'numpy') else chunk 
            for _, _, chunk in generator
        ]
        
        if not audio_chunks:
            return False
            
        audio = np.concatenate(audio_chunks)
        sf.write(output_path, audio, 24000)
        return True
    except Exception as e:
        print(f"Error during TTS synthesis: {e}")
        return False
