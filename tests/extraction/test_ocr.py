"""Tests for the pluggable OCR backend's availability detection.

The default backend (TesseractOCRBackend) is genuinely uninstalled in this
project's environment on purpose (extras group "ocr" is declared but not
installed — see pyproject.toml) so the "missing packages" case below is a
real, unmocked assertion. The "packages present, binary missing" case is
simulated via sys.modules injection so it stays deterministic regardless
of what is actually installed on the machine running the suite.
"""

import shutil
import sys
import types

from uncorrupt.extraction.ocr import TesseractOCRBackend, default_ocr_backend


def test_default_backend_is_tesseract():
    """GIVEN default_ocr_backend WHEN called THEN it returns a
    TesseractOCRBackend instance."""
    backend = default_ocr_backend()

    assert isinstance(backend, TesseractOCRBackend)
    assert backend.name == "tesseract"


def test_missing_python_packages_are_reported_by_name():
    """GIVEN pypdfium2, pytesseract and pillow are not installed in this
    environment (the real, current state — no mocking) WHEN
    unavailable_reason runs THEN it names all three and gives a pip install
    hint, and is_available is False."""
    backend = TesseractOCRBackend()

    reason = backend.unavailable_reason()

    assert backend.is_available() is False
    assert "pypdfium2" in reason
    assert "pytesseract" in reason
    assert "pillow" in reason
    assert "pip install" in reason


def test_missing_tesseract_binary_is_reported_when_packages_present(monkeypatch):
    """GIVEN the Python packages are importable but the tesseract binary is
    not on PATH WHEN unavailable_reason runs THEN it reports the binary as
    missing with an install hint, distinct from the missing-package
    message."""
    for module_name in ("pypdfium2", "pytesseract", "PIL"):
        monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    backend = TesseractOCRBackend()
    reason = backend.unavailable_reason()

    assert backend.is_available() is False
    assert "tesseract" in reason.lower()
    assert "PATH" in reason
    assert "pypdfium2" not in reason


def test_backend_available_when_packages_and_binary_present(monkeypatch):
    """GIVEN both the Python packages and the tesseract binary are present
    WHEN is_available runs THEN it returns True with an empty
    unavailable_reason."""
    for module_name in ("pypdfium2", "pytesseract", "PIL"):
        monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/tesseract")

    backend = TesseractOCRBackend()

    assert backend.is_available() is True
    assert backend.unavailable_reason() == ""
