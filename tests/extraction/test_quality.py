"""Tests for the extraction quality gate.

Verifies the gate decides text-layer-vs-OCR on measurable signals — chars
per page, alphanumeric ratio, word-shape ratio — not a guess, and catches
both an empty text layer and a dense-but-garbled one.
"""

from uncorrupt.extraction.quality import assess_text_quality

_REAL_PROSE = (
    "The statutory inquiry found that trustees failed to exercise adequate "
    "oversight of the charity's financial controls over a period of several "
    "years, resulting in significant unaccounted expenditure. "
) * 3


def test_dense_real_prose_passes_the_gate():
    """GIVEN dense, real-word text across one page WHEN assessed THEN the gate
    passes on all three signals."""
    assessment = assess_text_quality(_REAL_PROSE, page_count=1)

    assert assessment.passed is True


def test_empty_text_fails_on_chars_per_page():
    """GIVEN zero extracted characters WHEN assessed THEN the gate fails and
    reports chars_per_page as the (only possible) failing signal."""
    assessment = assess_text_quality("", page_count=1)

    assert assessment.passed is False
    assert assessment.chars_per_page == 0.0
    assert "chars_per_page" in assessment.reason


def test_sparse_text_fails_on_chars_per_page():
    """GIVEN a handful of characters spread over several pages WHEN assessed
    THEN chars_per_page falls below threshold and the gate fails."""
    assessment = assess_text_quality("ok", page_count=5)

    assert assessment.passed is False
    assert assessment.chars_per_page == 0.4


def test_dense_garbled_text_fails_on_alnum_and_word_shape():
    """GIVEN dense but non-alphanumeric noise (a mis-decoded stream) WHEN
    assessed THEN chars_per_page alone would pass, but alnum_ratio and
    word_shape_ratio catch it and the gate fails."""
    garbled = "��� #$%^&*()_+ " * 20

    assessment = assess_text_quality(garbled, page_count=1)

    assert assessment.passed is False
    assert assessment.chars_per_page >= 50.0
    assert assessment.alnum_ratio < 0.5


def test_page_count_zero_does_not_divide_by_zero():
    """GIVEN a page_count of 0 (a reader that detected no pages at all) WHEN
    assessed THEN chars_per_page is computed against 1 page, not 0."""
    assessment = assess_text_quality(_REAL_PROSE, page_count=0)

    assert assessment.chars_per_page == len(_REAL_PROSE)
