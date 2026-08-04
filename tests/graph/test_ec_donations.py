"""Tests for the Electoral Commission donations ingest (Phase 1.2).

Verifies the core invariants:
- Company-number join produces match_confidence=1.0 (zero name matching)
- Ambiguous donor name (2+ companies) never guesses — no edge created
- amount_cents is parsed as an integer, never a float
- Donation date (ReceivedDate) maps to Edge.valid_from
- Individual donors are never turned into Entities (ADR-004 D1)
- fetch_ec_donations_csv/ingest_ec_donations_csv refuse to run without a
  sources/uk_ec_donations.yml register entry
"""

import csv
from datetime import date
from pathlib import Path

import httpx
import pytest

import uncorrupt.graph.ec_donations as ec_donations_module
from uncorrupt.core.errors import RegisterError
from uncorrupt.graph.ec_donations import fetch_ec_donations_csv, ingest_ec_donations_csv
from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.staging.models import Company

CSV_HEADER = [
    "ECRef",
    "RegulatedEntityName",
    "RegulatedEntityType",
    "Value",
    "AcceptedDate",
    "DonorName",
    "DonorStatus",
    "CompanyRegistrationNumber",
    "ReceivedDate",
    "RegulatedEntityId",
]


def _write_csv(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    csv_path = tmp_path / "ec_donations.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return csv_path


@pytest.mark.django_db
class TestEcDonationsIngest:
    def test_company_number_join_produces_full_confidence(self, tmp_path):
        """A donor row with a CompanyRegistrationNumber joins with zero name matching."""
        Company.objects.create(
            company_number="12410514",
            company_name="PPE Medpro Ltd",
            normalised_name="PPE MEDPRO LTD",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501001",
                    "RegulatedEntityName": "Conservative and Unionist Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£10,000.00",
                    "AcceptedDate": "17/01/2020",
                    "DonorName": "PPE Medpro Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "12410514",
                    "ReceivedDate": "02/01/2020",
                    "RegulatedEntityId": "52",
                }
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        assert summary["matched"] == 1
        attestation = Attestation.objects.get(source_reference="C0501001")
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"

    def test_unpadded_company_number_still_resolves(self, tmp_path):
        """An unpadded CompanyRegistrationNumber (as EC supplies it) still resolves to
        the zero-padded Company row (as CH stores it) — the padding-bug regression test."""
        Company.objects.create(
            company_number="07015428",
            company_name="Example Donor Ltd",
            normalised_name="EXAMPLE DONOR LTD",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501020",
                    "RegulatedEntityName": "Conservative and Unionist Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£7,500.00",
                    "AcceptedDate": "17/01/2020",
                    "DonorName": "Example Donor Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "7015428",
                    "ReceivedDate": "02/01/2020",
                    "RegulatedEntityId": "52",
                }
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        assert summary["matched"] == 1
        attestation = Attestation.objects.get(source_reference="C0501020")
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"
        assert attestation.edge.source_entity.company_number == "07015428"

    def test_ambiguous_donor_name_creates_no_edge(self, tmp_path):
        """Two companies sharing a normalised name must never be guessed (uniqueness guard)."""
        Company.objects.create(
            company_number="00000001", company_name="Example Ltd", normalised_name="EXAMPLE LTD"
        )
        Company.objects.create(
            company_number="00000002", company_name="Example Ltd", normalised_name="EXAMPLE LTD"
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501002",
                    "RegulatedEntityName": "Labour Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£500.00",
                    "AcceptedDate": "10/01/2020",
                    "DonorName": "Example Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "",
                    "ReceivedDate": "05/01/2020",
                    "RegulatedEntityId": "53",
                }
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        assert summary["matched"] == 0
        assert summary["unmatched_donor"] == 1
        assert Attestation.objects.filter(source_reference="C0501002").count() == 0

    def test_amount_cents_is_an_integer(self, tmp_path):
        """Money must be integer cents — never a float — regardless of the CSV formatting."""
        Company.objects.create(
            company_number="00000003",
            company_name="Donor Co Ltd",
            normalised_name="DONOR CO LTD",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501003",
                    "RegulatedEntityName": "Liberal Democrats",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£1,234.56",
                    "AcceptedDate": "10/01/2020",
                    "DonorName": "Donor Co Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000003",
                    "ReceivedDate": "05/01/2020",
                    "RegulatedEntityId": "54",
                }
            ],
        )

        ingest_ec_donations_csv(csv_path)

        attestation = Attestation.objects.get(source_reference="C0501003")
        edge = attestation.edge
        assert edge.amount_cents == 123456
        assert isinstance(edge.amount_cents, int)
        assert edge.currency == "GBP"

    def test_donation_date_maps_to_valid_from(self, tmp_path):
        """ReceivedDate (when the money changed hands) becomes Edge.valid_from."""
        Company.objects.create(
            company_number="00000004",
            company_name="Timing Co Ltd",
            normalised_name="TIMING CO LTD",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501004",
                    "RegulatedEntityName": "Conservative and Unionist Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£2,000.00",
                    "AcceptedDate": "20/03/2020",
                    "DonorName": "Timing Co Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000004",
                    "ReceivedDate": "01/03/2020",
                    "RegulatedEntityId": "52",
                }
            ],
        )

        ingest_ec_donations_csv(csv_path)

        attestation = Attestation.objects.get(source_reference="C0501004")
        edge = attestation.edge
        assert edge.valid_from.isoformat() == "2020-03-01"

    def test_idless_recipients_with_different_names_stay_distinct(self, tmp_path):
        """Two different regulated entities with no RegulatedEntityId must never merge."""
        Company.objects.create(
            company_number="00000010",
            company_name="Donor One Ltd",
            normalised_name="DONOR ONE LTD",
        )
        Company.objects.create(
            company_number="00000011",
            company_name="Donor Two Ltd",
            normalised_name="DONOR TWO LTD",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501010",
                    "RegulatedEntityName": "Members Association Alpha",
                    "RegulatedEntityType": "Members association",
                    "Value": "£1,000.00",
                    "AcceptedDate": "10/01/2020",
                    "DonorName": "Donor One Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000010",
                    "ReceivedDate": "05/01/2020",
                    "RegulatedEntityId": "",
                },
                {
                    "ECRef": "C0501011",
                    "RegulatedEntityName": "Members Association Beta",
                    "RegulatedEntityType": "Members association",
                    "Value": "£2,000.00",
                    "AcceptedDate": "11/01/2020",
                    "DonorName": "Donor Two Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000011",
                    "ReceivedDate": "06/01/2020",
                    "RegulatedEntityId": "",
                },
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        assert summary["matched"] == 2
        alpha = Entity.objects.get(name="Members Association Alpha")
        beta = Entity.objects.get(name="Members Association Beta")
        assert alpha.pk != beta.pk

    def test_idless_recipient_with_no_name_is_skipped(self, tmp_path):
        """No RegulatedEntityId and no RegulatedEntityName: skip, never create an anonymous node."""
        Company.objects.create(
            company_number="00000012",
            company_name="Donor Three Ltd",
            normalised_name="DONOR THREE LTD",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501012",
                    "RegulatedEntityName": "",
                    "RegulatedEntityType": "Members association",
                    "Value": "£1,000.00",
                    "AcceptedDate": "10/01/2020",
                    "DonorName": "Donor Three Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000012",
                    "ReceivedDate": "05/01/2020",
                    "RegulatedEntityId": "",
                }
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        assert summary["matched"] == 0
        assert summary["skipped_no_recipient_name"] == 1
        assert Attestation.objects.filter(source_reference="C0501012").count() == 0

    def test_unparseable_received_date_does_not_fall_back_to_accepted_date(self, tmp_path):
        """A malformed (non-blank) ReceivedDate must not silently become AcceptedDate."""
        Company.objects.create(
            company_number="00000005",
            company_name="Bad Date Co Ltd",
            normalised_name="BAD DATE CO LTD",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501006",
                    "RegulatedEntityName": "Conservative and Unionist Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£1,000.00",
                    "AcceptedDate": "20/03/2020",
                    "DonorName": "Bad Date Co Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000005",
                    "ReceivedDate": "not-a-date",
                    "RegulatedEntityId": "52",
                }
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        assert summary["invalid_received_date"] == 1
        attestation = Attestation.objects.get(source_reference="C0501006")
        edge = attestation.edge
        assert edge.valid_from is None

    def test_blank_received_date_falls_back_to_accepted_date(self, tmp_path):
        """A blank ReceivedDate is the only case that falls back to AcceptedDate."""
        Company.objects.create(
            company_number="00000006",
            company_name="Blank Date Co Ltd",
            normalised_name="BLANK DATE CO LTD",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501007",
                    "RegulatedEntityName": "Conservative and Unionist Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£1,000.00",
                    "AcceptedDate": "20/03/2020",
                    "DonorName": "Blank Date Co Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000006",
                    "ReceivedDate": "",
                    "RegulatedEntityId": "52",
                }
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        assert summary["invalid_received_date"] == 0
        attestation = Attestation.objects.get(source_reference="C0501007")
        edge = attestation.edge
        assert edge.valid_from.isoformat() == "2020-03-20"

    def test_reingest_updates_stale_amount_instead_of_duplicating(self, tmp_path):
        """Re-running ingest on a corrected CSV must refresh the edge, not duplicate it."""
        Company.objects.create(
            company_number="00000007",
            company_name="Rerun Co Ltd",
            normalised_name="RERUN CO LTD",
        )
        row = {
            "ECRef": "C0501008",
            "RegulatedEntityName": "Conservative and Unionist Party",
            "RegulatedEntityType": "Political Party",
            "Value": "£1,000.00",
            "AcceptedDate": "20/03/2020",
            "DonorName": "Rerun Co Ltd",
            "DonorStatus": "Company",
            "CompanyRegistrationNumber": "00000007",
            "ReceivedDate": "05/03/2020",
            "RegulatedEntityId": "52",
        }
        csv_path = _write_csv(tmp_path, [row])
        summary_1 = ingest_ec_donations_csv(csv_path)
        assert summary_1["matched"] == 1

        corrected_row = dict(row, Value="£1,500.00")
        csv_path_2 = _write_csv(tmp_path, [corrected_row])
        summary_2 = ingest_ec_donations_csv(csv_path_2)

        assert summary_2["matched"] == 0  # nothing newly created, only refreshed
        assert Attestation.objects.filter(source_reference="C0501008").count() == 1
        attestation = Attestation.objects.get(source_reference="C0501008")
        edge = attestation.edge
        assert edge.amount_cents == 150000

    def test_distinct_donations_without_ecref_stay_distinct(self, tmp_path):
        """Two distinct donations lacking an ECRef must not collapse into one edge."""
        Company.objects.create(
            company_number="00000008",
            company_name="No Ref Co Ltd",
            normalised_name="NO REF CO LTD",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "",
                    "RegulatedEntityName": "Conservative and Unionist Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£1,000.00",
                    "AcceptedDate": "20/03/2020",
                    "DonorName": "No Ref Co Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000008",
                    "ReceivedDate": "05/03/2020",
                    "RegulatedEntityId": "52",
                },
                {
                    "ECRef": "",
                    "RegulatedEntityName": "Conservative and Unionist Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£2,000.00",
                    "AcceptedDate": "21/06/2020",
                    "DonorName": "No Ref Co Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000008",
                    "ReceivedDate": "06/06/2020",
                    "RegulatedEntityId": "52",
                },
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        assert summary["matched"] == 2
        assert Edge.objects.filter(amount_cents__in=[100000, 200000]).count() == 2

    def test_individual_donor_creates_no_entity_or_edge(self, tmp_path):
        """Individual donors are out of scope (ADR-004 D1) — no Entity/Edge created."""
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501005",
                    "RegulatedEntityName": "Conservative and Unionist Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£50,000.00",
                    "AcceptedDate": "07/01/2020",
                    "DonorName": "Anthony P Clarke",
                    "DonorStatus": "Individual",
                    "CompanyRegistrationNumber": "",
                    "ReceivedDate": "03/01/2020",
                    "RegulatedEntityId": "52",
                }
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        assert summary["skipped_individual"] == 1
        assert Attestation.objects.filter(source_reference="C0501005").count() == 0
        assert not Entity.objects.filter(name="Anthony P Clarke").exists()


