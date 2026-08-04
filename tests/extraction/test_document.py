"""Tests for the extract_document dispatcher.

Verifies dispatch by file extension and that no network call is ever made
— this layer only reads bytes already on disk.
"""

from tests.extraction.pdf_fixtures import build_text_pdf
from uncorrupt.extraction.document import extract_document
from uncorrupt.extraction.types import ExtractionStatus

_PAGE_TEXT = "The register lists the members declared financial interests for the year."


def test_dispatches_pdf_by_extension(tmp_path):
    """GIVEN a .pdf file WHEN extract_document runs THEN it dispatches to
    the PDF extractor and returns TEXT_LAYER for a digital-native PDF."""
    path = tmp_path / "report.pdf"
    path.write_bytes(build_text_pdf([_PAGE_TEXT]))

    result = extract_document(path)

    assert result.status is ExtractionStatus.TEXT_LAYER
    assert result.source_format == "pdf"
    assert _PAGE_TEXT in result.text


def test_dispatches_html_by_extension(tmp_path):
    """GIVEN a .html file WHEN extract_document runs THEN it dispatches to
    the HTML extractor and returns text with markup stripped."""
    path = tmp_path / "register.html"
    path.write_text("<html><body><p>Declared interest: Example Ltd</p></body></html>")

    result = extract_document(path)

    assert result.status is ExtractionStatus.TEXT_LAYER
    assert result.source_format == "html"
    assert "Declared interest: Example Ltd" in result.text
    assert "<p>" not in result.text


def test_htm_extension_also_dispatches_to_html(tmp_path):
    """GIVEN a .htm (not .html) file WHEN extract_document runs THEN it is
    still dispatched to the HTML extractor."""
    path = tmp_path / "page.htm"
    path.write_text("<p>short form extension</p>")

    result = extract_document(path)

    assert result.source_format == "html"


def test_unsupported_extension_is_failed(tmp_path):
    """GIVEN a file with an unsupported extension WHEN extract_document runs
    THEN it reports FAILED with an actionable error rather than guessing a
    format or crashing."""
    path = tmp_path / "notes.docx"
    path.write_bytes(b"not actually a docx")

    result = extract_document(path)

    assert result.status is ExtractionStatus.FAILED
    assert ".docx" in result.error


def test_missing_file_is_failed(tmp_path):
    """GIVEN a path that does not exist WHEN extract_document runs THEN it
    reports FAILED rather than raising."""
    result = extract_document(tmp_path / "missing.pdf")

    assert result.status is ExtractionStatus.FAILED
    assert result.error is not None
