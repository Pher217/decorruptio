"""Tests for `scripts/lead_scan.py` -- the reproducible two-hop lead-scan funnel.

Covers: candidate-set construction (`build_candidates`, including dedup of
two spellings of one company number), registry-ID-only resolution
(`resolve_candidates`), the funnel's status vocabulary (`unresolved` /
`no_path` / `undated_only` / `dated_post_award` / `path_no_award_date` /
`pre_award`, each distinct and never conflated), that the funnel stages sum
correctly, that a path's reported `min_identity_confidence` matches its
weakest `same_as` bridge (and the paired tier label), that emitted edges
carry real `valid_from`/`attesting_sources` evidence, determinism across
repeated runs and across hash-colliding start ids, the CLI (`main`,
including `--max-hops` validation and that the caveats survive into the
written file, not just the in-memory dict), and that the emitted artifact
carries the not-the-historical-funnel caveat rather than letting a reader
mistake it for `findings.md` SS6's published figures.
"""

from __future__ import annotations

import json
import sys
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

    def test_two_spellings_of_the_same_company_number_collapse_to_one_candidate(self):
        """GIVEN two SupplierResolution rows for DIFFERENT supplier names
        that resolve to the same real company under two different spellings
        of its company_number ("4125764" unpadded vs "04125764" zero-padded
        -- upstream stores the raw supplier_id on a Companies House miss and
        the normalised spelling on a hit) WHEN build_candidates runs THEN
        they collapse to exactly ONE candidate keyed by the normalised
        number, not two -- otherwise the same real company would be double
        counted in `candidates`, and if both spellings ever resolve, emitted
        as two separate leads."""
        _resolution("Acme Ltd (raw spelling)", "4125764")
        _resolution("Acme Ltd (padded spelling)", "04125764")
        _award("Acme Ltd (raw spelling)", date(2021, 6, 1))
        _award("Acme Ltd (padded spelling)", date(2019, 3, 15))

        candidates = build_candidates()

        assert candidates == {"04125764": date(2019, 3, 15)}

    def test_normalised_candidate_key_matches_normalise_company_number(self):
        """GIVEN a single SupplierResolution row carrying the UNPADDED
        spelling of a company number WHEN build_candidates runs THEN the
        candidate key is the NORMALISED spelling
        (`phase_c_paths.normalise_company_number`), not the raw string
        exactly as stored -- pinning that normalisation happens at
        candidate-construction time, not left to whatever resolves it
        downstream."""
        _resolution("Acme Ltd", "4125764")
        _award("Acme Ltd", date(2020, 1, 1))

        candidates = build_candidates()

        assert "4125764" not in candidates
        assert "04125764" in candidates


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

    def test_calls_resolve_supplier_with_an_empty_name(self, monkeypatch):
        """GIVEN a candidate WHEN resolve_candidates runs THEN
        resolve_supplier is called with name="" -- pinning the deliberate
        choice not to engage the name-matching fallback tier directly,
        independent of whether any particular fixture entity happens to
        coincidentally name-match (which the scenario-based test above
        cannot rule out on its own)."""
        calls: list[tuple[str, dict, str | None]] = []

        def fake_resolve_supplier(name, ch_cache, company_number=None):
            calls.append((name, ch_cache, company_number))
            return None

        monkeypatch.setattr(lead_scan, "resolve_supplier", fake_resolve_supplier)

        resolve_candidates({"00000001": date(2020, 1, 1)})

        assert calls == [("", {}, "00000001")]


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
        assert rows[0]["any_path_within_max_hops"] is False

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
        assert rows[0]["any_path_within_max_hops"] is False

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
        assert rows[0]["any_path_within_max_hops"] is True

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
        assert rows[0]["any_path_within_max_hops"] is True

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

    def test_n_undated_paths_is_none_when_award_date_unknown(self):
        """GIVEN a resolved candidate with NO known award date (so pre-award
        admissibility can never be tested) WHEN scan runs THEN
        n_undated_paths is None, not 0 -- 0 would read as "checked and found
        none dated"; None honestly says the check was never run."""
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

        assert rows[0]["n_undated_paths"] is None

    def test_fully_dated_path_after_cutoff_is_dated_post_award_not_undated_only(self):
        """GIVEN a resolved candidate whose ONLY path is bridged by an
        officer_of edge that carries a valid_from dated AFTER the company's
        award cutoff (so it is fully dated, just not admissible pre-award)
        WHEN scan runs THEN its status is 'dated_post_award', not
        'undated_only' -- the register DOES publish a start date here, and
        it is dispositive-negative evidence (the relationship began after
        the award), not missing data."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        _same_as(member, officer, 0.60, "surname_title_only")
        _officer_of(officer, company, date(2021, 6, 1))
        member_ids = {member.id}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        assert rows[0]["status"] == "dated_post_award"
        assert rows[0]["n_dated_post_award_paths"] == 1
        assert rows[0]["n_pre_award_paths"] == 0
        assert rows[0]["any_path_within_max_hops"] is True
        assert rows[0]["dated_post_award_paths"][0]["edges"][-1]["valid_from"] == "2021-06-01"

    def test_undated_edge_still_reports_undated_only_not_dated_post_award(self):
        """GIVEN a resolved candidate whose only path has an officer_of edge
        with NO valid_from at all WHEN scan runs THEN its status stays
        'undated_only' -- proving the new dated_post_award status is scoped
        to paths that ARE fully dated, not applied to every path that fails
        pre-award admissibility."""
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
        assert rows[0]["n_dated_post_award_paths"] == 0
        assert rows[0]["dated_post_award_paths"] == []

    def test_post_cutoff_dated_path_never_inflates_pre_award_count(self):
        """GIVEN a resolved candidate with TWO paths from different members
        -- one genuinely pre-award (dated strictly before the cutoff) and
        one fully dated but AFTER the cutoff -- WHEN scan runs THEN status
        is 'pre_award' with EXACTLY ONE reported pre-award path, not two.
        This pins the per-company cutoff actually being used: a mutant that
        replaced the real cutoff with `date.max` would make the post-cutoff
        edge's date pass the `< cutoff` test too, silently doubling
        n_pre_award_paths."""
        company = _company_entity("00000001", "ACME LTD")
        pre_member = _member("Lord Pre", "1001")
        post_member = _member("Lord Post", "1002")
        pre_officer = _officer("PRE, Lord")
        post_officer = _officer("POST, Lord")
        _same_as(pre_member, pre_officer, 0.60, "surname_title_only")
        _same_as(post_member, post_officer, 0.60, "surname_title_only")
        _officer_of(pre_officer, company, date(2015, 1, 1))
        _officer_of(post_officer, company, date(2021, 6, 1))
        member_ids = {pre_member.id, post_member.id}
        members_by_id = {pre_member.id: pre_member, post_member.id: post_member}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, members_by_id, max_hops=2)

        assert rows[0]["status"] == "pre_award"
        assert rows[0]["n_pre_award_paths"] == 1
        assert rows[0]["pre_award_paths"][0]["member_registry_id"] == "1001"


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

        # dated_post_award
        dated_post_award_company = _company_entity("00000005", "DATED POST AWARD LTD")
        officer4 = _officer("EXAMPLE, Lord (4)")
        _same_as(member, officer4, 0.60, "surname_title_only")
        _officer_of(officer4, dated_post_award_company, date(2021, 6, 1))
        candidates["00000005"] = date(2020, 1, 1)
        resolved["00000005"] = dated_post_award_company

        member_ids = {member.id}
        adj = lead_scan.build_adjacency()
        return scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

    def test_resolved_plus_unresolved_equals_candidates(self):
        """GIVEN a fixture spanning every terminal status WHEN the funnel is
        computed THEN resolved + unresolved == candidates exactly."""
        rows = self._rows_for_mixed_fixture()

        funnel = compute_funnel(rows)

        assert funnel["resolved"] + funnel["unresolved"] == funnel["candidates"]
        assert funnel["candidates"] == 6

    def test_terminal_statuses_sum_to_resolved(self):
        """GIVEN the same mixed fixture WHEN the funnel is computed THEN the
        five terminal buckets (no_path, undated_only, dated_post_award,
        path_no_award_date, pre_award_companies) sum to exactly 'resolved'."""
        rows = self._rows_for_mixed_fixture()

        funnel = compute_funnel(rows)

        terminal_sum = (
            funnel["no_path"]
            + funnel["undated_only"]
            + funnel["dated_post_award_companies"]
            + funnel["path_no_award_date"]
            + funnel["pre_award_companies"]
        )
        assert terminal_sum == funnel["resolved"] == 5

    def test_any_path_within_max_hops_equals_sum_of_path_bearing_statuses(self):
        """GIVEN the same mixed fixture WHEN the funnel is computed THEN
        any_path_within_max_hops == undated_only + dated_post_award_companies
        + path_no_award_date + pre_award_companies (every status where a path
        structurally exists, whether or not it is pre-award admissible)."""
        rows = self._rows_for_mixed_fixture()

        funnel = compute_funnel(rows)

        assert funnel["any_path_within_max_hops"] == (
            funnel["undated_only"]
            + funnel["dated_post_award_companies"]
            + funnel["path_no_award_date"]
            + funnel["pre_award_companies"]
        )
        assert funnel["any_path_within_max_hops"] == 4

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

    def test_only_same_as_edges_contribute_to_the_tier_search(self):
        """GIVEN a path whose officer_of edge happens to carry the exact
        same confidence value (0.60) as its same_as bridge, but a
        DIFFERENT, lower edge id (created first), WHEN scanned THEN
        same_as_tier still reports the same_as bridge's own method label --
        proving the tier search is scoped to same_as edges only, not to
        every edge on the path."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        officer_of_edge = Edge.objects.create(
            edge_type="officer_of",
            source_entity=officer,
            target_entity=company,
            valid_from=date(2015, 1, 1),
        )
        Attestation.objects.create(
            edge=officer_of_edge,
            source_name="Companies House",
            match_confidence=0.60,
            match_method="identifier",
        )
        same_as_edge = _same_as(member, officer, 0.60, "surname_title_only")
        assert officer_of_edge.id < same_as_edge.id
        member_ids = {member.id}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, {member.id: member}, max_hops=2)

        path_row = rows[0]["pre_award_paths"][0]
        assert path_row["same_as_tier"] == "surname_title_only"


