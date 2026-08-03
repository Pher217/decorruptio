"""Tests for historical register-snapshot reconstruction (evidence ladder).

Verifies the core invariants:
- A pre-award register snapshot promotes an edge to PRE_AWARD_OBSERVED
- Absence of a pre-award snapshot never demotes an edge — no "refuted" level
- ATEMPORAL_CORROBORATION (level 3) never counts as pre-award admissible
- The ladder levels are mutually exclusive and correctly ordered
- Multiple historical snapshots of the same interest produce distinct,
  correctly-dated attestations (not one collapsed record)
- The Wayback CDX API and Interests API calls are fully mocked — no network
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.graph.register_snapshots import (
    EvidenceLevel,
    WaybackCapture,
    describe_page_coverage_bias,
    edge_evidence_level,
    find_all_paths,
    ingest_lords_snapshot,
    is_pre_award_admissible,
    nearest_capture_before,
    parliament_registration_date_coverage,
    path_evidence_level,
    query_wayback_cdx,
    relationship_evidence_level,
    snapshot_evidence_pages,
    wilson_interval,
)
from uncorrupt.staging.companies_house import _normalise_name
from uncorrupt.staging.models import Company

SAMPLE_HTML = """\
<html><body>
<div class="results">
  <div class="card-expandable">
    <a class="card card-member" href="/member/3898/contact">
      <div class="card-inner"><div class="content">
        <div class="primary-info">Lord Aberdare</div>
        <div class="secondary-info">Crossbench</div>
        <div class="secondary-info">Excepted Hereditary</div>
      </div></div>
    </a>
    <div class="expand-area"><div class="expand-area-content">
      <div class="card card-child">
        <div class="card-inner"><div class="content">
          <div class="primary-info">Category 1: Directorships</div>
          <ul><li>Chairman, Microlink PC (UK) Ltd (computing and software)</li></ul>
        </div></div>
      </div>
    </div></div>
  </div>
</div>
</body></html>
"""


def _make_capture(timestamp: str, digest: str = "DIGESTABC") -> WaybackCapture:
    return WaybackCapture(
        timestamp=timestamp,
        original_url="https://members.parliament.uk/members/lords/interests/register-of-lords-interests",
        mimetype="text/html",
        statuscode="200",
        digest=digest,
        length=1000,
    )


# ---------------------------------------------------------------------------
# WaybackCapture
# ---------------------------------------------------------------------------


class TestWaybackCapture:
    def test_captured_at_parses_wayback_timestamp(self):
        """A 14-digit Wayback timestamp parses to the correct UTC datetime."""
        capture = _make_capture("20200617183732")
        assert capture.captured_at == datetime(2020, 6, 17, 18, 37, 32, tzinfo=UTC)

    def test_wayback_url_embeds_timestamp_and_original(self):
        """wayback_url is the standard archive.org replay URL."""
        capture = _make_capture("20200617183732")
        assert capture.wayback_url == (
            "https://web.archive.org/web/20200617183732/"
            "https://members.parliament.uk/members/lords/interests/register-of-lords-interests"
        )


# ---------------------------------------------------------------------------
# Wayback CDX API
# ---------------------------------------------------------------------------


class TestQueryWaybackCdx:
    def test_parses_captures_skipping_header_row(self):
        """The first CDX row is a column header, not a capture, and is skipped."""

        def handler(request: httpx.Request) -> httpx.Response:
            rows = [
                ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
                [
                    "uk,parliament,members)/...",
                    "20200617183732",
                    "https://members.parliament.uk/x",
                    "text/html",
                    "200",
                    "ABC123",
                    "23077",
                ],
            ]
            return httpx.Response(200, json=rows)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        captures = query_wayback_cdx("members.parliament.uk/x", client=client)

        assert len(captures) == 1
        assert captures[0].timestamp == "20200617183732"
        assert captures[0].digest == "ABC123"
        assert captures[0].length == 23077

    def test_empty_result_returns_empty_list_not_error(self):
        """No captures at all is a valid, non-error result (archive coverage gap)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        captures = query_wayback_cdx("members.parliament.uk/nonexistent", client=client)

        assert captures == []

    def test_collapse_digest_param_sent(self):
        """The CDX query requests digest-collapsed results (distinct editions)."""
        captured_urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_urls.append(str(request.url))
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        query_wayback_cdx("members.parliament.uk/x", client=client)

        assert "collapse=digest" in captured_urls[0]

    def test_backs_off_on_429_then_succeeds(self, monkeypatch):
        """A 429 response triggers a retry rather than an immediate failure."""
        import uncorrupt.graph.register_snapshots as rs_module

        monkeypatch.setattr(rs_module.time, "sleep", lambda _seconds: None)
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429)
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        captures = query_wayback_cdx("members.parliament.uk/x", client=client)

        assert attempts == 2
        assert captures == []


