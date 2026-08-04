"""UK Register of Overseas Entities (ROE) ingest.

Source: the same Companies House REST API `ch_officers.py` already uses
(https://developer.company-information.service.gov.uk/). Since 2022
(Economic Crime (Transparency and Enforcement) Act 2022) an overseas entity
that owns UK land must register with Companies House and declare its
beneficial owners; on registration it is issued an OE-prefixed company
number (e.g. `OE027594`) and becomes queryable through the *ordinary*
company endpoints — no separate API. Verified live 2026-08-03 against real
records (not sample/fixture data):

- `/company/{oe_number}` resolves exactly like any other company, with
  `type: "registered-overseas-entity"` and a `foreign_company_details` block
  (the entity's registration in its *home* jurisdiction — its own registry ID
  there, e.g. Luxembourg RCS `B164962`).
- Beneficial owners are served by the *same* PSC endpoint ordinary companies
  use, `/company/{oe_number}/persons-with-significant-control` — CH
  distinguishes an overseas entity's beneficial owners from an ordinary
  company's PSCs purely by `kind`, which ends in `-beneficial-owner`
  (`individual-beneficial-owner`, `corporate-entity-beneficial-owner`,
  `legal-person-beneficial-owner`, `super-secure-beneficial-owner`) instead
  of `-person-with-significant-control`.
- An overseas entity that has no beneficial owner meeting the threshold
  instead registers "managing officers", served by the *same*
  `/company/{oe_number}/officers` endpoint `ch_officers.py` already uses,
  with `officer_role` of `managing-officer` (individual) or
  `corporate-managing-officer` (a corporate appointee) — CH's own
  `officer_role` enum convention throughout the API (`director` vs
  `corporate-director`, etc.).
- Enumeration: `/advanced-search/companies?company_type=registered-overseas-entity`
  lists all ROE entities directly — ~33,400 as of 2026-08-03. Like GLEIF's
  offset pagination (see `gleif.py`), it is capped at `start_index < 10,000`
  (verified: `start_index=9980&size=20` succeeds, `start_index=10000` returns
  HTTP 500). Unlike GLEIF there is no cursor escape hatch, but
  `incorporated_from`/`incorporated_to` slice the result set (verified live:
  Jan 2023 alone — the transitional registration deadline — is 9,784 of the
  9,973 in the whole of H2 2022), so a full crawl slices by incorporation-date
  window rather than raw offset.

Land-title join deliberately NOT implemented (verified, not assumed): the
assessment that prioritised this source described a join to HM Land
Registry's OCOD (Overseas Companies Ownership Data) dataset by OE number.
Checked live 2026-08-03 against HMLR's own dataset page and technical
specification (`use-land-property-data.service.gov.uk/datasets/ocod{,/tech-spec}`):
OCOD is free of charge (Open Government Licence v3.0, no fee) but is **not**
an anonymous bulk download — it requires creating an account and agreeing to
a data licence before an API key is issued, and its own documentation warns
of "mistakes in company registration numbers or no company registration
number" and "2 different companies with the same registration number." OCOD
has no dedicated OE-number column; it reuses a generic "Company Registration
No." field that (per HMLR's own Practice Guide 78 and Land Registry
practice-guidance, cross-checked via web search, not a downloaded file) is
populated with the OE ID for entities that have complied with the Land
Registry restriction, but is not guaranteed clean. Building that join without
downloading and inspecting a real OCOD file — which needs an account this
environment is not authorised to create — would be exactly the kind of
unverified assumption Phase 1 was meant to catch. This connector therefore
ingests the entity + beneficial-owner/managing-officer graph only; the land
join is future work for whoever can obtain OCOD access.

Scope boundary (ADR-004 D1) — the privacy-sensitive part of this connector:
overseas entities must declare beneficial owners, and unlike ordinary company
officers, a large fraction of those are private individuals (DOB, nationality,
residential address, a "principal office address"). Person-level work here is
gated behind an LIA/DPIA that has not been drafted, so:

- Corporate/legal-entity beneficial owners (`kind` in
  `corporate-entity-beneficial-owner`, `legal-person-beneficial-owner`)
  become full Entities — no personal data involved, only organisation facts
  (`identification`: legal form, legal authority, home registration number).
- Individual beneficial owners (`kind` in `individual-beneficial-owner`,
  `super-secure-beneficial-owner`) are NEVER turned into an Entity or Edge.
  Their existence is recorded only as a count
  (`Entity.properties["individual_beneficial_owner_count"]`) — no name, no
  DOB, no nationality, no address, ever, on disk or in the database. This is
  enforced by an allowlist keyed on `kind` at fetch time (`_filter_bo_item`),
  the same fail-closed allowlist-not-blocklist mechanism `ch_officers.py`
  uses for `_ALLOWED_OFFICER_FIELDS` — a personal field the API starts
  returning tomorrow is dropped by default, not persisted by default.
- Managing officers mirror `ch_officers.py`'s own precedent exactly, because
  they are the same endpoint and the same category of fact this codebase
  already accepts for ordinary company directors: `corporate-managing-officer`
  becomes a full Entity (no personal data); `managing-officer` (an
  individual) is treated with the SAME caution as an individual beneficial
  owner — count only
  (`Entity.properties["individual_managing_officer_count"]`), no name — on
  the view that a managing officer under this specific regime is disclosed
  *as an alternative to* naming a beneficial owner, so it sits inside the
  same beneficial-ownership privacy boundary this module is careful about,
  not the routine-officer-appointment case `ch_officers.py` already covers
  for ordinary companies.

Registry entry: `sources/uk_roe.yml` (`load_source("uk_roe")` below) — this
connector refuses to run without it, same contract as the older Connector
protocol (`uncorrupt.register.loader`).

Bitemporal correctness (the thing this module is asked to get right that a
prior review found `lords_interests.py` getting wrong — populating
`Attestation.observed_at` with today's download time instead of the source's
own date): CH's PSC `notified_on` and officer `appointed_on` are the source's
own transaction-time dates — when Companies House's register captured the
claim — so they become `Attestation.observed_at`, never `datetime.now()`.
`Attestation.snapshot_ref` is the SHA-256 of this company's cached (already
personal-data-filtered) raw JSON, mirroring `ch_officers.py`'s
`content_hash`.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import httpx
from django.db import transaction

from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.register.loader import load_source
from uncorrupt.register.models import SourceEntry
from uncorrupt.staging.companies_house import _normalise_name, normalise_company_number
from uncorrupt.staging.raw import read_cached_fetch, write_cached_fetch

CH_API_BASE = "https://api.company-information.service.gov.uk"
SOURCE_NAME = "Companies House"
SOURCE_ID = "uk_roe"
API_KEY_ENV_VAR = "COMPANIES_HOUSE_API_KEY"
CONNECTOR_VERSION = "0.1"

# The overseas entity itself.
REGISTRY_SCHEME = "GB-ROE"
# A corporate/legal-entity beneficial owner resolved by its home-registry number.
BO_REGISTRY_SCHEME = "GB-ROE-BENEFICIAL-OWNER"
# A corporate/legal-entity beneficial owner with no usable registration number —
# scoped to the OE entity it was found at (duplication over merging).
BO_REGISTRY_SCHEME_UNRESOLVED = "GB-ROE-BENEFICIAL-OWNER-UNRESOLVED"
# A corporate managing officer resolved by CH officer ID.
MANAGING_OFFICER_REGISTRY_SCHEME = "GB-ROE-MANAGING-OFFICER"
# A corporate managing officer with no stable CH officer ID — scoped to the OE entity.
MANAGING_OFFICER_REGISTRY_SCHEME_UNRESOLVED = "GB-ROE-MANAGING-OFFICER-UNRESOLVED"

# CH's own kind/officer_role vocabulary (verified live 2026-08-03, cross-checked
# against companieshouse/api-enumerations constants.yml) — never guessed.
CORPORATE_BO_KINDS = frozenset(
    {"corporate-entity-beneficial-owner", "legal-person-beneficial-owner"}
)
INDIVIDUAL_BO_KINDS = frozenset({"individual-beneficial-owner", "super-secure-beneficial-owner"})
CORPORATE_MANAGING_OFFICER_ROLE = "corporate-managing-officer"
INDIVIDUAL_MANAGING_OFFICER_ROLE = "managing-officer"

# Allowlist of fields kept for a corporate/legal-entity beneficial owner —
# organisation facts only. Never includes anything from the individual-BO
# shape (name_elements, date_of_birth, nationality) because that shape is
# dropped wholesale, not trimmed, by `_filter_bo_item`.
_ALLOWED_CORPORATE_BO_FIELDS = (
    "kind",
    "name",
    "notified_on",
    "ceased_on",
    "ceased",
    "natures_of_control",
    "identification",
    "is_sanctioned",
    "address",
    "links",
)

# Allowlist for officer records (managing officers), fail-closed exactly like
# `ch_officers._ALLOWED_OFFICER_FIELDS` — a new field the API returns
# tomorrow (date_of_birth, nationality, country_of_residence, occupation,
# person_number, address, responsibilities, ...) is dropped by default.
_ALLOWED_OFFICER_FIELDS = ("name", "officer_role", "appointed_on", "resigned_on", "links")

# Allowlist for the overseas entity's own company profile — organisation
# facts only, no person-level data exists on this endpoint at all.
_ALLOWED_PROFILE_FIELDS = (
    "company_name",
    "company_number",
    "company_status",
    "type",
    "date_of_creation",
    "jurisdiction",
    "foreign_company_details",
    "registered_office_address",
    "has_super_secure_pscs",
)

_OFFICER_ID_RE = re.compile(r"/officers/([^/]+)/appointments")

# CH advanced-search offset pagination is capped like GLEIF's page[number] —
# verified live: start_index=9980&size=20 succeeds, start_index=10000 fails
# with HTTP 500. Slice by incorporated_from/incorporated_to to go further.
ADVANCED_SEARCH_OFFSET_CAP = 10_000

_PERSON_MATCH_CONFIDENCE_NO_ID = 0.5
DEFAULT_MAX_CACHE_AGE_DAYS = 30


def _require_api_key() -> str:
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        raise RuntimeError(
            f"{API_KEY_ENV_VAR} is not set. Get a free API key from "
            "https://developer.company-information.service.gov.uk/ and export it "
            f"as {API_KEY_ENV_VAR} before running this ingest."
        )
    return api_key


def _require_source_registered() -> SourceEntry:
    """Refuse to run without `sources/uk_roe.yml` (mirrors the Connector protocol contract)."""
    return load_source(SOURCE_ID)


@dataclass(frozen=True)
class EnumerationFetchResult:
    """Provenance record for a downloaded slice of the ROE entity list."""

    jsonl_path: Path
    provenance_path: Path
    company_count: int
    hits: int
    truncated: bool
    source_url_template: str
    retrieved_at: datetime
    content_hash: str


@dataclass(frozen=True)
class EntityFetchResult:
    """Provenance record for one overseas entity's cached (filtered) detail bundle."""

    company_number: str
    json_path: Path
    provenance_path: Path
    source_url: str
    retrieved_at: datetime
    content_hash: str
    cached: bool


