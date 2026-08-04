"""OpenSanctions — sanctioned/watchlisted legal entities, non-personal slice only.

Source: OpenSanctions' free "default" collection bulk export (FollowTheMoney
JSON, one entity per line — `entities.ftm.json`,
https://www.opensanctions.org/datasets/default/), licensed CC-BY-NC 4.0
(verified live 2026-08-04). The default collection mixes many FtM entity
schemas in one file — Person, Company, Organization, LegalEntity, Vessel,
Airplane, Security, PublicBody, Address, plus relationship schemas
(Directorship, Ownership, Family, Occupancy, Succession, UnknownLink).

This connector ingests ONLY `ALLOWED_SCHEMAS` (Company, Organization,
LegalEntity) — non-personal legal-entity records, the same data_class as
GLEIF (sources/gleif.yml). Every Person-schema record and every
relationship-schema record (which could re-introduce a named individual via
a `holder`/`owner`/`director` reference) is counted and dropped before any
write reaches the raw cache or the database. This keeps the connector
structurally out of ADR-004 D1's DPIA gate, which governs processing of
named natural persons: the person-level majority of OpenSanctions (PEPs,
sanctioned individuals, family/associates — data_class A2) is registered
separately and untouched at sources/opensanctions.yml
(dpia_cleared: false, legal_basis "TBD"). See sources/opensanctions_entities.yml
for the full split rationale.

No automated network fetch (deliberately): data.opensanctions.org's
robots.txt returns `Disallow: /` for every user agent, with no path
exceptions (verified 2026-08-04) — in tension with the docs page's "no
login or API key needed" framing for bulk downloads. Rather than resolve
that tension unilaterally inside reusable, repeatable pipeline
infrastructure, `fetch_opensanctions` takes a path to an already-downloaded
local file (obtained by a human, out of band, via the documented bulk-
download page) and only hashes/caches it — it never issues an HTTP request.

Cross-linking (identifier-only, never by name — ADR-004 D2): `leiCode`
exact-matches `Entity(registry_scheme="GLEIF-LEI")`; `registrationNumber`
(only on a GB-jurisdiction/country record) is normalised via
`uncorrupt.staging.companies_house.normalise_company_number` and exact-
matched against `staging.Company.company_number`. Unlike GLEIF's
`registeredAt.id` (a registration-authority code), OpenSanctions' FtM
schema carries no registering-authority disambiguator for
`registrationNumber` — so this join is weaker evidence than GLEIF's
RA-code-gated one and is reported as its own distinct count
(`gb_coh_linked`), never conflated with the LEI join.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from django.db import transaction

from uncorrupt.graph.models import Entity
from uncorrupt.register.loader import load_source
from uncorrupt.staging.companies_house import normalise_company_number
from uncorrupt.staging.models import Company
from uncorrupt.staging.raw import write_cached_fetch

logger = logging.getLogger(__name__)

SOURCE_ID = "opensanctions_entities"  # sources/opensanctions_entities.yml — connector
# refuses to run without it (ADR-001 D5). NOT sources/opensanctions.yml — that entry
# stays A2/dpia_cleared:false/unused; see the module docstring.
REGISTRY_SCHEME = "OPENSANCTIONS-ORG"
CONNECTOR_VERSION = "0.1"
DATASET_URL = "https://www.opensanctions.org/datasets/default/"

# Non-personal FtM entity schemas only. Person and every relationship schema
# that could carry a named individual (Directorship, Ownership, Family,
# Occupancy, Succession, UnknownLink, ...) are excluded by construction:
# anything not in this set is dropped (see `skipped_non_entity_schema`).
ALLOWED_SCHEMAS = frozenset({"Company", "Organization", "LegalEntity"})


@dataclass(frozen=True)
class FetchResult:
    """Provenance record for a locally-cached OpenSanctions entities slice."""

    jsonl_path: Path
    provenance_path: Path
    record_count: int
    retrieved_at: datetime
    content_hash: str


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    v = values[0].strip()
    return v or None


def fetch_opensanctions(input_path: str | Path, output_dir: str | Path) -> FetchResult:
    """Cache an already-downloaded OpenSanctions `entities.ftm.json` slice.

    `input_path` must point to a file a human already fetched via the
    documented no-login bulk-download page (see the module docstring for
    why this connector never fetches it itself). Writes a hashed copy plus
    a provenance sidecar into `output_dir` — the same cache-with-provenance
    contract every other connector's fetch layer uses
    (`uncorrupt.staging.raw.write_cached_fetch`) — and never mutates or
    reads beyond `input_path`.
    """
    source = load_source(SOURCE_ID)  # refuses to run without the register entry (ADR-001 D5)

    input_path = Path(input_path)
    payload = input_path.read_bytes()
    record_count = sum(1 for line in payload.splitlines() if line.strip())

    output_dir = Path(output_dir)
    jsonl_path = output_dir / "opensanctions_entities.jsonl"
    provenance_path = output_dir / "opensanctions_entities.provenance.json"

    cached = write_cached_fetch(
        payload,
        jsonl_path,
        provenance_path,
        source=source,
        source_url=DATASET_URL,
        connector_version=CONNECTOR_VERSION,
        extra={"record_count": record_count, "input_path": str(input_path)},
    )

    return FetchResult(
        jsonl_path=jsonl_path,
        provenance_path=provenance_path,
        record_count=record_count,
        retrieved_at=cached.provenance.retrieved_at,
        content_hash=cached.provenance.content_hash,
    )


def _find_gleif_entity(lei_codes: list[str] | None) -> Entity | None:
    """Exact-match the first `leiCode` that resolves to an existing GLEIF Entity.

    Identifier-only: an LEI either matches an ingested GLEIF record exactly
    or it does not — never a guess.
    """
    for lei in lei_codes or []:
        lei = lei.strip()
        if not lei:
            continue
        match = Entity.objects.filter(registry_scheme="GLEIF-LEI", registry_id=lei).first()
        if match is not None:
            return match
    return None


def _find_gb_company(
    jurisdiction_values: list[str] | None,
    country_values: list[str] | None,
    registration_numbers: list[str] | None,
) -> Company | None:
    """Exact-match a GB `registrationNumber` against `staging.Company`.

    Only attempted when the record's jurisdiction or country includes "gb"
    — never inferred from the number's shape alone. OpenSanctions' FtM
    schema has no equivalent of GLEIF's registration-authority code, so
    (unlike gleif.py's `_resolve_gb_company`) this cannot confirm the
    number came from Companies House specifically; it is reported as its
    own distinct, weaker-evidence count (`gb_coh_linked`), not merged into
    the LEI join.
    """
    locales = {v.strip().lower() for v in (jurisdiction_values or []) + (country_values or []) if v}
    if "gb" not in locales:
        return None
    for raw_number in registration_numbers or []:
        normalised = normalise_company_number(raw_number)
        if not normalised:
            continue
        match = Company.objects.filter(company_number=normalised).first()
        if match is not None:
            return match
    return None


def ingest_opensanctions(jsonl_path: str | Path) -> dict[str, Any]:
    """Ingest a locally-cached OpenSanctions entities slice into graph Entity rows.

    Returns summary stats: {created, updated, skipped_non_entity_schema,
    skipped_no_id, gleif_lei_linked, gb_coh_linked, total}.
    `skipped_non_entity_schema` counts every Person and relationship-schema
    record dropped before any write (never silent — see module docstring).
    """
    load_source(SOURCE_ID)  # refuses to run without the register entry (ADR-001 D5)
    jsonl_path = Path(jsonl_path)

    created = 0
    updated = 0
    skipped_non_entity_schema = 0
    skipped_no_id = 0
    gleif_lei_linked = 0
    gb_coh_linked = 0
    total = 0

    with open(jsonl_path, encoding="utf-8") as f, transaction.atomic():
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            record = json.loads(line)

            os_id = record.get("id")
            if not os_id:
                skipped_no_id += 1
                continue

            schema = record.get("schema")
            if schema not in ALLOWED_SCHEMAS:
                skipped_non_entity_schema += 1
                continue

            properties = record.get("properties") or {}
            name = record.get("caption") or _first(properties.get("name")) or os_id

            gleif_entity = _find_gleif_entity(properties.get("leiCode"))
            gb_company = _find_gb_company(
                properties.get("jurisdiction"),
                properties.get("country"),
                properties.get("registrationNumber"),
            )
            if gleif_entity is not None:
                gleif_lei_linked += 1
            if gb_company is not None:
                gb_coh_linked += 1

            entity_properties = {
                "schema": schema,
                "jurisdiction": _first(properties.get("jurisdiction")),
                "country": _first(properties.get("country")),
                "topics": properties.get("topics") or [],
                "program_ids": properties.get("programId") or [],
                "datasets": record.get("datasets") or [],
                "target": record.get("target", False),
                "first_seen": record.get("first_seen"),
                "last_seen": record.get("last_seen"),
                "lei": _first(properties.get("leiCode")),
                "gleif_lei_linked": gleif_entity is not None,
                "registration_number": _first(properties.get("registrationNumber")),
                "gb_coh_linked": gb_company is not None,
            }

            _entity, entity_created = Entity.objects.update_or_create(
                entity_type="company",
                registry_scheme=REGISTRY_SCHEME,
                registry_id=os_id,
                defaults={
                    "name": name,
                    "company_number": gb_company.company_number if gb_company else None,
                    "properties": entity_properties,
                },
            )
            if entity_created:
                created += 1
            else:
                updated += 1

    return {
        "created": created,
        "updated": updated,
        "skipped_non_entity_schema": skipped_non_entity_schema,
        "skipped_no_id": skipped_no_id,
        "gleif_lei_linked": gleif_lei_linked,
        "gb_coh_linked": gb_coh_linked,
        "total": total,
    }
