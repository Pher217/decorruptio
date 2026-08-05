"""Tests for scripts/measure_s9b_seed_footprint.py.

No network: these exercise `classify`, the pure function that decides whether a
disqualification's named company is still live, was renamed (the phoenix path), or is
dead. The renamed-company case is the hard positive control from
`scripts/disqualified_director_cross_register.py`: CANTILLON LIMITED (00916538) became
MORRISROE DEMOLITION LIMITED six weeks after the CMA decision that triggered the ban.
Current-name-only matching returns nothing on it — that is exactly why it is the
control this suite (and the script's own `control_check`, run live on every invocation)
must recover.
"""

from __future__ import annotations

from scripts.measure_s9b_seed_footprint import CompanyOutcome, classify, parse_named_companies


def _profile(**overrides):
    base = {
        "company_number": "00916538",
        "company_name": "CANTILLON LIMITED",
        "company_status": "active",
        "previous_company_names": [],
        "registered_office_address": {
            "address_line_1": "10 Elton Way",
            "locality": "Watford",
            "postal_code": "WD25 8HH",
        },
    }
    return {**base, **overrides}


class TestClassify:
    def test_unresolved_company_is_dissolved_or_unresolved(self):
        """GIVEN a named company that could not be resolved to any live profile
        WHEN it is classified
        THEN the outcome is dissolved_or_unresolved, not silently dropped."""
        outcome = classify("SOME DEFUNCT LTD", None)
        assert outcome.outcome == "dissolved_or_unresolved"
        assert outcome.resolved_number is None

    def test_dissolved_profile_is_dissolved_or_unresolved(self):
        """GIVEN a resolved profile whose company_status is dissolved
        WHEN it is classified
        THEN the outcome is dissolved_or_unresolved even though a profile was found —
        being findable is not the same as being alive."""
        outcome = classify("CANTILLON LIMITED", _profile(company_status="dissolved"))
        assert outcome.outcome == "dissolved_or_unresolved"

    def test_liquidation_profile_is_dissolved_or_unresolved(self):
        """GIVEN a resolved profile in liquidation
        WHEN it is classified
        THEN the outcome is dissolved_or_unresolved, for the same reason as dissolution."""
        outcome = classify("CANTILLON LIMITED", _profile(company_status="liquidation"))
        assert outcome.outcome == "dissolved_or_unresolved"

    def test_active_company_under_the_same_name_is_active(self):
        """GIVEN a company still trading under the exact name the disqualification named
        WHEN it is classified
        THEN the outcome is 'active' — no rename was needed to survive."""
        outcome = classify("CANTILLON LIMITED", _profile())
        assert outcome.outcome == "active"
        assert outcome.resolved_number == "00916538"

    def test_active_company_under_the_same_name_ignoring_case_and_punctuation_is_active(self):
        """GIVEN the disqualification's name differs only in case/punctuation from the
        profile's current name
        WHEN it is classified
        THEN it is still 'active', not misread as a rename."""
        outcome = classify("Cantillon Ltd.", _profile(company_name="CANTILLON LTD"))
        assert outcome.outcome == "active"

    def test_renamed_company_is_a_successor_hit(self):
        """GIVEN CANTILLON LIMITED (00916538) was renamed MORRISROE DEMOLITION LIMITED
        six weeks after the CMA decision that triggered the ban
        WHEN the disqualification's named company is classified against the current
        (renamed) profile
        THEN the outcome is 'successor', not 'dissolved_or_unresolved' — this is the
        hard positive control the whole probe depends on recovering."""
        outcome = classify(
            "CANTILLON LIMITED",
            _profile(company_name="MORRISROE DEMOLITION LIMITED"),
        )
        assert outcome.outcome == "successor"
        assert outcome.resolved_number == "00916538"
        assert outcome.resolved_current_name == "MORRISROE DEMOLITION LIMITED"

    def test_renamed_but_dissolved_company_is_dissolved_not_successor(self):
        """GIVEN a company that was renamed AND has since been dissolved
        WHEN it is classified
        THEN it counts as dissolved_or_unresolved — a rename alone does not make a
        dead company count as a successor."""
        outcome = classify(
            "CANTILLON LIMITED",
            _profile(company_name="MORRISROE DEMOLITION LIMITED", company_status="dissolved"),
        )
        assert outcome.outcome == "dissolved_or_unresolved"

    def test_registered_office_address_is_carried_through_for_live_outcomes(self):
        """GIVEN a live (active or successor) outcome
        WHEN it is classified
        THEN the registered office address is captured, so formation-agent clustering
        can be computed downstream."""
        outcome = classify("CANTILLON LIMITED", _profile())
        assert outcome.registered_office_address == "10 Elton Way, Watford, WD25 8HH"

    def test_registered_office_address_is_none_for_dead_outcomes(self):
        """GIVEN a dissolved_or_unresolved outcome
        WHEN it is classified
        THEN no address is reported — a dead company cannot feed the formation-agent
        address-clustering check."""
        outcome = classify("CANTILLON LIMITED", _profile(company_status="dissolved"))
        assert outcome.registered_office_address is None


class TestParseNamedCompanies:
    def test_single_plain_name_with_no_number_passes_through(self):
        """GIVEN a company_names entry that is just a company name
        WHEN it is parsed
        THEN it is returned unchanged with no company number."""
        assert parse_named_companies(["CANTILLON LIMITED"]) == [("CANTILLON LIMITED", None)]

    def test_two_companies_packed_into_one_entry_are_split_by_embedded_number(self):
        """GIVEN the live-observed Companies House quirk where one company_names entry
        packs TWO companies together, each with its own number in parentheses
        (case_identifier 50697, s.9B) — "BROWN AND MASON GROUP LIMITED (01892133)
        BROWN AND MASON LIMITED (00686405)" as a SINGLE array element
        WHEN it is parsed
        THEN it splits into two (name, number) pairs, each independently resolvable —
        treating the whole string as one search query resolves nothing, and silently
        turned a live company into a false 'dissolved_or_unresolved' on the first run."""
        pairs = parse_named_companies(
            ["BROWN AND MASON GROUP LIMITED (01892133)  BROWN AND MASON LIMITED (00686405)"]
        )
        assert pairs == [
            ("BROWN AND MASON GROUP LIMITED", "01892133"),
            ("BROWN AND MASON LIMITED", "00686405"),
        ]

    def test_multiple_separate_entries_are_each_kept(self):
        """GIVEN two distinct company_names list entries, neither carrying a number
        WHEN they are parsed
        THEN both are preserved as separate unresolved-number pairs."""
        pairs = parse_named_companies(["CANTILLON LIMITED", "CANTILLON HOLDINGS LIMITED"])
        assert pairs == [("CANTILLON LIMITED", None), ("CANTILLON HOLDINGS LIMITED", None)]


class TestCompanyOutcomeShape:
    def test_outcome_is_a_plain_dataclass_with_expected_fields(self):
        """GIVEN a classified outcome
        WHEN its fields are inspected
        THEN they match the shape the script's aggregation and JSON output rely on."""
        outcome = classify("CANTILLON LIMITED", _profile())
        assert isinstance(outcome, CompanyOutcome)
        assert outcome.named_company == "CANTILLON LIMITED"
