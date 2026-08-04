"""Oversea-company branch cross-reference resolver — the general remediation for
the NF/FC/SF gap `uncorrupt.staging.aliases` documented but could not close.

`aliases.py` (former company names, built from the bulk CSV) found that
NF/FC/SF-prefixed identifiers are NOT a rename/supersession mechanism — they are
Companies House's oversea-company branch-registration scheme. A branch's own
cross-reference to the same legal entity's separate "home" UK registration lives
in `foreign_company_details.registration_number` on the live REST API's
`/company/{legacy_number}` profile, a field the bulk CSV does not carry. This
module makes the one live API call per branch registration `aliases.py`
identified as the missing piece, and settles whether that mechanism generalises.

Fetch/build are two separate phases (mirrors `ch_officers.py` /
`ch_appointments.py`): `fetch_oversea_company_cross_references` hits the network
and caches one JSON body + provenance sidecar per legacy identifier, resumable
and rate-limited; `build_oversea_company_alias_table` reads only from that cache
(no network) and is therefore trivially deterministic given fixed cache
contents. Every outcome the API can produce is a distinct, counted, typed bucket
— never a silent drop:

- `resolved` — `type == "oversea-company"` and `foreign_company_details.
  registration_number` is present and normalises to a company number distinct
  from the legacy identifier itself. The only case that yields an alias.
- `no_cross_reference` — the profile is an oversea-company but carries no
  `registration_number` at all (the field is genuinely absent for that record,
  not a network failure).
- `not_oversea_company` — the profile exists but its `type` is not
  `"oversea-company"` (defensive: `oversea_company_legacy_numbers()` only
  selects NF/FC/SF-prefixed numbers, which the parent finding confirmed are
  exclusively this type, but this module verifies rather than assumes).
- `not_found` — CH returned 404 for this identifier. A real, informative
  answer (this legacy number does not exist as a live CH company), cached as
  a typed outcome (`{"status": "not_found"}`), not conflated with "no
  cross-reference" and not silently skipped.
- `self_referential` — the resolved target normalises to the same value as
  the legacy identifier itself (a degenerate, structurally meaningless
  "alias" no real branch registration should ever produce). Dropped rather
  than emitted.
- `unfetched` — no cache entry exists yet for this identifier (never
  attempted, or a prior attempt exhausted its retries and was never cached
  — see `fetch_oversea_company_cross_references`'s `failed` bucket) — distinct
  from every outcome above, all of which required a completed fetch.

Unlike `aliases.py`'s former-name matching (free-text, many-to-one, needs a
"which company ever carried this exact name" ambiguity guard), each legacy
identifier here is queried by its own unique registry ID — the CH company
number primary key — so the "two different targets claim the same alias name"
collision `aliases.py` guards against cannot structurally arise. The
`not_oversea_company`/`self_referential` buckets above are this module's
equivalent discipline: never emit an alias from data that does not
unambiguously mean what it would take to trust it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from uncorrupt.core.circuit_breaker import (
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    CircuitBreaker,
    CircuitOpenError,
)
from uncorrupt.register.loader import load_source
from uncorrupt.staging.aliases import CompanyAlias
from uncorrupt.staging.companies_house import normalise_company_number
from uncorrupt.staging.models import Company
from uncorrupt.staging.raw import read_cached_fetch, write_cached_fetch

CH_API_BASE = "https://api.company-information.service.gov.uk"
API_KEY_ENV_VAR = "COMPANIES_HOUSE_API_KEY"
# sources/uk_companies_house_oversea_company.yml — refuses to run without it (ADR-001 D5)
SOURCE_ID = "uk_companies_house_oversea_company"
CONNECTOR_VERSION = "0.1"

# CompanyAlias.source for rows this module produces (distinct from
# aliases.SOURCE_BULK_PREVIOUS_NAMES — a different mechanism, a different
# provenance trail, never merged into one string).
SOURCE_OVERSEA_CROSS_REFERENCE = "companies_house_api.foreign_company_details"

# NF/FC/SF are the three prefixes the parent finding (aliases.py) confirmed are
# Companies House's oversea-company branch-registration scheme. A Django ORM
# `__regex` anchored to end-of-string so a hypothetical longer prefix that
# merely starts with one of these two letters (never observed in the bulk
# snapshot, but not to be assumed) is not swept in by a loose `startswith`.
OVERSEA_COMPANY_PREFIXES = ("NF", "FC", "SF")
_OVERSEA_COMPANY_NUMBER_REGEX = r"^(?:NF|FC|SF)\d+$"

# CH allows 600 requests / 5 minutes. Stay under it (mirrors ch_appointments.py).
_THROTTLE_SECONDS = 0.55

# Cache entries older than this are refetched rather than trusted forever.
DEFAULT_MAX_CACHE_AGE_DAYS = 30

# Allowlist of profile fields kept before anything touches disk — organisation
# facts only, mirrors overseas_entities.py's _ALLOWED_PROFILE_FIELDS pattern.
_ALLOWED_PROFILE_FIELDS = ("company_number", "type", "foreign_company_details")

_STATUS_OK = "ok"
_STATUS_NOT_FOUND = "not_found"

logger = logging.getLogger(__name__)


def _require_api_key() -> str:
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        raise RuntimeError(
            f"{API_KEY_ENV_VAR} is not set. Get a free API key from "
            "https://developer.company-information.service.gov.uk/ and export it "
            f"as {API_KEY_ENV_VAR} before running this ingest."
        )
    return api_key


def oversea_company_legacy_numbers() -> list[str]:
    """`company_number` values in `staging.Company` with an NF/FC/SF prefix.

    Ordered by company_number for reproducibility. This is the pre-registered
    universe for this resolver — every NF/FC/SF row in the bulk snapshot,
    defined without reference to any benchmark row (mirrors
    `ch_officers.procurement_supplier_universe`'s reasoning for why the
    universe must be defined independently of what a benchmark happens to
    reference).
    """
    numbers = (
        Company.objects.filter(company_number__regex=_OVERSEA_COMPANY_NUMBER_REGEX)
        .order_by("company_number")
        .values_list("company_number", flat=True)
    )
    return list(numbers)


def _fetch_profile_with_backoff(
    client: httpx.Client, url: str, max_retries: int
) -> tuple[str, dict[str, Any] | None]:
    """GET `url`, returning (status, profile). `status` is `_STATUS_OK` with the
    parsed body, or `_STATUS_NOT_FOUND` with `None` — a 404 is a typed outcome,
    never silently folded into "no data". 429/5xx are retried with backoff
    (respecting `Retry-After` when present); exhausting `max_retries` raises
    RuntimeError so the caller can count and retry the identifier on the next
    invocation rather than caching a wrong answer.
    """
    delay = 2.0
    for _attempt in range(max_retries):
        response = client.get(url)
        if response.status_code == 200:
            result: dict[str, Any] = response.json()
            return _STATUS_OK, result
        if response.status_code == 404:
            return _STATUS_NOT_FOUND, None
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else delay)
            delay *= 2
            continue
        response.raise_for_status()
    raise RuntimeError(f"oversea-company profile fetch failed after {max_retries} retries: {url}")


def fetch_oversea_company_cross_references(
    legacy_company_numbers: Sequence[str],
    output_dir: str | Path,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    max_retries: int = 5,
    polite_delay_seconds: float = _THROTTLE_SECONDS,
    max_cache_age_days: int = DEFAULT_MAX_CACHE_AGE_DAYS,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
) -> dict[str, int]:
    """Fetch and cache each legacy identifier's CH company profile.

    Resumable: an identifier already validly cached in `output_dir` is skipped
    (`read_cached_fetch` — fresh and hash-verified), so an interrupted sweep
    across thousands of identifiers can be re-invoked with the same
    `output_dir` to pick up where it left off. A 404 is cached as a typed
    `{"status": "not_found"}` body — a real answer, counted in `not_found`,
    never silently dropped and never mistaken for "resolved: no
    cross-reference" on a later re-run. A fetch that exhausts its retries
    (429/5xx persisting, or any other HTTP error) writes NO cache file for
    that identifier, so it is picked up again as pending on the next
    invocation rather than being silently treated as done.

    A session-level circuit breaker aborts the whole sweep after
    `max_consecutive_failures` identifiers in a row fail (mirrors
    `ch_officers.fetch_company_officers`) — distinct from the per-request
    retry inside `_fetch_profile_with_backoff`.

    Returns `{fetched, cached, not_found, failed}`.
    """
    source = load_source(SOURCE_ID)
    api_key = api_key or _require_api_key()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    breaker = CircuitBreaker(threshold=max_consecutive_failures)

    counts = {"fetched": 0, "cached": 0, "not_found": 0, "failed": 0}

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, auth=httpx.BasicAuth(api_key, ""))

    try:
        for legacy_id in legacy_company_numbers:
            json_path = output_dir / f"{legacy_id}.json"
            provenance_path = output_dir / f"{legacy_id}.provenance.json"

            cached = read_cached_fetch(
                json_path,
                provenance_path,
                source=source,
                connector_version=CONNECTOR_VERSION,
                max_age_days=max_cache_age_days,
            )
            if cached is not None:
                breaker.record_success()
                counts["cached"] += 1
                if cached.extra.get("status") == _STATUS_NOT_FOUND:
                    counts["not_found"] += 1
                continue

            source_url = f"{CH_API_BASE}/company/{legacy_id}"
            try:
                status, profile = _fetch_profile_with_backoff(client, source_url, max_retries)
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning("oversea-company fetch failed for %s: %s", legacy_id, exc)
                counts["failed"] += 1
                try:
                    breaker.record_failure()
                except CircuitOpenError:
                    logger.error(
                        "oversea-company fetch: %d consecutive failures, aborting sweep "
                        "after %d identifiers",
                        breaker.consecutive_failures,
                        counts["fetched"] + counts["cached"],
                    )
                    break
                continue
            breaker.record_success()

            body: dict[str, Any]
            if status == _STATUS_NOT_FOUND:
                body = {"status": _STATUS_NOT_FOUND}
                counts["not_found"] += 1
            else:
                stripped = {
                    k: profile[k] for k in _ALLOWED_PROFILE_FIELDS if profile and k in profile
                }
                body = {"status": _STATUS_OK, **stripped}
                counts["fetched"] += 1

            # observed_at left unset -- a live current-register snapshot, no
            # separate capture date of its own (mirrors ch_officers.py/ch_appointments.py).
            write_cached_fetch(
                json.dumps(body, indent=2).encode(),
                json_path,
                provenance_path,
                source=source,
                source_url=source_url,
                connector_version=CONNECTOR_VERSION,
                extra={"legacy_company_number": legacy_id, "status": body["status"]},
            )
            time.sleep(polite_delay_seconds)
    finally:
        if owns_client:
            client.close()

    return counts


@dataclass(frozen=True)
class OverseaCompanyAliasReport:
    """Coverage report for one build run — every number measured, none guessed."""

    legacy_ids_considered: int
    resolved: int
    no_cross_reference: int
    not_oversea_company: int
    not_found: int
    self_referential: int
    unfetched: int
    aliases_written: int
    source: str


def build_oversea_company_alias_table(
    legacy_company_numbers: Sequence[str],
    input_dir: str | Path,
    source: str = SOURCE_OVERSEA_CROSS_REFERENCE,
) -> tuple[list[CompanyAlias], OverseaCompanyAliasReport]:
    """Turn cached oversea-company profiles into `CompanyAlias` rows.

    Pure read of `input_dir` — no network access — so this is deterministic
    given fixed cache contents: re-running against the same cache directory
    produces byte-identical `aliases` (sorted by (live_company_number,
    normalised_alias_name), mirrors `aliases.build_alias_table`) and an
    identical report.

    `retrieved_at`/`source_url` on each row come from that identifier's own
    provenance sidecar (the actual fetch time), not a single date passed in
    for the whole run — a multi-day resumable sweep fetches different
    identifiers on different days, and each alias row must stay independently
    auditable (mirrors `CompanyAlias`'s own docstring rationale).
    """
    input_dir = Path(input_dir)

    resolved = 0
    no_cross_reference = 0
    not_oversea_company = 0
    not_found = 0
    self_referential = 0
    unfetched = 0
    aliases: list[CompanyAlias] = []

    for legacy_id in legacy_company_numbers:
        json_path = input_dir / f"{legacy_id}.json"
        provenance_path = input_dir / f"{legacy_id}.provenance.json"
        if not json_path.exists() or not provenance_path.exists():
            unfetched += 1
            continue

        try:
            body = json.loads(json_path.read_text())
            provenance = json.loads(provenance_path.read_text())
            retrieved_at = provenance["retrieved_at"]
            source_url = provenance["source_url"]
        except (json.JSONDecodeError, KeyError, OSError):
            unfetched += 1
            continue

        status = body.get("status")
        if status == _STATUS_NOT_FOUND:
            not_found += 1
            continue
        if status != _STATUS_OK:
            unfetched += 1
            continue

        if body.get("type") != "oversea-company":
            not_oversea_company += 1
            continue

        foreign_details = body.get("foreign_company_details") or {}
        raw_target = (foreign_details.get("registration_number") or "").strip()
        if not raw_target:
            no_cross_reference += 1
            continue

        target = normalise_company_number(raw_target)
        normalised_legacy = normalise_company_number(legacy_id) or legacy_id
        if not target or target == normalised_legacy:
            self_referential += 1
            continue

        resolved += 1
        aliases.append(
            CompanyAlias(
                alias_name=legacy_id,
                normalised_alias_name=normalised_legacy,
                live_company_number=target,
                name_changed_on=None,
                source=source,
                source_url=source_url,
                retrieved_at=retrieved_at,
                alias_kind="legacy_identifier",
            )
        )

    aliases.sort(key=lambda a: (a.live_company_number, a.normalised_alias_name))

    report = OverseaCompanyAliasReport(
        legacy_ids_considered=len(legacy_company_numbers),
        resolved=resolved,
        no_cross_reference=no_cross_reference,
        not_oversea_company=not_oversea_company,
        not_found=not_found,
        self_referential=self_referential,
        unfetched=unfetched,
        aliases_written=len(aliases),
        source=source,
    )
    return aliases, report
