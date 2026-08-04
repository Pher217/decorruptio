"""The extraction quality gate.

Decides text-layer-vs-OCR on something measurable, not a guess. A
digital-native PDF yields dense, mostly-alphanumeric, real-word text; a
scanned or image-only PDF yields an empty or near-empty text layer, and a
badly decoded stream yields dense but garbled text (mojibake) — a
chars-per-page check alone would pass that case, so it is checked alongside
an alphanumeric ratio and a word-shape ratio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A digital-native PDF page typically yields hundreds to thousands of
# characters; a scanned page's text layer yields zero (or near-zero, from
# stray OCR-like artefacts some scanners embed).
MIN_CHARS_PER_PAGE = 50.0

# Fraction of non-whitespace characters that must be alphanumeric. Catches
# a mis-decoded stream that is dense but mostly punctuation/symbol noise.
MIN_ALNUM_RATIO = 0.5

# Fraction of whitespace-separated tokens that must look like real words
# (start with a letter/digit, otherwise only ordinary word punctuation).
MIN_WORD_SHAPE_RATIO = 0.6

_WORD_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9'\-.,;:()/]*$")


@dataclass(frozen=True)
class QualityAssessment:
    """Measured signals behind one pass/fail gate decision."""

    chars_per_page: float
    alnum_ratio: float
    word_shape_ratio: float
    passed: bool
    reason: str


def assess_text_quality(text: str, page_count: int) -> QualityAssessment:
    """Score extracted text against the text-layer-vs-OCR gate thresholds.

    ``page_count`` is clamped to at least 1 so a zero-page reading (e.g. a
    reader that failed to detect any pages) still produces a finite,
    comparable chars-per-page figure rather than dividing by zero.
    """
    effective_pages = max(page_count, 1)
    chars_per_page = len(text) / effective_pages

    non_whitespace = [c for c in text if not c.isspace()]
    alnum_ratio = (
        sum(1 for c in non_whitespace if c.isalnum()) / len(non_whitespace)
        if non_whitespace
        else 0.0
    )

    words = text.split()
    word_shape_ratio = (
        sum(1 for w in words if _WORD_SHAPE_RE.match(w)) / len(words) if words else 0.0
    )

    failures = []
    if chars_per_page < MIN_CHARS_PER_PAGE:
        failures.append(f"chars_per_page {chars_per_page:.1f} < {MIN_CHARS_PER_PAGE}")
    if alnum_ratio < MIN_ALNUM_RATIO:
        failures.append(f"alnum_ratio {alnum_ratio:.2f} < {MIN_ALNUM_RATIO}")
    if word_shape_ratio < MIN_WORD_SHAPE_RATIO:
        failures.append(f"word_shape_ratio {word_shape_ratio:.2f} < {MIN_WORD_SHAPE_RATIO}")

    passed = not failures
    reason = "passed" if passed else "; ".join(failures)

    return QualityAssessment(
        chars_per_page=chars_per_page,
        alnum_ratio=alnum_ratio,
        word_shape_ratio=word_shape_ratio,
        passed=passed,
        reason=reason,
    )