def _filter_bo_item(item: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw PSC/beneficial-owner record before it ever touches disk.

    A corporate/legal-entity beneficial owner keeps its allowlisted
    organisation fields. An individual or super-secure beneficial owner is
    reduced to a bare existence stub — `kind` and `ceased` only — so no name,
    DOB, nationality, or address is ever cached, let alone ingested.
    """
    kind = item.get("kind")
    if kind in CORPORATE_BO_KINDS:
        return {k: item[k] for k in _ALLOWED_CORPORATE_BO_FIELDS if k in item}
    return {"kind": kind, "ceased": item.get("ceased", False)}


def _filter_officer_item(item: dict[str, Any]) -> dict[str, Any]:
    """Strip a managing-officer record before it ever touches disk.

    A `corporate-managing-officer` keeps the same allowlisted fields
    `ch_officers._ALLOWED_OFFICER_FIELDS` keeps for ordinary company
    officers (name, role, dates, links) — no personal data involved. An
    individual `managing-officer` sits inside the same beneficial-ownership
    privacy boundary as an individual beneficial owner (see module
    docstring): reduced to a bare existence stub — `officer_role` and
    `resigned_on` only (enough to count active vs ceased) — no name, no
    links, not even on disk.
    """
    if item.get("officer_role") == CORPORATE_MANAGING_OFFICER_ROLE:
        return {k: item[k] for k in _ALLOWED_OFFICER_FIELDS if k in item}
    return {"officer_role": item.get("officer_role"), "resigned_on": item.get("resigned_on")}


def _filter_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {k: profile[k] for k in _ALLOWED_PROFILE_FIELDS if k in profile}


def fetch_overseas_entities(
    output_dir: str | Path,
    incorporated_from: date | None = None,
    incorporated_to: date | None = None,
    size: int = 100,
    max_retries: int = 5,
    polite_delay_seconds: float = 0.2,
    client: httpx.Client | None = None,
) -> EnumerationFetchResult:
    """Enumerate registered-overseas-entity companies via CH advanced search.

    Paginates `start_index`/`size` up to `ADVANCED_SEARCH_OFFSET_CAP`. If the
    window (`incorporated_from`..`incorporated_to`, or unbounded) has more
    matches than the cap can reach, `truncated=True` is returned and the
    provenance records the true `hits` count — callers doing a full crawl
    must narrow the date window rather than trust an under-count silently.

    Writes one JSON object per line (company_number, company_name,
    company_status, date_of_creation, registered_office_address) plus a
    provenance record into `output_dir`. Never writes personal data — this
    endpoint returns none.
    """
    source = _require_source_registered()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{incorporated_from or 'all'}_{incorporated_to or 'all'}"
    jsonl_path = output_dir / f"roe_{suffix}.jsonl"
    provenance_path = output_dir / f"roe_{suffix}.provenance.json"

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, auth=httpx.BasicAuth(_require_api_key(), ""))

    base_params: dict[str, str] = {"company_type": "registered-overseas-entity"}
    if incorporated_from:
        base_params["incorporated_from"] = incorporated_from.isoformat()
    if incorporated_to:
        base_params["incorporated_to"] = incorporated_to.isoformat()

    companies: list[dict[str, Any]] = []
    hits = 0
    start_index = 0

    try:
        while start_index < ADVANCED_SEARCH_OFFSET_CAP:
            # Clamp so a page never straddles the verified-safe offset cap
            # (confirmed live: start_index=9980&size=20, i.e. up to exactly
            # 10,000, succeeds; start_index=10000 returns HTTP 500).
            page_size = min(size, ADVANCED_SEARCH_OFFSET_CAP - start_index)
            params = {**base_params, "size": str(page_size), "start_index": str(start_index)}
            url = httpx.URL(f"{CH_API_BASE}/advanced-search/companies", params=params)
            payload = _fetch_page_with_backoff(client, url, max_retries)
            hits = payload.get("hits", 0)
            items = payload.get("items", [])
            if not items:
                break
            companies.extend(
                {
                    "company_number": item["company_number"],
                    "company_name": item.get("company_name"),
                    "company_status": item.get("company_status"),
                    "date_of_creation": item.get("date_of_creation"),
                    "registered_office_address": item.get("registered_office_address"),
                }
                for item in items
            )
            start_index += len(items)
            if start_index >= hits:
                break
            time.sleep(polite_delay_seconds)
    finally:
        if owns_client:
            client.close()

    truncated = hits > start_index and start_index >= ADVANCED_SEARCH_OFFSET_CAP

    jsonl_bytes = "".join(json.dumps(company) + "\n" for company in companies).encode("utf-8")
    source_url_template = (
        f"{CH_API_BASE}/advanced-search/companies?company_type=registered-overseas-entity"
        f"&incorporated_from={incorporated_from or ''}&incorporated_to={incorporated_to or ''}"
    )
    # observed_at left unset -- a live enumeration of the CURRENT register,
    # no separate capture date of its own.
    cached = write_cached_fetch(
        jsonl_bytes,
        jsonl_path,
        provenance_path,
        source=source,
        source_url=source_url_template,
        connector_version=CONNECTOR_VERSION,
        extra={
            "source_url_template": source_url_template,
            "company_count": len(companies),
            "hits": hits,
            "truncated": truncated,
            "incorporated_from": incorporated_from.isoformat() if incorporated_from else None,
            "incorporated_to": incorporated_to.isoformat() if incorporated_to else None,
        },
    )

    return EnumerationFetchResult(
        jsonl_path=jsonl_path,
        provenance_path=provenance_path,
        company_count=len(companies),
        hits=hits,
        truncated=truncated,
        source_url_template=source_url_template,
        retrieved_at=cached.provenance.retrieved_at,
        content_hash=cached.provenance.content_hash,
    )


def fetch_overseas_entity_details(
    company_numbers: Sequence[str],
    output_dir: str | Path,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    max_retries: int = 5,
    polite_delay_seconds: float = 1.0,
    items_per_page: int = 35,
    max_cache_age_days: int = DEFAULT_MAX_CACHE_AGE_DAYS,
) -> list[EntityFetchResult]:
    """Fetch profile + beneficial owners + managing officers for a bounded list of OE numbers.

    Resumable exactly like `ch_officers.fetch_company_officers`: a company
    already cached in `output_dir` is skipped unless stale
    (`max_cache_age_days`) or its content hash no longer matches the file on
    disk. Personal data is filtered out (see `_filter_bo_item` /
    `_filter_officer_item`) before anything is written — the cache on disk
    is exactly what gets ingested, nothing more.
    """
    source = _require_source_registered()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    client = client or httpx.Client(
        timeout=30.0, auth=httpx.BasicAuth(api_key or _require_api_key(), "")
    )

    results: list[EntityFetchResult] = []
    try:
        for company_number in company_numbers:
            json_path = output_dir / f"{company_number}.json"
            provenance_path = output_dir / f"{company_number}.provenance.json"

            cached = read_cached_fetch(
                json_path,
                provenance_path,
                source=source,
                connector_version=CONNECTOR_VERSION,
                max_age_days=max_cache_age_days,
            )
            if cached is not None:
                results.append(
                    EntityFetchResult(
                        company_number=company_number,
                        json_path=json_path,
                        provenance_path=provenance_path,
                        source_url=cached.provenance.source_url,
                        retrieved_at=cached.provenance.retrieved_at,
                        content_hash=cached.provenance.content_hash,
                        cached=True,
                    )
                )
                continue

            source_url = f"{CH_API_BASE}/company/{company_number}"
            profile = _fetch_json_with_backoff(client, source_url, max_retries)
            psc_items = _fetch_all_pages(
                client,
                f"{CH_API_BASE}/company/{company_number}/persons-with-significant-control",
                items_per_page,
                max_retries,
            )
            officer_items = _fetch_all_pages(
                client,
                f"{CH_API_BASE}/company/{company_number}/officers",
                items_per_page,
                max_retries,
            )

            bundle = {
                "profile": _filter_profile(profile) if profile else {},
                "psc": [_filter_bo_item(item) for item in psc_items],
                "officers": [_filter_officer_item(item) for item in officer_items],
            }

            # observed_at left unset -- a live current-register snapshot; the
            # ingest side sets Attestation.observed_at from each item's own
            # notified_on/appointed_on date, never from this cache's fetch time.
            written = write_cached_fetch(
                json.dumps(bundle, indent=2).encode(),
                json_path,
                provenance_path,
                source=source,
                source_url=source_url,
                connector_version=CONNECTOR_VERSION,
                extra={"company_number": company_number},
            )

            results.append(
                EntityFetchResult(
                    company_number=company_number,
                    json_path=json_path,
                    provenance_path=provenance_path,
                    source_url=source_url,
                    retrieved_at=written.provenance.retrieved_at,
                    content_hash=written.provenance.content_hash,
                    cached=False,
                )
            )
            time.sleep(polite_delay_seconds)
    finally:
        if owns_client:
            client.close()

    return results


def _fetch_page_with_backoff(
    client: httpx.Client, url: httpx.URL | str, max_retries: int
) -> dict[str, Any]:
    delay = 2.0
    for _attempt in range(max_retries):
        response = client.get(url)
        if response.status_code == 200:
            result: dict[str, Any] = response.json()
            return result
        if response.status_code == 404:
            return {}
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else delay)
            delay *= 2
            continue
        response.raise_for_status()
    raise RuntimeError(f"ROE fetch failed after {max_retries} retries: {url}")


def _fetch_json_with_backoff(client: httpx.Client, url: str, max_retries: int) -> dict[str, Any]:
    return _fetch_page_with_backoff(client, url, max_retries)


def _fetch_all_pages(
    client: httpx.Client, base_url: str, items_per_page: int, max_retries: int
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start_index = 0
    while True:
        params = {"items_per_page": items_per_page, "start_index": start_index}
        page = _fetch_page_with_backoff(client, httpx.URL(base_url, params=params), max_retries)
        page_items = page.get("items", [])
        items.extend(page_items)
        total_results = page.get("total_results", len(items))
        start_index += len(page_items)
        if not page_items or start_index >= total_results:
            break
    return items


def _parse_ch_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _observed_at(value: date | None) -> datetime | None:
    """The source's own transaction-time date, never today's download time."""
    if value is None:
        return None
    return datetime.combine(value, dt_time.min, tzinfo=UTC)


def _parse_officer_id(item: dict[str, Any]) -> str | None:
    officer_link = (item.get("links") or {}).get("officer") or {}
    appointments_link = officer_link.get("appointments")
    if not appointments_link:
        return None
    match = _OFFICER_ID_RE.search(appointments_link)
    return match.group(1) if match else None


def _parse_appointment_self_link(item: dict[str, Any]) -> str | None:
    self_link = (item.get("links") or {}).get("self")
    return self_link if isinstance(self_link, str) and self_link else None


def _bo_identity(item: dict[str, Any], oe_number: str) -> tuple[str, str, float, str]:
    """Resolve a corporate/legal-entity beneficial owner's identity.

    Returns (registry_scheme, registry_id, match_confidence, match_method).
    Prefers the owner's own home-registry number (real registry ID, per
    ADR-004 D2); falls back to a name scoped to the OE entity it was found
    at — duplication over merging, mirroring `ch_officers`'s unresolved-ID
    fallback.
    """
    identification = item.get("identification") or {}
    registration_number = (identification.get("registration_number") or "").strip()
    authority = (
        identification.get("legal_authority") or identification.get("place_registered") or ""
    ).strip()
    if registration_number and authority:
        registry_id = f"{_normalise_name(authority)}:{registration_number.upper()}"
        return BO_REGISTRY_SCHEME, registry_id, 1.0, "identifier"
    name = (item.get("name") or "").strip()
    registry_id = f"{oe_number}:{_normalise_name(name)}"
    return (
        BO_REGISTRY_SCHEME_UNRESOLVED,
        registry_id,
        _PERSON_MATCH_CONFIDENCE_NO_ID,
        "name_company_scoped",
    )


def ingest_overseas_entities(
    company_numbers: Sequence[str], input_dir: str | Path
) -> dict[str, Any]:
    """Ingest previously-fetched ROE detail bundles into Entity/Edge/Attestation rows.

    Returns summary stats: {entities_created, entities_updated,
    companies_unmatched, corporate_bo_edges_created, individual_bo_count,
    corporate_managing_officer_edges_created,
    individual_managing_officer_count, total_companies}.
    """
    _require_source_registered()
    input_dir = Path(input_dir)

    stats = {
        "entities_created": 0,
        "entities_updated": 0,
        "companies_unmatched": 0,
        "corporate_bo_edges_created": 0,
        "individual_bo_count": 0,
        "corporate_managing_officer_edges_created": 0,
        "individual_managing_officer_count": 0,
        "total_companies": 0,
    }

    with transaction.atomic():
        for raw_number in company_numbers:
            stats["total_companies"] += 1
            oe_number = normalise_company_number(raw_number) or raw_number
            json_path = input_dir / f"{raw_number}.json"
            provenance_path = input_dir / f"{raw_number}.provenance.json"
            if not json_path.exists() or not provenance_path.exists():
                stats["companies_unmatched"] += 1
                continue

            bundle = json.loads(json_path.read_text())
            provenance = json.loads(provenance_path.read_text())
            snapshot_ref = provenance["content_hash"].removeprefix("sha256:")
            profile = bundle.get("profile") or {}
            psc_items = bundle.get("psc") or []
            officer_items = bundle.get("officers") or []

            individual_bo_count = sum(
                1
                for item in psc_items
                if item.get("kind") in INDIVIDUAL_BO_KINDS and not item.get("ceased")
            )
            individual_officer_count = sum(
                1
                for item in officer_items
                if item.get("officer_role") == INDIVIDUAL_MANAGING_OFFICER_ROLE
                and not item.get("resigned_on")
            )
            stats["individual_bo_count"] += individual_bo_count
            stats["individual_managing_officer_count"] += individual_officer_count

            oe_entity, oe_created = Entity.objects.update_or_create(
                entity_type="company",
                registry_scheme=REGISTRY_SCHEME,
                registry_id=oe_number,
                defaults={
                    "name": profile.get("company_name") or oe_number,
                    "properties": {
                        **profile,
                        "individual_beneficial_owner_count": individual_bo_count,
                        "individual_managing_officer_count": individual_officer_count,
                    },
                },
            )
            if oe_created:
                stats["entities_created"] += 1
            else:
                stats["entities_updated"] += 1

            psc_url = f"{CH_API_BASE}/company/{raw_number}/persons-with-significant-control"
            for item in psc_items:
                if item.get("kind") not in CORPORATE_BO_KINDS:
                    continue
                registry_scheme, registry_id, confidence, match_method = _bo_identity(
                    item, oe_number
                )
                bo_entity, _ = Entity.objects.get_or_create(
                    entity_type="company",
                    registry_scheme=registry_scheme,
                    registry_id=registry_id,
                    defaults={"name": item.get("name") or registry_id},
                )
                valid_from = _parse_ch_date(item.get("notified_on"))
                valid_to = _parse_ch_date(item.get("ceased_on"))
                edge, _ = Edge.objects.get_or_create(
                    edge_type="ownership",
                    source_entity=bo_entity,
                    target_entity=oe_entity,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    defaults={
                        "properties": {
                            "bo_kind": item.get("kind"),
                            "natures_of_control": item.get("natures_of_control") or [],
                        }
                    },
                )
                source_reference = _parse_appointment_self_link(item) or (
                    f"{raw_number}:{registry_id}"
                )
                Attestation.objects.get_or_create(
                    edge=edge,
                    source_name=SOURCE_NAME,
                    source_reference=source_reference,
                    defaults={
                        "source_url": psc_url,
                        "match_confidence": confidence,
                        "match_method": match_method,
                        "observed_at": _observed_at(valid_from),
                        "snapshot_ref": snapshot_ref,
                    },
                )
                stats["corporate_bo_edges_created"] += 1

            officers_url = f"{CH_API_BASE}/company/{raw_number}/officers"
            for item in officer_items:
                if item.get("officer_role") != CORPORATE_MANAGING_OFFICER_ROLE:
                    continue
                name = (item.get("name") or "").strip()
                officer_id = _parse_officer_id(item)
                if officer_id:
                    officer_entity, _ = Entity.objects.get_or_create(
                        entity_type="company",
                        registry_scheme=MANAGING_OFFICER_REGISTRY_SCHEME,
                        registry_id=officer_id,
                        defaults={"name": name},
                    )
                    confidence = 1.0
                    match_method = "identifier"
                else:
                    officer_entity, _ = Entity.objects.get_or_create(
                        entity_type="company",
                        registry_scheme=MANAGING_OFFICER_REGISTRY_SCHEME_UNRESOLVED,
                        registry_id=f"{raw_number}:{_normalise_name(name)}",
                        defaults={"name": name},
                    )
                    confidence = _PERSON_MATCH_CONFIDENCE_NO_ID
                    match_method = "name_company_scoped"

                valid_from = _parse_ch_date(item.get("appointed_on"))
                valid_to = _parse_ch_date(item.get("resigned_on"))
                edge, _ = Edge.objects.get_or_create(
                    edge_type="officer_of",
                    source_entity=officer_entity,
                    target_entity=oe_entity,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    defaults={"properties": {"officer_role": CORPORATE_MANAGING_OFFICER_ROLE}},
                )
                source_reference = _parse_appointment_self_link(item) or (
                    officer_id or f"{raw_number}:{name}:{item.get('appointed_on') or ''}"
                )
                Attestation.objects.get_or_create(
                    edge=edge,
                    source_name=SOURCE_NAME,
                    source_reference=source_reference,
                    defaults={
                        "source_url": officers_url,
                        "match_confidence": confidence,
                        "match_method": match_method,
                        "observed_at": _observed_at(valid_from),
                        "snapshot_ref": snapshot_ref,
                    },
                )
                stats["corporate_managing_officer_edges_created"] += 1

    return stats
