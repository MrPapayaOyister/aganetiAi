"""Unit tests for RAG chunking (backend/ingest.py)."""
from backend.ingest import chunk_text


def test_short_text_single_chunk():
    assert chunk_text("a b c", 300, 60) == ["a b c"]


def test_empty_text():
    assert chunk_text("   ", 300, 60) == []


def test_chunking_with_overlap():
    words = " ".join(f"w{i}" for i in range(700))
    chunks = chunk_text(words, 300, 60)
    assert len(chunks) >= 3
    # each chunk no longer than `size` words
    assert all(len(c.split()) <= 300 for c in chunks)
    # consecutive chunks overlap (last words of chunk0 reappear in chunk1)
    first_tail = chunks[0].split()[-60:]
    second_head = chunks[1].split()[:60]
    assert any(w in second_head for w in first_tail)
