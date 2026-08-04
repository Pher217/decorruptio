"""Tests for `scripts/lead_scan.py` -- the reproducible two-hop lead-scan funnel.

Covers: candidate-set construction (`build_candidates`), registry-ID-only
resolution (`resolve_candidates`), the funnel's status vocabulary
(`unresolved` / `no_path` / `undated_only` / `path_no_award_date` /
`pre_award`, each distinct and never conflated), that the funnel stages sum
correctly, that a path's reported `min_identity_confidence` matches its
weakest `same_as` bridge (and the paired tier label), determinism across
repeated runs on the same fixture, and that the emitted artifact carries the
not-the-historical-funnel caveat rather than letting a reader mistake it for
`findings.md` SS6's published figures.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from scripts import lead_scan
from scripts.lead_scan import (
    build_candidates,
    build_report,
    compute_funnel,
    member_entity_ids,
    resolve_candidates,
    scan,
)

from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.graph.register_snapshots import path_min_identity_confidence
from uncorrupt.staging.models import Award, SupplierResolution

SOURCE_ID = "uk_contracts_finder"


def _award(supplier_name: str, award_date: date | None, *, tender_id: str | None = None) -> Award:
    return Award.objects.create(
        source_id=SOURCE_ID,
        tender_id=tender_id or f"tender-{supplier_name}",
        award_id=f"award-{supplier_name}",
        supplier_name=supplier_name,
        award_date=datetime(award_date.year, award_date.month, award_date.day, tzinfo=UTC)
        if award_date
        else None,
        raw_json={},
    )


def _resolution(supplier_name: str, company_number: str | None) -> SupplierResolution:
    return SupplierResolution.objects.create(
        source_id=SOURCE_ID,
        supplier_name=supplier_name,
        company_number=company_number,
        match_confidence=1.0 if company_number else 0.0,
        match_method="identifier" if company_number else None,
    )


def _company_entity(company_number: str, name: str) -> Entity:
    return Entity.objects.create(
        entity_type="company",
        name=name,
        registry_scheme="GB-COH",
        registry_id=company_number,
        company_number=company_number,
    )


def _member(name: str, registry_id: str) -> Entity:
    return Entity.objects.create(
        entity_type="person",
        name=name,
        registry_scheme="UK-PARLIAMENT-MEMBER",
        registry_id=registry_id,
    )


def _officer(name: str) -> Entity:
    return Entity.objects.create(entity_type="person", name=name, registry_scheme="GB-COH-OFFICER")


def _same_as(member: Entity, officer: Entity, confidence: float, method: str) -> Edge:
    edge = Edge.objects.create(edge_type="same_as", source_entity=member, target_entity=officer)
    Attestation.objects.create(
        edge=edge,
        source_name="Cross-register name match",
        match_confidence=confidence,
        match_method=method,
    )
    return edge


def _officer_of(officer: Entity, company: Entity, valid_from: date | None) -> Edge:
    edge = Edge.objects.create(
        edge_type="officer_of", source_entity=officer, target_entity=company, valid_from=valid_from
    )
    Attestation.objects.create(
        edge=edge, source_name="Companies House", match_confidence=1.0, match_method="identifier"
    )
    return edge


@pytest.mark.django_db
class TestBuildCandidates:
    def test_distinct_company_number_becomes_a_candidate(self):
        """GIVEN one resolved SupplierResolution row for uk_contracts_finder
        WHEN build_candidates runs THEN its company_number appears as a
        candidate key."""
        _resolution("Acme Ltd", "00000001")
        _award("Acme Ltd", date(2020, 1, 1))

        candidates = build_candidates()

        assert "00000001" in candidates

    def test_earliest_award_date_used_across_multiple_supplier_names(self):
        """GIVEN two different supplier_name strings resolving to the SAME
        company_number, with different award dates, WHEN build_candidates
        runs THEN the candidate's cutoff is the EARLIER of the two dates."""
        _resolution("Acme Ltd", "00000001")
        _resolution("Acme Limited", "00000001")
        _award("Acme Ltd", date(2021, 6, 1))
        _award("Acme Limited", date(2019, 3, 15))

        candidates = build_candidates()

        assert candidates["00000001"] == date(2019, 3, 15)

    def test_excludes_supplier_resolution_with_null_company_number(self):
        """GIVEN a SupplierResolution row with no company_number (an
        unmatched supplier) WHEN build_candidates runs THEN it contributes no
        candidate."""
        _resolution("Unmatched Supplier", None)
        _award("Unmatched Supplier", date(2020, 1, 1))

        candidates = build_candidates()

        assert candidates == {}

    def test_excludes_other_source_ids(self):
        """GIVEN a resolved SupplierResolution for a DIFFERENT source
        WHEN build_candidates runs THEN it is excluded -- this script's
        candidate set is scoped to uk_contracts_finder only."""
        SupplierResolution.objects.create(
            source_id="ua_prozorro",
            supplier_name="Other Source Ltd",
            company_number="00000009",
            match_confidence=1.0,
            match_method="identifier",
        )

        candidates = build_candidates()

        assert candidates == {}

    def test_candidate_with_no_dated_award_has_none_cutoff(self):
        """GIVEN a resolved SupplierResolution whose Award carries no
        award_date WHEN build_candidates runs THEN the candidate's cutoff is
        None -- reported honestly, not defaulted to any particular date."""
        _resolution("No Date Ltd", "00000002")
        _award("No Date Ltd", None)

        candidates = build_candidates()

        assert candidates["00000002"] is None

    def test_excludes_supplier_resolution_with_empty_string_company_number(self):
        """GIVEN a SupplierResolution row whose company_number is an empty
        string (not NULL, but not a usable identifier either) WHEN
        build_candidates runs THEN it contributes no candidate."""
        _resolution("Blank Number Ltd", "")
        _award("Blank Number Ltd", date(2020, 1, 1))

        candidates = build_candidates()

        assert candidates == {}


