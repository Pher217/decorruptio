"""Regression tests for indicator semantics fixes (consult 20260723T231935 + review pass).

Tests credibility fixes:
1. i003 repeat-winner: requires total_awards >= 4 (1-of-2 = 50% is noise, not signal)
2. i001 UK: excludes framework call-offs, only flags genuinely competitive procedures
3. i003: excludes placeholder/junk supplier names ("Various", "N/A", etc.)
4. i005: minimum denominator guard (>= 4 tenders required)
5. i001: framework detection checks tender title, not just method fields
6. i004: below-estimate is weak/excluded from curated output; above-estimate still flags
7. source_id scoping: evaluating one source yields zero cross-source flags
"""

from __future__ import annotations

import pytest

from uncorrupt.indicators.catalog.i001_single_bidder import SingleBidder
from uncorrupt.indicators.catalog.i003_repeat_winner_share import RepeatWinnerShare
from uncorrupt.indicators.catalog.i004_price_vs_estimate import PriceVsEstimate
from uncorrupt.indicators.catalog.i005_direct_award_share import DirectAwardShare
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.loader import load_locale
from uncorrupt.staging.models import Award, Bid, Tender


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


@pytest.mark.django_db
def test_i003_excludes_placeholder_supplier() -> None:
    """A placeholder supplier name like 'Various' must NOT produce a concentration flag."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    buyer = "Test Buyer Placeholder"
    placeholder = "Various"
    real_supplier = "Real Supplier Co"
    for i in range(5):
        t = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id=f"placeholder-{i}",
            buyer_name=buyer,
            title=f"Placeholder tender {i}",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id=t.tender_id,
            award_id=f"award-pl-{i}",
            tender_ref=t,
            supplier_name=placeholder if i < 4 else real_supplier,
            value_amount_cents=100000,
        )

    indicator = RepeatWinnerShare()
    flags = list(indicator.evaluate(ctx))
    flag_subjects = [f.subject_ref for f in flags]
    assert not any(placeholder in s for s in flag_subjects), (
        f"i003 flagged a placeholder supplier '{placeholder}' — these are not real "
        f"entities. Flags: {flag_subjects}"
    )


@pytest.mark.django_db
def test_i005_excludes_low_denominator() -> None:
    """A buyer with only 3 tenders must NOT flag even at 100% direct award rate."""
    locale = load_locale("ua")
    ctx = EvaluationContext(locale=locale, source_id="ua_prozorro")

    buyer = "Test Buyer Low Denom i005"
    for i in range(3):
        Tender.objects.create(
            source_id="ua_prozorro",
            tender_id=f"i005-low-{i}",
            buyer_name=buyer,
            title=f"Low denom i005 tender {i}",
            procurement_method="limited",
            procurement_method_details="negotiation",
            source_url="https://example.com",
        )

    indicator = DirectAwardShare()
    flags = list(indicator.evaluate(ctx))
    flag_subjects = [f.subject_ref for f in flags]
    assert buyer not in flag_subjects, (
        f"i005 flagged a buyer with only 3 tenders — denominator too low for signal. "
        f"Flags: {flag_subjects}"
    )


@pytest.mark.django_db
def test_i005_includes_high_denominator() -> None:
    """A buyer with 5 tenders and 100% direct award rate MUST flag."""
    locale = load_locale("ua")
    ctx = EvaluationContext(locale=locale, source_id="ua_prozorro")

    buyer = "Test Buyer High Denom i005"
    for i in range(5):
        Tender.objects.create(
            source_id="ua_prozorro",
            tender_id=f"i005-high-{i}",
            buyer_name=buyer,
            title=f"High denom i005 tender {i}",
            procurement_method="limited",
            procurement_method_details="negotiation, direct",
            source_url="https://example.com",
        )

    indicator = DirectAwardShare()
    flags = list(indicator.evaluate(ctx))
    flag_subjects = [f.subject_ref for f in flags]
    assert buyer in flag_subjects, (
        f"i005 failed to flag a buyer with 5 tenders at 100% direct. Flags: {flag_subjects}"
    )


@pytest.mark.django_db
def test_i001_uk_excludes_framework_title() -> None:
    """A tender titled '...Framework Agreement...' with competitive-looking method must NOT flag."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    t = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id="framework-title-1",
        buyer_name="Test Buyer Framework Title",
        title="ICT Hardware & Peripherals Equipment Framework Agreement",
        procurement_method="open",
        procurement_method_details="Open procedure",
    )
    Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id="framework-title-1",
        award_id="fw-title-award-1",
        tender_ref=t,
        supplier_name="Framework Supplier",
        value_amount_cents=100000,
    )

    indicator = SingleBidder()
    flags = list(indicator.evaluate(ctx))
    flag_subjects = [f.subject_ref for f in flags]
    assert "framework-title-1" not in flag_subjects, (
        f"i001 flagged a tender with 'Framework Agreement' in the title — "
        f"frameworks are single-supplier by design. Flags: {flag_subjects}"
    )


