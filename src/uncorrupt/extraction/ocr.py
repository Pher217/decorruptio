"""Pluggable OCR backend for the extraction quality gate.

No OCR engine is hard-wired: ``OCRBackend`` is a Protocol, so any engine
(a different local library, a cloud OCR API, a test stub) can be plugged in
via ``extract_pdf_bytes(data, ocr_backend=...)``. ``TesseractOCRBackend`` is
the shipped default — entirely optional (extras group ``ocr`` in
pyproject.toml) and lazily imported, so a machine without it still imports
this module fine and gets a clear, actionable message instead of an import
crash or a silent empty result.

Rendering uses pypdfium2 rather than pdf2image on purpose: pypdfium2 ships
prebuilt wheels and needs no system binary, whereas pdf2image needs the
poppler system binary — the exact "brew install poppler mid-run" pain this
layer exists to remove. The ``tesseract`` binary itself is unavoidable: no
Python OCR wrapper works without an OCR engine of some kind on the machine.
"""

from __future__ import annotations

import logging
import shutil
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class OCRBackend(Protocol):
    """The contract a pluggable OCR backend must satisfy."""

    name: str

    def is_available(self) -> bool:
        """Whether this backend's libraries and binaries are all present."""
        ...

    def unavailable_reason(self) -> str:
        """Actionable message explaining why ``is_available()`` is False.

        Empty string when the backend is available.
        """
        ...

    def extract(self, pdf_bytes: bytes) -> tuple[str, int]:
        """Run OCR over every page of ``pdf_bytes``, return (text, page_count)."""
        ...


class TesseractOCRBackend:
    """Default OCR backend: pypdfium2 rasterization + pytesseract recognition."""

    name = "tesseract"

    def is_available(self) -> bool:
        return not self.unavailable_reason()

    def unavailable_reason(self) -> str:
        missing = []
        try:
            import pypdfium2  # noqa: F401
        except ImportError:
            missing.append("pypdfium2")
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            missing.append("pytesseract")
        try:
            import PIL  # noqa: F401
        except ImportError:
            missing.append("pillow")
        if missing:
            return (
                f"missing Python package(s): {', '.join(missing)} — install with: "
                "pip install 'uncorrupt[ocr]'"
            )
        if shutil.which("tesseract") is None:
            return (
                "the 'tesseract' binary is not on PATH — install it with: "
                "brew install tesseract (macOS) or apt-get install tesseract-ocr "
                "(Debian/Ubuntu)"
            )
        return ""

    def extract(self, pdf_bytes: bytes) -> tuple[str, int]:
        import pypdfium2 as pdfium
        import pytesseract

        pdf = pdfium.PdfDocument(pdf_bytes)
        try:
            page_texts = []
            for page in pdf:
                bitmap = page.render(scale=2.0)
                image = bitmap.to_pil()
                page_texts.append(pytesseract.image_to_string(image))
            return "\n".join(page_texts), len(pdf)
        finally:
            pdf.close()


def default_ocr_backend() -> OCRBackend:
    """The reference OCR backend. Callers may pass their own to override it."""
    return TesseractOCRBackend()