class TestNearestCaptureBefore:
    def test_picks_latest_capture_strictly_before_target(self):
        """Among several captures before the target, the latest one is chosen."""
        captures = [
            _make_capture("20200617183732"),
            _make_capture("20200804143539"),
            _make_capture("20210401143530"),  # after target — excluded
        ]
        result = nearest_capture_before(captures, date(2020, 9, 1))
        assert result is not None
        assert result.timestamp == "20200804143539"

    def test_no_capture_before_target_returns_none(self):
        """No capture before the target date returns None — an archive-coverage
        gap, not evidence the relationship didn't exist."""
        captures = [_make_capture("20210401143530")]
        result = nearest_capture_before(captures, date(2020, 1, 1))
        assert result is None

    def test_capture_exactly_on_target_date_is_excluded(self):
        """The boundary is strict: a capture ON the target date does not count
        as 'before' it."""
        captures = [_make_capture("20200301000000")]
        result = nearest_capture_before(captures, date(2020, 3, 1))
        assert result is None


# ---------------------------------------------------------------------------
# Lords snapshot ingest
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIngestLordsSnapshot:
    def test_creates_edge_and_snapshot_attestation(self, tmp_path):
        """A historical snapshot creates an Edge and an Attestation whose
        observed_at is the CAPTURE date, not the current wall-clock time."""
        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        (tmp_path / "page_01.html").write_text(SAMPLE_HTML, encoding="utf-8")
        capture = _make_capture("20200617183732")

        summary = ingest_lords_snapshot(tmp_path, capture, content_hash="deadbeef" * 8)

        assert summary["new_attestations"] == 1
        lord = Entity.objects.get(registry_id="3898")
        edge = Edge.objects.get(source_entity=lord, edge_type="declared_interest")
        att = edge.attestations.get(source_reference__endswith=":20200617183732:p01")
        assert att.observed_at == datetime(2020, 6, 17, 18, 37, 32, tzinfo=UTC)
        assert att.snapshot_ref == "deadbeef" * 8
        assert "Wayback archive snapshot" in att.source_name

    def test_ambiguous_company_number_is_counted_not_crashed(self, tmp_path):
        """A company_number with 2+ pre-existing Entity rows (a real,
        intentional condition confirmed live in production — commit
        a8355c0, "MULTIPLE LEIS PER COMPANY": distinct claims that must not
        be merged) makes `_resolve_counterparty`'s
        `Entity.objects.get_or_create(entity_type="company",
        company_number=...)` raise MultipleObjectsReturned. One ambiguous
        interest must not crash the whole snapshot ingest — it is counted
        and skipped, and does not prevent OTHER interests from being
        ingested."""
        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        # Two pre-existing Entity rows sharing a company_number but
        # different registry_scheme — exactly the real production state
        # that raised MultipleObjectsReturned.
        Entity.objects.create(
            entity_type="company",
            company_number="01234567",
            registry_scheme="GB-COH",
            registry_id="01234567",
            name="Microlink PC (UK) Ltd",
        )
        Entity.objects.create(
            entity_type="company",
            company_number="01234567",
            registry_scheme="GLEIF",
            registry_id="some-lei",
            name="Microlink PC (UK) Ltd",
        )
        (tmp_path / "page_01.html").write_text(SAMPLE_HTML, encoding="utf-8")
        capture = _make_capture("20200617183732")

        summary = ingest_lords_snapshot(tmp_path, capture, content_hash="hash")

        # Root fix supersedes this workaround: lords_interests._canonical_company_entity
        # now resolves on registry_scheme="GB-COH", so a coexisting GLEIF Entity with
        # the same company_number no longer raises MultipleObjectsReturned. The
        # defensive counter stays 0 and the interest resolves to the GB-COH Entity --
        # neither Entity is merged or altered (ADR-006, duplicate over merge).
        assert summary["ambiguous_company_number"] == 0
        assert summary["new_attestations"] == 1
        assert summary["total_interests"] == 1
        assert Entity.objects.filter(company_number="01234567").count() == 2

    def test_one_ambiguous_interest_does_not_roll_back_other_interests(self, tmp_path):
        """A MultipleObjectsReturned on one member's interest must not roll
        back a DIFFERENT member's interest ingested in the same snapshot —
        each interest commits independently."""
        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        Entity.objects.create(
            entity_type="company",
            company_number="01234567",
            registry_scheme="GB-COH",
            registry_id="01234567",
            name="Microlink PC (UK) Ltd",
        )
        Entity.objects.create(
            entity_type="company",
            company_number="01234567",
            registry_scheme="GLEIF",
            registry_id="some-lei",
            name="Microlink PC (UK) Ltd",
        )
        html_with_second_member = SAMPLE_HTML.replace(
            "</body></html>",
            """
  <div class="card-expandable">
    <a class="card card-member" href="/member/9999/contact">
      <div class="card-inner"><div class="content">
        <div class="primary-info">Lord Second</div>
        <div class="secondary-info">Crossbench</div>
        <div class="secondary-info">Life peer</div>
      </div></div>
    </a>
    <div class="expand-area"><div class="expand-area-content">
      <div class="card card-child">
        <div class="card-inner"><div class="content">
          <div class="primary-info">Category 1: Directorships</div>
          <ul><li>Director, Untangled Holdings Ltd (finance)</li></ul>
        </div></div>
      </div>
    </div></div>
  </div>
</body></html>""",
        )
        (tmp_path / "page_01.html").write_text(html_with_second_member, encoding="utf-8")
        capture = _make_capture("20200617183732")

        summary = ingest_lords_snapshot(tmp_path, capture, content_hash="hash")

        # The root fix in lords_interests._canonical_company_entity resolves on
        # registry_scheme="GB-COH", so a coexisting GLEIF Entity with the same
        # company_number no longer raises MultipleObjectsReturned. The defensive
        # counter in ingest_lords_snapshot therefore stays at 0 -- ambiguity is
        # PREVENTED at the root, not caught downstream. Both interests resolve.
        assert summary["ambiguous_company_number"] == 0
        assert summary["new_attestations"] == 2
        second_lord = Entity.objects.get(registry_id="9999")
        assert Edge.objects.filter(
            source_entity=second_lord, edge_type="declared_interest"
        ).exists()

    def test_reingesting_same_capture_is_idempotent(self, tmp_path):
        """Re-running the same snapshot does not duplicate the attestation."""
        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        (tmp_path / "page_01.html").write_text(SAMPLE_HTML, encoding="utf-8")
        capture = _make_capture("20200617183732")

        ingest_lords_snapshot(tmp_path, capture, content_hash="hash1")
        summary = ingest_lords_snapshot(tmp_path, capture, content_hash="hash1")

        assert summary["new_attestations"] == 0
        assert summary["existing_attestations"] == 1
        lord = Entity.objects.get(registry_id="3898")
        edge = Edge.objects.get(source_entity=lord, edge_type="declared_interest")
        assert edge.attestations.count() == 1

    def test_two_different_captures_produce_two_distinct_attestations(self, tmp_path):
        """Two historical snapshots of the SAME still-registered interest
        produce two SEPARATE, correctly-dated attestations — not one
        collapsed record (the defect this module exists to avoid)."""
        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        (dir_a / "page_01.html").write_text(SAMPLE_HTML, encoding="utf-8")
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        (dir_b / "page_01.html").write_text(SAMPLE_HTML, encoding="utf-8")

        capture_a = _make_capture("20200617183732")
        capture_b = _make_capture("20210401143530")

        ingest_lords_snapshot(dir_a, capture_a, content_hash="hash_a")
        ingest_lords_snapshot(dir_b, capture_b, content_hash="hash_b")

        lord = Entity.objects.get(registry_id="3898")
        edge = Edge.objects.get(source_entity=lord, edge_type="declared_interest")
        assert edge.attestations.count() == 2
        observed_dates = sorted(a.observed_at for a in edge.attestations.all())
        assert observed_dates == [
            datetime(2020, 6, 17, 18, 37, 32, tzinfo=UTC),
            datetime(2021, 4, 1, 14, 35, 30, tzinfo=UTC),
        ]

    def test_does_not_create_duplicate_edge_across_snapshots(self, tmp_path):
        """Both snapshots resolve to the SAME Edge (get_or_create on identical
        claim fields) — evidence accumulates on one claim, it doesn't fork it."""
        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        (dir_a / "page_01.html").write_text(SAMPLE_HTML, encoding="utf-8")
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        (dir_b / "page_01.html").write_text(SAMPLE_HTML, encoding="utf-8")

        ingest_lords_snapshot(dir_a, _make_capture("20200617183732"), content_hash="hash_a")
        ingest_lords_snapshot(dir_b, _make_capture("20210401143530"), content_hash="hash_b")

        lord = Entity.objects.get(registry_id="3898")
        assert Edge.objects.filter(source_entity=lord, edge_type="declared_interest").count() == 1


