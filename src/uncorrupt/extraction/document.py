"""Single entry point: a document path in, an ``ExtractionResult`` out.

Dispatches to the PDF or HTML extractor by file extension. No network
calls — this layer only reads bytes already on disk; fetching them is a
separate concern handled upstream.
"""

from __future__ import annotations

from pathlib import Path

from uncorrupt.extraction.html import extract_html
from uncorrupt.extraction.ocr import OCRBackend
from uncorrupt.extraction.pdf import extract_pdf_bytes
from uncorrupt.extraction.types import ExtractionResult, ExtractionStatus

_SUPPORTED_SUFFIXES = {".pdf": "pdf", ".html": "html", ".htm": "html"}


def extract_document(path: str | Path, ocr_backend: OCRBackend | None = None) -> ExtractionResult:
    """Extract text from a document on disk, dispatching by file extension.

    ``ocr_backend`` is forwarded to the PDF extractor's quality-gated OCR
    fallback; ignored for HTML (which has no OCR fallback).
    """
    resolved_path = Path(path)
    fmt = _SUPPORTED_SUFFIXES.get(resolved_path.suffix.lower())
    if fmt is None:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            page_count=None,
            char_count=0,
            source_format=resolved_path.suffix.lstrip(".") or "unknown",
            method=None,
            quality=None,
            error=(
                f"unsupported file extension {resolved_path.suffix!r} — "
                "supported: .pdf, .html, .htm"
            ),
        )

    try:
        data = resolved_path.read_bytes()
    except OSError as exc:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            page_count=None,
            char_count=0,
            source_format=fmt,
            method=None,
            quality=None,
            error=f"could not read {resolved_path}: {exc}",
        )

    if fmt == "pdf":
        return extract_pdf_bytes(data, ocr_backend=ocr_backend)
    return extract_html(data.decode("utf-8", errors="replace"))
