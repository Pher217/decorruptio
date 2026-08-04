"""Tests for scripts/indicator_kill_test.py — the i002/i003/i004 kill-test.

No live database: all model instances are unsaved (attribute access only, never
.save()/.objects), so these tests run entirely against the pure Python logic that
decides stratification, field-coverage/unscoreable status, join disambiguation, and
within-stratum ranking. The one thing this suite exists to pin down: an indicator
that COULD NOT be scored must never be reported the same as one that WAS scored and
did not fire.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from scripts.indicator_kill_test import (
    FLAGGED,
    FLAGGED_WEAK_BELOW_ESTIMATE,
    NOT_FLAGGED,
    IndicatorRun,
    JoinCandidate,
    RankEntry,
    match_award,
    per_stratum_firing_rates,
    ppe_medpro_rank,
    procedural_stratum,
    rank_within_stratum,
    score_i002_coverage,
    score_i003_coverage,
    score_i004_coverage,
    score_record,
    stratum_key,
    temporal_stratum,
)

from uncorrupt.staging.models import Award, Tender


class TestTemporalStratum:
    def test_first_day_of_emergency_window_is_emergency(self):
        """GIVEN 2020-03-01 (the emergency window's first day) WHEN classified THEN
        it is emergency_mar_jun_2020, not other_2020."""
        assert temporal_stratum(date(2020, 3, 1)) == "emergency_mar_jun_2020"

    def test_last_day_of_emergency_window_is_emergency(self):
        """GIVEN 2020-06-30 (the emergency window's last day) WHEN classified THEN
        it is still emergency_mar_jun_2020 (inclusive upper bound)."""
        assert temporal_stratum(date(2020, 6, 30)) == "emergency_mar_jun_2020"

    def test_day_before_window_is_other(self):
        """GIVEN 2020-02-29 (one day before the window) WHEN classified THEN it is
        other_2020."""
        assert temporal_stratum(date(2020, 2, 29)) == "other_2020"

    def test_day_after_window_is_other(self):
        """GIVEN 2020-07-01 (one day after the window) WHEN classified THEN it is
        other_2020."""
        assert temporal_stratum(date(2020, 7, 1)) == "other_2020"


class TestProceduralStratum:
    def test_limited_method_is_direct_award(self):
        """GIVEN procurement_method == 'limited' WHEN classified THEN direct_award."""
        assert procedural_stratum("limited", "") == "direct_award"

    def test_without_prior_publication_details_is_direct_award(self):
        """GIVEN method_details containing 'without prior publication' WHEN classified
        THEN direct_award, regardless of the method field."""
        assert (
            procedural_stratum("negotiated", "Negotiated procedure without prior publication")
            == "direct_award"
        )

    def test_direct_keyword_in_details_is_direct_award(self):
        """GIVEN method_details containing 'direct' WHEN classified THEN direct_award."""
        assert procedural_stratum("", "Direct award under regulation X") == "direct_award"

    def test_empty_method_is_unknown(self):
        """GIVEN no procurement_method and no matching details WHEN classified THEN
        unknown_method — never silently competitive."""
        assert procedural_stratum(None, None) == "unknown_method"

    def test_open_method_is_competitive(self):
        """GIVEN procurement_method == 'open' WHEN classified THEN competitive."""
        assert procedural_stratum("open", "Open procedure") == "competitive"


class TestStratumKey:
    def test_combines_procedural_and_temporal(self):
        """GIVEN a direct-award tender awarded inside the emergency window WHEN keyed
        THEN the stratum is the joined procedural__temporal label."""
        key = stratum_key(date(2020, 4, 1), "limited", "")
        assert key == "direct_award__emergency_mar_jun_2020"


class TestI002Coverage:
    def test_both_dates_present_is_scoreable(self):
        """GIVEN tender_start and tender_end both set WHEN coverage is checked THEN
        scoreable."""
        assert score_i002_coverage(date(2020, 1, 1), date(2020, 1, 20)) == "scoreable"

    def test_missing_start_is_unscoreable(self):
        """GIVEN tender_start is None WHEN coverage is checked THEN
        unscoreable_missing_window — not 'not_flagged'."""
        assert score_i002_coverage(None, date(2020, 1, 20)) == "unscoreable_missing_window"

    def test_missing_end_is_unscoreable(self):
        """GIVEN tender_end is None WHEN coverage is checked THEN
        unscoreable_missing_window."""
        assert score_i002_coverage(date(2020, 1, 1), None) == "unscoreable_missing_window"

    def test_both_missing_is_unscoreable(self):
        """GIVEN neither date is set (the actual GB source: 0/109,511 tenders publish
        tenderPeriod.startDate) WHEN coverage is checked THEN unscoreable_missing_window."""
        assert score_i002_coverage(None, None) == "unscoreable_missing_window"


class TestI004Coverage:
    def test_positive_values_not_framework_is_scoreable(self):
        """GIVEN a positive tender estimate and award value, not a framework/DPS
        tender, WHEN coverage is checked THEN scoreable."""
        assert score_i004_coverage(100_00, 120_00, is_framework=False) == "scoreable"

    def test_framework_excludes_regardless_of_values(self):
        """GIVEN a framework/DPS tender WHEN coverage is checked THEN
        excluded_framework_dps even if both values are positive — the "estimate" there
        is a ceiling, not a market-price estimate."""
        assert score_i004_coverage(100_00, 120_00, is_framework=True) == "excluded_framework_dps"

    def test_zero_tender_value_is_unscoreable(self):
        """GIVEN tender_value_cents == 0 (no ex-ante estimate published) WHEN coverage
        is checked THEN unscoreable_missing_estimate — not 'not_flagged'."""
        assert score_i004_coverage(0, 120_00, is_framework=False) == "unscoreable_missing_estimate"

    def test_zero_award_value_is_unscoreable(self):
        """GIVEN award_value_cents == 0 WHEN coverage is checked THEN
        unscoreable_missing_estimate."""
        assert score_i004_coverage(100_00, 0, is_framework=False) == "unscoreable_missing_estimate"


class TestI003Coverage:
    def test_real_buyer_real_supplier_above_floor_is_scoreable(self):
        """GIVEN a named buyer, a real supplier, and a buyer with >= 4 total awards
        WHEN coverage is checked THEN scoreable."""
        assert score_i003_coverage("DHSC", "Acme Ltd", buyer_total_awards=4) == "scoreable"

    def test_missing_buyer_is_unscoreable(self):
        """GIVEN no buyer_name WHEN coverage is checked THEN unscoreable_no_buyer."""
        assert (
            score_i003_coverage(None, "Acme Ltd", buyer_total_awards=10) == "unscoreable_no_buyer"
        )

    def test_placeholder_supplier_is_unscoreable(self):
        """GIVEN a placeholder supplier name ('Various') WHEN coverage is checked THEN
        unscoreable_placeholder_supplier — grouping on it would be meaningless."""
        assert (
            score_i003_coverage("DHSC", "Various", buyer_total_awards=10)
            == "unscoreable_placeholder_supplier"
        )

    def test_buyer_below_floor_is_unscoreable(self):
        """GIVEN a buyer with only 3 total awards (below RepeatWinnerShare's own
        MIN_BUYER_AWARDS=4 floor) WHEN coverage is checked THEN
        unscoreable_buyer_below_floor — the pair could never produce a flag."""
        assert (
            score_i003_coverage("DHSC", "Acme Ltd", buyer_total_awards=3)
            == "unscoreable_buyer_below_floor"
        )


class TestMatchAward:
    def test_single_candidate_returned_unconditionally(self):
        """GIVEN exactly one candidate award on the tender WHEN matching THEN it is
        returned even if the name doesn't match (single-supplier tenders are
        unambiguous)."""
        cand = JoinCandidate(
            award_id="a1", supplier_name="Different Name Ltd", value_amount_cents=500
        )
        assert match_award([cand], raw_supplier_name="Acme Ltd", award_value_gbp=999.0) == cand

    def test_multiple_candidates_matched_by_exact_name(self):
        """GIVEN several candidates WHEN one has an exact supplier-name match THEN
        that one is returned, not the first in the list."""
        a = JoinCandidate(award_id="a1", supplier_name="Other Ltd", value_amount_cents=100)
        b = JoinCandidate(award_id="a2", supplier_name="Acme Ltd", value_amount_cents=200)
        assert match_award([a, b], raw_supplier_name="Acme Ltd", award_value_gbp=2.0) == b

    def test_falls_back_to_value_when_no_name_matches(self):
        """GIVEN several candidates and none matches by name WHEN one matches the
        award value (converted to cents) THEN that one is returned."""
        a = JoinCandidate(award_id="a1", supplier_name="X Ltd", value_amount_cents=10_000)
        b = JoinCandidate(award_id="a2", supplier_name="Y Ltd", value_amount_cents=25_000)
        assert match_award([a, b], raw_supplier_name="Not Present Ltd", award_value_gbp=250.0) == b

    def test_no_match_returns_none(self):
        """GIVEN several candidates matching neither name nor value WHEN matching
        THEN None — an ambiguous join must not guess."""
        a = JoinCandidate(award_id="a1", supplier_name="X Ltd", value_amount_cents=10_000)
        b = JoinCandidate(award_id="a2", supplier_name="Y Ltd", value_amount_cents=25_000)
        assert match_award([a, b], raw_supplier_name="Z Ltd", award_value_gbp=999.0) is None

    def test_empty_candidates_returns_none(self):
        """GIVEN no candidate awards at all WHEN matching THEN None."""
        assert match_award([], raw_supplier_name="Acme Ltd", award_value_gbp=100.0) is None


class TestRankWithinStratum:
    def test_more_flags_ranks_higher(self):
        """GIVEN two suppliers, one with 2 flagged indicators and one with 0 WHEN
        ranked THEN the 2-flag supplier is rank 1 and the 0-flag supplier is rank 2."""
        entries = [
            RankEntry("Quiet Co", frozenset(), frozenset({"i003", "i004"})),
            RankEntry("Loud Co", frozenset({"i003", "i004"}), frozenset({"i003", "i004"})),
        ]
        ranked = rank_within_stratum(entries)
        assert ranked[0]["supplier"] == "Loud Co"
        assert ranked[0]["rank"] == 1
        assert ranked[1]["supplier"] == "Quiet Co"
        assert ranked[1]["rank"] == 2

    def test_ties_share_a_competition_rank(self):
        """GIVEN three suppliers all with 0 flagged indicators WHEN ranked THEN all
        three share rank 1 (competition ranking — 'above' must mean strictly more
        flags, so a tie cannot separate anyone)."""
        entries = [
            RankEntry("A Co", frozenset(), frozenset({"i004"})),
            RankEntry("B Co", frozenset(), frozenset({"i004"})),
            RankEntry("C Co", frozenset(), frozenset({"i004"})),
        ]
        ranked = rank_within_stratum(entries)
        assert [r["rank"] for r in ranked] == [1, 1, 1]

    def test_ties_broken_alphabetically_for_reproducibility(self):
        """GIVEN a tie WHEN ranked THEN the tied group is ordered alphabetically by
        supplier name, so re-running produces an identical order."""
        entries = [
            RankEntry("Zebra Ltd", frozenset(), frozenset()),
            RankEntry("Alpha Ltd", frozenset(), frozenset()),
        ]
        ranked = rank_within_stratum(entries)
        assert [r["supplier"] for r in ranked] == ["Alpha Ltd", "Zebra Ltd"]

    def test_rank_after_a_tied_group_skips_ahead(self):
        """GIVEN two tied suppliers at 1 flag and one supplier at 0 flags WHEN ranked
        THEN the 0-flag supplier is rank 3, not rank 2 (1,1,3 — competition ranking)."""
        entries = [
            RankEntry("Tied A", frozenset({"i004"}), frozenset({"i004"})),
            RankEntry("Tied B", frozenset({"i004"}), frozenset({"i004"})),
            RankEntry("Last", frozenset(), frozenset({"i004"})),
        ]
        ranked = rank_within_stratum(entries)
        assert [r["rank"] for r in ranked] == [1, 1, 3]


def _unsaved_tender(**overrides) -> Tender:
    defaults = dict(
        source_id="uk_contracts_finder",
        tender_id="t1",
        ocid="ocds-test-1",
        title="Test tender",
        procurement_method="limited",
        procurement_method_details="Negotiated procedure without prior publication",
        tender_start=None,
        tender_end=date(2020, 4, 1),
        value_amount_cents=100_000_00,
        buyer_name="Test Buyer",
        raw_json={},
    )
    defaults.update(overrides)
    return Tender(**defaults)


def _unsaved_award(tender: Tender, **overrides) -> Award:
    defaults = dict(
        source_id="uk_contracts_finder",
        tender_id=tender.tender_id,
        award_id="a1",
        supplier_name="Test Supplier Ltd",
        value_amount_cents=100_000_00,
        award_date=datetime(2020, 4, 5, tzinfo=UTC),
        raw_json={},
    )
    defaults.update(overrides)
    award = Award(**defaults)
    award.tender_ref = tender
    return award


def _empty_run() -> IndicatorRun:
    return IndicatorRun(
        units_evaluated={"i002": 0, "i003": 0, "i004": 0},
        flags={"i002": set(), "i003": set(), "i004": set()},
        i004_direction={},
        buyer_totals={},
    )


class TestScoreRecordUnscoreableVsNoFlag:
    def test_unjoined_record_is_unscoreable_not_no_flag(self):
        """GIVEN a cohort record whose OCDS release was never ingested into staging
        (join_status='tender_missing') WHEN scored THEN every indicator status is
        unscoreable_not_joined, and stratum is None — never 'not_flagged', which would
        misreport absence-of-data as a tested negative."""
        record = {
            "cohort": "vip_lane",
            "supplier_name": "Ghost Supplier Ltd",
            "ocid": "ocds-missing",
            "award_value_gbp": 1000.0,
            "join_status": "tender_missing",
            "award": None,
            "tender": None,
        }
        scored = score_record(record, _empty_run())
        assert scored["stratum"] is None
        assert scored["i002_status"] == "unscoreable_not_joined"
        assert scored["i003_status"] == "unscoreable_not_joined"
        assert scored["i004_status"] == "unscoreable_not_joined"

    def test_joined_award_missing_bid_window_is_unscoreable_not_no_flag(self):
        """GIVEN a joined award whose tender has no tender_start (the systematic GB
        gap: 0/109,511 tenders publish it) WHEN scored THEN i002_status is
        unscoreable_missing_window — never 'not_flagged'."""
        tender = _unsaved_tender(tender_start=None, tender_end=date(2020, 4, 1))
        award = _unsaved_award(tender)
        record = {
            "cohort": "vip_lane",
            "supplier_name": "Test Supplier Ltd",
            "ocid": "ocds-test-1",
            "award_value_gbp": 1000.0,
            "join_status": "matched",
            "award": award,
            "tender": tender,
        }
        scored = score_record(record, _empty_run())
        assert scored["i002_status"] == "unscoreable_missing_window"

    def test_joined_award_with_window_but_not_in_flag_set_is_not_flagged(self):
        """GIVEN a joined tender with both window dates present, and its tender_id is
        NOT in i002's flagged set WHEN scored THEN i002_status is 'not_flagged' — a
        real tested negative, distinct from unscoreable."""
        tender = _unsaved_tender(tender_start=date(2020, 3, 1), tender_end=date(2020, 4, 1))
        award = _unsaved_award(tender)
        record = {
            "cohort": "vip_lane",
            "supplier_name": "Test Supplier Ltd",
            "ocid": "ocds-test-1",
            "award_value_gbp": 1000.0,
            "join_status": "matched",
            "award": award,
            "tender": tender,
        }
        scored = score_record(record, _empty_run())
        assert scored["i002_status"] == NOT_FLAGGED

    def test_joined_award_with_window_in_flag_set_is_flagged(self):
        """GIVEN a joined tender with both window dates present, and its tender_id IS
        in i002's flagged set WHEN scored THEN i002_status is 'flagged'."""
        tender = _unsaved_tender(
            tender_id="short-window-tender",
            tender_start=date(2020, 3, 1),
            tender_end=date(2020, 3, 3),
        )
        award = _unsaved_award(tender)
        run = IndicatorRun(
            units_evaluated={"i002": 1, "i003": 0, "i004": 0},
            flags={"i002": {"short-window-tender"}, "i003": set(), "i004": set()},
            i004_direction={},
            buyer_totals={},
        )
        record = {
            "cohort": "vip_lane",
            "supplier_name": "Test Supplier Ltd",
            "ocid": "ocds-test-1",
            "award_value_gbp": 1000.0,
            "join_status": "matched",
            "award": award,
            "tender": tender,
        }
        scored = score_record(record, run)
        assert scored["i002_status"] == FLAGGED

    def test_i004_zero_tender_estimate_is_unscoreable_not_no_flag(self):
        """GIVEN a joined award whose tender has no published estimate
        (value_amount_cents == 0) WHEN scored THEN i004_status is
        unscoreable_missing_estimate — never 'not_flagged'."""
        tender = _unsaved_tender(value_amount_cents=0)
        award = _unsaved_award(tender)
        record = {
            "cohort": "control",
            "supplier_name": "Test Supplier Ltd",
            "ocid": "ocds-test-1",
            "award_value_gbp": 1000.0,
            "join_status": "matched",
            "award": award,
            "tender": tender,
        }
        scored = score_record(record, _empty_run())
        assert scored["i004_status"] == "unscoreable_missing_estimate"

    def test_i004_below_estimate_flag_is_reported_as_weak_not_flagged(self):
        """GIVEN a joined award whose subject_ref is in i004's flagged set with
        direction 'below' WHEN scored THEN i004_status is
        flagged_weak_below_estimate, distinct from a real (above-estimate) flag."""
        tender = _unsaved_tender(tender_id="t-weak", value_amount_cents=200_000_00)
        award = _unsaved_award(tender, award_id="a-weak", value_amount_cents=100_000_00)
        run = IndicatorRun(
            units_evaluated={"i002": 0, "i003": 0, "i004": 1},
            flags={"i002": set(), "i003": set(), "i004": {"t-weak:a-weak"}},
            i004_direction={"t-weak:a-weak": "below"},
            buyer_totals={},
        )
        record = {
            "cohort": "control",
            "supplier_name": "Test Supplier Ltd",
            "ocid": "ocds-test-1",
            "award_value_gbp": 1000.0,
            "join_status": "matched",
            "award": award,
            "tender": tender,
        }
        scored = score_record(record, run)
        assert scored["i004_status"] == FLAGGED_WEAK_BELOW_ESTIMATE

    def test_i003_buyer_below_floor_is_unscoreable_not_no_flag(self):
        """GIVEN a joined award whose buyer has fewer than 4 total awards system-wide
        WHEN scored THEN i003_status is unscoreable_buyer_below_floor — never
        'not_flagged'."""
        tender = _unsaved_tender(buyer_name="Tiny Buyer")
        award = _unsaved_award(tender)
        run = IndicatorRun(
            units_evaluated={"i002": 0, "i003": 0, "i004": 0},
            flags={"i002": set(), "i003": set(), "i004": set()},
            i004_direction={},
            buyer_totals={"Tiny Buyer": 2},
        )
        record = {
            "cohort": "vip_lane",
            "supplier_name": "Test Supplier Ltd",
            "ocid": "ocds-test-1",
            "award_value_gbp": 1000.0,
            "join_status": "matched",
            "award": award,
            "tender": tender,
        }
        scored = score_record(record, run)
        assert scored["i003_status"] == "unscoreable_buyer_below_floor"

    def test_stratum_assigned_from_procedural_and_temporal_fields(self):
        """GIVEN a joined direct-award tender awarded inside the emergency window
        WHEN scored THEN stratum is direct_award__emergency_mar_jun_2020."""
        tender = _unsaved_tender(
            procurement_method="limited",
            procurement_method_details="",
        )
        award = _unsaved_award(tender, award_date=datetime(2020, 5, 1, tzinfo=UTC))
        record = {
            "cohort": "vip_lane",
            "supplier_name": "Test Supplier Ltd",
            "ocid": "ocds-test-1",
            "award_value_gbp": 1000.0,
            "join_status": "matched",
            "award": award,
            "tender": tender,
        }
        scored = score_record(record, _empty_run())
        assert scored["stratum"] == "direct_award__emergency_mar_jun_2020"


class TestPerStratumFiringRates:
    def _record(self, stratum: str, i004_status: str) -> dict:
        return {
            "stratum": stratum,
            "i002_status": "unscoreable_missing_window",
            "i003_status": "unscoreable_buyer_below_floor",
            "i004_status": i004_status,
        }

    def test_flags_at_20pct_are_not_flagged_as_stratifier(self):
        """GIVEN exactly 1 flagged of 5 scoreable (20.0%) in a stratum WHEN firing
        rates are computed THEN is_stratifier is False — the threshold is a strict
        '>20-30%', not '>=20%'."""
        records = [self._record("s1", FLAGGED)] + [
            self._record("s1", NOT_FLAGGED) for _ in range(4)
        ]
        rates = per_stratum_firing_rates(records)
        assert rates["s1"]["i004"]["firing_rate"] == 0.20
        assert rates["s1"]["i004"]["is_stratifier"] is False

    def test_flags_above_20pct_are_flagged_as_stratifier(self):
        """GIVEN 2 flagged of 5 scoreable (40%) in a stratum WHEN firing rates are
        computed THEN is_stratifier is True."""
        records = [self._record("s1", FLAGGED) for _ in range(2)] + [
            self._record("s1", NOT_FLAGGED) for _ in range(3)
        ]
        rates = per_stratum_firing_rates(records)
        assert rates["s1"]["i004"]["is_stratifier"] is True

    def test_weak_below_estimate_flags_count_as_scoreable_not_flagged(self):
        """GIVEN all scoreable i004 hits are weak (below-estimate) WHEN firing rates
        are computed THEN firing_rate is 0.0, not treating weak hits as real flags —
        below-estimate is excluded from the curated signal by the indicator's own
        design."""
        records = [self._record("s1", FLAGGED_WEAK_BELOW_ESTIMATE) for _ in range(3)]
        rates = per_stratum_firing_rates(records)
        assert rates["s1"]["i004"]["scoreable"] == 3
        assert rates["s1"]["i004"]["flagged"] == 0
        assert rates["s1"]["i004"]["firing_rate"] == 0.0

    def test_zero_scoreable_reports_none_rate_not_zero(self):
        """GIVEN a stratum where every i002 row is unscoreable (the systematic GB bid
        -window gap) WHEN firing rates are computed THEN firing_rate is None, not 0.0
        — an untested population must not read as a clean negative."""
        records = [self._record("s1", NOT_FLAGGED)]
        rates = per_stratum_firing_rates(records)
        assert rates["s1"]["i002"]["scoreable"] == 0
        assert rates["s1"]["i002"]["firing_rate"] is None
        assert rates["s1"]["i002"]["is_stratifier"] is False


class TestPpeMedproRank:
    def test_no_joined_medpro_awards_reports_not_joinable(self):
        """GIVEN no PPE Medpro Ltd record has a stratum (none of its awards joined to
        staging) WHEN ranked THEN joinable is False and no rank is fabricated."""
        vip_scored = [
            {
                "supplier_name": "PPE Medpro Ltd",
                "stratum": None,
                "i002_status": "unscoreable_not_joined",
                "i003_status": "unscoreable_not_joined",
                "i004_status": "unscoreable_not_joined",
            },
            {
                "supplier_name": "Other Ltd",
                "stratum": "direct_award__emergency_mar_jun_2020",
                "i002_status": "unscoreable_missing_window",
                "i003_status": NOT_FLAGGED,
                "i004_status": NOT_FLAGGED,
            },
        ]
        result = ppe_medpro_rank(vip_scored)
        assert result["joinable"] is False

    def test_benign_peer_outranking_medpro_fails_the_acceptance_test(self):
        """GIVEN PPE Medpro fires no indicator and a benign peer in the same stratum
        fires one WHEN ranked THEN Medpro's rank is worse than 1 and
        beats_all_peers is False — the exact acceptance-test failure mode."""
        stratum = "direct_award__emergency_mar_jun_2020"
        vip_scored = [
            {
                "supplier_name": "PPE Medpro Ltd",
                "stratum": stratum,
                "i002_status": "unscoreable_missing_window",
                "i003_status": NOT_FLAGGED,
                "i004_status": NOT_FLAGGED,
            },
            {
                "supplier_name": "Benign Peer Ltd",
                "stratum": stratum,
                "i002_status": "unscoreable_missing_window",
                "i003_status": NOT_FLAGGED,
                "i004_status": FLAGGED,
            },
        ]
        result = ppe_medpro_rank(vip_scored)
        assert result["joinable"] is True
        info = result["per_stratum"][stratum]
        assert info["medpro_rank"] == 2
        assert info["beats_all_peers"] is False

    def test_medpro_alone_with_a_flag_beats_all_peers(self):
        """GIVEN PPE Medpro is the only supplier with a flag in its stratum WHEN
        ranked THEN rank is 1 and beats_all_peers is True."""
        stratum = "direct_award__emergency_mar_jun_2020"
        vip_scored = [
            {
                "supplier_name": "PPE Medpro Ltd",
                "stratum": stratum,
                "i002_status": "unscoreable_missing_window",
                "i003_status": FLAGGED,
                "i004_status": NOT_FLAGGED,
            },
            {
                "supplier_name": "Benign Peer Ltd",
                "stratum": stratum,
                "i002_status": "unscoreable_missing_window",
                "i003_status": NOT_FLAGGED,
                "i004_status": NOT_FLAGGED,
            },
        ]
        result = ppe_medpro_rank(vip_scored)
        info = result["per_stratum"][stratum]
        assert info["medpro_rank"] == 1
        assert info["beats_all_peers"] is True