# ---------------------------------------------------------------------------
# Alphabetical-coverage bias
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSnapshotEvidencePages:
    def test_reports_the_page_a_snapshot_was_ingested_from(self, tmp_path):
        """A snapshot ingested from page_03.html records page 3 on the edge."""
        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        (tmp_path / "page_03.html").write_text(SAMPLE_HTML, encoding="utf-8")
        capture = _make_capture("20200617183732")

        ingest_lords_snapshot(tmp_path, capture, content_hash="hash_a")

        lord = Entity.objects.get(registry_id="3898")
        edge = Edge.objects.get(source_entity=lord, edge_type="declared_interest")
        assert snapshot_evidence_pages(edge) == [3]

    def test_excludes_attestations_on_or_after_award_date(self, tmp_path):
        """A snapshot observed on/after the award date is not pre-award
        evidence and is excluded when `award_date` is supplied."""
        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        (tmp_path / "page_01.html").write_text(SAMPLE_HTML, encoding="utf-8")
        # Capture dated AFTER the award date used below.
        capture = _make_capture("20210401143530")

        ingest_lords_snapshot(tmp_path, capture, content_hash="hash_a")

        lord = Entity.objects.get(registry_id="3898")
        edge = Edge.objects.get(source_entity=lord, edge_type="declared_interest")
        assert snapshot_evidence_pages(edge, award_date=date(2020, 3, 1)) == []

    def test_edge_with_no_snapshot_attestations_returns_empty_list(self):
        """An edge with no snapshot evidence at all reports no pages — not an
        error, just nothing to report."""
        person = Entity.objects.create(entity_type="person", name="Test")
        company = Entity.objects.create(entity_type="company", name="Test Ltd")
        edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )

        assert snapshot_evidence_pages(edge) == []


