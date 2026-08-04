"""Electoral Commission donations ingest (Phase 1.2).

Source: https://search.electoralcommission.org.uk — the public donation
search tool. It exposes a CSV export via `/api/csv/Donations` (discovered
from the site's own `pefsearch.js`, which builds the "Export Results" link
the search UI renders — this is the same export a human clicking the button
would get, not HTML scraping of rendered pages).

Scope boundary (ADR-004 D1): company-level data and public-function office
holders only, no private-individual profiling. Donor rows from individuals
are recorded in the raw CSV but deliberately NOT turned into graph Entities
or Edges here — only donors identifiable as companies (by registration
number, or a uniqueness-guarded exact name match) are ingested. This mirrors
`uncorrupt.staging.companies_house.resolve_suppliers`.

Money: `Value` in the CSV is a formatted string like "£7,500.00". Parsed via
Decimal (no float round-trip) into integer cents on `Edge.amount_cents`.

Dates: EC donation rows carry both `AcceptedDate` (when the party/regulated
entity formally accepted the donation into its accounts) and `ReceivedDate`
(when the money/gift was actually received). `Edge.valid_from` is set to
`ReceivedDate` because that is when the relationship materially began —
temporal correctness against award dates is the point of this phase, and
"accepted" is a downstream administrative step that can lag receipt by
weeks. Falls back to `AcceptedDate` only if `ReceivedDate` is blank.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from django.db import transaction

from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.register.loader import load_source
from uncorrupt.staging.companies_house import _normalise_name, normalise_company_number
from uncorrupt.staging.models import Company
from uncorrupt.staging.raw import write_cached_fetch

logger = logging.getLogger(__name__)

EC_API_BASE = "https://search.electoralcommission.org.uk/api/csv/Donations"
SOURCE_NAME = "Electoral Commission"
# sources/uk_ec_donations.yml — connector refuses to run without it (ADR-001 D5)
SOURCE_ID = "uk_ec_donations"
CONNECTOR_VERSION = "0.1"

# Donor statuses that identify an organisation we can resolve to a company.
# "Individual" and similar person-level statuses are excluded by design
# (ADR-004 D1) — see module docstring.
ORGANISATION_DONOR_STATUSES = frozenset(
    {
        "Company",
        "Trade Union",
        "Building Society",
        "Limited Liability Partnership",
        "Unincorporated Association",
        "Friendly Society",
        "Registered Political Party",
        "Public Fund",
    }
)

ENTITY_TYPE_BY_REGULATED_TYPE: dict[str, str] = {
    "Political Party": "political_party",
}
DEFAULT_REGULATED_ENTITY_TYPE = "regulated_entity"


@dataclass(frozen=True)
class FetchResult:
    """Provenance record for a downloaded EC donations CSV."""

    csv_path: Path
    provenance_path: Path
    row_count: int
    source_url_template: str
    retrieved_at: datetime
    content_hash: str


def fetch_ec_donations_csv(
    from_date: date,
    to_date: date,
    output_dir: str | Path,
    entity_types: tuple[str, ...] = ("pp", "ppm", "tp"),
    page_size: int = 1000,
    max_retries: int = 5,
    polite_delay_seconds: float = 1.0,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Download EC donation records for a date range into a local CSV.

    Paginates via `start`/`rows`. Backs off with exponential delay on 429
    (or any 5xx) rather than hammering the service; gives up after
    `max_retries` consecutive failures on the same page.

    Writes the raw CSV plus a JSON provenance record (source URL template,
    retrieval timestamp, content hash) into `output_dir`. Callers are
    expected to point `output_dir` at a gitignored path (e.g. `experiments/`)
    — this function does not commit anything.
    """
    source = load_source(SOURCE_ID)  # refuses to run without sources/uk_ec_donations.yml
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "ec_donations.csv"
    provenance_path = output_dir / "ec_donations.provenance.json"

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)

    header: list[str] | None = None
    rows: list[list[str]] = []

    # The EC CSV export endpoint IGNORES `start` and returns the complete
    # result set for the date range in one response. Verified 2026-07-27:
    # start=0 and start=1000 returned byte-identical 5,745,843-byte bodies
    # (24,242 lines) for 2018-01-01..2024-12-31.
    #
    # This is why the previous paginating implementation could never finish:
    # every "page" came back full (24,241 rows >= page_size), so the
    # `len(page_data) < page_size` break was unreachable, `start` incremented
    # forever, and the same 24k rows accumulated in memory on every pass. A
    # run was killed after 20 minutes having produced no output at all.
    #
    # One request is therefore both correct and complete. If the endpoint ever
    # starts honouring `start`, this must become a loop again — the guard
    # below is what would catch that, by reporting a suspiciously round count.
    params = {
        "rows": page_size,
        "query": "",
        "sort": "AcceptedDate",
        "order": "desc",
        "date": "Received",
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
    }
    query = [*params.items(), *[("et", e) for e in entity_types]]
    url = httpx.URL(EC_API_BASE, params=query)

    try:
        all_rows = _fetch_page_with_backoff(client, url, max_retries)
        if all_rows:
            header, *data = all_rows
            rows.extend(data)
    finally:
        if owns_client:
            client.close()

    header = header or []
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    csv_bytes = buffer.getvalue().encode("utf-8")

    et_list = list(entity_types)
    source_url_template = f"{EC_API_BASE}?from={from_date}&to={to_date}&et={et_list}"
    # observed_at: left unset -- this is a live export of the CURRENT donation
    # register, not a historical snapshot with a capture date of its own.
    cached = write_cached_fetch(
        csv_bytes,
        csv_path,
        provenance_path,
        source=source,
        source_url=source_url_template,
        connector_version=CONNECTOR_VERSION,
        extra={
            "source_url_template": source_url_template,
            "row_count": len(rows),
            "date_range": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        },
    )

    return FetchResult(
        csv_path=csv_path,
        provenance_path=provenance_path,
        row_count=len(rows),
        source_url_template=source_url_template,
        retrieved_at=cached.provenance.retrieved_at,
        content_hash=cached.provenance.content_hash,
    )