@pytest.mark.django_db
class TestResolveCandidates:
    def test_resolves_by_company_number_to_matching_entity(self):
        """GIVEN a candidate whose company_number matches a GB-COH Entity's
        registry_id WHEN resolve_candidates runs THEN it resolves to that
        Entity."""
        entity = _company_entity("00000001", "ACME LTD")

        resolved = resolve_candidates({"00000001": date(2020, 1, 1)})

        assert resolved["00000001"] == entity

    def test_unresolved_company_number_returns_none(self):
        """GIVEN a candidate company_number with no matching graph Entity
        WHEN resolve_candidates runs THEN it resolves to None."""
        resolved = resolve_candidates({"00000099": date(2020, 1, 1)})

        assert resolved["00000099"] is None

    def test_does_not_fall_back_to_name_matching(self):
        """GIVEN a candidate company_number that resolves to NO Entity, even
        though an Entity with a similar company NAME exists in the graph
        under a DIFFERENT company_number, WHEN resolve_candidates runs THEN
        it still resolves to None -- proving resolution is registry-ID-only
        and never falls back to a name-string guess (ADR-004 D2)."""
        _company_entity("00000002", "SIMILARLY NAMED LTD")

        resolved = resolve_candidates({"00000001": date(2020, 1, 1)})

        assert resolved["00000001"] is None