@pytest.mark.django_db
class TestAttestationConfidenceStaleness:
    """`Attestation.objects.get_or_create(..., defaults={...})` silently
    discards `defaults` once a row exists. `ECRef` is a STABLE key across
    ingests (the Electoral Commission's own donation reference), but
    `_resolve_donor_company()`'s (confidence, method) is computed by
    re-querying the live `staging.Company` table on every run against a
    source the module's own docstring calls "a live export of the CURRENT
    donation register" (see `fetch_ec_donations_csv`) -- so the SAME ECRef
    can legitimately resolve via a different tier on a later re-ingest (e.g.
    a CompanyRegistrationNumber the EC backfills between two exports) even
    though the underlying donor company, and therefore the edge, never
    changes. A stale confidence left in place is a published number
    (`mcp.tools.get_attestations` returns `match_confidence` verbatim for
    any attestation, not just cross-register identity ones)."""

    def test_a_confidence_upgrade_on_reingest_is_applied(self, tmp_path):
        """GIVEN a donation already ingested once, when the CSV had no
        CompanyRegistrationNumber and the donor was resolved only by a
        uniqueness-guarded exact name match (exact_name / 0.9)
        WHEN the SAME ECRef is re-ingested from a corrected CSV that now
        carries the CompanyRegistrationNumber for the SAME donor company
        THEN the persisted attestation is corrected UP to the stronger
        identifier / 1.0 match in place -- not left frozen at the earlier
        run's weaker tier, and not duplicated into a second row."""
        Company.objects.create(
            company_number="00000020",
            company_name="Upgrade Co Ltd",
            normalised_name="UPGRADE CO LTD",
        )
        base_row = {
            "ECRef": "C0501020U",
            "RegulatedEntityName": "Conservative and Unionist Party",
            "RegulatedEntityType": "Political Party",
            "Value": "£1,000.00",
            "AcceptedDate": "10/01/2020",
            "DonorName": "Upgrade Co Ltd",
            "DonorStatus": "Company",
            "ReceivedDate": "05/01/2020",
            "RegulatedEntityId": "52",
        }
        csv_path_1 = _write_csv(tmp_path, [dict(base_row, CompanyRegistrationNumber="")])
        summary_1 = ingest_ec_donations_csv(csv_path_1)
        assert summary_1["matched"] == 1
        attestation = Attestation.objects.get(source_reference="C0501020U")
        assert attestation.match_confidence == 0.9
        assert attestation.match_method == "exact_name"

        csv_path_2 = _write_csv(tmp_path, [dict(base_row, CompanyRegistrationNumber="00000020")])
        summary_2 = ingest_ec_donations_csv(csv_path_2)

        assert Attestation.objects.filter(source_reference="C0501020U").count() == 1
        attestation.refresh_from_db()
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"
        assert summary_2["attestations_updated"] == 1

    def test_an_unchanged_resolution_on_reingest_is_not_rewritten(self, tmp_path):
        """GIVEN a donation already ingested with a resolution that the
        current code would compute identically again (same
        CompanyRegistrationNumber, same donor, nothing about the Company
        table changed)
        WHEN the SAME ECRef is re-ingested unchanged
        THEN no attestation update is recorded -- the fix only corrects a
        STALE confidence, it must not needlessly rewrite a row that already
        matches what the current run would decide."""
        Company.objects.create(
            company_number="00000022",
            company_name="Stable Co Ltd",
            normalised_name="STABLE CO LTD",
        )
        row = {
            "ECRef": "C0501022S",
            "RegulatedEntityName": "Conservative and Unionist Party",
            "RegulatedEntityType": "Political Party",
            "Value": "£1,000.00",
            "AcceptedDate": "10/01/2020",
            "DonorName": "Stable Co Ltd",
            "DonorStatus": "Company",
            "CompanyRegistrationNumber": "00000022",
            "ReceivedDate": "05/01/2020",
            "RegulatedEntityId": "52",
        }
        csv_path = _write_csv(tmp_path, [row])
        summary_1 = ingest_ec_donations_csv(csv_path)
        assert summary_1["matched"] == 1

        summary_2 = ingest_ec_donations_csv(csv_path)

        assert summary_2["attestations_updated"] == 0
        attestation = Attestation.objects.get(source_reference="C0501022S")
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"

    def test_a_confidence_downgrade_on_reingest_is_applied_not_protected(self, tmp_path):
        """GIVEN a donation already ingested once at the stronger identifier
        / 1.0 tier (CompanyRegistrationNumber present)
        WHEN the SAME ECRef is re-ingested from a later export where
        CompanyRegistrationNumber has gone missing (a data-quality
        regression at the source) but the donor name still resolves
        uniquely by exact name to the SAME company
        THEN the persisted confidence is corrected DOWN to exact_name / 0.9
        to match this run's weaker evidence -- a stale HIGH confidence left
        in place would be fail-open (a stronger claim than the current
        evidence supports stays published), so the correction must apply in
        the downgrade direction too, not just upgrades."""
        Company.objects.create(
            company_number="00000021",
            company_name="Downgrade Co Ltd",
            normalised_name="DOWNGRADE CO LTD",
        )
        base_row = {
            "ECRef": "C0501021D",
            "RegulatedEntityName": "Conservative and Unionist Party",
            "RegulatedEntityType": "Political Party",
            "Value": "£1,000.00",
            "AcceptedDate": "10/01/2020",
            "DonorName": "Downgrade Co Ltd",
            "DonorStatus": "Company",
            "ReceivedDate": "05/01/2020",
            "RegulatedEntityId": "52",
        }
        csv_path_1 = _write_csv(tmp_path, [dict(base_row, CompanyRegistrationNumber="00000021")])
        summary_1 = ingest_ec_donations_csv(csv_path_1)
        assert summary_1["matched"] == 1
        attestation = Attestation.objects.get(source_reference="C0501021D")
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"

        csv_path_2 = _write_csv(tmp_path, [dict(base_row, CompanyRegistrationNumber="")])
        summary_2 = ingest_ec_donations_csv(csv_path_2)

        attestation.refresh_from_db()
        assert attestation.match_confidence == 0.9
        assert attestation.match_method == "exact_name"
        assert summary_2["attestations_updated"] == 1

    def test_a_same_method_confidence_only_drift_is_still_corrected(self, tmp_path):
        """GIVEN a persisted attestation whose match_method already equals
        what the current run would compute (identifier) but whose
        match_confidence disagrees with the value that method always carries
        (1.0) -- isolating a confidence-only correction from a method change,
        so a fix that checks only `match_method` (or only ever raises
        `match_confidence`) cannot pass by accident
        WHEN the same donation is (re-)ingested
        THEN the confidence is corrected to what this run actually decided,
        even though the method string alone gave no signal anything was
        stale."""
        company = Company.objects.create(
            company_number="00000023",
            company_name="Same Method Co Ltd",
            normalised_name="SAME METHOD CO LTD",
        )
        donor_entity = Entity.objects.create(
            entity_type="company",
            company_number=company.company_number,
            name=company.company_name,
            registry_scheme="GB-COH",
            registry_id=company.company_number,
        )
        recipient_entity = Entity.objects.create(
            entity_type="political_party",
            registry_scheme="EC-REGULATED-ENTITY",
            registry_id="52",
            name="Conservative and Unionist Party",
        )
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=donor_entity,
            target_entity=recipient_entity,
            valid_from=date(2020, 1, 5),
            amount_cents=100000,
            currency="GBP",
        )
        attestation = Attestation.objects.create(
            edge=edge,
            source_name="Electoral Commission",
            source_reference="C0501023M",
            match_confidence=0.65,
            match_method="identifier",
        )
        csv_path = _write_csv(
            tmp_path,
            [
                {
                    "ECRef": "C0501023M",
                    "RegulatedEntityName": "Conservative and Unionist Party",
                    "RegulatedEntityType": "Political Party",
                    "Value": "£1,000.00",
                    "AcceptedDate": "10/01/2020",
                    "DonorName": "Same Method Co Ltd",
                    "DonorStatus": "Company",
                    "CompanyRegistrationNumber": "00000023",
                    "ReceivedDate": "05/01/2020",
                    "RegulatedEntityId": "52",
                }
            ],
        )

        summary = ingest_ec_donations_csv(csv_path)

        attestation.refresh_from_db()
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"
        assert summary["attestations_updated"] == 1


class TestEcDonationsRegisterContract:
    def test_ingest_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/uk_ec_donations.yml cannot be resolved (its source_id is absent
        from the register) WHEN ingest_ec_donations_csv is called THEN it raises
        RegisterError and writes nothing to the database."""
        monkeypatch.setattr(ec_donations_module, "SOURCE_ID", "does_not_exist_xyz")
        csv_path = _write_csv(tmp_path, [])

        with pytest.raises(RegisterError):
            ingest_ec_donations_csv(csv_path)

    def test_fetch_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/uk_ec_donations.yml cannot be resolved WHEN
        fetch_ec_donations_csv is called THEN it raises RegisterError before making
        any HTTP request."""
        monkeypatch.setattr(ec_donations_module, "SOURCE_ID", "does_not_exist_xyz")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("fetch_ec_donations_csv must not make an HTTP request")

        client = httpx.Client(transport=httpx.MockTransport(handler))

        with pytest.raises(RegisterError):
            fetch_ec_donations_csv(date(2020, 1, 1), date(2020, 12, 31), tmp_path, client=client)
