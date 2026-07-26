"""Companies House officers ingest (Phase 1.4).

Source: Companies House REST API `/company/{company_number}/officers`
(https://developer.company-information.service.gov.uk/). Requires a free
API key, read from the `COMPANIES_HOUSE_API_KEY` env var — never
hardcoded, never committed. Authenticates via HTTP Basic auth with the key
as username and an empty password (CH convention).

Rate limit: 600 requests / 5 minutes. `fetch_company_officers` throttles
between requests and backs off (respecting `Retry-After` when present) on
429/5xx. Per-company JSON is cached to `output_dir`, and a company already
cached is skipped on re-run, so an interrupted run can be resumed by
re-invoking with the same `output_dir` without losing progress or
re-fetching companies already done. A cached entry is only trusted while
it is fresher than `max_cache_age_days` (default 30) and its stored
content hash still matches the file on disk — otherwise it is refetched.

Scope boundary (ADR-004 D1): only officers of companies already in our
resolved set (`staging.Company`, joined by `company_number`) are ingested,
and only in their capacity as company officers. Fields are allowlisted
(`_ALLOWED_OFFICER_FIELDS`: name, officer_role, appointed_on, resigned_on,
links) rather than blacklisted — anything the API returns beyond that
(date of birth, residential address, nationality, former_names,
occupation, country_of_residence, contact details, ...) is dropped before
the raw response ever touches disk; it is never cached, never ingested,
never stored anywhere in this module. No PSC / beneficial-ownership data
is ingested here at all (that is a separate gate).

Resolution by registry ID: companies are matched by `company_number` only,
officers by their CH officer ID (parsed from `links.officer.appointments`)
where present. When an officer has no such link (some appointments omit
it), the person Entity is still recorded but scoped to the company it was
found at (`registry_scheme=GB-COH-OFFICER-UNRESOLVED`,
`registry_id={company_number}:{normalised_name}`) so same-named officers
at different companies are never merged into one person (governing
principle: duplicate over merge). The edge's `match_confidence` reflects
the weaker identification (see `_PERSON_MATCH_CONFIDENCE_NO_ID`).

Edge identity: `source_reference` prefers the per-appointment
`links.self` resource (so reappointments/multiple roles at the same
company stay distinct edges), falling back to the officer ID — a weaker
claim, noted via `properties["source_reference_scope"]` — only when
`links.self` is absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from django.db import transaction

from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.staging.companies_house import _normalise_name
from uncorrupt.staging.models import Company

CH_API_BASE = "https://api.company-information.service.gov.uk"
SOURCE_NAME = "Companies House"
API_KEY_ENV_VAR = "COMPANIES_HOUSE_API_KEY"

# Allowlist of officer fields we actually need — everything else the API
# returns (former_names, occupation, country_of_residence, contact_details,
# date_of_birth, address, nationality, ...) is dropped before the raw
# response ever touches disk. Fail-closed: a new field the API starts
# returning tomorrow is dropped by default, not persisted by default.
_ALLOWED_OFFICER_FIELDS = (
    "name",
    "officer_role",
    "appointed_on",
    "resigned_on",
    "links",
)

_OFFICER_ID_RE = re.compile(r"/officers/([^/]+)/appointments")
_APPOINTMENT_SELF_RE = re.compile(r"/appointments/([^/]+)$")

# Confidence assigned to an officer_of edge when the officer has no stable
# CH officer ID to key on (weaker than the default identifier match).
_PERSON_MATCH_CONFIDENCE_NO_ID = 0.5

# Cache entries older than this are refetched rather than trusted forever.
DEFAULT_MAX_CACHE_AGE_DAYS = 30


@dataclass(frozen=True)
class OfficersFetchResult:
    """Provenance record for one company's cached officers response."""

    company_number: str
    json_path: Path
    provenance_path: Path
    officer_count: int
    source_url: str
    retrieved_at: datetime
    content_hash: str
    cached: bool


def _require_api_key() -> str:
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        raise RuntimeError(
            f"{API_KEY_ENV_VAR} is not set. Get a free API key from "
            "https://developer.company-information.service.gov.uk/ and export it "
            f"as {API_KEY_ENV_VAR} before running this ingest."
        )
    return api_key


def _strip_personal_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item[k] for k in _ALLOWED_OFFICER_FIELDS if k in item}


def _cache_is_valid(json_path: Path, provenance: dict[str, Any], max_age_days: int) -> bool:
    """A cache entry is trusted only if it is fresh and its content hash matches."""
    retrieved_at = datetime.fromisoformat(provenance["retrieved_at"])
    age_days = (datetime.now(UTC) - retrieved_at).days
    if age_days > max_age_days:
        return False
    actual_hash = f"sha256:{hashlib.sha256(json_path.read_bytes()).hexdigest()}"
    return actual_hash == provenance["content_hash"]