@pytest.mark.django_db
class TestScanStatuses:
    def test_unresolved_candidate_is_distinct_from_no_path(self):
        """GIVEN a candidate whose company_number never resolves WHEN
        scan runs THEN its status is 'unresolved', not 'no_path' -- a
        resolution failure must never be conflated with a resolved company
        that genuinely has no path."""
        candidates = {"00000099": date(2020, 1, 1)}
        resolved = {"00000099": None}

        rows = scan({}, candidates, resolved, set(), {}, max_hops=2)

        assert rows[0]["status"] == "unresolved"
        assert rows[0]["any_2hop_path"] is False

    def test_resolved_candidate_with_no_path_is_no_path(self):
        """GIVEN a resolved candidate with no edges connecting it to any
        member within 2 hops WHEN scan runs THEN its status is 'no_path'."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        member_ids = {member.id}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        assert rows[0]["status"] == "no_path"
        assert rows[0]["any_2hop_path"] is False

    def test_resolved_candidate_with_pre_award_path_is_pre_award(self):
        """GIVEN a resolved candidate reachable from a member via
        same_as -> officer_of, with the officer_of edge dated strictly
        before the company's award cutoff, WHEN scan runs THEN its status is
        'pre_award' and it carries exactly one recovered path."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        _same_as(member, officer, 0.60, "surname_title_only")
        _officer_of(officer, company, date(2015, 1, 1))
        member_ids = {member.id}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        assert rows[0]["status"] == "pre_award"
        assert rows[0]["n_pre_award_paths"] == 1
        assert rows[0]["any_2hop_path"] is True

    def test_pre_award_wins_when_the_same_company_also_has_an_undated_path(self):
        """GIVEN a resolved candidate with TWO paths -- one pre-award
        (dated, before the cutoff) from one member, and one undated from a
        DIFFERENT member -- WHEN scan runs THEN its status is 'pre_award',
        not 'undated_only': ANY pre-award path is enough, regardless of
        which status branch happens to be checked first."""
        company = _company_entity("00000001", "ACME LTD")
        dated_member = _member("Lord Dated", "1001")
        undated_member = _member("Lord Undated", "1002")
        dated_officer = _officer("DATED, Lord")
        undated_officer = _officer("UNDATED, Lord")
        _same_as(dated_member, dated_officer, 0.60, "surname_title_only")
        _same_as(undated_member, undated_officer, 0.60, "surname_title_only")
        _officer_of(dated_officer, company, date(2015, 1, 1))
        _officer_of(undated_officer, company, None)
        member_ids = {dated_member.id, undated_member.id}
        members_by_id = {dated_member.id: dated_member, undated_member.id: undated_member}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, members_by_id, max_hops=2)

        assert rows[0]["status"] == "pre_award"
        assert rows[0]["n_pre_award_paths"] == 1
        assert rows[0]["n_undated_paths"] == 1

    def test_resolved_candidate_with_only_undated_path_is_undated_only(self):
        """GIVEN a resolved candidate reachable via same_as -> officer_of,
        but the officer_of edge has NO valid_from date, WHEN scan runs THEN
        its status is 'undated_only', not 'pre_award' and not 'no_path'."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        _same_as(member, officer, 0.60, "surname_title_only")
        _officer_of(officer, company, None)
        member_ids = {member.id}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        assert rows[0]["status"] == "undated_only"
        assert rows[0]["n_pre_award_paths"] == 0
        assert rows[0]["any_2hop_path"] is True

    def test_unknown_award_date_with_a_path_is_path_no_award_date(self):
        """GIVEN a resolved candidate with a path to a member, but NO known
        award date for that candidate, WHEN scan runs THEN its status is
        'path_no_award_date' -- distinct from 'pre_award' (nothing here can
        be verified pre-award) and distinct from 'no_path' (a path DOES
        exist)."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        _same_as(member, officer, 0.60, "surname_title_only")
        _officer_of(officer, company, date(2015, 1, 1))
        member_ids = {member.id}

        candidates = {"00000001": None}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        assert rows[0]["status"] == "path_no_award_date"
        assert rows[0]["n_pre_award_paths"] == 0
        assert rows[0]["pre_award_paths"] == []

    def test_unknown_award_date_with_no_path_is_no_path(self):
        """GIVEN a resolved candidate with NO known award date and NO path
        to any member at all WHEN scan runs THEN its status is 'no_path',
        not 'path_no_award_date' -- the absence of a path is the same fact
        regardless of whether the cutoff is known."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        member_ids = {member.id}

        candidates = {"00000001": None}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        assert rows[0]["status"] == "no_path"


@pytest.mark.django_db
class TestFunnelSumsCorrectly:
    def _rows_for_mixed_fixture(self) -> list[dict]:
        # unresolved
        candidates: dict = {"00000000": date(2020, 1, 1)}
        resolved: dict = {"00000000": None}

        # no_path
        no_path_company = _company_entity("00000001", "NO PATH LTD")
        candidates["00000001"] = date(2020, 1, 1)
        resolved["00000001"] = no_path_company

        # pre_award
        pre_award_company = _company_entity("00000002", "PRE AWARD LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        _same_as(member, officer, 0.60, "surname_title_only")
        _officer_of(officer, pre_award_company, date(2015, 1, 1))
        candidates["00000002"] = date(2020, 1, 1)
        resolved["00000002"] = pre_award_company

        # undated_only
        undated_company = _company_entity("00000003", "UNDATED LTD")
        officer2 = _officer("EXAMPLE, Lord (2)")
        _same_as(member, officer2, 0.60, "surname_title_only")
        _officer_of(officer2, undated_company, None)
        candidates["00000003"] = date(2020, 1, 1)
        resolved["00000003"] = undated_company

        # path_no_award_date
        no_date_company = _company_entity("00000004", "NO AWARD DATE LTD")
        officer3 = _officer("EXAMPLE, Lord (3)")
        _same_as(member, officer3, 0.60, "surname_title_only")
        _officer_of(officer3, no_date_company, date(2010, 1, 1))
        candidates["00000004"] = None
        resolved["00000004"] = no_date_company

        member_ids = {member.id}
        adj = lead_scan.build_adjacency()
        return scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

    def test_resolved_plus_unresolved_equals_candidates(self):
        """GIVEN a fixture spanning every terminal status WHEN the funnel is
        computed THEN resolved + unresolved == candidates exactly."""
        rows = self._rows_for_mixed_fixture()

        funnel = compute_funnel(rows)

        assert funnel["resolved"] + funnel["unresolved"] == funnel["candidates"]
        assert funnel["candidates"] == 5

    def test_terminal_statuses_sum_to_resolved(self):
        """GIVEN the same mixed fixture WHEN the funnel is computed THEN the
        four terminal buckets (no_path, undated_only, path_no_award_date,
        pre_award_companies) sum to exactly 'resolved'."""
        rows = self._rows_for_mixed_fixture()

        funnel = compute_funnel(rows)

        terminal_sum = (
            funnel["no_path"]
            + funnel["undated_only"]
            + funnel["path_no_award_date"]
            + funnel["pre_award_companies"]
        )
        assert terminal_sum == funnel["resolved"] == 4

    def test_any_2hop_path_equals_sum_of_path_bearing_statuses(self):
        """GIVEN the same mixed fixture WHEN the funnel is computed THEN
        any_2hop_path == undated_only + path_no_award_date +
        pre_award_companies (every status where a path structurally exists,
        whether or not it is pre-award admissible)."""
        rows = self._rows_for_mixed_fixture()

        funnel = compute_funnel(rows)

        assert funnel["any_2hop_path"] == (
            funnel["undated_only"] + funnel["path_no_award_date"] + funnel["pre_award_companies"]
        )
        assert funnel["any_2hop_path"] == 3

    def test_pre_award_paths_total_counts_every_path_not_just_every_company(self):
        """GIVEN one company reachable via TWO distinct pre-award paths
        (two different members) WHEN the funnel is computed THEN
        pre_award_paths_total counts both paths, not one per company --
        proving the total is a PATH count, not a COMPANY count."""
        company = _company_entity("00000001", "ACME LTD")
        member_a = _member("Lord A", "1001")
        member_b = _member("Lord B", "1002")
        officer_a = _officer("A, Lord")
        officer_b = _officer("B, Lord")
        _same_as(member_a, officer_a, 0.60, "surname_title_only")
        _same_as(member_b, officer_b, 0.60, "surname_title_only")
        _officer_of(officer_a, company, date(2015, 1, 1))
        _officer_of(officer_b, company, date(2016, 1, 1))
        member_ids = {member_a.id, member_b.id}
        members_by_id = {member_a.id: member_a, member_b.id: member_b}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, members_by_id, max_hops=2)
        funnel = compute_funnel(rows)

        assert rows[0]["n_pre_award_paths"] == 2
        assert funnel["pre_award_companies"] == 1
        assert funnel["pre_award_paths_total"] == 2


@pytest.mark.django_db
class TestMinIdentityConfidenceAndTier:
    def test_pre_award_path_reports_its_bridges_confidence_and_tier(self):
        """GIVEN a pre-award path bridged by one same_as edge WHEN scanned
        THEN the reported min_identity_confidence and same_as_tier both
        describe THAT bridge's attestation."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        _same_as(member, officer, 0.60, "surname_title_only")
        _officer_of(officer, company, date(2015, 1, 1))
        member_ids = {member.id}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        path_row = rows[0]["pre_award_paths"][0]
        assert path_row["min_identity_confidence"] == 0.60
        assert path_row["same_as_tier"] == "surname_title_only"

    def test_weakest_bridge_wins_when_path_has_two_same_as_edges(self):
        """GIVEN a path bridged by TWO same_as edges of differing confidence
        WHEN scanned THEN both min_identity_confidence and same_as_tier
        describe the WEAKER bridge -- proving the tier label is paired with
        the confidence value path_min_identity_confidence actually reduced
        to, not independently picked."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        mid = _officer("EXAMPLE (mid record)")
        officer = _officer("EXAMPLE, Lord")
        strong_hop = _same_as(member, mid, 0.85, "surname_forename_title")
        weak_hop = _same_as(mid, officer, 0.60, "surname_title_only")
        _officer_of(officer, company, date(2015, 1, 1))
        member_ids = {member.id}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        path_row = rows[0]["pre_award_paths"][0]
        # Sanity: path_min_identity_confidence agrees independently, computed
        # directly from the real edges rather than trusting scan()'s own call.
        officer_of_edge = Edge.objects.get(edge_type="officer_of")
        recovered_path = [strong_hop, weak_hop, officer_of_edge]
        assert path_min_identity_confidence(recovered_path) == 0.60
        assert path_row["min_identity_confidence"] == 0.60
        assert path_row["same_as_tier"] == "surname_title_only"

    def test_tie_break_picks_the_lower_edge_id_bridge(self):
        """GIVEN two same_as bridges tied at the SAME minimum confidence but
        with match_method labels that would sort in the OPPOSITE order
        alphabetically, WHEN scanned THEN same_as_tier reports the tier of
        the LOWER-id edge (created first) -- proving the tie-break is by
        edge id, not by an incidental alphabetical sort of the method
        label."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        mid = _officer("EXAMPLE (mid record)")
        officer = _officer("EXAMPLE, Lord")
        first_hop = _same_as(member, mid, 0.60, "zzz_created_first")
        second_hop = _same_as(mid, officer, 0.60, "aaa_created_second")
        _officer_of(officer, company, date(2015, 1, 1))
        assert first_hop.id < second_hop.id
        member_ids = {member.id}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        path_row = rows[0]["pre_award_paths"][0]
        assert path_row["min_identity_confidence"] == 0.60
        assert path_row["same_as_tier"] == "zzz_created_first"


