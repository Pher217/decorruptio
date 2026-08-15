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

Coverage expansion (2026-08): the target set was originally ~1,000
companies, leaving most `GB-COH` company Entities with no direct roster
fetch at all — a benchmark row that cannot reach the officer layer through
any of them is untestable, not a negative result. Two things must both
hold for an expansion to be scientifically defensible, not a manufactured
result:

1. **The universe must be defined without reference to the benchmark at
   all.** Expanding "symmetrically" to positive AND matched-negative
   companies still gives every benchmark-referenced company preferential
   ascertainment over the rest of the register — that is the same
   ascertainment bias wearing a fairness label. `procurement_supplier_universe`
   instead pulls the company_numbers Companies House suppliers were already
   resolved to (`staging.AwardResolution`, built from the frozen
   procurement corpus) — a set that exists independently of which rows
   ended up in any golden/benchmark set.
2. **The traversal order over that universe must not correlate with a
   real-world company attribute.** Company-number ascending is a bad
   stopping rule: CH numbers correlate with incorporation cohort, so a
   partial sweep in that order systematically over-samples older
   companies. `salted_hash_order` sorts by `sha256(salt + company_number)`
   instead — deterministic and reproducible given the same salt, but
   uncorrelated with age, sector, or size. The salt must be pinned once
   per sweep (recorded via `append_run_manifest`) and reused by every
   partial run of that sweep, not regenerated each time.

`select_next_pending` turns `--limit` into a resumable partial run over
that (already hash-ordered) list: already fresh-cached companies are
skipped rather than reprocessed or counted against the limit, so
re-invoking with the same list and salt picks up exactly where a previous
partial run left off. `coverage_report` / `procurement_universe_coverage_report`
tell apart a company with zero officer edges from one whose only
officer_of edge is incidental (landed there via someone else's appointment
walk, never itself roster-fetched). `append_run_manifest` records the
selection rule, salt, timestamp, and counts for every run so a later
reader can tell whether a given company was ever attempted, and why.

