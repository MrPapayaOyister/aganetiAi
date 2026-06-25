"""Unit tests for document drafting helpers (backend/documents.py)."""
from backend.documents import md_to_html, normalize_doc_type


def test_normalize_doc_type_aliases():
    assert normalize_doc_type("draft a one pager") == "one-pager"
    assert normalize_doc_type("write an SOP") == "sop"
    assert normalize_doc_type("procedure for onboarding") == "sop"
    assert normalize_doc_type(None) == "memo"
    assert normalize_doc_type("something random") == "memo"


def test_md_to_html_headings_and_lists():
    md = "# Title\n\nIntro paragraph.\n\n## Section\n- one\n- two\n\n1. first\n2. second"
    html = md_to_html(md)
    assert "<h1>Title</h1>" in html
    assert "<h2>Section</h2>" in html
    assert "<ul>" in html and "<li>one</li>" in html
    assert "<ol>" in html and "<li>first</li>" in html
    assert "<p>Intro paragraph.</p>" in html


def test_md_to_html_inline_formatting_and_escaping():
    html = md_to_html("This is **bold** and `code` and <script>x</script>")
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html
    # raw HTML must be escaped, not passed through
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