def fetch_company_officers(
    company_numbers: Sequence[str],
    output_dir: str | Path,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    max_retries: int = 5,
    polite_delay_seconds: float = 1.0,
    items_per_page: int = 35,
    max_cache_age_days: int = DEFAULT_MAX_CACHE_AGE_DAYS,
) -> list[OfficersFetchResult]:
    """Fetch officers for a bounded list of company numbers.

    Resumable: a company_number whose cache file already exists in
    `output_dir` is skipped and returned with `cached=True`, so an
    interrupted run can be re-invoked with the same `output_dir` to pick
    up where it left off. A cached entry is only trusted while it is fresher
    than `max_cache_age_days` and its stored content hash still matches the
    file on disk — otherwise it is refetched.

    Writes filtered raw JSON (DOB/address/nationality already stripped —
    see module docstring) plus a provenance record per company into
    `output_dir`. Callers are expected to point `output_dir` at a
    gitignored path (e.g. `experiments/`) — this function does not commit
    anything.
    """
    api_key = api_key or _require_api_key()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, auth=httpx.BasicAuth(api_key, ""))

    results: list[OfficersFetchResult] = []
    try:
        for company_number in company_numbers:
            json_path = output_dir / f"{company_number}.json"
            provenance_path = output_dir / f"{company_number}.provenance.json"

            if json_path.exists() and provenance_path.exists():
                provenance = json.loads(provenance_path.read_text())
                if _cache_is_valid(json_path, provenance, max_cache_age_days):
                    results.append(
                        OfficersFetchResult(
                            company_number=company_number,
                            json_path=json_path,
                            provenance_path=provenance_path,
                            officer_count=provenance["officer_count"],
                            source_url=provenance["source_url"],
                            retrieved_at=datetime.fromisoformat(provenance["retrieved_at"]),
                            content_hash=provenance["content_hash"],
                            cached=True,
                        )
                    )
                    continue

            source_url = f"{CH_API_BASE}/company/{company_number}/officers"
            items = _fetch_all_officer_pages(client, source_url, items_per_page, max_retries)
            items = [_strip_personal_fields(item) for item in items]

            json_path.write_text(json.dumps(items, indent=2))
            content_hash = hashlib.sha256(json_path.read_bytes()).hexdigest()
            retrieved_at = datetime.now(UTC)
            provenance = {
                "company_number": company_number,
                "source_url": source_url,
                "retrieved_at": retrieved_at.isoformat(),
                "content_hash": f"sha256:{content_hash}",
                "officer_count": len(items),
            }
            provenance_path.write_text(json.dumps(provenance, indent=2))

            results.append(
                OfficersFetchResult(
                    company_number=company_number,
                    json_path=json_path,
                    provenance_path=provenance_path,
                    officer_count=len(items),
                    source_url=source_url,
                    retrieved_at=retrieved_at,
                    content_hash=f"sha256:{content_hash}",
                    cached=False,
                )
            )
            time.sleep(polite_delay_seconds)
    finally:
        if owns_client:
            client.close()

    return results


def _fetch_all_officer_pages(
    client: httpx.Client, source_url: str, items_per_page: int, max_retries: int
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start_index = 0
    while True:
        params = {"items_per_page": items_per_page, "start_index": start_index}
        page = _fetch_page_with_backoff(client, source_url, params, max_retries)
        page_items = page.get("items", [])
        items.extend(page_items)
        total_results = page.get("total_results", len(items))
        start_index += len(page_items)
        if not page_items or start_index >= total_results:
            break
    return items


def _fetch_page_with_backoff(
    client: httpx.Client, url: str, params: dict[str, Any], max_retries: int
) -> dict[str, Any]:
    delay = 2.0
    for _attempt in range(max_retries):
        response = client.get(url, params=params)
        if response.status_code == 200:
            result: dict[str, Any] = response.json()
            return result
        if response.status_code == 404:
            # No officers on record (or company not found) — not an error.
            return {"items": [], "total_results": 0}
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else delay)
            delay *= 2
            continue
        response.raise_for_status()
    raise RuntimeError(f"CH officers fetch failed after {max_retries} retries: {url}")


def _parse_officer_id(item: dict[str, Any]) -> str | None:
    """Parse the CH officer ID out of `links.officer.appointments`."""
    officer_link = (item.get("links") or {}).get("officer") or {}
    appointments_link = officer_link.get("appointments")
    if not appointments_link:
        return None
    match = _OFFICER_ID_RE.search(appointments_link)
    return match.group(1) if match else None


def _parse_appointment_self_link(item: dict[str, Any]) -> str | None:
    """Parse `links.self` — the per-appointment resource, the strongest identity key.

    Distinct from the officer ID, which identifies the *person* across all
    their appointments: `links.self` identifies *this specific appointment*,
    so reappointments or multiple roles at the same company don't collapse
    into one edge.
    """
    self_link = (item.get("links") or {}).get("self")
    return self_link if isinstance(self_link, str) and self_link else None


