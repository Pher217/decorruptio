"""Tests for spec A2.4.2 coverage-gate measurement.

Covers the delegation packet's required scenarios:
- a partial ingest fails the coverage gate (not a 90%-style pass)
- a valid zero-officer response is not counted as a fetch failure
- an explicitly-failed record is distinguished from one never attempted
- the Commons denominator is read with the SAME query shape the fetch
  actually issues (ExpandChildInterests=true AND the ingest's own date
  window), and the measurement records which query produced each half of
  the ratio
- the ExpandChildInterests permissiveness gap (3,415 vs. 4,057, unexplained
  per parliament_interests.py) is disclosed on every live measurement
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from uncorrupt.gates.coverage import (
    CoverageMeasurement,
    _read_commons_ingest_date_window,
    fetch_commons_total_results,
    measure_ch_officer_coverage,
    measure_commons_coverage,
    measure_lords_snapshot_coverage,
    verify_lords_snapshot_integrity,
)
from uncorrupt.graph.models import Attestation, Edge, Entity

# ---------------------------------------------------------------------------
# CoverageMeasurement -- the 100%-accounted standard
# ---------------------------------------------------------------------------


class TestCoverageMeasurementPassedIs100PercentNotNinety:
    def test_92_of_100_accounted_for_fails_despite_exceeding_a_90_percent_bar(self):
        """GIVEN 92 ingested, 0 explicitly failed, 8 never attempted (92% accounted)
        WHEN passed is evaluated
        THEN it is False -- a 90%-style ratio would have passed this, spec A2.4.2
        requires 100%, not 90%."""
        m = CoverageMeasurement(
            name="x", ingested=92, explicitly_failed=0, not_attempted=8, total=100
        )
        assert m.passed is False

    def test_100_percent_accounted_for_passes_even_with_genuine_failures(self):
        """GIVEN 90 ingested and 10 explicitly failed (100% accounted, 0 never attempted)
        WHEN passed is evaluated
        THEN it is True -- the gate is about ACCOUNTING completeness, not success rate."""
        m = CoverageMeasurement(
            name="x", ingested=90, explicitly_failed=10, not_attempted=0, total=100
        )
        assert m.passed is True

    def test_a_single_never_attempted_record_fails_the_gate(self):
        """GIVEN 99 accounted for and exactly 1 never attempted, of 100
        WHEN passed is evaluated
        THEN it is False -- there is no partial-credit threshold."""
        m = CoverageMeasurement(
            name="x", ingested=99, explicitly_failed=0, not_attempted=1, total=100
        )
        assert m.passed is False

    def test_zero_total_never_passes(self):
        """GIVEN a measurement with total=0
        WHEN passed is evaluated
        THEN it is False -- an empty universe is not a vacuous pass."""
        m = CoverageMeasurement(name="x", ingested=0, explicitly_failed=0, not_attempted=0, total=0)
        assert m.passed is False


class TestCoverageMeasurementValidation:
    def test_mismatched_counts_raise(self):
        """GIVEN component counts that do not sum to total
        WHEN CoverageMeasurement is constructed
        THEN ValueError is raised -- a malformed count must never silently produce
        a wrong ratio."""
        with pytest.raises(ValueError):
            CoverageMeasurement(
                name="x", ingested=5, explicitly_failed=5, not_attempted=5, total=100
            )

    def test_negative_component_raises(self):
        """GIVEN a negative not_attempted count
        WHEN CoverageMeasurement is constructed
        THEN ValueError is raised."""
        with pytest.raises(ValueError):
            CoverageMeasurement(
                name="x", ingested=105, explicitly_failed=0, not_attempted=-5, total=100
            )

    def test_to_gate_dict_reports_ingested_as_covered_never_accounted_for(self):
        """GIVEN a measurement with both ingested and explicitly_failed records
        WHEN to_gate_dict is called
        THEN 'covered' equals ingested alone, not accounted_for -- crediting a
        logged failure toward 'covered' would let it also pass the downstream
        (out-of-scope) 90% ratio check."""
        m = CoverageMeasurement(
            name="x", ingested=60, explicitly_failed=40, not_attempted=0, total=100
        )
        gate_dict = m.to_gate_dict("covered", "total")
        assert gate_dict == {"covered": 60, "total": 100}


# ---------------------------------------------------------------------------
# Companies House officer-roster coverage
# ---------------------------------------------------------------------------


def _write_ch_cache(output_dir: Path, company_number: str, officer_count: int) -> None:
    (output_dir / f"{company_number}.json").write_text("[]", encoding="utf-8")
    (output_dir / f"{company_number}.provenance.json").write_text(
        json.dumps({"officer_count": officer_count}), encoding="utf-8"
    )


@pytest.mark.django_db
class TestMeasureChOfficerCoverage:
    def test_valid_zero_officer_response_is_ingested_not_a_failure(self, tmp_path, monkeypatch):
        """GIVEN a company that received a genuine roster fetch returning zero
        officers (a cache file exists, officer_count=0, no graph edges result)
        WHEN CH officer coverage is measured
        THEN the company counts toward `ingested`, never `explicitly_failed` or
        `not_attempted` -- distinguishing a valid empty response from a fetch
        failure is the whole point (delegation packet)."""
        monkeypatch.setattr(
            "uncorrupt.gates.coverage.ch_officers.procurement_supplier_universe",
            lambda: ["00000001"],
        )
        _write_ch_cache(tmp_path, "00000001", officer_count=0)

        result = measure_ch_officer_coverage(output_dir=tmp_path)

        assert result.ingested == 1
        assert result.explicitly_failed == 0
        assert result.not_attempted == 0
        assert result.passed is True

    def test_company_never_attempted_is_not_attempted(self, tmp_path, monkeypatch):
        """GIVEN a universe company with no cache file and no run manifest record
        WHEN CH officer coverage is measured
        THEN it is counted as not_attempted, and the gate fails."""
        monkeypatch.setattr(
            "uncorrupt.gates.coverage.ch_officers.procurement_supplier_universe",
            lambda: ["00000002"],
        )

        result = measure_ch_officer_coverage(output_dir=tmp_path)

        assert result.ingested == 0
        assert result.explicitly_failed == 0
        assert result.not_attempted == 1
        assert result.passed is False
        assert "00000002" in result.failure_manifest

    def test_selected_but_uncached_company_is_explicitly_failed(self, tmp_path, monkeypatch):
        """GIVEN a run_manifest.jsonl 'selected' record naming a company that has
        no cache file
        WHEN CH officer coverage is measured
        THEN it is counted as explicitly_failed, not not_attempted -- a logged
        attempt is a terminal, auditable state even when it failed."""
        monkeypatch.setattr(
            "uncorrupt.gates.coverage.ch_officers.procurement_supplier_universe",
            lambda: ["00000003"],
        )
        manifest_line = json.dumps({"phase": "selected", "selected_companies": ["00000003"]})
        (tmp_path / "run_manifest.jsonl").write_text(manifest_line + "\n", encoding="utf-8")

        result = measure_ch_officer_coverage(output_dir=tmp_path)

        assert result.ingested == 0
        assert result.explicitly_failed == 1
        assert result.not_attempted == 0
        assert result.passed is True  # 100% accounted for -- a logged failure still counts

    def test_partial_ingest_fails_the_gate(self, tmp_path, monkeypatch):
        """GIVEN a universe of 3 companies where only 1 has a cache file
        WHEN CH officer coverage is measured
        THEN passed is False -- a partial ingest must never pass, even though
        1/3 clears no meaningful threshold by coincidence."""
        monkeypatch.setattr(
            "uncorrupt.gates.coverage.ch_officers.procurement_supplier_universe",
            lambda: ["A1", "A2", "A3"],
        )
        _write_ch_cache(tmp_path, "A1", officer_count=2)

        result = measure_ch_officer_coverage(output_dir=tmp_path)

        assert result.ingested == 1
        assert result.not_attempted == 2
        assert result.passed is False

    def test_fully_accounted_universe_passes(self, tmp_path, monkeypatch):
        """GIVEN every universe company either has a cache file or is logged as a
        failed attempt
        WHEN CH officer coverage is measured
        THEN passed is True (100% accounted for)."""
        monkeypatch.setattr(
            "uncorrupt.gates.coverage.ch_officers.procurement_supplier_universe",
            lambda: ["B1", "B2"],
        )
        _write_ch_cache(tmp_path, "B1", officer_count=3)
        manifest_line = json.dumps({"phase": "selected", "selected_companies": ["B2"]})
        (tmp_path / "run_manifest.jsonl").write_text(manifest_line + "\n", encoding="utf-8")

        result = measure_ch_officer_coverage(output_dir=tmp_path)

        assert result.ingested == 1
        assert result.explicitly_failed == 1
        assert result.not_attempted == 0
        assert result.passed is True
        assert result.known_limits == ()

    def test_missing_run_manifest_is_flagged_as_a_known_limit(self, tmp_path, monkeypatch):
        """GIVEN an output_dir with no run_manifest.jsonl at all
        WHEN CH officer coverage is measured
        THEN a known_limits entry explains that attempted-but-failed cannot be
        distinguished from never-attempted."""
        monkeypatch.setattr(
            "uncorrupt.gates.coverage.ch_officers.procurement_supplier_universe",
            lambda: ["C1"],
        )

        result = measure_ch_officer_coverage(output_dir=tmp_path)

        assert any("run_manifest.jsonl" in limit for limit in result.known_limits)


# ---------------------------------------------------------------------------
# Commons register ingest completeness
# ---------------------------------------------------------------------------


def _make_declared_interest_edge(source_name: str, reference: str) -> None:
    member = Entity.objects.create(
        entity_type="person",
        name=f"Member {reference}",
        registry_scheme="UK-PARLIAMENT-MEMBER",
        registry_id=reference,
    )
    company = Entity.objects.create(
        entity_type="company",
        name=f"Company {reference}",
        registry_scheme="GB-COH",
        registry_id=reference,
        company_number=reference,
    )
    edge = Edge.objects.create(
        edge_type="declared_interest", source_entity=member, target_entity=company
    )
    Attestation.objects.create(edge=edge, source_name=source_name, source_reference=reference)


@pytest.mark.django_db
class TestMeasureCommonsCoverage:
    def test_ingested_counts_commons_attestations_only(self):
        """GIVEN two Commons-attributed attestations and one Lords-attributed one
        WHEN Commons coverage is measured against a total of 10
        THEN ingested is 2 -- the Lords attestation must never be counted."""
        _make_declared_interest_edge("UK Parliament Register of Interests", "c1")
        _make_declared_interest_edge("UK Parliament Register of Interests", "c2")
        _make_declared_interest_edge("UK House of Lords Register of Interests", "l1")

        result = measure_commons_coverage(total_results=10)

        assert result.ingested == 2
        assert result.not_attempted == 8

    def test_partial_ingest_fails_the_gate(self):
        """GIVEN 25 ingested Commons records against a frozen total of 4057
        WHEN Commons coverage is measured
        THEN passed is False."""
        for i in range(25):
            _make_declared_interest_edge("UK Parliament Register of Interests", f"c{i}")

        result = measure_commons_coverage(total_results=4057)

        assert result.ingested == 25
        assert result.passed is False

    def test_full_ingest_passes_the_gate(self):
        """GIVEN every record of a small frozen total ingested
        WHEN Commons coverage is measured
        THEN passed is True."""
        for i in range(3):
            _make_declared_interest_edge("UK Parliament Register of Interests", f"c{i}")

        result = measure_commons_coverage(total_results=3)

        assert result.ingested == 3
        assert result.passed is True


class TestCommonsDenominatorMatchesTheFetchQueryShape:
    """The defect: the fetch (parliament_interests.fetch_parliament_interests)
    always sends ExpandChildInterests=true, but the coverage gate's own
    totalResults probe did not -- 4,057 (without the flag) vs. the real
    3,415-record corpus (with it). The denominator must be read with the SAME
    query shape the fetch actually issues, and the measurement must record
    which query produced each half of the ratio."""

    def test_fetch_commons_total_results_sends_expand_child_interests_true(self):
        """GIVEN the live Interests API
        WHEN fetch_commons_total_results issues its totalResults request
        THEN the request includes ExpandChildInterests=true -- the exact query
        shape fetch_parliament_interests always sends -- so the denominator is
        apples-to-apples with what can ever be ingested, not a different
        totalResults reading from an unexpanded query."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, json={"totalResults": 3415})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        total = fetch_commons_total_results(client=client)

        assert captured["ExpandChildInterests"] == "true"
        assert total == 3415

    @pytest.mark.django_db
    def test_measure_commons_coverage_records_which_live_query_produced_the_total(
        self, monkeypatch, tmp_path
    ):
        """GIVEN a live (mocked) totalResults fetch and no ingest provenance file
        at the configured path
        WHEN Commons coverage is measured with no total_results override
        THEN extra['total_source'] records that the total came from a live
        query including ExpandChildInterests=true -- so a future reader can
        verify the two halves of the ratio are comparable, not just trust it."""

        def fake_fetch(client=None, max_retries=5, registered_from=None, registered_to=None):
            return 3415

        monkeypatch.setattr("uncorrupt.gates.coverage.fetch_commons_total_results", fake_fetch)

        result = measure_commons_coverage(ingest_provenance_path=tmp_path / "missing.json")

        assert result.total == 3415
        assert "ExpandChildInterests" in result.extra["total_source"]
        assert "live" in result.extra["total_source"].lower()

    @pytest.mark.django_db
    def test_measure_commons_coverage_records_explicit_override_provenance(self):
        """GIVEN total_results is passed explicitly (no live query made)
        WHEN Commons coverage is measured
        THEN extra['total_source'] records that the total was explicitly
        provided rather than live-queried -- never silently indistinguishable
        from a live reading."""
        result = measure_commons_coverage(total_results=10)

        assert "explicitly provided" in result.extra["total_source"].lower()

    @pytest.mark.django_db
    def test_measure_commons_coverage_records_the_ingested_side_of_the_ratio_too(self):
        """GIVEN any Commons coverage measurement
        WHEN it is produced
        THEN extra['ingested_source'] documents how `ingested` was obtained --
        generalising the fix: a coverage ratio must record how BOTH halves
        were obtained, not just the denominator."""
        result = measure_commons_coverage(total_results=10)

        assert "Attestation" in result.extra["ingested_source"]

    @pytest.mark.django_db
    def test_live_measurement_documents_the_unexplained_expand_child_interests_gap(
        self, monkeypatch, tmp_path
    ):
        """GIVEN a live (mocked) measurement
        WHEN Commons coverage is measured with no total_results override
        THEN known_limits documents the ~640-record gap between the
        ExpandChildInterests=true total used here and the unexpanded total --
        a smaller denominator flatters coverage, and parliament_interests.py's
        own docstring calls the gap unexplained; this must be disclosed on
        every live measurement, not discovered by accident."""

        def fake_fetch(client=None, max_retries=5, registered_from=None, registered_to=None):
            return 3415

        monkeypatch.setattr("uncorrupt.gates.coverage.fetch_commons_total_results", fake_fetch)

        result = measure_commons_coverage(ingest_provenance_path=tmp_path / "missing.json")

        assert any("4,057" in limit and "unexplained" in limit for limit in result.known_limits)

    @pytest.mark.django_db
    def test_explicit_override_does_not_claim_the_gap_disclosure(self):
        """GIVEN total_results is passed explicitly (offline path, no live query)
        WHEN Commons coverage is measured
        THEN known_limits does NOT include the live-only ExpandChildInterests
        gap disclosure -- that disclosure describes a live query this call
        never made, and asserting it unconditionally would be misleading."""
        result = measure_commons_coverage(total_results=10)

        assert not any("4,057" in limit for limit in result.known_limits)