class TestDescribePageCoverageBias:
    def test_reports_capture_count_per_requested_page(self):
        """Each requested page number gets its own CDX-derived capture count —
        used to show the register's archival density falls off with depth."""
        responses = {
            "1": 3,
            "2": 1,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            requested_url = request.url.params.get("url", "")
            count = responses["2"] if "page=2" in requested_url else responses["1"]
            rows = [
                ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
            ]
            rows += [
                ["k", f"2020010{i}000000", "https://x", "text/html", "200", f"D{i}", "100"]
                for i in range(count)
            ]
            return httpx.Response(200, json=rows)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = describe_page_coverage_bias([1, 2], client=client)

        assert result == {1: 3, 2: 1}


# ---------------------------------------------------------------------------
# Parliament Interests API — investigated as a shortcut
# ---------------------------------------------------------------------------


class TestParliamentRegistrationDateCoverage:
    def test_flags_migration_artifact_published_dates(self):
        """publishedDate clustered at/after 2024-03-18 (the platform's own
        earliest published register) is reported as a migration artifact,
        not real edition history, even when registrationDate is years earlier."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": 1,
                            "registrationDate": "2020-01-08",
                            "publishedDate": "2024-07-31",
                            "updatedDates": ["2020-04-14"],
                        },
                        {
                            "id": 2,
                            "registrationDate": None,
                            "publishedDate": "2024-03-18",
                            "updatedDates": [],
                        },
                    ]
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = parliament_registration_date_coverage(
            date(2019, 1, 1), date(2020, 12, 31), client=client
        )

        assert result["total"] == 2
        assert result["with_registration_date"] == 1
        assert result["without_registration_date"] == 1
        assert result["with_nonempty_updated_dates"] == 1
        assert result["published_date_looks_like_migration_artifact"] == 2


# ---------------------------------------------------------------------------
# The evidence ladder
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEdgeEvidenceLevel:
    def _make_edge(self) -> tuple[Entity, Entity, Edge]:
        person = Entity.objects.create(entity_type="person", name="Baroness Test")
        company = Entity.objects.create(entity_type="company", name="Test Ltd")
        edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )
        return person, company, edge

    def test_event_dated_edge_is_level_1(self):
        """A dated edge with valid_from <= award_date is EVENT_DATED,
        regardless of any attestations."""
        _, _, edge = self._make_edge()
        edge.valid_from = date(2019, 1, 1)
        edge.save()

        level = edge_evidence_level(edge, date(2020, 3, 1))

        assert level == EvidenceLevel.EVENT_DATED

    def test_pre_award_snapshot_promotes_to_level_2(self):
        """An undated edge with a snapshot attestation observed BEFORE the
        award date is promoted to PRE_AWARD_OBSERVED."""
        _, _, edge = self._make_edge()
        Attestation.objects.create(
            edge=edge,
            source_name="UK House of Lords Register of Interests (Wayback archive snapshot)",
            source_reference="ref:20190601000000",
            observed_at=datetime(2019, 6, 1, tzinfo=UTC),
            snapshot_ref="somehash",
        )

        level = edge_evidence_level(edge, date(2020, 3, 1))

        assert level == EvidenceLevel.PRE_AWARD_OBSERVED

    def test_post_award_attestation_does_not_promote(self):
        """An attestation observed AFTER (or on) the award date does not
        count as pre-award evidence — it stays at level 3."""
        _, _, edge = self._make_edge()
        Attestation.objects.create(
            edge=edge,
            source_name="UK House of Lords Register of Interests",
            source_reference="ref:live",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            snapshot_ref="livehash",
        )

        level = edge_evidence_level(edge, date(2020, 3, 1))

        assert level == EvidenceLevel.ATEMPORAL_CORROBORATION

    def test_no_snapshot_available_does_not_demote_to_refuted(self):
        """An edge with no snapshot evidence at all — because none was
        available, not because the relationship was checked and found
        absent — stays at ATEMPORAL_CORROBORATION. There is no 'refuted'
        level; absence of a pre-award snapshot is never scored as negative."""
        _, _, edge = self._make_edge()

        level = edge_evidence_level(edge, date(2020, 3, 1))

        assert level == EvidenceLevel.ATEMPORAL_CORROBORATION
        assert level != EvidenceLevel.NO_TRACE

    def test_attestation_without_snapshot_ref_does_not_promote(self):
        """A live-register attestation (no snapshot_ref) never counts as
        pre-award evidence, even if its observed_at happens to be early —
        only a genuine snapshot (snapshot_ref set) can promote to level 2."""
        _, _, edge = self._make_edge()
        Attestation.objects.create(
            edge=edge,
            source_name="UK House of Lords Register of Interests",
            source_reference="ref:no-snapshot",
            observed_at=datetime(2019, 1, 1, tzinfo=UTC),
            snapshot_ref=None,
        )

        level = edge_evidence_level(edge, date(2020, 3, 1))

        assert level == EvidenceLevel.ATEMPORAL_CORROBORATION


class TestEvidenceLevelOrdering:
    def test_levels_are_ordered_strongest_first(self):
        """The ladder is ordered EVENT_DATED < PRE_AWARD_OBSERVED <
        ATEMPORAL_CORROBORATION < NO_TRACE (lower = stronger evidence)."""
        assert (
            EvidenceLevel.EVENT_DATED
            < EvidenceLevel.PRE_AWARD_OBSERVED
            < EvidenceLevel.ATEMPORAL_CORROBORATION
            < EvidenceLevel.NO_TRACE
        )

    def test_exactly_four_levels_exist(self):
        """The ladder has exactly four mutually exclusive levels — no fifth
        'refuted' level exists."""
        assert {level.name for level in EvidenceLevel} == {
            "EVENT_DATED",
            "PRE_AWARD_OBSERVED",
            "ATEMPORAL_CORROBORATION",
            "NO_TRACE",
        }

    def test_is_pre_award_admissible_true_only_for_levels_1_and_2(self):
        """Only EVENT_DATED and PRE_AWARD_OBSERVED satisfy the strict
        pre-award endpoint. ATEMPORAL_CORROBORATION must never satisfy it —
        it is an investigative lead, not evidence of a pre-award conflict."""
        assert is_pre_award_admissible(EvidenceLevel.EVENT_DATED) is True
        assert is_pre_award_admissible(EvidenceLevel.PRE_AWARD_OBSERVED) is True
        assert is_pre_award_admissible(EvidenceLevel.ATEMPORAL_CORROBORATION) is False
        assert is_pre_award_admissible(EvidenceLevel.NO_TRACE) is False


@pytest.mark.django_db
class TestPathEvidenceLevel:
    def test_path_takes_the_weakest_edge(self):
        """A path is only as strong as its weakest temporally-meaningful edge."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="company", name="B")
        c = Entity.objects.create(entity_type="company", name="C")
        dated_edge = Edge.objects.create(
            edge_type="officer_of", source_entity=a, target_entity=b, valid_from=date(2015, 1, 1)
        )
        undated_edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=b, target_entity=c
        )

        level = path_evidence_level([dated_edge, undated_edge], date(2020, 3, 1))

        assert level == EvidenceLevel.ATEMPORAL_CORROBORATION

    def test_same_as_edge_is_excluded_from_weakening(self):
        """A same_as identity hop carries no temporal claim and cannot drag a
        path's evidence level down."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="person", name="A (CH record)")
        c = Entity.objects.create(entity_type="company", name="C")
        same_as_edge = Edge.objects.create(edge_type="same_as", source_entity=a, target_entity=b)
        dated_edge = Edge.objects.create(
            edge_type="officer_of", source_entity=b, target_entity=c, valid_from=date(2015, 1, 1)
        )

        level = path_evidence_level([same_as_edge, dated_edge], date(2020, 3, 1))

        assert level == EvidenceLevel.EVENT_DATED

    def test_path_of_only_same_as_edges_is_strongest_level(self):
        """A path made entirely of identity hops has nothing to weaken it."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="person", name="B")
        same_as_edge = Edge.objects.create(edge_type="same_as", source_entity=a, target_entity=b)

        level = path_evidence_level([same_as_edge], date(2020, 3, 1))

        assert level == EvidenceLevel.EVENT_DATED