def _parse_ch_date(value: str | None) -> date | None:
    """Parse a CH API date like '2010-01-01' (already ISO 8601)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def ingest_company_officers(
    company_numbers: Sequence[str], input_dir: str | Path
) -> dict[str, Any]:
    """Ingest previously-fetched officer JSON files into Entity/Edge rows.

    Reads `{company_number}.json` files written by `fetch_company_officers`
    out of `input_dir`. Returns summary stats: {edges_created,
    companies_processed, companies_unmatched, officers_no_id,
    missing_appointed_on, unparseable_resigned_on, total_officers}.
    """
    input_dir = Path(input_dir)
    edges_created = 0
    companies_processed = 0
    companies_unmatched = 0
    officers_no_id = 0
    missing_appointed_on = 0
    unparseable_resigned_on = 0
    total_officers = 0

    with transaction.atomic():
        for company_number in company_numbers:
            json_path = input_dir / f"{company_number}.json"
            if not json_path.exists():
                companies_unmatched += 1
                continue

            company = Company.objects.filter(company_number=company_number).first()
            if company is None:
                companies_unmatched += 1
                continue

            items = json.loads(json_path.read_text())
            company_entity, _ = Entity.objects.get_or_create(
                entity_type="company",
                company_number=company.company_number,
                defaults={
                    "name": company.company_name,
                    "registry_scheme": "GB-COH",
                    "registry_id": company.company_number,
                },
            )

            officers_url = f"{CH_API_BASE}/company/{company_number}/officers"
            companies_processed += 1

            for item in items:
                total_officers += 1
                name = (item.get("name") or "").strip()
                if not name:
                    continue

                officer_id = _parse_officer_id(item)
                if officer_id:
                    person_entity, _ = Entity.objects.get_or_create(
                        entity_type="person",
                        registry_scheme="GB-COH-OFFICER",
                        registry_id=officer_id,
                        defaults={"name": name},
                    )
                    confidence = 1.0
                    match_method = "identifier"
                else:
                    # No stable CH officer ID: never merge same-named people
                    # across companies (governing principle — duplication
                    # over merging). Scope the identity to THIS company so
                    # "John Smith" at Company A and "John Smith" at Company
                    # B are always distinct entities unless proven otherwise.
                    officers_no_id += 1
                    person_entity, _ = Entity.objects.get_or_create(
                        entity_type="person",
                        registry_scheme="GB-COH-OFFICER-UNRESOLVED",
                        registry_id=f"{company_number}:{_normalise_name(name)}",
                        defaults={"name": name},
                    )
                    confidence = _PERSON_MATCH_CONFIDENCE_NO_ID
                    match_method = "name_company_scoped"

                appointed_on = item.get("appointed_on")
                valid_from = _parse_ch_date(appointed_on)
                if valid_from is None:
                    missing_appointed_on += 1

                resigned_on_raw = item.get("resigned_on")
                edge_properties: dict[str, Any] = {}
                if resigned_on_raw:
                    valid_to = _parse_ch_date(resigned_on_raw)
                    if valid_to is None:
                        # Present but unparseable: do NOT read as still
                        # serving. Record it as ended-but-unknown-when
                        # rather than open-ended.
                        unparseable_resigned_on += 1
                        edge_properties["resigned_on_unparsed"] = resigned_on_raw
                        edge_properties["resignation_status"] = "ended_date_unknown"
                else:
                    valid_to = None

                role = (item.get("officer_role") or "").strip()
                if role:
                    edge_properties["officer_role"] = role

                appointment_ref = _parse_appointment_self_link(item)
                if appointment_ref:
                    source_reference = appointment_ref
                elif officer_id:
                    source_reference = officer_id
                    edge_properties["source_reference_scope"] = "officer_id_not_appointment"
                else:
                    source_reference = f"{company_number}:{name}:{appointed_on or ''}"

                # Edge = THE CLAIM (no citation — spec v0.3 §7-bis)
                edge, _ = Edge.objects.get_or_create(
                    edge_type="officer_of",
                    source_entity=person_entity,
                    target_entity=company_entity,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    defaults={
                        "properties": edge_properties,
                    },
                )

                # Attestation = THE EVIDENCE
                Attestation.objects.get_or_create(
                    edge=edge,
                    source_name=SOURCE_NAME,
                    source_reference=source_reference,
                    defaults={
                        "source_url": officers_url,
                        "match_confidence": confidence,
                        "match_method": match_method,
                    },
                )
                edges_created += 1

    return {
        "edges_created": edges_created,
        "companies_processed": companies_processed,
        "companies_unmatched": companies_unmatched,
        "officers_no_id": officers_no_id,
        "missing_appointed_on": missing_appointed_on,
        "unparseable_resigned_on": unparseable_resigned_on,
        "total_officers": total_officers,
    }