@pytest.mark.django_db
def test_i004_above_estimate_flags() -> None:
    """An award >25% above the tender estimate MUST flag."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    t = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id="above-est-1",
        buyer_name="Test Buyer Above Est",
        title="Above-estimate tender",
        procurement_method="open",
        procurement_method_details="Open procedure",
        currency="GBP",
        value_amount_cents=100_00,  # 100.00 GBP
        source_url="https://example.com",
    )
    Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id="above-est-1",
        award_id="above-award-1",
        tender_ref=t,
        supplier_name="Expensive Supplier",
        currency="GBP",
        value_amount_cents=200_00,  # 200.00 GBP — 100% above estimate
    )

    indicator = PriceVsEstimate()
    flags = list(indicator.evaluate(ctx))
    assert len(flags) == 1, f"Expected 1 flag for above-estimate, got {len(flags)}"
    assert "above" in flags[0].explanation


@pytest.mark.django_db
def test_i004_below_estimate_excluded_from_curation() -> None:
    """Below-estimate flags must carry the [WEAK: below-estimate] marker and be
    excluded by the curate() function."""
    from scripts.curate_flags import curate

    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    t = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id="below-est-1",
        buyer_name="Test Buyer Below Est",
        title="Below-estimate tender",
        procurement_method="open",
        procurement_method_details="Open procedure",
        currency="GBP",
        value_amount_cents=200_00,
        source_url="https://example.com",
    )
    Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id="below-est-1",
        award_id="below-award-1",
        tender_ref=t,
        supplier_name="Cheap Supplier",
        currency="GBP",
        value_amount_cents=50_00,  # 75% below estimate
    )

    indicator = PriceVsEstimate()
    flags = list(indicator.evaluate(ctx))
    assert len(flags) == 1
    assert "[WEAK: below-estimate]" in flags[0].explanation

    # The curation script must exclude below-estimate flags
    raw_flags = [
        {
            "indicator_id": "i004_price_vs_estimate",
            "subject_ref": "below-est-1:below-award-1",
            "explanation": flags[0].explanation,
            "evidence": [{"jurisdiction": "GB", "source_url": "https://example.com"}],
            "tender_value_cents": 200_00,
            "tender_currency": "GBP",
            "tender_title": "Below-estimate tender",
            "buyer_name": "Test Buyer",
            "procurement_method": "open",
            "stamp": {"data_snapshot": "2026-07-23", "code_version": "0.0.1"},
        }
    ]
    curated = curate(raw_flags, top_n=10)
    assert len(curated) == 0, (
        f"Below-estimate flag should be excluded from curation, got {len(curated)}"
    )


@pytest.mark.django_db
def test_i004_excludes_framework_tenders() -> None:
    """Framework/DPS tenders must NOT produce price-deviation flags — ceiling, not estimate."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id="uk_contracts_finder")

    t = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id="fw-price-1",
        buyer_name="Test Buyer FW Price",
        title="Framework Agreement for Services",
        procurement_method="open",
        procurement_method_details="Open procedure",
        currency="GBP",
        value_amount_cents=100_00,
        source_url="https://example.com",
    )
    Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id="fw-price-1",
        award_id="fw-price-award-1",
        tender_ref=t,
        supplier_name="Framework Supplier",
        currency="GBP",
        value_amount_cents=500_00,  # 400% above — would normally flag
    )

    indicator = PriceVsEstimate()
    flags = list(indicator.evaluate(ctx))
    assert len(flags) == 0, (
        f"i004 flagged a framework tender — price deviation is meaningless there. "
        f"Flags: {[f.subject_ref for f in flags]}"
    )


@pytest.mark.django_db
def test_source_id_scoping_no_cross_source_flags() -> None:
    """Evaluating with one source_id must yield ZERO flags from another source.

    This is the headline source-scoping fix — ingest two sources, evaluate one,
    assert no cross-contamination.
    """
    # Source A: Ukraine with a single-bidder tender
    locale_ua = load_locale("ua")
    t_ua = Tender.objects.create(
        source_id="ua_prozorro",
        tender_id="ua-scoping-1",
        buyer_name="UA Buyer",
        title="UA single bid tender",
        source_url="https://example.com/ua",
    )
    Award.objects.create(
        source_id="ua_prozorro",
        tender_id="ua-scoping-1",
        award_id="ua-award-1",
        tender_ref=t_ua,
        supplier_name="UA Supplier",
        value_amount_cents=100000,
    )
    Bid.objects.create(
        source_id="ua_prozorro",
        tender_id="ua-scoping-1",
        bid_id="ua-bid-1",
        tender_ref=t_ua,
        bidder_name="UA Bidder",
    )

    # Source B: UK with a single-bidder tender (same pattern)
    locale_gb = load_locale("gb")
    t_uk = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id="uk-scoping-1",
        buyer_name="UK Buyer",
        title="UK competitive single",
        procurement_method="open",
        procurement_method_details="Open procedure",
        source_url="https://example.com/uk",
    )
    Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id="uk-scoping-1",
        award_id="uk-award-1",
        tender_ref=t_uk,
        supplier_name="UK Supplier",
        value_amount_cents=100000,
    )

    # Evaluate UK source only — must NOT see UA tender
    ctx_uk = EvaluationContext(locale=locale_gb, source_id="uk_contracts_finder")
    indicator = SingleBidder()
    flags = list(indicator.evaluate(ctx_uk))
    flag_subjects = [f.subject_ref for f in flags]
    assert "ua-scoping-1" not in flag_subjects, (
        f"source_id scoping failed — UA tender appeared in UK evaluation. Flags: {flag_subjects}"
    )

    # Evaluate UA source only — must NOT see UK tender
    ctx_ua = EvaluationContext(locale=locale_ua, source_id="ua_prozorro")
    flags_ua = list(indicator.evaluate(ctx_ua))
    flag_subjects_ua = [f.subject_ref for f in flags_ua]
    assert "uk-scoping-1" not in flag_subjects_ua, (
        f"source_id scoping failed — UK tender appeared in UA evaluation. Flags: {flag_subjects_ua}"
    )