@pytest.mark.django_db
class TestRelationshipEvidenceLevel:
    def test_no_path_is_no_trace(self):
        """Two unconnected entities classify as NO_TRACE — the only level
        that means 'no record of the relationship at all'."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="company", name="B")

        level = relationship_evidence_level({a.id}, b.id, {}, 2, date(2020, 3, 1))

        assert level == EvidenceLevel.NO_TRACE

    def test_best_path_wins_when_multiple_paths_exist(self):
        """When several paths connect the same two entities, the STRONGEST
        (lowest-numbered) evidence level found is reported."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="company", name="B")
        mid = Entity.objects.create(entity_type="company", name="Mid")

        weak_edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=a, target_entity=b
        )
        hop1 = Edge.objects.create(
            edge_type="officer_of", source_entity=a, target_entity=mid, valid_from=date(2010, 1, 1)
        )
        hop2 = Edge.objects.create(
            edge_type="supplier_of", source_entity=mid, target_entity=b, valid_from=date(2011, 1, 1)
        )

        adj = {
            a.id: [weak_edge, hop1],
            b.id: [weak_edge, hop2],
            mid.id: [hop1, hop2],
        }
        paths = find_all_paths({a.id}, b.id, adj, 2)
        assert len(paths) == 2  # sanity: both the direct and 2-hop path are found

        level = relationship_evidence_level({a.id}, b.id, adj, 2, date(2020, 3, 1))

        assert level == EvidenceLevel.EVENT_DATED


class TestWilsonInterval:
    def test_zero_successes_is_not_reported_as_bare_zero(self):
        """0/200 has a non-zero Wilson upper bound — never a bare 0%."""
        lower, upper = wilson_interval(0, 200)
        assert lower == 0.0
        assert upper > 0.0
        assert upper == pytest.approx(0.0188, abs=0.001)

    def test_zero_n_returns_zero_interval(self):
        """An empty sample has a degenerate (0, 0) interval rather than a
        division-by-zero error."""
        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_interval_bounds_are_within_unit_range(self):
        """Lower and upper bounds are always valid probabilities."""
        lower, upper = wilson_interval(15, 30)
        assert 0.0 <= lower <= upper <= 1.0