@pytest.mark.django_db
class TestDeterminism:
    def test_two_runs_on_the_same_fixture_produce_identical_output(self):
        """GIVEN a fixed graph and fixed candidates WHEN scan runs TWICE
        THEN the JSON-serialized output is byte-identical -- no unordered
        iteration leaks into the result."""
        company_a = _company_entity("00000001", "ACME LTD")
        company_b = _company_entity("00000002", "BETA LTD")
        member = _member("Lord Example", "1001")
        officer_a = _officer("EXAMPLE, Lord (A)")
        officer_b = _officer("EXAMPLE, Lord (B)")
        _same_as(member, officer_a, 0.60, "surname_title_only")
        _same_as(member, officer_b, 0.85, "surname_forename_title")
        _officer_of(officer_a, company_a, date(2015, 1, 1))
        _officer_of(officer_b, company_b, date(2016, 1, 1))
        member_ids = {member.id}
        members_by_id = {member.id: member}

        candidates = {"00000001": date(2020, 1, 1), "00000002": date(2020, 1, 1)}
        resolved = {"00000001": company_a, "00000002": company_b}

        adj_1 = lead_scan.build_adjacency()
        rows_1 = scan(adj_1, candidates, resolved, member_ids, members_by_id, max_hops=2)
        adj_2 = lead_scan.build_adjacency()
        rows_2 = scan(adj_2, candidates, resolved, member_ids, members_by_id, max_hops=2)

        assert json.dumps(rows_1, sort_keys=True) == json.dumps(rows_2, sort_keys=True)