def _fetch_page_with_backoff(
    client: httpx.Client, url: httpx.URL, max_retries: int
) -> list[list[str]]:
    delay = 2.0
    for _attempt in range(max_retries):
        response = client.get(url)
        if response.status_code == 200:
            text = response.text.lstrip("﻿")
            return list(csv.reader(text.splitlines()))
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
    raise RuntimeError(f"EC donations fetch failed after {max_retries} retries: {url}")


def _to_cents(value: str) -> int | None:
    """Parse an EC-formatted money string like '£7,500.00' into integer cents."""
    if not value:
        return None
    cleaned = value.replace("£", "").replace(",", "").strip()
    try:
        d = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def _parse_ec_date(value: str) -> date | None:
    """Parse an EC CSV date like '05/01/2020' (dd/mm/yyyy)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None


def _recipient_entity_type(regulated_entity_type: str) -> str:
    return ENTITY_TYPE_BY_REGULATED_TYPE.get(regulated_entity_type, DEFAULT_REGULATED_ENTITY_TYPE)


def _resolve_donor_company(row: dict[str, str]) -> tuple[Company | None, float, str]:
    """Resolve a donation's donor to a Companies House Company.

    Returns (company_or_none, match_confidence, match_method). Only resolves
    organisation-status donors (never individuals — ADR-004 D1).
    """
    donor_status = (row.get("DonorStatus") or "").strip()
    if donor_status not in ORGANISATION_DONOR_STATUSES:
        return None, 0.0, "identifier"

    company_number = normalise_company_number(row.get("CompanyRegistrationNumber"))
    if company_number:
        company = Company.objects.filter(company_number=company_number).first()
        if company:
            return company, 1.0, "identifier"
        return None, 0.0, "identifier"

    donor_name = (row.get("DonorName") or "").strip()
    if not donor_name:
        return None, 0.0, "identifier"

    normalised = _normalise_name(donor_name)
    matches = Company.objects.filter(normalised_name=normalised)
    if matches.count() == 1:
        return matches.get(), 0.9, "exact_name"

    # 0 or 2+ candidates: uniqueness guard — never guess.
    return None, 0.0, "identifier"


def ingest_ec_donations_csv(csv_path: str | Path) -> dict[str, Any]:
    """Ingest a previously-downloaded EC donations CSV into Entity/Edge rows.

    Returns summary stats: {matched, unmatched_donor, skipped_individual,
    attestations_updated, total}.
    """
    load_source(SOURCE_ID)  # refuses to run without sources/uk_ec_donations.yml
    csv_path = Path(csv_path)
    matched = 0
    unmatched_donor = 0
    skipped_individual = 0
    skipped_no_recipient_name = 0
    invalid_received_date = 0
    attestations_updated = 0
    total = 0

    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        with transaction.atomic():
            for row in reader:
                total += 1
                donor_status = (row.get("DonorStatus") or "").strip()

                company, confidence, method = _resolve_donor_company(row)
                if donor_status not in ORGANISATION_DONOR_STATUSES:
                    skipped_individual += 1
                    continue
                if company is None:
                    unmatched_donor += 1
                    continue

                donor_entity, _ = Entity.objects.get_or_create(
                    entity_type="company",
                    company_number=company.company_number,
                    defaults={
                        "name": company.company_name,
                        "registry_scheme": "GB-COH",
                        "registry_id": company.company_number,
                    },
                )

                recipient_name = (row.get("RegulatedEntityName") or "").strip()
                regulated_entity_id = (row.get("RegulatedEntityId") or "").strip()
                # Never create a shared node from a bare name (governing
                # principle: duplication over merging). When there is no
                # registry ID, the name IS the identity key, so it must be
                # part of the lookup — otherwise every ID-less recipient of
                # the same entity_type would collapse into the first row
                # ever created. If there is no name either, skip the row
                # rather than inventing an anonymous shared node.
                if not regulated_entity_id and not recipient_name:
                    skipped_no_recipient_name += 1
                    continue

                recipient_lookup: dict[str, Any] = {
                    "entity_type": _recipient_entity_type(
                        (row.get("RegulatedEntityType") or "").strip()
                    ),
                    "registry_scheme": "EC-REGULATED-ENTITY",
                }
                if regulated_entity_id:
                    recipient_lookup["registry_id"] = regulated_entity_id
                    recipient_defaults = {"name": recipient_name}
                else:
                    recipient_lookup["registry_id"] = None
                    recipient_lookup["name"] = recipient_name
                    recipient_defaults = {}
                recipient_entity, _ = Entity.objects.get_or_create(
                    **recipient_lookup, defaults=recipient_defaults
                )

                amount_cents = _to_cents(row.get("Value") or "")
                received_raw = row.get("ReceivedDate") or ""
                if received_raw.strip():
                    valid_from = _parse_ec_date(received_raw)
                    if valid_from is None:
                        invalid_received_date += 1
                        logger.warning(
                            "Unparseable ReceivedDate %r for ECRef %r; not falling back to "
                            "AcceptedDate",
                            received_raw,
                            row.get("ECRef"),
                        )
                else:
                    valid_from = _parse_ec_date(row.get("AcceptedDate") or "")
                ec_ref = (row.get("ECRef") or "").strip()

                # Edge = THE CLAIM (no citation — spec v0.3 §7-bis)
                edge, edge_created = Edge.objects.update_or_create(
                    edge_type="donation",
                    source_entity=donor_entity,
                    target_entity=recipient_entity,
                    valid_from=valid_from,
                    defaults={
                        "amount_cents": amount_cents,
                        "currency": "GBP",
                    },
                )

                # Attestation = THE EVIDENCE
                att_lookup: dict[str, Any] = {
                    "edge": edge,
                    "source_name": SOURCE_NAME,
                }
                if ec_ref:
                    att_lookup["source_reference"] = ec_ref
                source_url = (
                    f"https://search.electoralcommission.org.uk/Search/Donations?ecref={ec_ref}"
                    if ec_ref
                    else None
                )

                # get_or_create's `defaults` are silently discarded once a
                # row exists (the same bug class identity_resolution.py had
                # -- see its fix for the general shape). ECRef is a stable
                # key across ingests, but _resolve_donor_company() re-queries
                # the live Company table on every run, and this module is a
                # "live export of the CURRENT donation register" (see
                # fetch_ec_donations_csv) that gets re-fetched over time --
                # so the SAME ECRef can legitimately resolve via a different
                # tier on a later re-ingest (e.g. a CompanyRegistrationNumber
                # the EC backfills, or drops, between two exports) even
                # though the donor company -- and therefore this edge --
                # never changes. Correct the persisted confidence/method to
                # this run's decision in either direction, but only when it
                # actually changed. `observed_at` is deliberately left alone:
                # this connector never sets it (a live current-register
                # snapshot has no capture date of its own to record -- see
                # the module docstring), so there is no bitemporal field
                # here to update or preserve.
                existing_attestation = Attestation.objects.filter(**att_lookup).first()
                if existing_attestation is None:
                    Attestation.objects.create(
                        **att_lookup,
                        source_url=source_url,
                        match_confidence=confidence,
                        match_method=method,
                    )
                elif (
                    existing_attestation.match_confidence != confidence
                    or existing_attestation.match_method != method
                ):
                    existing_attestation.match_confidence = confidence
                    existing_attestation.match_method = method
                    existing_attestation.save(update_fields=["match_confidence", "match_method"])
                    attestations_updated += 1
                if edge_created:
                    matched += 1

    return {
        "matched": matched,
        "unmatched_donor": unmatched_donor,
        "skipped_individual": skipped_individual,
        "skipped_no_recipient_name": skipped_no_recipient_name,
        "invalid_received_date": invalid_received_date,
        "attestations_updated": attestations_updated,
        "total": total,
    }
