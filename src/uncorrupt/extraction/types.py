"""Result types for the document-extraction layer.

The governing requirement (per ADR-008) is that a wrong or unreliable
extraction must be detectable, never a silent empty success. Every
``ExtractionResult`` therefore carries an explicit ``status`` plus page and
character counts, so a caller can tell "this document has little text" from
"extraction broke" — and, crucially, never mistake either one for evidence
that a quote is absent from the source document.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from uncorrupt.extraction.quality import QualityAssessment


class ExtractionStatus(Enum):
    """The outcome of one extraction attempt.

    TEXT_LAYER: the embedded text layer was read and passed the quality gate.
    OCR: the text layer failed the gate; OCR ran and passed the gate.
    EXTRACTION_UNRELIABLE: both the text layer and OCR (if attempted) failed
        the gate — e.g. an image-only PDF whose scan quality defeats OCR too.
        Per ADR-008 this must never be read as "the document has no text".
    BACKEND_UNAVAILABLE: OCR was needed but no working backend is installed
        or configured. Distinct from EXTRACTION_UNRELIABLE: the document may
        well be readable, we just cannot currently read it.
    FAILED: the input could not be parsed as the claimed format at all (e.g.
        corrupt bytes, an unreadable file) — the extraction machinery itself
        broke, before any quality question could even be asked.
    """

    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    EXTRACTION_UNRELIABLE = "extraction_unreliable"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    FAILED = "failed"


_RELIABLE_STATUSES = frozenset({ExtractionStatus.TEXT_LAYER, ExtractionStatus.OCR})


@dataclass(frozen=True)
class ExtractionResult:
    """The outcome of extracting text from one document.

    ``text`` is always a string (never None) so ``char_count`` is always
    meaningful, but ``text`` must only be treated as authoritative when
    ``is_reliable`` is True — a caller checking whether a quote is present in
    a source document must gate on that flag first (ADR-008): an unreliable
    result's empty or partial text is not evidence the quote is absent.
    """

    status: ExtractionStatus
    text: str
    page_count: int | None
    char_count: int
    source_format: str
    method: str | None
    quality: QualityAssessment | None
    error: str | None

    @property
    def is_reliable(self) -> bool:
        """Whether ``text`` passed the quality gate and can be trusted.

        False for EXTRACTION_UNRELIABLE, BACKEND_UNAVAILABLE and FAILED — a
        caller must not treat the absence of a quote in ``text`` as evidence
        the quote is absent from the source unless this is True.
        """
        return self.status in _RELIABLE_STATUSES
