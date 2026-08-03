"""Tests for PDF text extraction: text-layer first, OCR as a
quality-gated fallback.

The OCR routing tests use a controllable stub backend rather than the real
default (TesseractOCRBackend) — the default's own availability-detection
logic is covered separately in test_ocr.py. This keeps the routing tests
deterministic regardless of whether tesseract happens to be installed on
the machine running the suite.
"""

from tests.extraction.pdf_fixtures import build_blank_pdf, build_text_pdf
from uncorrupt.extraction.pdf import extract_pdf_bytes, extract_pdf_path
from uncorrupt.extraction.types import ExtractionStatus

_PAGE_ONE_TEXT = (
    "The statutory inquiry found that trustees failed to exercise adequate "
    "oversight of the charitys financial controls over several years."
)
_PAGE_TWO_TEXT = (
    "As a result the regulator issued a formal action plan requiring the "
    "trustees to remediate governance failings within six months."
)
_OCR_RECOVERED_TEXT = (
    "Recovered by OCR: the scanned board minutes record that the director "
    "approved the disputed payment without reference to the audit committee."
)


class _StubOCRBackend:
    """A controllable OCRBackend for testing the pdf.py routing logic."""

    name = "stub-ocr"

    def __init__(self, text: str = "", available: bool = True, error: Exception | None = None):
        self._text = text
        self._available = available
        self._error = error

    def is_available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str:
        return "" if self._available else "stub backend intentionally disabled for this test"

    def extract(self, pdf_bytes: bytes) -> tuple[str, int]:
        if self._error is not None:
            raise self._error
        return self._text, 1


def test_digital_native_pdf_uses_text_layer():
    """GIVEN a PDF with a real embedded text layer WHEN extract_pdf_bytes
    runs THEN it reports TEXT_LAYER via pypdf, with the page's text and no
    OCR involved."""
    pdf_bytes = build_text_pdf([_PAGE_ONE_TEXT])

    result = extract_pdf_bytes(pdf_bytes)

    assert result.status is ExtractionStatus.TEXT_LAYER
    assert result.method == "pypdf"
    assert _PAGE_ONE_TEXT in result.text
    assert result.page_count == 1
    assert result.is_reliable is True
    assert result.quality is not None
    assert result.quality.passed is True


def test_digital_native_multi_page_pdf_reports_all_pages():
    """GIVEN a two-page PDF with a real text layer on each page WHEN
    extract_pdf_bytes runs THEN page_count is 2 and both pages' text is
    present."""
    pdf_bytes = build_text_pdf([_PAGE_ONE_TEXT, _PAGE_TWO_TEXT])

    result = extract_pdf_bytes(pdf_bytes)

    assert result.status is ExtractionStatus.TEXT_LAYER
    assert result.page_count == 2
    assert _PAGE_ONE_TEXT in result.text
    assert _PAGE_TWO_TEXT in result.text


def test_scanned_pdf_fails_gate_and_routes_to_ocr():
    """GIVEN a structurally valid PDF with no embedded text (a scanned
    page) WHEN extract_pdf_bytes runs with an available OCR backend THEN
    the text layer fails the quality gate and OCR recovers the text,
    reporting status OCR."""
    pdf_bytes = build_blank_pdf(page_count=1)
    stub = _StubOCRBackend(text=_OCR_RECOVERED_TEXT)

    result = extract_pdf_bytes(pdf_bytes, ocr_backend=stub)

    assert result.status is ExtractionStatus.OCR
    assert result.method == "stub-ocr"
    assert result.text == _OCR_RECOVERED_TEXT
    assert result.is_reliable is True


def test_unavailable_ocr_backend_never_returns_empty_success():
    """GIVEN a scanned PDF and an OCR backend that reports itself
    unavailable WHEN extract_pdf_bytes runs THEN it returns
    BACKEND_UNAVAILABLE with an actionable error — never TEXT_LAYER or OCR,
    and never an empty success."""
    pdf_bytes = build_blank_pdf(page_count=1)
    stub = _StubOCRBackend(available=False)

    result = extract_pdf_bytes(pdf_bytes, ocr_backend=stub)

    assert result.status is ExtractionStatus.BACKEND_UNAVAILABLE
    assert result.status not in (ExtractionStatus.TEXT_LAYER, ExtractionStatus.OCR)
    assert result.is_reliable is False
    assert result.error is not None
    assert "stub backend intentionally disabled" in result.error


