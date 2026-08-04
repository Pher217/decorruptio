"""Tests for scripts/disqualified_director_cross_register.py.

No network: these exercise the pure logic that decides which register inconsistencies
are real. Two of them exist because both artifacts actually fired on the first live run
and produced wrong numbers — a 50.3% rate that was truly 1.9% (dissolved companies), and
a "Lee Brown at PPG Industries (UK) Ltd" hit (namesake collision on name + partial DOB).
"""

from __future__ import annotations

from datetime import date

from scripts.disqualified_director_cross_register import (
    ban_in_force,
    corroborated_hits,
    normalise_company_name,
)


def _appointment(**overrides):
    base = {
        "company_name": "CANTILLON LIMITED",
        "company_number": "00916538",
        "company_status": "active",
        "officer_role": "director",
        "appointed_on": "2015-02-18",
        "resigned_on": None,
    }
    return {**base, **overrides}


def _disqualification(**overrides):
    base = {
        "disqualified_from": "2023-05-01",
        "disqualified_until": "2027-10-31",
        "company_names": ["CANTILLON LIMITED", "CANTILLON HOLDINGS LIMITED"],
        "reason": {"section": "9B"},
    }
    return {**base, **overrides}


class TestNormaliseCompanyName:
    def test_punctuation_and_case_are_ignored(self):
        """GIVEN the same company written with different punctuation and case
        WHEN both are normalised
        THEN they compare equal."""
        assert normalise_company_name("Cantillon Ltd.") == normalise_company_name("CANTILLON LTD")

    def test_missing_name_normalises_to_empty(self):
        """GIVEN a null company name
        WHEN it is normalised
        THEN the result is the empty string, so it can never match a real name."""
        assert normalise_company_name(None) == ""


class TestBanInForce:
    def test_date_inside_window_is_in_force(self):
        """GIVEN a ban running 2023-05-01 to 2027-10-31
        WHEN the measurement date falls inside it
        THEN the ban is in force."""
        assert ban_in_force(_disqualification(), date(2026, 8, 4)) is True

    def test_date_after_window_is_not_in_force(self):
        """GIVEN a ban that expired in 2027
        WHEN the measurement date is 2028
        THEN the ban is not in force."""
        assert ban_in_force(_disqualification(), date(2028, 1, 1)) is False

    def test_date_before_window_is_not_in_force(self):
        """GIVEN a ban starting 2023-05-01
        WHEN the measurement date precedes it
        THEN the ban is not in force."""
        assert ban_in_force(_disqualification(), date(2023, 4, 30)) is False

    def test_boundary_start_date_is_in_force(self):
        """GIVEN a ban starting 2023-05-01
        WHEN the measurement date is exactly the start date
        THEN the ban is in force (window is inclusive)."""
        assert ban_in_force(_disqualification(), date(2023, 5, 1)) is True

    def test_missing_end_date_fails_closed(self):
        """GIVEN a disqualification with no end date
        WHEN it is evaluated
        THEN it is reported not-in-force rather than assumed open-ended (ADR-008)."""
        record = _disqualification()
        del record["disqualified_until"]
        assert ban_in_force(record, date(2026, 8, 4)) is False

    def test_unparseable_date_fails_closed(self):
        """GIVEN a disqualification whose dates are malformed
        WHEN it is evaluated
        THEN it is reported not-in-force rather than raising."""
        record = _disqualification(disqualified_from="not-a-date")
        assert ban_in_force(record, date(2026, 8, 4)) is False


class TestCorroboratedHits:
    def test_unresigned_role_at_named_active_company_is_a_hit(self):
        """GIVEN a person whose disqualification names CANTILLON LIMITED
        WHEN they hold an unresigned directorship of that still-active company
        THEN exactly one inconsistency is reported."""
        hits = corroborated_hits(_disqualification(), [_appointment()])
        assert len(hits) == 1

    def test_dissolved_company_is_excluded(self):
        """GIVEN an unresigned role at a company named in the disqualification
        WHEN that company is dissolved (nobody files TM01s for dissolved companies)
        THEN it is not reported — this artifact inflated a true 1.9% to 50.3%."""
        hits = corroborated_hits(
            _disqualification(), [_appointment(company_status="dissolved")]
        )
        assert hits == []

    def test_company_in_liquidation_is_excluded(self):
        """GIVEN an unresigned role at a company in liquidation
        WHEN hits are computed
        THEN it is not reported, for the same filing-inertia reason as dissolution."""
        hits = corroborated_hits(
            _disqualification(), [_appointment(company_status="liquidation")]
        )
        assert hits == []

    def test_resigned_officer_is_excluded(self):
        """GIVEN a director who has a TM01 on file
        WHEN hits are computed
        THEN there is no inconsistency to report."""
        hits = corroborated_hits(_disqualification(), [_appointment(resigned_on="2023-04-30")])
        assert hits == []

    def test_company_not_named_in_the_disqualification_is_excluded(self):
        """GIVEN an active unresigned directorship at a company Companies House does NOT
        link to this person's disqualification
        WHEN hits are computed
        THEN it is rejected — accepting it would be a name+partial-DOB match, the
        namesake collision ADR-004 D2 forbids."""
        hits = corroborated_hits(
            _disqualification(),
            [_appointment(company_name="PPG INDUSTRIES (UK) LIMITED", company_number="02110620")],
        )
        assert hits == []

    def test_disqualification_naming_no_companies_yields_nothing(self):
        """GIVEN a disqualification record with an empty company_names list
        WHEN hits are computed
        THEN nothing is corroborated, because CH asserts no person-company link."""
        assert corroborated_hits(_disqualification(company_names=[]), [_appointment()]) == []

    def test_renamed_company_matches_through_its_previous_name(self):
        """GIVEN CANTILLON LIMITED (00916538) was renamed MORRISROE DEMOLITION LIMITED
        six weeks after the CMA decision that triggered the ban
        WHEN the appointment carries the new name and the aliases carry the old one
        THEN the hit is still reported — a rename must not hide the case."""
        hits = corroborated_hits(
            _disqualification(),
            [
                _appointment(
                    company_name="MORRISROE DEMOLITION LIMITED",
                    company_name_aliases=[
                        "MORRISROE DEMOLITION LIMITED",
                        "CANTILLON LIMITED",
                        "CANTILLON HAULAGE LIMITED",
                    ],
                )
            ],
        )
        assert len(hits) == 1

    def test_rename_to_an_unrelated_company_is_still_rejected(self):
        """GIVEN an active company whose current and previous names are all unrelated to
        the disqualification's named companies
        WHEN hits are computed
        THEN nothing is reported — alias matching widens the join, it does not loosen it."""
        hits = corroborated_hits(
            _disqualification(),
            [
                _appointment(
                    company_name="PPG INDUSTRIES (UK) LIMITED",
                    company_name_aliases=["PPG INDUSTRIES (UK) LIMITED", "PPG HOLDINGS LIMITED"],
                )
            ],
        )
        assert hits == []