The second-hop appointment expansion (`ch_appointments`) is deliberately a
single frontier: officers discovered while seeding this batch of companies
get their *other* appointments expanded once, and that is where it stops —
no recursive re-expansion of companies discovered via that hop. Anything
beyond that pre-registered stopping point is a separate, clearly-labelled
sensitivity analysis, never folded into the same run.
"""

from __future__ import annotations

import hashlib
import json
import logging
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

from uncorrupt.core.circuit_breaker import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    CircuitBreaker,
    CircuitOpenError,
)
from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.register.loader import load_source
from uncorrupt.register.models import SourceEntry
from uncorrupt.staging.companies_house import _normalise_name, normalise_company_number
from uncorrupt.staging.models import AwardResolution, Company
from uncorrupt.staging.raw import read_cached_fetch, write_cached_fetch

CH_API_BASE = "https://api.company-information.service.gov.uk"
SOURCE_NAME = "Companies House"
API_KEY_ENV_VAR = "COMPANIES_HOUSE_API_KEY"
# sources/uk_companies_house_officers.yml — connector refuses to run without it (ADR-001 D5)
SOURCE_ID = "uk_companies_house_officers"
CONNECTOR_VERSION = "0.1"

logger = logging.getLogger(__name__)

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


def fetch_company_officers(
    company_numbers: Sequence[str],
    output_dir: str | Path,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    max_retries: int = 5,
    polite_delay_seconds: float = 1.0,
    items_per_page: int = 35,
    max_cache_age_days: int = DEFAULT_MAX_CACHE_AGE_DAYS,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
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

    A single company's fetch failing (exhausted retries, a non-retryable
    HTTP error) does not abort the whole run: the failure is logged and
    that company is skipped -- it is simply absent from the returned list
    (no cache files are written for it either, so it is picked up again as
    pending on the next invocation). Mirrors the same per-item resilience
    already used in `ch_appointments.fetch_officer_appointments` -- an
    unattended multi-hour sweep across tens of thousands of companies must
    survive one bad company, not die on it.

    A session-level circuit breaker (`uncorrupt.core.circuit_breaker`) aborts
    the whole sweep -- returning whatever was already fetched -- after
    `max_consecutive_failures` companies IN A ROW fail. This is distinct from
    the per-request retry inside `_fetch_all_officer_pages`: it is for the
    case where those retries themselves keep failing across many companies
    (a revoked API key, an outage), so a run over thousands of companies
    gives up instead of grinding through the rest at the same failure rate.
    A cache hit or a successful fetch resets the counter.
    """
    source = load_source(SOURCE_ID)  # refuses without uk_companies_house_officers.yml
    api_key = api_key or _require_api_key()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    breaker = CircuitBreaker(threshold=max_consecutive_failures)

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, auth=httpx.BasicAuth(api_key, ""))

    results: list[OfficersFetchResult] = []
    try:
        for company_number in company_numbers:
            json_path = output_dir / f"{company_number}.json"
            provenance_path = output_dir / f"{company_number}.provenance.json"
            source_url = f"{CH_API_BASE}/company/{company_number}/officers"

            cached = read_cached_fetch(
                json_path,
                provenance_path,
                source=source,
                connector_version=CONNECTOR_VERSION,
                max_age_days=max_cache_age_days,
            )
            if cached is not None:
                breaker.record_success()
                results.append(
                    OfficersFetchResult(
                        company_number=company_number,
                        json_path=json_path,
                        provenance_path=provenance_path,
                        officer_count=cached.extra["officer_count"],
                        source_url=cached.provenance.source_url,
                        retrieved_at=cached.provenance.retrieved_at,
                        content_hash=cached.provenance.content_hash,
                        cached=True,
                    )
                )
                continue

            try:
                items = _fetch_all_officer_pages(client, source_url, items_per_page, max_retries)
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning("officers fetch failed for %s: %s", company_number, exc)
                try:
                    breaker.record_failure()
                except CircuitOpenError:
                    logger.error(
                        "officers fetch: %d consecutive failures, aborting sweep after "
                        "%d companies",
                        breaker.consecutive_failures,
                        len(results),
                    )
                    break
                continue
            breaker.record_success()
            items = [_strip_personal_fields(item) for item in items]

            # observed_at left unset -- a live current-register snapshot, no
            # separate capture date of its own (mirrors gleif.py/ec_donations.py).
            written = write_cached_fetch(
                json.dumps(items, indent=2).encode(),
                json_path,
                provenance_path,
                source=source,
                source_url=source_url,
                connector_version=CONNECTOR_VERSION,
                extra={"company_number": company_number, "officer_count": len(items)},
            )

            results.append(
                OfficersFetchResult(
                    company_number=company_number,
                    json_path=json_path,
                    provenance_path=provenance_path,
                    officer_count=len(items),
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


def procurement_supplier_universe() -> list[str]:
    """Company numbers of every CH supplier resolved from the frozen procurement corpus.

    This is the pre-registered universe for officer-coverage expansion. It
    is built from `staging.AwardResolution` -- one resolution row per Award,
    already resolved to a Companies House company from the procurement award
    data (ADR-012 D1: `AwardResolution` replaced `SupplierResolution` as the
    per-award resolution grain; this function moved with it, same invariant)
    -- and defined **without any reference to benchmark/gold-row
    membership**. Deriving the universe from which companies happen to
    appear in a positive or negative benchmark row -- even applied
    identically to both -- still gives every benchmark-referenced company
    preferential ascertainment over the rest of the register: expanding
    coverage only where the benchmark already looks would manufacture the
    very result the expansion is meant to test.

    The true invariant is `company__isnull=False` -- a verified FK to an
    actual `staging.Company` row -- NOT `company_number__isnull=False`.
    `resolve_suppliers` (`staging/companies_house.py`) sets `company_number`
    to the normalised external identifier even when the match against
    the CH bulk snapshot **failed** (`company=None`, `match_confidence=0.0`,
    see the "GB-COH identifier ... not found" branch there) -- so filtering
    on `company_number` alone silently admits unmatched identifiers into the
    universe. `company__isnull=False` and `match_confidence__gt=0` are
    equivalent given how `resolve_suppliers` writes rows (every branch that
    sets `company` also sets a positive confidence, and vice versa); the FK
    is used here because it is the structural fact, not a derived number
    that could drift if a future match tier assigns partial confidence
    without a resolved company.

    Ordering is stable (company_number ascending) only so the *set* this
    function returns is reproducible before `salted_hash_order` is applied
    to it for traversal -- it is not meant to be used as the traversal
    order itself.
    """
    numbers = (
        AwardResolution.objects.filter(company__isnull=False)
        .values_list("company_number", flat=True)
        .distinct()
        .order_by("company_number")
    )
    return [n for n in numbers if n is not None]


def salted_hash_order(company_numbers: Sequence[str], salt: str) -> list[str]:
    """Order company_numbers by sha256(salt + company_number), ascending.

    Company-number ascending is a bad stopping order for a partial sweep:
    CH numbers correlate with incorporation cohort, so stopping partway
    through an ascending sweep systematically over-samples older
    companies. A precommitted salted-hash order is deterministic and fully
    reproducible given the same salt, but uncorrelated with any real-world
    attribute of the company (age, sector, size, ...), so a partial sweep
    is an unbiased subsample of the universe at whatever point it stops --
    not just once it eventually completes.

    The salt must be pinned once per sweep and reused by every partial run
    of that sweep (see `append_run_manifest` / `scripts/ingest_ch_officers.py`);
    regenerating it on each invocation would silently reshuffle which
    companies count as "already sampled".
    """
    return sorted(company_numbers, key=lambda n: hashlib.sha256(f"{salt}{n}".encode()).hexdigest())


def select_next_pending(
    company_numbers: Sequence[str],
    output_dir: str | Path,
    limit: int | None,
    max_cache_age_days: int = DEFAULT_MAX_CACHE_AGE_DAYS,
) -> list[str]:
    """Select the next `limit` companies, in the given order, not already validly cached.

    This is what makes `--limit` resumable rather than a fixed head-slice:
    re-invoking with the same ordered company list picks up wherever the
    previous partial run left off, because already-attempted companies (a
    fresh, hash-verified cache entry present) are skipped rather than
    reprocessed or counted against the limit. The input order is never
    reshuffled, so a partial run over a deterministically-ordered company
    list stays reproducible and cannot be cherry-picked. Callers should
    pass a list already ordered by `salted_hash_order` rather than a raw
    attribute like company number ascending, which correlates with
    incorporation cohort and would bias a partial sweep towards older
    companies.

    `limit=None` returns every company unchanged (existing behaviour), with
    no register lookup at all.
    """
    if limit is None:
        return list(company_numbers)

    source = load_source(SOURCE_ID)  # refuses without uk_companies_house_officers.yml
    output_dir = Path(output_dir)
    pending: list[str] = []
    for company_number in company_numbers:
        if len(pending) >= limit:
            break
        if _is_freshly_cached(company_number, output_dir, max_cache_age_days, source):
            continue
        pending.append(company_number)
    return pending


def _is_freshly_cached(
    company_number: str, output_dir: Path, max_cache_age_days: int, source: SourceEntry
) -> bool:
    """True if company_number already has a fresh, hash-verified cache entry.

    Mirrors the resumability check inside `fetch_company_officers` (both go
    through `read_cached_fetch`, which treats unreadable/tampered/stale
    provenance as "not cached" rather than raising) -- an unattended
    multi-hour sweep across tens of thousands of companies must survive one
    corrupted file, matching the pattern already used for the same problem
    in `ch_appointments`.
    """
    json_path = output_dir / f"{company_number}.json"
    provenance_path = output_dir / f"{company_number}.provenance.json"
    cached = read_cached_fetch(
        json_path,
        provenance_path,
        source=source,
        connector_version=CONNECTOR_VERSION,
        max_age_days=max_cache_age_days,
    )
    return cached is not None


def officer_ids_for_companies(company_numbers: Sequence[str]) -> list[str]:
    """Distinct GB-COH-OFFICER registry IDs currently linked to any of the given companies.

    Used to scope the second-hop appointment expansion (`ch_appointments`) to
    officers just discovered while seeding this batch of companies, rather
    than silently re-expanding the whole graph every time coverage is
    widened for an arbitrary subset.
    """
    normalised = [normalise_company_number(n) for n in company_numbers]
    registry_ids = (
        Entity.objects.filter(
            entity_type="person",
            registry_scheme="GB-COH-OFFICER",
            outgoing_edges__edge_type="officer_of",
            outgoing_edges__target_entity__company_number__in=normalised,
        )
        .order_by("registry_id")
        .values_list("registry_id", flat=True)
        .distinct()
    )
    return [r for r in registry_ids if r is not None]


def coverage_report() -> dict[str, Any]:
    """Report GB-COH officer coverage without mutating any data.

    Splits all `GB-COH` company Entities into three tiers so "no officer
    edge found" can be told apart from "never actually queried" -- the
    distinction that matters when a benchmark row cannot reach the officer
    layer at all (a "no path found" result is untestable, not a negative):

    - `direct_roster_fetch`: at least one officer_of edge attested by a
      direct `/company/{number}/officers` call -- a full roster fetch was
      made for this company.
    - `appointment_hop_only`: has an officer_of edge, but only because an
      already-known officer's `/officers/{id}/appointments` walk happened to
      land here -- an incidental, partial officer list, not a fetched
      roster. A missing officer at this company cannot be distinguished
      from "we never looked."
    - `zero_officers`: no officer_of edge at all -- coverage is completely
      absent.

    `direct_roster_fetch + appointment_hop_only + zero_officers ==
    total_gb_coh_companies` always (strict partition).

    `direct_roster_fetch_by_universe_membership` further splits
    `direct_roster_fetch` by whether the company is inside TODAY'S
    benchmark-independent `procurement_supplier_universe()` -- blending the
    pre-feature seed (which may have been assembled by hand, potentially
    from benchmark rows -- there is no recorded provenance for edges
    created before `selection_rule` tagging existed) with the clean
    universe would hide exactly the contamination this expansion exists to
    fix. This is a live re-check against the current universe, not a read
    of any persisted tag -- most existing edges predate `selection_rule`
    tagging entirely and would otherwise show as unlabelled either way.
    """
    company_ids = set(
        Entity.objects.filter(entity_type="company", registry_scheme="GB-COH").values_list(
            "id", flat=True
        )
    )
    tiers = _officer_coverage_tiers(company_ids)
    direct_fetch_ids = _direct_roster_fetch_entity_ids(company_ids)
    universe = set(procurement_supplier_universe())
    in_universe = len(
        set(
            Entity.objects.filter(id__in=direct_fetch_ids, company_number__in=universe).values_list(
                "id", flat=True
            )
        )
    )
    return {
        "total_gb_coh_companies": len(company_ids),
        **tiers,
        "direct_roster_fetch_by_universe_membership": {
            "in_procurement_universe": in_universe,
            "outside_procurement_universe": tiers["direct_roster_fetch"] - in_universe,
        },
    }


def procurement_universe_coverage_report() -> dict[str, int]:
    """Officer coverage restricted to the benchmark-independent procurement-supplier universe.

    Same three-tier split as `coverage_report` (see there for what each
    tier means), but scoped to `procurement_supplier_universe()` rather
    than all ~210k GB-COH entities the graph happens to already contain --
    the universe pre-registered for a defensible coverage-expansion run.
    `universe_with_graph_entity` is how many of the universe even have a
    graph Entity yet; the rest have never been touched by the graph
    pipeline at all and fall into `zero_officers`.
    """
    universe = set(procurement_supplier_universe())
    company_ids = set(
        Entity.objects.filter(
            entity_type="company", registry_scheme="GB-COH", company_number__in=universe
        ).values_list("id", flat=True)
    )
    tiers = _officer_coverage_tiers(company_ids)
    tiers["zero_officers"] += len(universe) - len(company_ids)
    return {
        "universe_size": len(universe),
        "universe_with_graph_entity": len(company_ids),
        **tiers,
    }


def _direct_roster_fetch_entity_ids(company_ids: set[int]) -> set[int]:
    """Entity ids among `company_ids` with at least one direct `/officers` roster fetch."""
    return set(
        Attestation.objects.filter(
            source_name=SOURCE_NAME,
            source_url__endswith="/officers",
            edge__edge_type="officer_of",
            edge__target_entity_id__in=company_ids,
        ).values_list("edge__target_entity_id", flat=True)
    )


def _any_officer_entity_ids(company_ids: set[int]) -> set[int]:
    """Entity ids among `company_ids` with at least one officer_of edge of any provenance."""
    return set(
        Edge.objects.filter(edge_type="officer_of", target_entity_id__in=company_ids).values_list(
            "target_entity_id", flat=True
        )
    )


def _officer_coverage_tiers(company_ids: set[int]) -> dict[str, int]:
    """Partition `company_ids` (GB-COH company Entity ids) into three officer-coverage tiers.

    See `coverage_report` for what `direct_roster_fetch` /
    `appointment_hop_only` / `zero_officers` mean.
    """
    direct_fetch_ids = _direct_roster_fetch_entity_ids(company_ids)
    any_officer_ids = _any_officer_entity_ids(company_ids)
    return {
        "direct_roster_fetch": len(direct_fetch_ids),
        "appointment_hop_only": len(any_officer_ids - direct_fetch_ids),
        "zero_officers": len(company_ids) - len(any_officer_ids),
    }


def append_run_manifest(output_dir: str | Path, **fields: Any) -> Path:
    """Append one audit record to `{output_dir}/run_manifest.jsonl`.

    Each CLI invocation appends a line recording the selection rule used,
    the run timestamp, and outcome counts -- so a later reader can tell
    whether (and why) a given company set was ever attempted, without having
    to enumerate every per-company cache file (auditability requirement for
    an arbitrary, partial, resumable expansion of officer coverage).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "run_manifest.jsonl"
    record = {"recorded_at": datetime.now(UTC).isoformat(), **fields}
    with manifest_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return manifest_path


def _canonical_company_entity(company: Company) -> Entity:
    """The Companies House node for a company, creating it if absent.

    Mirrors ch_appointments._canonical_company_entity. A plain
    ``get_or_create(company_number=...)`` raises MultipleObjectsReturned
    here: GLEIF can hold a distinct Entity for the same company under
    registry_scheme="GLEIF-LEI" that also carries this company_number --
    those are legitimately separate claims and must never be merged
    (ADR-006, duplicate over merge). Resolving on registry_scheme="GB-COH"
    + registry_id is unique by DB constraint, so this can never be
    ambiguous.
    """
    coh = Entity.objects.filter(
        entity_type="company",
        registry_scheme="GB-COH",
        registry_id=company.company_number,
    ).first()
    if coh:
        return coh
    entity, _ = Entity.objects.get_or_create(
        entity_type="company",
        registry_scheme="GB-COH",
        registry_id=company.company_number,
        defaults={
            "name": company.company_name,
            "company_number": company.company_number,
        },
    )
    return entity


def ingest_company_officers(
    company_numbers: Sequence[str],
    input_dir: str | Path,
    selection_rule: str | None = None,
) -> dict[str, Any]:
    """Ingest previously-fetched officer JSON files into Entity/Edge rows.

    Reads `{company_number}.json` files written by `fetch_company_officers`
    out of `input_dir`. Returns summary stats: {edges_created,
    companies_processed, companies_unmatched, ambiguous_company_number,
    officers_no_id, missing_appointed_on, unparseable_resigned_on,
    total_officers}.

    `selection_rule`, when given, is stamped onto `Edge.properties
    ["selection_rule"]` for every officer_of edge **newly created** by this
    call -- provenance of *why* this edge was fetched (e.g.
    "universe=procurement-suppliers"), independent of `coverage_report`'s
    live universe-membership check, which can only answer that question
    for whatever the universe looks like today. Because the edge is
    `get_or_create`d, only the first ingest to create a given edge sets
    this: a later re-fetch of the same company under a different rule does
    not overwrite it. That is intentional -- the tag answers "was this edge
    ever selected via an unlabelled/legacy path", and a fetch that finds an
    edge already exists changes nothing about how it first came to exist.
    """
    load_source(SOURCE_ID)  # refuses to run without sources/uk_companies_house_officers.yml
    input_dir = Path(input_dir)
    edges_created = 0
    companies_processed = 0
    companies_unmatched = 0
    ambiguous_company_number = 0
    officers_no_id = 0
    missing_appointed_on = 0
    unparseable_resigned_on = 0
    total_officers = 0

    for company_number in company_numbers:
        json_path = input_dir / f"{company_number}.json"
        if not json_path.exists():
            companies_unmatched += 1
            continue

        company = Company.objects.filter(
            company_number=normalise_company_number(company_number)
        ).first()
        if company is None:
            companies_unmatched += 1
            continue

        items = json.loads(json_path.read_text())
        officers_url = f"{CH_API_BASE}/company/{company_number}/officers"

        # Commit per company, not the whole sweep in one transaction: a
        # giant transaction over tens of thousands of companies holds locks
        # for the entire ingest and loses everything already processed if
        # one row (or the process) dies partway through -- mirrors the same
        # discipline in lords_interests.py / parliament_interests.py.
        try:
            with transaction.atomic():
                company_entity = _canonical_company_entity(company)
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
                        # No stable CH officer ID: never merge same-named
                        # people across companies (governing principle —
                        # duplication over merging). Scope the identity to
                        # THIS company so "John Smith" at Company A and
                        # "John Smith" at Company B are always distinct
                        # entities unless proven otherwise.
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

                    if selection_rule:
                        edge_properties["selection_rule"] = selection_rule

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
        except Entity.MultipleObjectsReturned:
            # A company_number can legitimately resolve to 2+ Entity rows
            # under different registry schemes (GB-COH, GLEIF-LEI -- ADR-006
            # duplicate-over-merge). Count and move on rather than losing
            # the whole run to one row -- this killed one multi-hour sweep
            # at the ingest step after ~24,500 artifacts had been fetched.
            ambiguous_company_number += 1
            continue

    return {
        "edges_created": edges_created,
        "companies_processed": companies_processed,
        "companies_unmatched": companies_unmatched,
        "ambiguous_company_number": ambiguous_company_number,
        "officers_no_id": officers_no_id,
        "missing_appointed_on": missing_appointed_on,
        "unparseable_resigned_on": unparseable_resigned_on,
        "total_officers": total_officers,
    }
