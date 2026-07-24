"""Regression tests for indicator semantics fixes (consult 20260723T231935).

Tests two credibility fixes:
1. i003 repeat-winner: requires total_awards >= 4 (1-of-2 = 50% is noise, not signal)
2. i001 UK: excludes framework call-offs, only flags genuinely competitive procedures
"""

from __future__ import annotations

import pytest

from uncorrupt.indicators.catalog.i001_single_bidder import SingleBidder
from uncorrupt.indicators.catalog.i003_repeat_winner_share import RepeatWinnerShare
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.loader import load_locale
from uncorrupt.staging.models import Award, Tender


@pytest.mark.django_db
def test_i003_excludes_low_denominator() -> None:
    """A supplier winning 1 of 2 awards (50%) must NOT flag — it's the statistical floor."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    buyer = "Test Buyer Low Denominator"
    supplier = "Test Supplier"
    for i in range(2):
        t = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id=f"low-denom-{i}",
            buyer_name=buyer,
            title=f"Low denom tender {i}",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id=t.tender_id,
            award_id=f"award-{i}",
            tender_ref=t,
            supplier_name=supplier if i == 0 else f"Other Supplier {i}",
            value_amount_cents=100000,
        )

    indicator = RepeatWinnerShare()
    flags = list(indicator.evaluate(ctx))
    flag_subjects = [f.subject_ref for f in flags]
    # The 1-of-2 supplier must NOT appear in flags
    assert not any(supplier in s for s in flag_subjects), (
        f"i003 flagged a 1-of-2 (50%) pair — this is statistical noise, not signal. "
        f"Flags: {flag_subjects}"
    )


@pytest.mark.django_db
def test_i003_includes_high_denominator() -> None:
    """A supplier winning 4 of 5 awards (80%) from a buyer with 5 awards MUST flag."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    buyer = "Test Buyer High Denominator"
    dominant_supplier = "Dominant Supplier"
    for i in range(5):
        t = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id=f"high-denom-{i}",
            buyer_name=buyer,
            title=f"High denom tender {i}",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id=t.tender_id,
            award_id=f"award-hd-{i}",
            tender_ref=t,
            supplier_name=dominant_supplier if i < 4 else f"Other Supplier {i}",
            value_amount_cents=100000,
        )

    indicator = RepeatWinnerShare()
    flags = list(indicator.evaluate(ctx))
    flag_subjects = [f.subject_ref for f in flags]
    # The 4-of-5 supplier MUST appear in flags
    assert any(dominant_supplier in s for s in flag_subjects), (
        f"i003 failed to flag a 4-of-5 (80%) pair with 5 total awards. Flags: {flag_subjects}"
    )


@pytest.mark.django_db
def test_i001_uk_excludes_framework_calloff() -> None:
    """A framework call-off with single award must NOT flag — single-supplier by design."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    t = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id="framework-calloff-1",
        buyer_name="Test Buyer Framework",
        title="Framework Call-off Test",
        procurement_method="selective",
        procurement_method_details="Call-off from a framework agreement",
    )
    Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id="framework-calloff-1",
        award_id="fw-award-1",
        tender_ref=t,
        supplier_name="Framework Supplier",
        value_amount_cents=100000,
    )

    indicator = SingleBidder()
    flags = list(indicator.evaluate(ctx))
    flag_subjects = [f.subject_ref for f in flags]
    assert "framework-calloff-1" not in flag_subjects, (
        f"i001 flagged a framework call-off — these are single-supplier by design, "
        f"not suspicious. Flags: {flag_subjects}"
    )


@pytest.mark.django_db
def test_i001_uk_includes_competitive_single_award() -> None:
    """A genuinely competitive procedure (open) with single award MUST flag."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    t = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id="competitive-single-1",
        buyer_name="Test Buyer Competitive",
        title="Competitive Single Award Test",
        procurement_method="open",
        procurement_method_details="Open procedure",
    )
    Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id="competitive-single-1",
        award_id="comp-award-1",
        tender_ref=t,
        supplier_name="Only Supplier",
        value_amount_cents=100000,
    )

    indicator = SingleBidder()
    flags = list(indicator.evaluate(ctx))
    flag_subjects = [f.subject_ref for f in flags]
    assert "competitive-single-1" in flag_subjects, (
        f"i001 failed to flag an open procedure with single award. Flags: {flag_subjects}"
    )
