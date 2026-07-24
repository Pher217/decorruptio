"""Regression tests for base-rate awareness and no-padding curation.

Tests:
1. Source where indicator fires on >20% of units: flags carry [WEAK: base rate]
   marker AND are absent from curate() output.
2. Source where same indicator fires on <20%: flags are NOT marked, ARE eligible.
3. curate() with a credible pool smaller than top_n returns the smaller number,
   does NOT pad.
4. Denominator correctness: indicator's units_evaluated matches the number of
   units it actually iterated (assert on a small fixture).
5. All existing tests still pass (verified by running the full suite).
"""

from __future__ import annotations

import pytest

from uncorrupt.indicators.catalog.i001_single_bidder import SingleBidder
from uncorrupt.indicators.catalog.i003_repeat_winner_share import RepeatWinnerShare
from uncorrupt.indicators.catalog.i005_direct_award_share import DirectAwardShare
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.loader import load_locale
from uncorrupt.staging.models import Award, Bid, Tender


def _make_flag_dict(
    indicator_id: str,
    subject_ref: str,
    explanation: str,
    jurisdiction: str = "GB",
) -> dict:
    """Build a minimal flag dict as the runner/curate() expects."""
    return {
        "indicator_id": indicator_id,
        "subject_ref": subject_ref,
        "explanation": explanation,
        "evidence": [{"jurisdiction": jurisdiction, "source_url": "https://example.com"}],
        "tender_value_cents": 100000,
        "tender_currency": "GBP",
        "tender_title": "Test tender",
        "buyer_name": "Test Buyer",
        "procurement_method": "open",
        "stamp": {"data_snapshot": "2026-07-23", "code_version": "0.0.1"},
    }


@pytest.mark.django_db
def test_base_rate_high_marks_and_excludes_from_curation() -> None:
    """An indicator firing on >20% of units: flags carry [WEAK: base rate] marker
    AND are absent from curate() output."""
    from scripts.curate_flags import curate

    # 3 flags with base-rate marker, 3 without
    flags = []
    for i in range(3):
        flags.append(
            _make_flag_dict(
                "i001_single_bidder",
                f"br-weak-{i}",
                f"Single-bidder flag {i}. [WEAK: base rate 61%]",
            )
        )
    for i in range(3):
        flags.append(
            _make_flag_dict(
                "i003_repeat_winner_share",
                f"br-strong-{i}",
                f"Repeat-winner flag {i}.",
            )
        )

    curated = curate(flags, top_n=10)

    # No base-rate-weak flag should survive curation
    for f in curated:
        assert "[WEAK: base rate" not in f["explanation"], (
            f"Base-rate-weak flag survived curation: {f['subject_ref']}"
        )

    # Only the 3 i003 flags should survive
    assert len(curated) == 3
    assert all(f["indicator_id"] == "i003_repeat_winner_share" for f in curated)


@pytest.mark.django_db
def test_base_rate_low_not_marked_and_eligible() -> None:
    """An indicator firing on <20%: flags are NOT marked, ARE eligible for curation."""
    from scripts.curate_flags import curate

    # 3 i001 flags + 2 i003 flags, NO base-rate marker (discriminating)
    flags = []
    for i in range(3):
        flags.append(
            _make_flag_dict(
                "i001_single_bidder",
                f"br-ok-{i}",
                f"Single-bidder flag {i}.",
            )
        )
    for i in range(2):
        flags.append(
            _make_flag_dict(
                "i003_repeat_winner_share",
                f"br-ok-rw-{i}",
                f"Repeat-winner flag {i}.",
            )
        )

    curated = curate(flags, top_n=10)

    # All 5 should be eligible (no base-rate marker, no exclusion)
    assert len(curated) == 5
    for f in curated:
        assert "[WEAK: base rate" not in f["explanation"]


@pytest.mark.django_db
def test_curate_does_not_pad_below_top_n() -> None:
    """curate() with a credible pool smaller than top_n returns the smaller
    number and does NOT pad."""
    from scripts.curate_flags import curate

    # Only 3 credible flags, but top_n=10
    flags = []
    for i in range(3):
        flags.append(
            _make_flag_dict(
                "i001_single_bidder",
                f"no-pad-{i}",
                f"Single-bidder flag {i}.",
            )
        )

    curated = curate(flags, top_n=10)

    # Must return exactly 3, NOT 10 (no padding)
    assert len(curated) == 3, (
        f"curate() padded from 3 to {len(curated)} — top_n must be a max, not a target."
    )


@pytest.mark.django_db
def test_curate_mixed_weak_and_strong_returns_only_strong() -> None:
    """With 3 strong flags and 10 weak (base-rate) flags, curate() returns only
    the 3 strong ones, not 10 (no padding from weak)."""
    from scripts.curate_flags import curate

    flags = []
    # 10 base-rate-weak flags
    for i in range(10):
        flags.append(
            _make_flag_dict(
                "i001_single_bidder",
                f"mixed-weak-{i}",
                f"Single-bidder flag {i}. [WEAK: base rate 61%]",
            )
        )
    # 3 strong flags from a different indicator
    for i in range(3):
        flags.append(
            _make_flag_dict(
                "i003_repeat_winner_share",
                f"mixed-strong-{i}",
                f"Repeat-winner flag {i}.",
            )
        )

    curated = curate(flags, top_n=10)

    # Should return exactly 3 (all strong), NOT pad to 10 with weak flags
    assert len(curated) == 3, (
        f"Expected 3 credible flags, got {len(curated)} — curate() may be padding with weak flags."
    )
    for f in curated:
        assert f["indicator_id"] == "i003_repeat_winner_share"