@pytest.mark.django_db
class TestEdgeEvidencePayload:
    """ADR-000: a named person is described strictly by what the registers
    factually attest. `valid_from` and `attesting_sources` ARE that attested
    evidence on each emitted edge -- pinned here so neither can silently
    disappear from the artifact."""

    def test_pre_award_path_edges_carry_valid_from_and_attesting_sources(self):
        """GIVEN a pre-award path WHEN scanned THEN every serialized edge on
        it reports its real valid_from date and its real attesting source
        name -- not just the edge_type."""
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

        edges = rows[0]["pre_award_paths"][0]["edges"]
        officer_of_edge = next(e for e in edges if e["edge_type"] == "officer_of")
        assert officer_of_edge["valid_from"] == "2015-01-01"
        assert officer_of_edge["attesting_sources"] == ["Companies House"]
        same_as_edge = next(e for e in edges if e["edge_type"] == "same_as")
        assert same_as_edge["attesting_sources"] == ["Cross-register name match"]

    def test_edge_with_no_valid_from_reports_none_not_a_fabricated_date(self):
        """GIVEN a real officer_of edge with no valid_from WHEN serialized
        THEN it reports valid_from as None -- never a fabricated or
        defaulted date standing in for missing data."""
        company = _company_entity("00000001", "ACME LTD")
        officer = _officer("EXAMPLE, Lord")
        officer_of_edge = _officer_of(officer, company, None)

        serialized = lead_scan._serialize_edge(officer_of_edge)

        assert serialized["valid_from"] is None


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

    def test_pre_award_paths_sorted_by_edge_id_even_when_start_ids_collide_in_a_hash_bucket(self):
        """GIVEN two UK-PARLIAMENT-MEMBER entities whose ids differ by
        exactly 8 -- which always collide in the same bucket of an 8-slot
        Python set table (module docstring's `1 % 8 == 9 % 8 == 1` example;
        any pair `n` and `n + 8` collides the same way), so `set` iteration
        order follows INSERTION order for this pair rather than numeric
        order -- and member_ids is built with the HIGHER id inserted first,
        so find_paths would naturally visit the higher-id member before the
        lower-id one, WHEN scan runs THEN pre_award_paths is still
        edge-id ascending (the lower-id member's path first): the explicit
        `sorted(..., key=_sort_key)` call in `scan`, not the set's own
        hash-driven order, is what makes this deterministic. Without that
        sort, this fixture would report the higher-id member's path first --
        pinning the load-bearing sort against exactly the collision the
        module docstring warns about."""
        company = _company_entity("00000001", "ACME LTD")
        # Explicit ids far above anything auto-assigned in this test, so
        # there is no risk of colliding with the company/officer rows
        # created above -- chosen to differ by exactly 8 from each other
        # (forces the hash-bucket collision) AND empirically verified (see
        # this module's own dev notes) to preserve textual/insertion order
        # under CPython's actual probe sequence, unlike some other
        # mod-8-colliding pairs whose higher hash bits perturb differently.
        low_id = 100_001
        high_id = low_id + 8
        member_low = Entity.objects.create(
            id=low_id,
            entity_type="person",
            name="Lord Low",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="1001",
        )
        officer_low = _officer("LOW, Lord")
        same_as_low = _same_as(member_low, officer_low, 0.60, "surname_title_only")
        officer_of_low = _officer_of(officer_low, company, date(2015, 1, 1))

        member_high = Entity.objects.create(
            id=high_id,
            entity_type="person",
            name="Lord High",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="1002",
        )
        officer_high = _officer("HIGH, Lord")
        same_as_high = _same_as(member_high, officer_high, 0.60, "surname_title_only")
        officer_of_high = _officer_of(officer_high, company, date(2016, 1, 1))

        assert same_as_low.id < officer_of_low.id < same_as_high.id < officer_of_high.id
        assert list({high_id, low_id}) == [high_id, low_id], (
            "test assumption: this pair collides mod 8 and iterates in insertion order"
        )

        member_ids = {high_id, low_id}
        members_by_id = {low_id: member_low, high_id: member_high}

        candidates = {"00000001": date(2020, 1, 1)}
        resolved = {"00000001": company}
        adj = lead_scan.build_adjacency()

        rows = scan(adj, candidates, resolved, member_ids, members_by_id, max_hops=2)

        assert rows[0]["n_pre_award_paths"] == 2
        assert rows[0]["pre_award_paths"][0]["member_entity_id"] == low_id
        assert rows[0]["pre_award_paths"][1]["member_entity_id"] == high_id


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

    def test_investigative_lead_caveat_names_the_actual_hop_budget_used(self):
        """GIVEN a report built with a NON-default max_hops WHEN inspected
        THEN the investigative_lead_caveat text names that actual max_hops
        value, not a hardcoded "two-hop" -- otherwise a run at --max-hops 3
        would publish a path of up to 3 real hops under a caveat that still
        says "two-hop", contradicting the max_hops field in the same
        artifact."""
        report = build_report(max_hops=3)

        assert "3 hop" in report["investigative_lead_caveat"]
        assert report["max_hops"] == 3

    def test_any_path_within_max_hops_key_is_not_named_for_a_fixed_hop_count(self):
        """GIVEN a built report at the default max_hops WHEN the funnel keys
        are inspected THEN there is no key literally called 'any_2hop_path'
        -- the funnel key must not hardcode a specific hop count that can
        drift out of sync with the actual --max-hops used."""
        report = build_report()

        assert "any_2hop_path" not in report["funnel"]
        assert "any_path_within_max_hops" in report["funnel"]

    def test_report_carries_sealed_cohort_overlap_note(self):
        """GIVEN a built report WHEN inspected THEN it states the sealed
        cohort is not excluded from candidates and discloses the measured
        overlap, so a reader knows sealed-benchmark company numbers can
        appear among the rows."""
        report = build_report()

        assert "sealed_cohort_overlap_note" in report
        assert "NOT exclude" in report["sealed_cohort_overlap_note"]

    def test_report_carries_stage1_denominator_context(self):
        """GIVEN a built report WHEN inspected THEN it states, as an
        explicit field, how many real awardee supplier names have no
        SupplierResolution row at all -- the denominator a reader would
        otherwise have to infer is missing entirely from candidates/resolved."""
        report = build_report()

        assert "stage1_context" in report
        ctx = report["stage1_context"]
        assert "awardee_supplier_names_total" in ctx
        assert "awardee_supplier_names_without_supplier_resolution_row" in ctx

    def test_graph_state_records_supplier_resolution_provenance(self):
        """GIVEN a built report WHEN graph_state is inspected THEN it
        records the SupplierResolution row count for this source -- the one
        funnel input whose provenance graph_state previously left
        unstamped, in a script that exists because a funnel could not be
        audited."""
        report = build_report()

        assert "supplier_resolution_rows" in report["graph_state"]
        assert isinstance(report["graph_state"]["supplier_resolution_rows"], int)