@pytest.mark.django_db
class TestArtifactCaveats:
    def test_report_carries_not_historical_funnel_statement(self):
        """GIVEN a built report WHEN inspected THEN it explicitly states this
        is not a reproduction of findings.md SS6's historical funnel -- a
        reader of the JSON alone must not mistake this for those figures."""
        report = build_report()

        assert "not_a_reproduction_of_historical_funnel" in report
        statement = report["not_a_reproduction_of_historical_funnel"]
        assert "never committed" in statement
        assert "NOT a reproduction" in statement

    def test_report_carries_investigative_lead_and_confidence_caveats(self):
        """GIVEN a built report WHEN inspected THEN it carries the ADR-000
        investigative-lead caveat and the uncalibrated-confidence caveat as
        top-level, self-describing fields."""
        report = build_report()

        assert "ADR-000" in report["investigative_lead_caveat"]
        assert "UNCALIBRATED" in report["identity_confidence_caveat"]

    def test_report_never_emits_score_or_verdict_shaped_keys(self):
        """GIVEN a built report WHEN every key at every nesting level is
        inspected THEN none contains 'score' or 'verdict' -- this script is
        exploratory analysis, not a scorer, and must never emit anything
        resembling one."""

        def walk_keys(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    yield key
                    yield from walk_keys(value)
            elif isinstance(obj, list):
                for item in obj:
                    yield from walk_keys(item)

        report = build_report()

        for key in walk_keys(report):
            assert "score" not in key.lower()
            assert "verdict" not in key.lower()


@pytest.mark.django_db
class TestBuildReportIntegration:
    def test_end_to_end_report_reflects_the_fixture(self):
        """GIVEN a small end-to-end fixture (one pre-award lead, one
        unresolved candidate) WHEN build_report runs THEN the funnel and row
        detail reflect it exactly, including member_entity_ids being used to
        drive the scan."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        _same_as(member, officer, 0.60, "surname_title_only")
        _officer_of(officer, company, date(2015, 1, 1))
        _resolution("Acme Ltd", "00000001")
        _award("Acme Ltd", date(2020, 1, 1))
        _resolution("Ghost Supplier Ltd", "00000099")
        _award("Ghost Supplier Ltd", date(2020, 1, 1))

        assert member.id in member_entity_ids()

        report = build_report()

        assert report["funnel"]["candidates"] == 2
        assert report["funnel"]["resolved"] == 1
        assert report["funnel"]["unresolved"] == 1
        assert report["funnel"]["pre_award_companies"] == 1
        assert report["funnel"]["pre_award_paths_total"] == 1

        pre_award_row = next(r for r in report["rows"] if r["company_number"] == "00000001")
        assert pre_award_row["status"] == "pre_award"
        assert pre_award_row["pre_award_paths"][0]["member_registry_id"] == "1001"
        assert pre_award_row["pre_award_paths"][0]["company_registry_id"] == "00000001"
