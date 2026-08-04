"""GLEIF Legal Entity Identifier ingest — first step beyond UK-only data.

Source: the GLEIF public API (`https://api.gleif.org/api/v1/lei-records`),
which serves the same "Golden Copy" LEI dataset GLEIF publishes as a CC0
bulk download (verified live 2026-07-26: `meta.goldenCopy.publishDate` on
every response). The bulk CSV/XML Golden Copy is a single multi-GB file
covering all ~2.7M LEI records worldwide; the paginated API lets us pull a
bounded, resumable slice (e.g. one jurisdiction) without downloading the
whole file, which is the right trade-off for a first run. Both are the same
CC0-licensed GLEIF data — no scraping, no rate-limit workaround.

Company-level data only (ADR-004 D1): an LEI record identifies a legal
entity, never a natural person. No person fields exist in this dataset.

Cross-linking (the high-value part): a GLEIF record's `entity.jurisdiction`
is the jurisdiction of incorporation; `entity.registeredAs` is the entity's
number at its national company registry, and `entity.registeredAt.id` is
the registering authority's GLEIF Registration Authority (RA) code.
Jurisdiction "GB" alone is NOT sufficient to conclude `registeredAs` is a
Companies House number — GLEIF lists dozens of other GB registration
authorities (Charity Commission, FCA, Pensions Regulator, GLEIF's own
"not on the list" placeholders RA999999/RA888888, ...) whose number
formats can coincidentally pad to a real, unrelated Companies House
number. Cross-linking therefore additionally gates on
`entity.registeredAt.id` being one of the three Companies House RA codes
(see `COMPANIES_HOUSE_RA_CODES`) before the number goes through
`uncorrupt.staging.companies_house.normalise_company_number` and joins
`staging.Company` — the same padding fix used for EC donations and CH
officers. GB records from any other authority still become Entities (they
are legitimate global entities) but keep `company_number` NULL; the
authority code and raw number are preserved in `properties` either way.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from django.db import transaction

from uncorrupt.graph.models import Entity
from uncorrupt.register.loader import load_source
from uncorrupt.staging.companies_house import normalise_company_number
from uncorrupt.staging.models import Company
from uncorrupt.staging.raw import write_cached_fetch

logger = logging.getLogger(__name__)

GLEIF_API_BASE = "https://api.gleif.org/api/v1/lei-records"
SOURCE_NAME = "GLEIF"
REGISTRY_SCHEME = "GLEIF-LEI"
SOURCE_ID = "gleif"  # sources/gleif.yml — connector refuses to run without it (ADR-001 D5)
CONNECTOR_VERSION = "0.1"

# GLEIF's API caps page[size] at 200; larger values return HTTP 400.
MAX_PAGE_SIZE = 200

# GLEIF Registration Authority codes for Companies House — confirmed live
# against https://api.gleif.org/api/v1/registration-authorities/{code}
# (queried 2026-07-26). Companies House runs one register across three
# jurisdictions, each with its own RA code and number prefix convention:
#   RA000585 = Companies House, England and Wales (no prefix, e.g. "07015428")
#   RA000587 = Companies House, Scotland (SC/SL/SO prefix, e.g. "SC286832")
#   RA000586 = Companies House, Northern Ireland (NI prefix, e.g. "NI006176")
# All three feed the same "Basic Company Data" bulk snapshot ingested into
# staging.Company, so all three are valid cross-link sources. Every other GB
# RA code (e.g. RA000592 Financial Conduct Authority, RA000590 Scottish
# Charity Regulator, RA000591 The Pensions Regulator, RA999999/RA888888
# GLEIF's "authority not on the list" placeholders) issues numbers from a
# different register and must never be treated as a Companies House number.
COMPANIES_HOUSE_RA_CODES = frozenset({"RA000585", "RA000586", "RA000587"})


@dataclass(frozen=True)
class FetchResult:
    """Provenance record for a downloaded slice of GLEIF LEI records."""

    jsonl_path: Path
    provenance_path: Path
    record_count: int
    source_url_template: str
    retrieved_at: datetime
    content_hash: str


def fetch_gleif(
    output_dir: str | Path,
    country: str | None = None,
    limit: int = 50_000,
    page_size: int = MAX_PAGE_SIZE,
    max_retries: int = 5,
    polite_delay_seconds: float = 0.2,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Download up to `limit` GLEIF LEI records into a local JSONL file.

    Paginates via `page[cursor]` (following the API's own `links.next`),
    backing off exponentially on 429/5xx (never hammering the service).
    GLEIF's `page[number]` offset pagination is capped at 10,000 results
    total (`page[number] * page[size]`); cursor pagination has no such cap
    and is the mechanism GLEIF's own error message directs callers to.
    `country` filters on `entity.legalAddress.country` (ISO 3166-1 alpha-2);
    omit for a global (unfiltered) sample.

    Writes raw JSON:API records (one per line) plus a provenance record
    (source URL template, retrieval timestamp, SHA-256 content hash) into
    `output_dir`. Callers must point `output_dir` at a gitignored path
    (e.g. `experiments/`) — this function never commits anything, and never
    writes person-level data because GLEIF records contain none.
    """
    source = load_source(SOURCE_ID)  # refuses to run without sources/gleif.yml (ADR-001 D5)
    if page_size > MAX_PAGE_SIZE:
        raise ValueError(f"GLEIF API rejects page[size] > {MAX_PAGE_SIZE}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = country.lower() if country else "all"
    jsonl_path = output_dir / f"gleif_{suffix}.jsonl"
    provenance_path = output_dir / f"gleif_{suffix}.provenance.json"

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)

    records: list[dict[str, Any]] = []
    params: list[tuple[str, str]] = [
        ("page[size]", str(min(page_size, limit))),
        ("page[cursor]", "*"),
    ]
    if country:
        params.append(("filter[entity.legalAddress.country]", country))
    url: httpx.URL | None = httpx.URL(GLEIF_API_BASE, params=params)
    page_number = 0

    try:
        while url is not None and len(records) < limit:
            page_number += 1
            payload = _fetch_page_with_backoff(client, url, max_retries)
            page_data = payload.get("data", [])
            if not page_data:
                break
            records.extend(page_data)
            print(f"  GLEIF fetch: page {page_number}, {len(records)}/{limit} records", flush=True)

            next_link = (payload.get("links") or {}).get("next")
            if not next_link or len(records) >= limit:
                break
            url = httpx.URL(next_link)
            time.sleep(polite_delay_seconds)
    finally:
        if owns_client:
            client.close()

    records = records[:limit]

    jsonl_bytes = "".join(json.dumps(record) + "\n" for record in records).encode("utf-8")
    source_url_template = f"{GLEIF_API_BASE}?filter[entity.legalAddress.country]={country or '*'}"
    # observed_at: left unset (never retrieved_at) -- GLEIF's Golden Copy
    # publishDate is a per-record field (see module docstring), not a single
    # capture instant for the whole fetched slice, and this is a live API
    # pull with no separate historical-snapshot concept.
    cached = write_cached_fetch(
        jsonl_bytes,
        jsonl_path,
        provenance_path,
        source=source,
        source_url=source_url_template,
        connector_version=CONNECTOR_VERSION,
        extra={
            "source_url_template": source_url_template,
            "record_count": len(records),
            "country": country,
            "limit": limit,
        },
    )

    return FetchResult(
        jsonl_path=jsonl_path,
        provenance_path=provenance_path,
        record_count=len(records),
        source_url_template=source_url_template,
        retrieved_at=cached.provenance.retrieved_at,
        content_hash=cached.provenance.content_hash,
    )