@pytest.mark.django_db
class TestMainCLI:
    """`main()` had no test coverage at all before this: the CLI's own
    argument validation and the bytes it actually writes to `--out` were
    both unpinned."""

    def _run_main(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["lead_scan.py", *argv])
        lead_scan.main()

    def test_rejects_zero_max_hops(self, monkeypatch, capsys):
        """GIVEN --max-hops 0 WHEN main runs THEN it exits with a usage
        error instead of silently emitting a fully-caveated report over a
        budget that can never find a single edge (find_paths' walk bails
        before adding one)."""
        monkeypatch.setattr(sys, "argv", ["lead_scan.py", "--max-hops", "0"])

        with pytest.raises(SystemExit) as exc_info:
            lead_scan.main()

        assert exc_info.value.code == 2
        assert "positive integer" in capsys.readouterr().err

    def test_rejects_negative_max_hops(self, monkeypatch, capsys):
        """GIVEN --max-hops -1 WHEN main runs THEN it exits with a usage
        error, same as 0 -- a negative hop budget is equally meaningless."""
        monkeypatch.setattr(sys, "argv", ["lead_scan.py", "--max-hops", "-1"])

        with pytest.raises(SystemExit) as exc_info:
            lead_scan.main()

        assert exc_info.value.code == 2

    def test_written_file_carries_the_caveats_not_just_the_funnel_and_rows(
        self, monkeypatch, tmp_path
    ):
        """GIVEN a small fixture WHEN main runs and writes --out THEN the
        BYTES ON DISK carry the honesty caveats (not_a_reproduction,
        investigative_lead_caveat, identity_confidence_caveat) -- pinning
        that main() writes the full `report` dict it built, not some
        caveat-free subset like `{"funnel": ..., "rows": ...}`. Reading only
        `build_report()`'s return value cannot catch a bug in what main()
        actually persists."""
        company = _company_entity("00000001", "ACME LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        _same_as(member, officer, 0.60, "surname_title_only")
        _officer_of(officer, company, date(2015, 1, 1))
        _resolution("Acme Ltd", "00000001")
        _award("Acme Ltd", date(2020, 1, 1))

        out_path = tmp_path / "lead_scan.json"
        self._run_main(monkeypatch, ["--out", str(out_path)])

        written = json.loads(out_path.read_text())
        assert "not_a_reproduction_of_historical_funnel" in written
        assert "investigative_lead_caveat" in written
        assert "identity_confidence_caveat" in written
        assert "sealed_cohort_overlap_note" in written
        assert written["funnel"]["candidates"] == 1

    def test_custom_max_hops_is_reflected_in_the_written_file(self, monkeypatch, tmp_path):
        """GIVEN --max-hops 3 WHEN main runs THEN the written file's
        max_hops field is 3 and its investigative_lead_caveat names 3 hops,
        not the default 2 -- the CLI argument actually reaches the artifact,
        key and caveat text both."""
        out_path = tmp_path / "lead_scan.json"
        self._run_main(monkeypatch, ["--max-hops", "3", "--out", str(out_path)])

        written = json.loads(out_path.read_text())
        assert written["max_hops"] == 3
        assert "3 hop" in written["investigative_lead_caveat"]
        assert "any_2hop_path" not in written["funnel"]

    def test_creates_missing_out_directory(self, monkeypatch, tmp_path):
        """GIVEN --out pointing into a directory that does not exist yet
        WHEN main runs THEN it creates the directory and writes the file
        there, rather than crashing on a missing parent."""
        out_path = tmp_path / "nested" / "dir" / "lead_scan.json"
        self._run_main(monkeypatch, ["--out", str(out_path)])

        assert out_path.exists()


@pytest.mark.django_db
class TestMemberEntityIds:
    def test_returns_only_uk_parliament_members(self):
        """GIVEN a UK-PARLIAMENT-MEMBER person and an unrelated
        GB-COH-OFFICER person WHEN member_entity_ids runs THEN only the
        parliament member's id is returned."""
        member = _member("Lord Example", "1001")
        _officer("EXAMPLE, Lord")

        ids = member_entity_ids()

        assert ids == {member.id}

    def test_excludes_non_person_entities_even_with_the_member_registry_scheme(self):
        """GIVEN a non-person entity that happens to carry the
        UK-PARLIAMENT-MEMBER registry scheme WHEN member_entity_ids runs
        THEN it is excluded -- the entity_type filter is not redundant with
        the registry_scheme filter."""
        member = _member("Lord Example", "1001")
        Entity.objects.create(
            entity_type="company", name="Not A Person", registry_scheme="UK-PARLIAMENT-MEMBER"
        )

        ids = member_entity_ids()

        assert ids == {member.id}


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

    def test_default_max_hops_is_two_not_three(self):
        """GIVEN a company reachable from a member only via a chain that
        costs THREE real (non-same_as) hops (officer_of -> ownership ->
        ownership) WHEN build_report runs with its default max_hops THEN
        that company is reported as no_path -- pinning DEFAULT_MAX_HOPS at
        2, not some looser budget that would silently widen what this
        script counts as a 'two-hop' lead."""
        company_a = _company_entity("00000002", "INTERMEDIATE A LTD")
        company_b = _company_entity("00000003", "INTERMEDIATE B LTD")
        target = _company_entity("00000001", "TARGET LTD")
        member = _member("Lord Example", "1001")
        officer = _officer("EXAMPLE, Lord")
        _same_as(member, officer, 0.60, "surname_title_only")
        _officer_of(officer, company_a, date(2010, 1, 1))
        Edge.objects.create(
            edge_type="ownership",
            source_entity=company_a,
            target_entity=company_b,
            valid_from=date(2011, 1, 1),
        )
        Edge.objects.create(
            edge_type="ownership",
            source_entity=company_b,
            target_entity=target,
            valid_from=date(2012, 1, 1),
        )
        _resolution("Target Ltd", "00000001")
        _award("Target Ltd", date(2020, 1, 1))

        report = build_report()

        target_row = next(r for r in report["rows"] if r["company_number"] == "00000001")
        assert target_row["status"] == "no_path"
        assert target_row["any_path_within_max_hops"] is False