class TestCommonsDenominatorReadsTheIngestsOwnDateWindow:
    """Second independent-review follow-up: closing ExpandChildInterests alone
    left the DATE-WINDOW axis open -- a live ingest run with
    --registered-from/--registered-to fetches a windowed subset, but an
    unwindowed totalResults denominator silently assumes the whole corpus is
    comparable. _read_commons_ingest_date_window reads the ingest's own
    provenance.json instead of assuming default fetch parameters."""

    def test_no_provenance_path_configured_reports_unwindowed_and_says_so(self):
        """GIVEN provenance_path=None (not configured at all)
        WHEN the date window is read
        THEN both dates are None and the description explains the denominator
        was queried UNWINDOWED -- documented, not silently assumed comparable."""
        registered_from, registered_to, description = _read_commons_ingest_date_window(None)

        assert registered_from is None
        assert registered_to is None
        assert "unwindowed" in description.lower()

    def test_missing_provenance_file_reports_unwindowed_and_says_so(self, tmp_path):
        """GIVEN a provenance_path that does not exist on disk
        WHEN the date window is read
        THEN both dates are None and the description names the missing path."""
        missing_path = tmp_path / "parliament_interests.provenance.json"

        registered_from, registered_to, description = _read_commons_ingest_date_window(missing_path)

        assert registered_from is None
        assert registered_to is None
        assert str(missing_path) in description
        assert "unwindowed" in description.lower()

    def test_windowed_provenance_is_read_and_applied(self, tmp_path):
        """GIVEN a provenance.json recording a real RegisteredFrom/RegisteredTo
        window (mirrors fetch_parliament_interests's own written format)
        WHEN the date window is read
        THEN the exact from/to dates are returned, and the description confirms
        the denominator will be queried with the SAME window."""
        provenance_path = tmp_path / "parliament_interests.provenance.json"
        provenance_path.write_text(
            json.dumps(
                {
                    "registered_range": {"from": "2019-01-01", "to": "2021-12-31"},
                    "item_count": 130,
                }
            ),
            encoding="utf-8",
        )

        registered_from, registered_to, description = _read_commons_ingest_date_window(
            provenance_path
        )

        assert registered_from == date(2019, 1, 1)
        assert registered_to == date(2021, 12, 31)
        assert "2019-01-01" in description
        assert "2021-12-31" in description

    def test_unwindowed_provenance_is_read_as_unwindowed(self, tmp_path):
        """GIVEN a provenance.json recording a fetch with no date window at all
        (registered_range.from/to both null -- fetch_parliament_interests's own
        format when neither --registered-from nor --registered-to was passed)
        WHEN the date window is read
        THEN both dates are None, and the description distinguishes this from
        the "no provenance found" case -- it explicitly confirms the recorded
        fetch WAS unwindowed, not merely that provenance is missing."""
        provenance_path = tmp_path / "parliament_interests.provenance.json"
        provenance_path.write_text(
            json.dumps({"registered_range": {"from": None, "to": None}, "item_count": 3415}),
            encoding="utf-8",
        )

        registered_from, registered_to, description = _read_commons_ingest_date_window(
            provenance_path
        )

        assert registered_from is None
        assert registered_to is None
        assert "recorded an unwindowed fetch" in description.lower()

    @pytest.mark.django_db
    def test_measure_commons_coverage_windows_the_live_query_when_provenance_exists(
        self, monkeypatch, tmp_path
    ):
        """GIVEN an ingest provenance file recording a windowed fetch, and a live
        totalResults call that records what window it was actually called with
        WHEN Commons coverage is measured with no total_results override
        THEN fetch_commons_total_results is called with the SAME registered_from
        /registered_to the provenance recorded -- the denominator is windowed
        to match the actual ingest, not silently left unwindowed."""
        provenance_path = tmp_path / "parliament_interests.provenance.json"
        provenance_path.write_text(
            json.dumps({"registered_range": {"from": "2019-01-01", "to": "2021-12-31"}}),
            encoding="utf-8",
        )
        captured: dict[str, object] = {}

        def fake_fetch(client=None, max_retries=5, registered_from=None, registered_to=None):
            captured["registered_from"] = registered_from
            captured["registered_to"] = registered_to
            return 200

        monkeypatch.setattr("uncorrupt.gates.coverage.fetch_commons_total_results", fake_fetch)

        result = measure_commons_coverage(ingest_provenance_path=provenance_path)

        assert captured["registered_from"] == date(2019, 1, 1)
        assert captured["registered_to"] == date(2021, 12, 31)
        assert result.total == 200
        assert any("2019-01-01" in limit for limit in result.known_limits)


