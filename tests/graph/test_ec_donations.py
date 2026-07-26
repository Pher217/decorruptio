"""Tests for the Electoral Commission donations ingest (Phase 1.2).

Verifies the core invariants:
- Company-number join produces match_confidence=1.0 (zero name matching)
- Ambiguous donor name (2+ companies) never guesses — no edge created
- amount_cents is parsed as an integer, never a float
- Donation date (ReceivedDate) maps to Edge.valid_from
- Individual donors are never turned into Entities (ADR-004 D1)
"""

import csv
from pathlib import Path

import pytest

from uncorrupt.graph.ec_donations import ingest_ec_donations_csv
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
