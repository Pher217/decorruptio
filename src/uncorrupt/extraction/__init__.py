"""Document-extraction layer: text-layer first, OCR as a quality-gated fallback.

Research agents repeatedly hit documents they could not read — compressed
PDF streams that broke ``WebFetch``, hand-rolled ``pdftotext`` shell-outs,
a mid-run ``brew install poppler``. Inquiry reports, judgments and
statutory accounts are primary evidence for this project, so this layer
gives every caller one deterministic path to text:

1. Try the PDF's embedded text layer (:mod:`pypdf`) — cheap and lossless
   when present.
2. Assess quality on measurable signals — chars per page, alphanumeric
   ratio, word-shape ratio (:mod:`uncorrupt.extraction.quality`) — not a
   guess.
3. Fall back to OCR only when the gate fails, behind a pluggable backend
   (:mod:`uncorrupt.extraction.ocr`) so no engine is hard-wired.

No LLM anywhere in this layer (ADR-004 permits LLMs in extraction
generally, but this component is deterministic text recovery). Every
result is an :class:`~uncorrupt.extraction.types.ExtractionResult` with an
explicit status — never a silent empty success; see
``ExtractionResult.is_reliable`` and ADR-008.
"""

from __future__ import annotations

from uncorrupt.extraction.document import extract_document
from uncorrupt.extraction.html import extract_html
from uncorrupt.extraction.ocr import OCRBackend, TesseractOCRBackend, default_ocr_backend
from uncorrupt.extraction.pdf import extract_pdf_bytes, extract_pdf_path
from uncorrupt.extraction.quality import QualityAssessment, assess_text_quality
from uncorrupt.extraction.types import ExtractionResult, ExtractionStatus

__all__ = [
    "ExtractionResult",
    "ExtractionStatus",
    "OCRBackend",
    "QualityAssessment",
    "TesseractOCRBackend",
    "assess_text_quality",
    "default_ocr_backend",
    "extract_document",
    "extract_html",
    "extract_pdf_bytes",
    "extract_pdf_path",
]
