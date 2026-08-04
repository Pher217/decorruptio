"""Tests for the Commons-stratum external control runner.

`scripts/run_commons_controls.py` closes the per-stratum control-battery gap
required by amendment v2.4 for the House of Commons: every control is a real
(member, declared organisation, registrationDate) fact verified live against
`interests-api.parliament.uk` (see
`tests/fixtures/commons_retrieval_controls.json`'s own `source` block),
never sampled from our graph.

These tests verify the discipline the packet requires:
- a present declared_interest relationship is recovered
- an absent member/organisation is classified `not-found`, never counted as
  a retrieval-logic failure
- an ambiguous (2+) organisation match IS `unresolved` (a genuine
  resolution-logic limitation, correctly not guessed)
- an unresolved placeholder node (`UK-PARLIAMENT-UNRESOLVED`) never counts
  as a resolved organisation
- selection from the fixture is deterministic across repeated loads/runs
- retrieval and temporal are reported as genuinely distinct figures
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from scripts.run_commons_controls import (
    DEFAULT_CONTROLS_PATH,
    STATUS_NOT_FOUND,
    STATUS_RECOVERED,
    STATUS_UNRESOLVED,
    TEMPORAL_MATCHED,
    TEMPORAL_MISMATCH,
    TEMPORAL_NOT_APPLICABLE,
    classify_control,
    load_controls,
    resolve_organisation_candidates,
    run_controls,
)

from uncorrupt.graph.models import Edge, Entity


def _write_controls(tmp_path: Path, controls: list[dict]) -> Path:
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"controls": controls}), encoding="utf-8")
    return path


class TestLoadControlsIsDeterministic:
    def test_load_controls_returns_the_full_fixed_battery(self):
        """GIVEN the pre-registered 12-row control fixture
        WHEN it is loaded
        THEN all 12 rows are returned, in file order."""
        controls = load_controls(DEFAULT_CONTROLS_PATH)
        assert len(controls) == 12
        assert [c["id"] for c in controls] == list(range(1, 13))

    def test_load_controls_is_deterministic_across_repeated_calls(self):
        """GIVEN the same fixture path
        WHEN loaded twice
        THEN the ordered id sequence is identical both times."""
        first = load_controls(DEFAULT_CONTROLS_PATH)
        second = load_controls(DEFAULT_CONTROLS_PATH)
        assert [c["id"] for c in first] == [c["id"] for c in second]


@pytest.mark.django_db
class TestClassifyControlRecovered:
    def test_present_interest_is_recovered_with_matching_temporal(self):
        """GIVEN a declared_interest edge whose valid_from matches the
        externally verified registration_date
        WHEN the control is classified
        THEN status is 'recovered' AND temporal_status is 'matched'."""
        member = Entity.objects.create(
            entity_type="person",
            name="Sir Keir Starmer",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="4514",
        )
        org = Entity.objects.create(
            entity_type="company",
            name="The Arsenal Football Club Limited",
            registry_scheme="GB-COH",
            registry_id="00109244",
            company_number="00109244",
        )
        Edge.objects.create(
            edge_type="declared_interest",
            source_entity=member,
            target_entity=org,
            valid_from=date(2026, 5, 8),
        )
        control = {
            "id": 1,
            "member_id": 4514,
            "member_name": "Sir Keir Starmer",
            "organisation_name": "The Arsenal Football Club Limited",
            "company_number": "00109244",
            "registration_date": "2026-05-08",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_RECOVERED
        assert row["temporal_status"] == TEMPORAL_MATCHED

    def test_recovered_relationship_with_wrong_date_is_temporal_mismatch(self):
        """GIVEN a declared_interest edge connecting the same member and
        organisation but with a DIFFERENT valid_from
        WHEN classified
        THEN status is still 'recovered' but temporal_status is
        'date_mismatch_or_missing'."""
        member = Entity.objects.create(
            entity_type="person",
            name="Sir Keir Starmer",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="4514",
        )
        org = Entity.objects.create(
            entity_type="company",
            name="The Arsenal Football Club Limited",
            registry_scheme="GB-COH",
            registry_id="00109244",
            company_number="00109244",
        )
        Edge.objects.create(
            edge_type="declared_interest",
            source_entity=member,
            target_entity=org,
            valid_from=date(2020, 1, 1),
        )
        control = {
            "id": 1,
            "member_id": 4514,
            "organisation_name": "The Arsenal Football Club Limited",
            "company_number": "00109244",
            "registration_date": "2026-05-08",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_RECOVERED
        assert row["temporal_status"] == TEMPORAL_MISMATCH

    def test_organisation_resolves_via_company_number_when_present(self):
        """GIVEN a control with a company_number and an organisation Entity
        whose name text differs slightly from the control's organisation_name
        WHEN resolved
        THEN the company_number match wins -- identifier resolution beats
        name text, mirroring how an external table's own ID should be
        trusted over free-text spelling variance."""
        Entity.objects.create(
            entity_type="company",
            name="BPI (British Recorded Music Industry) Limited",
            registry_scheme="GB-COH",
            registry_id="01132389",
            company_number="01132389",
        )

        candidates = resolve_organisation_candidates(
            "BPI (British Recorded Music) Ltd", company_number="01132389"
        )

        assert len(candidates) == 1
        assert candidates[0].company_number == "01132389"

    def test_organisation_resolves_when_punctuation_differs_within_prefix_window(self):
        """GIVEN a real company Entity whose name differs from the declared
        organisation name only by punctuation ("&" vs "and") positioned
        WITHIN the first 15 characters of the declared name, and no
        company_number to disambiguate
        WHEN resolved
        THEN it is still found -- a raw-declared-name character-count
        prefix can straddle exactly this kind of difference and silently
        miss a company the graph already holds (found via control #1 of
        `commons_retrieval_controls.json`: "Guardian news and media" vs
        the real "Guardian News & Media Limited", company number 00908396,
        verified live 2026-08-04 -- the "&" sits before character 15,
        so `organisation_name.strip()[:15]` was never a substring of the
        real Entity name and the `icontains` pre-filter returned nothing)."""
        Entity.objects.create(
            entity_type="company",
            name="Guardian News & Media Limited",
            registry_scheme="GB-COH",
            registry_id="00908396",
            company_number="00908396",
        )

        candidates = resolve_organisation_candidates("Guardian news and media", company_number=None)

        assert len(candidates) == 1
        assert candidates[0].company_number == "00908396"


@pytest.mark.django_db
class TestClassifyControlNotFound:
    def test_absent_member_is_not_found_not_unresolved(self):
        """GIVEN an organisation that exists but no UK-PARLIAMENT-MEMBER
        entity for the member at all
        WHEN classified
        THEN status is 'not-found', never 'unresolved' -- absence is a
        data-coverage gap, not a retrieval-logic failure."""
        Entity.objects.create(
            entity_type="company",
            name="Test Org Ltd",
            registry_scheme="GB-COH",
            registry_id="01234567",
            company_number="01234567",
        )
        control = {
            "id": 1,
            "member_id": 999999,
            "organisation_name": "Test Org Ltd",
            "company_number": "01234567",
            "registration_date": "2026-01-01",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_NOT_FOUND
        assert row["temporal_status"] == TEMPORAL_NOT_APPLICABLE

    def test_absent_organisation_is_not_found(self):
        """GIVEN a member that exists but a declared organisation with no
        matching company entity anywhere in the graph
        WHEN classified
        THEN status is 'not-found'."""
        Entity.objects.create(
            entity_type="person",
            name="Someone MP",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="1",
        )
        control = {
            "id": 1,
            "member_id": 1,
            "organisation_name": "Nonexistent Org Ltd",
            "company_number": None,
            "registration_date": "2026-01-01",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_NOT_FOUND

    def test_unresolved_placeholder_entity_is_not_an_organisation_match(self):
        """GIVEN an interest already ingested to a UK-PARLIAMENT-UNRESOLVED
        regulated_entity placeholder (not a real company registry node)
        WHEN resolve_organisation_candidates searches for that same declared
        name
        THEN it returns no candidates -- a weak, per-interest scoped
        placeholder must never masquerade as a retrieved organisation link."""
        Entity.objects.create(
            entity_type="regulated_entity",
            name="Guardian news and media",
            registry_scheme="UK-PARLIAMENT-UNRESOLVED",
            registry_id="5336:GUARDIAN NEWS AND MEDIA",
        )

        candidates = resolve_organisation_candidates("Guardian news and media", company_number=None)

        assert candidates == []


@pytest.mark.django_db
class TestClassifyControlUnresolved:
    def test_ambiguous_organisation_match_is_unresolved(self):
        """GIVEN two distinct GB-COH company entities with the identical
        normalised name and no company_number to disambiguate
        WHEN classified
        THEN status is 'unresolved' -- the pipeline correctly refuses to
        guess which one is right (ADR-006 discipline)."""
        member = Entity.objects.create(
            entity_type="person",
            name="Someone MP",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="1",
        )
        Entity.objects.create(
            entity_type="company",
            name="Acme Ltd",
            registry_scheme="GB-COH",
            registry_id="11111111",
            company_number="11111111",
        )
        Entity.objects.create(
            entity_type="company",
            name="Acme Ltd",
            registry_scheme="GB-COH",
            registry_id="22222222",
            company_number="22222222",
        )
        control = {
            "id": 1,
            "member_id": 1,
            "member_name": member.name,
            "organisation_name": "Acme Ltd",
            "company_number": None,
            "registration_date": "2026-01-01",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_UNRESOLVED


@pytest.mark.django_db
class TestRunControlsDeterminism:
    def test_run_controls_is_deterministic_across_repeated_runs(self, tmp_path):
        """GIVEN a fixed control list and unchanged graph state
        WHEN run_controls executes twice in a row
        THEN both runs produce the identical per-row status sequence and
        summary counts."""
        member = Entity.objects.create(
            entity_type="person",
            name="Someone MP",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="1",
        )
        org = Entity.objects.create(
            entity_type="company",
            name="Test Org Ltd",
            registry_scheme="GB-COH",
            registry_id="01234567",
            company_number="01234567",
        )
        Edge.objects.create(edge_type="declared_interest", source_entity=member, target_entity=org)
        controls = [
            {
                "id": 1,
                "member_id": 1,
                "organisation_name": "Test Org Ltd",
                "company_number": "01234567",
                "registration_date": "2026-01-01",
            },
            {
                "id": 2,
                "member_id": 999999,
                "organisation_name": "Nonexistent Ltd",
                "company_number": None,
                "registration_date": "2026-01-01",
            },
        ]
        path = _write_controls(tmp_path, controls)

        first = run_controls(path)
        second = run_controls(path)

        assert [r["status"] for r in first["rows"]] == [r["status"] for r in second["rows"]]
        assert first["retrieval_recovered"] == 1
        assert first["not_found"] == 1
        assert (first["retrieval_recovered"], first["not_found"]) == (
            second["retrieval_recovered"],
            second["not_found"],
        )


@pytest.mark.django_db
class TestRunControlsReportsRetrievalAndTemporalSeparately:
    def test_output_carries_both_figures_with_independent_denominators(self, tmp_path):
        """GIVEN two recovered rows, one with a matching date and one with a
        mismatched date
        WHEN run_controls executes
        THEN retrieval_recovered counts both, temporal_recovered counts only
        the matching one."""
        member1 = Entity.objects.create(
            entity_type="person", name="M1", registry_scheme="UK-PARLIAMENT-MEMBER", registry_id="1"
        )
        org1 = Entity.objects.create(
            entity_type="company",
            name="Org One",
            registry_scheme="GB-COH",
            registry_id="00000001",
            company_number="00000001",
        )
        Edge.objects.create(
            edge_type="declared_interest",
            source_entity=member1,
            target_entity=org1,
            valid_from=date(2026, 1, 1),
        )

        member2 = Entity.objects.create(
            entity_type="person", name="M2", registry_scheme="UK-PARLIAMENT-MEMBER", registry_id="2"
        )
        org2 = Entity.objects.create(
            entity_type="company",
            name="Org Two",
            registry_scheme="GB-COH",
            registry_id="00000002",
            company_number="00000002",
        )
        Edge.objects.create(
            edge_type="declared_interest",
            source_entity=member2,
            target_entity=org2,
            valid_from=date(2020, 1, 1),
        )

        controls = [
            {
                "id": 1,
                "member_id": 1,
                "organisation_name": "Org One",
                "company_number": "00000001",
                "registration_date": "2026-01-01",
            },
            {
                "id": 2,
                "member_id": 2,
                "organisation_name": "Org Two",
                "company_number": "00000002",
                "registration_date": "2026-06-01",
            },
        ]
        path = _write_controls(tmp_path, controls)

        result = run_controls(path)

        assert result["retrieval_recovered"] == 2
        assert result["retrieval_total"] == 2
        assert result["temporal_recovered"] == 1
        assert result["temporal_total"] == 2


@pytest.mark.django_db
class TestRunControlsAgainstEmptyGraph:
    def test_full_fixture_against_empty_graph_is_all_not_found_and_does_not_crash(self):
        """GIVEN the real 12-row production fixture and an empty test graph
        WHEN run_controls executes
        THEN it completes without error and every row is 'not-found' --
        matching this stratum's known ~0.6% ingestion coverage, never
        'unresolved', since nothing here is ambiguous, just absent."""
        result = run_controls(DEFAULT_CONTROLS_PATH)

        assert result["n"] == 12
        assert result["not_found"] == 12
        assert result["retrieval_recovered"] == 0
        assert result["unresolved"] == 0
        assert result["temporal_recovered"] == 0
