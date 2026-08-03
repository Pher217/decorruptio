"""PDF text extraction: the embedded text layer first, OCR as a
quality-gated fallback.

No LLM anywhere in this module (ADR-004 permits LLMs in extraction
generally, but this component is deterministic text recovery — a model
here would make output unreproducible; structuring text into claims is a
separate, later concern).

Per ADR-008, an image-only PDF or a failed text layer must surface as
EXTRACTION_UNRELIABLE, never as evidence that a quote is absent from the
document — see ``ExtractionResult.is_reliable``.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

from uncorrupt.extraction.ocr import OCRBackend, default_ocr_backend
from uncorrupt.extraction.quality import assess_text_quality
from uncorrupt.extraction.types import ExtractionResult, ExtractionStatus

logger = logging.getLogger(__name__)

_PYPDF_INSTALL_HINT = (
    "pypdf is required for PDF text-layer extraction but is not installed. "
    "Install it with: pip install 'uncorrupt[pdf]' (or: pip install pypdf)."
)


class _BackendUnavailableError(Exception):
    """The text-layer backend (pypdf) itself is not installed."""


class _ParseFailedError(Exception):
    """The bytes could not be parsed as a PDF at all."""


def _read_text_layer(data: bytes) -> tuple[list[str], int]:
    """Read the embedded text layer of a PDF, page by page.

    Raises ``_BackendUnavailableError`` if pypdf is not installed, or
    ``_ParseFailedError`` if the bytes cannot be parsed as a PDF at all —
    never silently returns pages for a document that could not be opened.
    """
    try:
        import pypdf
    except ImportError as exc:
        raise _BackendUnavailableError(_PYPDF_INSTALL_HINT) from exc

    try:
        reader = pypdf.PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises several distinct error types across versions
        raise _ParseFailedError(str(exc)) from exc

    return pages, len(pages)


def extract_pdf_bytes(data: bytes, ocr_backend: OCRBackend | None = None) -> ExtractionResult:
    """Extract text from PDF bytes: embedded text layer first, OCR as fallback.

    ``ocr_backend`` overrides the default (``TesseractOCRBackend``) — pass a
    different implementation, or a test stub, to control the fallback path.
    """
    try:
        pages, page_count = _read_text_layer(data)
    except _BackendUnavailableError as exc:
        return ExtractionResult(
            status=ExtractionStatus.BACKEND_UNAVAILABLE,
            text="",
            page_count=None,
            char_count=0,
            source_format="pdf",
            method=None,
            quality=None,
            error=str(exc),
        )
    except _ParseFailedError as exc:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            page_count=None,
            char_count=0,
            source_format="pdf",
            method=None,
            quality=None,
            error=f"could not parse PDF: {exc}",
        )

    text = "\n".join(pages)
    quality = assess_text_quality(text, page_count)
    if quality.passed:
        return ExtractionResult(
            status=ExtractionStatus.TEXT_LAYER,
            text=text,
            page_count=page_count,
            char_count=len(text),
            source_format="pdf",
            method="pypdf",
            quality=quality,
            error=None,
        )

    # Text layer failed the gate — scanned or image-only PDF. Fall back to OCR.
    backend = ocr_backend if ocr_backend is not None else default_ocr_backend()
    if not backend.is_available():
        return ExtractionResult(
            status=ExtractionStatus.BACKEND_UNAVAILABLE,
            text="",
            page_count=page_count,
            char_count=0,
            source_format="pdf",
            method=None,
            quality=quality,
            error=(
                f"text layer failed the quality gate ({quality.reason}) and the "
                f"{backend.name!r} OCR backend is unavailable: {backend.unavailable_reason()}"
            ),
        )

    try:
        ocr_text, ocr_page_count = backend.extract(data)
    except Exception as exc:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            page_count=page_count,
            char_count=0,
            source_format="pdf",
            method=backend.name,
            quality=quality,
            error=f"OCR backend {backend.name!r} raised: {exc}",
        )

    resolved_page_count = ocr_page_count or page_count
    ocr_quality = assess_text_quality(ocr_text, resolved_page_count)
    if ocr_quality.passed:
        return ExtractionResult(
            status=ExtractionStatus.OCR,
            text=ocr_text,
            page_count=resolved_page_count,
            char_count=len(ocr_text),
            source_format="pdf",
            method=backend.name,
            quality=ocr_quality,
            error=None,
        )

    return ExtractionResult(
        status=ExtractionStatus.EXTRACTION_UNRELIABLE,
        text=ocr_text,
        page_count=resolved_page_count,
        char_count=len(ocr_text),
        source_format="pdf",
        method=backend.name,
        quality=ocr_quality,
        error=(
            f"OCR fallback also failed the quality gate ({ocr_quality.reason}); the "
            "document may be genuinely unreadable (poor scan, blank page, corrupted "
            "image) — treat as unreliable, never as evidence the text is absent."
        ),
    )


def extract_pdf_path(path: str | Path, ocr_backend: OCRBackend | None = None) -> ExtractionResult:
    """Read a PDF file from disk and extract its text. No network calls."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            page_count=None,
            char_count=0,
            source_format="pdf",
            method=None,
            quality=None,
            error=f"could not read {path}: {exc}",
        )
    return extract_pdf_bytes(data, ocr_backend=ocr_backend)