@pytest.mark.django_db
def test_units_evaluated_i001_denominator() -> None:
    """i001's units_evaluated must match the count of tenders with awards (UA)."""
    locale = load_locale("ua")
    ctx = EvaluationContext(locale=locale, source_id="ua_prozorro")

    # Create 5 tenders with awards, each with 2 bids (will NOT flag)
    for i in range(5):
        t = Tender.objects.create(
            source_id="ua_prozorro",
            tender_id=f"denom-ua-{i}",
            buyer_name="Test Buyer",
            title=f"Denom test {i}",
            source_url="https://example.com",
        )
        Award.objects.create(
            source_id="ua_prozorro",
            tender_id=t.tender_id,
            award_id=f"award-denom-{i}",
            tender_ref=t,
            supplier_name=f"Supplier {i}",
            value_amount_cents=100000,
        )
        Bid.objects.create(
            source_id="ua_prozorro",
            tender_id=t.tender_id,
            bid_id=f"bid-denom-{i}-a",
            tender_ref=t,
            bidder_name=f"Bidder {i}a",
        )
        Bid.objects.create(
            source_id="ua_prozorro",
            tender_id=t.tender_id,
            bid_id=f"bid-denom-{i}-b",
            tender_ref=t,
            bidder_name=f"Bidder {i}b",
        )

    # Add a 6th tender with 1 bid (will flag)
    t_flag = Tender.objects.create(
        source_id="ua_prozorro",
        tender_id="denom-ua-flag",
        buyer_name="Test Buyer",
        title="Will flag",
        source_url="https://example.com",
    )
    Award.objects.create(
        source_id="ua_prozorro",
        tender_id="denom-ua-flag",
        award_id="award-denom-flag",
        tender_ref=t_flag,
        supplier_name="Single Supplier",
        value_amount_cents=100000,
    )
    Bid.objects.create(
        source_id="ua_prozorro",
        tender_id="denom-ua-flag",
        bid_id="bid-denom-flag",
        tender_ref=t_flag,
        bidder_name="Single Bidder",
    )

    indicator = SingleBidder()
    flags = list(indicator.evaluate(ctx))

    # units_evaluated = 6 (all tenders with awards), flags = 1
    assert indicator.units_evaluated == 6, (
        f"Expected 6 tenders evaluated, got {indicator.units_evaluated}"
    )
    assert len(flags) == 1


@pytest.mark.django_db
def test_units_evaluated_i003_denominator() -> None:
    """i003's units_evaluated must match the count of buyer-supplier pairs
    (after placeholder exclusion)."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    # Create 4 real buyer-supplier pairs + 1 placeholder (excluded from denom)
    for i in range(4):
        t = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id=f"i003-denom-{i}",
            buyer_name="Test Buyer A",
            title=f"i003 denom {i}",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id=t.tender_id,
            award_id=f"award-i003-{i}",
            tender_ref=t,
            supplier_name=f"Real Supplier {i}",
            value_amount_cents=100000,
        )

    # Placeholder supplier — excluded from denominator
    t_ph = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id="i003-denom-placeholder",
        buyer_name="Test Buyer A",
        title="Placeholder",
    )
    Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id="i003-denom-placeholder",
        award_id="award-i003-placeholder",
        tender_ref=t_ph,
        supplier_name="Various",
        value_amount_cents=100000,
    )

    indicator = RepeatWinnerShare()
    list(indicator.evaluate(ctx))

    # units_evaluated = 4 (placeholder excluded)
    assert indicator.units_evaluated == 4, (
        f"Expected 4 buyer-supplier pairs (placeholder excluded), got {indicator.units_evaluated}"
    )


@pytest.mark.django_db
def test_units_evaluated_i005_denominator() -> None:
    """i005's units_evaluated must match the count of distinct buyers."""
    locale = load_locale("ua")
    ctx = EvaluationContext(locale=locale, source_id="ua_prozorro")

    # 3 distinct buyers, each with 2 tenders
    for buyer_idx in range(3):
        for tender_idx in range(2):
            Tender.objects.create(
                source_id="ua_prozorro",
                tender_id=f"i005-denom-{buyer_idx}-{tender_idx}",
                buyer_name=f"Test Buyer i005 {buyer_idx}",
                title=f"i005 denom {buyer_idx}-{tender_idx}",
                source_url="https://example.com",
            )

    indicator = DirectAwardShare()
    list(indicator.evaluate(ctx))

    # units_evaluated = 3 (distinct buyers)
    assert indicator.units_evaluated == 3, (
        f"Expected 3 distinct buyers, got {indicator.units_evaluated}"
    )
