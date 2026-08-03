"""Tests for HTML text extraction.

Verifies markup is stripped and a genuinely empty document is
distinguishable from input that could not be parsed at all.
"""

from uncorrupt.extraction.html import extract_html
from uncorrupt.extraction.types import ExtractionStatus


def test_html_extraction_strips_markup_and_returns_text():
    """GIVEN an HTML page with tags, attributes and nested elements WHEN
    extract_html runs THEN it returns TEXT_LAYER status with the visible
    text only, no markup."""
    html = (
        "<html><body><h1>Register of Interests</h1>"
        "<p class='entry'>Chairman, <b>Example Ltd</b> (software)</p>"
        "</body></html>"
    )

    result = extract_html(html)

    assert result.status is ExtractionStatus.TEXT_LAYER
    assert "<" not in result.text
    assert "Register of Interests" in result.text
    assert "Example Ltd" in result.text
    assert result.is_reliable is True


def test_genuinely_empty_html_document_is_distinguishable_from_failed_extraction():
    """GIVEN a well-formed HTML document with no text content WHEN
    extract_html runs THEN it succeeds with TEXT_LAYER and char_count 0 —
    not FAILED, which is reserved for input that could not be parsed."""
    empty_result = extract_html("<html><body></body></html>")

    assert empty_result.status is ExtractionStatus.TEXT_LAYER
    assert empty_result.char_count == 0
    assert empty_result.text == ""

    failed_result = extract_html(None)  # not a str/bytes/file-like — cannot be parsed

    assert failed_result.status is ExtractionStatus.FAILED
    assert failed_result.error is not None
    assert failed_result.is_reliable is False


def test_html_extraction_reports_source_format_and_method():
    """GIVEN any HTML input WHEN extract_html runs THEN the result records
    source_format 'html' and method 'beautifulsoup'."""
    result = extract_html("<p>text</p>")

    assert result.source_format == "html"
    assert result.method == "beautifulsoup"