def _fetch_page_with_backoff(
    client: httpx.Client, url: httpx.URL, max_retries: int
) -> dict[str, Any]:
    delay = 2.0
    for _attempt in range(max_retries):
        response = client.get(url, headers={"Accept": "application/vnd.api+json"})
        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
    raise RuntimeError(f"GLEIF fetch failed after {max_retries} retries: {url}")


def _resolve_gb_company(
    jurisdiction: str, registered_at: str | None, registered_as: str | None
) -> Company | None:
    """Resolve a GLEIF record's local registration number to a staging.Company.

    Only attempted for jurisdiction GB *and* a registering authority that is
    actually Companies House (`COMPANIES_HOUSE_RA_CODES`) — jurisdiction
    alone does not identify the register the number came from, and a number
    from a different GB authority (FCA, Charity Commission, Pensions
    Regulator, GLEIF's unknown-authority placeholders, ...) can coincidentally
    pad to an unrelated real company. Never guesses — a miss is a null
    `company_number`, not an invented match.
    """
    if jurisdiction != "GB" or registered_at not in COMPANIES_HOUSE_RA_CODES or not registered_as:
        return None
    normalised = normalise_company_number(registered_as)
    if not normalised:
        return None
    return Company.objects.filter(company_number=normalised).first()


def ingest_gleif(jsonl_path: str | Path) -> dict[str, Any]:
    """Ingest a previously-downloaded GLEIF JSONL slice into graph Entity rows.

    Returns summary stats: {created, updated, skipped_no_lei, gb_linked,
    gb_other_authority, countries, total}. `gb_linked` counts GB records
    cross-linked via a Companies House RA code; `gb_other_authority` counts
    GB records with a registration number from a different GB authority
    (correctly left unlinked, but still recorded in `properties`).
    """
    load_source(SOURCE_ID)  # refuses to run without sources/gleif.yml (ADR-001 D5)
    jsonl_path = Path(jsonl_path)
    created = 0
    updated = 0
    skipped_no_lei = 0
    gb_linked = 0
    gb_other_authority = 0
    countries: set[str] = set()
    total = 0

    with open(jsonl_path, encoding="utf-8") as f, transaction.atomic():
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            record = json.loads(line)
            attrs = record.get("attributes") or {}
            lei = attrs.get("lei") or record.get("id")
            if not lei:
                skipped_no_lei += 1
                continue

            entity_attrs = attrs.get("entity") or {}
            registration_attrs = attrs.get("registration") or {}
            legal_name = ((entity_attrs.get("legalName") or {}).get("name") or "").strip()
            jurisdiction = (entity_attrs.get("jurisdiction") or "").strip()
            country = ((entity_attrs.get("legalAddress") or {}).get("country") or "").strip()
            status = entity_attrs.get("status")
            registration_status = registration_attrs.get("status")
            registered_as = entity_attrs.get("registeredAs")
            registered_at = ((entity_attrs.get("registeredAt") or {}).get("id")) or None

            if country:
                countries.add(country)
            elif jurisdiction:
                countries.add(jurisdiction)

            company = _resolve_gb_company(jurisdiction, registered_at, registered_as)
            company_number = company.company_number if company else None
            if company is not None:
                gb_linked += 1
            elif jurisdiction == "GB" and registered_as:
                gb_other_authority += 1

            properties = {
                "jurisdiction": jurisdiction or None,
                "country": country or None,
                "status": status,
                "registration_status": registration_status,
                "local_registration_number": registered_as,
                "local_registration_authority": registered_at,
                "legal_form": (entity_attrs.get("legalForm") or {}).get("id"),
                "category": entity_attrs.get("category"),
            }

            _entity, entity_created = Entity.objects.update_or_create(
                entity_type="company",
                registry_scheme=REGISTRY_SCHEME,
                registry_id=lei,
                defaults={
                    "name": legal_name or lei,
                    "company_number": company_number,
                    "properties": properties,
                },
            )
            if entity_created:
                created += 1
            else:
                updated += 1

    return {
        "created": created,
        "updated": updated,
        "skipped_no_lei": skipped_no_lei,
        "gb_linked": gb_linked,
        "gb_other_authority": gb_other_authority,
        "countries": len(countries),
        "total": total,
    }