def test_ocr_that_also_fails_the_gate_is_extraction_unreliable():
    """GIVEN a scanned PDF whose OCR output is itself dense garbage (per
    ADR-008: a poor scan can defeat OCR too) WHEN extract_pdf_bytes runs
    THEN it reports EXTRACTION_UNRELIABLE, not a false TEXT_LAYER/OCR
    success and not a bare FAILED — the document was processed, just not
    reliably."""
    pdf_bytes = build_blank_pdf(page_count=1)
    stub = _StubOCRBackend(text="#$%^ &*() _+=- " * 20)

    result = extract_pdf_bytes(pdf_bytes, ocr_backend=stub)

    assert result.status is ExtractionStatus.EXTRACTION_UNRELIABLE
    assert result.is_reliable is False
    assert result.error is not None


def test_ocr_backend_raising_is_failed_not_empty_success():
    """GIVEN a scanned PDF and an OCR backend that raises during extraction
    (e.g. the engine crashed) WHEN extract_pdf_bytes runs THEN it reports
    FAILED with the underlying error, never a silent empty success."""
    pdf_bytes = build_blank_pdf(page_count=1)
    stub = _StubOCRBackend(error=RuntimeError("tesseract process crashed"))

    result = extract_pdf_bytes(pdf_bytes, ocr_backend=stub)

    assert result.status is ExtractionStatus.FAILED
    assert "tesseract process crashed" in result.error


def test_corrupt_bytes_are_failed_distinct_from_unreliable_blank_pdf():
    """GIVEN bytes that are not a PDF at all WHEN extract_pdf_bytes runs
    THEN it reports FAILED with page_count None (the file could not even be
    opened) — distinct from a structurally valid but textless PDF, which
    reports a real page_count and BACKEND_UNAVAILABLE/EXTRACTION_UNRELIABLE,
    never FAILED. A genuinely empty/unreadable document must not be
    confused with a parser that broke."""
    corrupt_result = extract_pdf_bytes(b"this is not a pdf file at all")

    assert corrupt_result.status is ExtractionStatus.FAILED
    assert corrupt_result.page_count is None
    assert corrupt_result.is_reliable is False

    blank_pdf_bytes = build_blank_pdf(page_count=1)
    blank_result = extract_pdf_bytes(blank_pdf_bytes, ocr_backend=_StubOCRBackend(available=False))

    assert blank_result.status is not ExtractionStatus.FAILED
    assert blank_result.page_count == 1


def test_extract_pdf_path_reads_from_disk(tmp_path):
    """GIVEN a PDF file written to disk WHEN extract_pdf_path runs THEN it
    reads the bytes and extracts the same text as extract_pdf_bytes."""
    pdf_path = tmp_path / "document.pdf"
    pdf_path.write_bytes(build_text_pdf([_PAGE_ONE_TEXT]))

    result = extract_pdf_path(pdf_path)

    assert result.status is ExtractionStatus.TEXT_LAYER
    assert _PAGE_ONE_TEXT in result.text


def test_extract_pdf_path_missing_file_is_failed(tmp_path):
    """GIVEN a path that does not exist WHEN extract_pdf_path runs THEN it
    reports FAILED with an actionable error rather than raising."""
    result = extract_pdf_path(tmp_path / "does_not_exist.pdf")

    assert result.status is ExtractionStatus.FAILED
    assert result.error is not None


def test_missing_pypdf_dependency_is_backend_unavailable(monkeypatch):
    """GIVEN pypdf is not importable (the primary text-layer backend is
    missing) WHEN extract_pdf_bytes runs THEN it reports BACKEND_UNAVAILABLE
    with an actionable install hint, never crashing on import."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("simulated: pypdf not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    result = extract_pdf_bytes(build_text_pdf([_PAGE_ONE_TEXT]))

    assert result.status is ExtractionStatus.BACKEND_UNAVAILABLE
    assert "pip install" in result.error