# ---------------------------------------------------------------------------
# Lords frozen-snapshot coverage
# ---------------------------------------------------------------------------


_MEMBER_CARD_TEMPLATE = """
<div class="card-expandable">
  <a href="/member/{member_id}" class="card card-member">
    <div class="primary-info">{name}</div>
    <div class="secondary-info">Crossbench</div>
    <div class="secondary-info">Life peer</div>
  </a>
  <div class="expand-area">
    <div class="card card-child">
      <div class="primary-info">Category 1: Directorships</div>
      <ul>
        {interest_items}
      </ul>
    </div>
  </div>
</div>
"""


def _build_lords_page_html(member_id: str, name: str, descriptions: list[str]) -> str:
    items = "".join(f"<li>{d}</li>" for d in descriptions)
    return _MEMBER_CARD_TEMPLATE.format(member_id=member_id, name=name, interest_items=items)


def _write_lords_snapshot(snapshot_dir: Path, pages: dict[str, str]) -> None:
    """Write page_NN.html files plus a matching provenance.json with real hashes."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    page_hashes = {}
    for filename, html in pages.items():
        path = snapshot_dir / filename
        path.write_text(html, encoding="utf-8")
        page_hashes[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    (snapshot_dir / "provenance.json").write_text(
        json.dumps({"total_entries": len(pages), "page_hashes_sha256": page_hashes}),
        encoding="utf-8",
    )


class TestVerifyLordsSnapshotIntegrity:
    def test_matching_hashes_report_intact(self, tmp_path):
        """GIVEN a snapshot whose page bytes match provenance.json's recorded hashes
        WHEN integrity is verified
        THEN intact is True."""
        _write_lords_snapshot(tmp_path, {"page_01.html": "<html>hello</html>"})

        integrity = verify_lords_snapshot_integrity(tmp_path)

        assert integrity.intact is True
        assert integrity.mismatched_pages == ()

    def test_tampered_page_is_reported_as_mismatched(self, tmp_path):
        """GIVEN a snapshot page modified after provenance.json was recorded
        WHEN integrity is verified
        THEN intact is False and the page is named in mismatched_pages."""
        _write_lords_snapshot(tmp_path, {"page_01.html": "<html>hello</html>"})
        (tmp_path / "page_01.html").write_text("<html>tampered</html>", encoding="utf-8")

        integrity = verify_lords_snapshot_integrity(tmp_path)

        assert integrity.intact is False
        assert "page_01.html" in integrity.mismatched_pages


@pytest.mark.django_db
class TestMeasureLordsSnapshotCoverage:
    def test_raises_on_failed_integrity_check(self, tmp_path):
        """GIVEN a snapshot with a tampered page
        WHEN Lords snapshot coverage is measured
        THEN it raises rather than silently measuring against unverified bytes."""
        _write_lords_snapshot(tmp_path, {"page_01.html": "<html>hello</html>"})
        (tmp_path / "page_01.html").write_text("<html>tampered</html>", encoding="utf-8")

        with pytest.raises(ValueError, match="integrity"):
            measure_lords_snapshot_coverage(tmp_path)

    def test_member_and_interest_ingested_when_edge_exists(self, tmp_path):
        """GIVEN a snapshot member with one extractable interest, and a graph
        that already carries that member's Entity plus a matching
        declared_interest edge
        WHEN Lords snapshot coverage is measured
        THEN both the member and the interest are counted as ingested."""
        html = _build_lords_page_html(
            "9001", "Lord Testington", ["Director, Acme Widgets Ltd (manufacturing)"]
        )
        _write_lords_snapshot(tmp_path, {"page_01.html": html})

        member = Entity.objects.create(
            entity_type="person",
            name="Lord Testington",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="9001",
        )
        company = Entity.objects.create(
            entity_type="company", name="Acme Widgets Ltd", registry_scheme="GB-COH"
        )
        Edge.objects.create(
            edge_type="declared_interest",
            source_entity=member,
            target_entity=company,
            properties={
                "category": "Category 1: Directorships",
                "description": "Director, Acme Widgets Ltd (manufacturing)",
            },
        )

        result = measure_lords_snapshot_coverage(tmp_path)

        assert result.members.ingested == 1
        assert result.members.total == 1
        assert result.interests.ingested == 1
        assert result.interests.total == 1
        assert result.interests.passed is True

    def test_private_individual_interest_is_structurally_excluded_not_a_failure(self, tmp_path):
        """GIVEN a snapshot interest describing a family/private-individual tie
        (no extractable organisation name)
        WHEN Lords snapshot coverage is measured
        THEN it is counted toward explicitly_failed (a predeclared, terminal
        exclusion), never toward not_attempted."""
        html = _build_lords_page_html(
            "9002", "Lord Private", ["Family member, spouse's part-time employer Ltd (retail)"]
        )
        _write_lords_snapshot(tmp_path, {"page_01.html": html})
        Entity.objects.create(
            entity_type="person",
            name="Lord Private",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="9002",
        )

        result = measure_lords_snapshot_coverage(tmp_path)

        assert result.interests.total == 1
        assert result.interests.explicitly_failed == 1
        assert result.interests.not_attempted == 0

    def test_extractable_interest_with_no_matching_edge_is_not_attempted(self, tmp_path):
        """GIVEN a snapshot interest naming a real organisation, but no matching
        edge exists in the graph at all
        WHEN Lords snapshot coverage is measured
        THEN it is counted as not_attempted and the gate fails."""
        html = _build_lords_page_html(
            "9003", "Lord Ungathered", ["Director, Nowhere Holdings Ltd (holding company)"]
        )
        _write_lords_snapshot(tmp_path, {"page_01.html": html})
        Entity.objects.create(
            entity_type="person",
            name="Lord Ungathered",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="9003",
        )

        result = measure_lords_snapshot_coverage(tmp_path)

        assert result.interests.ingested == 0
        assert result.interests.not_attempted == 1
        assert result.interests.passed is False
