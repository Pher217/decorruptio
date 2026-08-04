"""UK Parliament Register of Members' Financial Interests ingest (Phase 1.3).

Source: https://interests-api.parliament.uk — the public Interests API behind
the Register of Members' Financial Interests. Structured JSON, no auth
required. Verified live against the API on 2026-07-26.

Coverage note: this API only serves the House of Commons register (every
`Registers`/`Categories` row returned is `type: "Commons"`; there is no
`house=Lords` data available through this endpoint). The House of Lords
Register of Interests is a separate, non-API (document-based) publication —
out of scope for this module. Only Commons members are ingested here.

Scope boundary (ADR-004 D1): public-function office holders only. Two of the
register's categories — "Family members employed" and "Family members
engaged in third-party lobbying" — concern the member's relatives, not the
member's own public role, and are excluded entirely (see
`_is_family_category`). Individual (non-organisation) donors/payers are
likewise never turned into Entities, mirroring `uncorrupt.graph.ec_donations`.

Interest hierarchy: some categories (notably "Employment and earnings")
register a payer once as a *parent* interest, then each payment against that
payer as a *child* interest carrying its own value/dates. Fetching with
`ExpandChildInterests=true` embeds children under their parent in a single
response, avoiding one HTTP call per payment. Each child is ingested as its
own `declared_interest` edge (child fields take priority; parent fields —
e.g. `PayerName` — fill gaps the child doesn't carry). Interests with no
children are ingested directly.

Nested donors: "Visits outside the UK" does not carry a flat counterparty
field at all — its sponsor(s) live under a `Donors` field of API type
`Donor[]`, whose real payload is nested in that field's own `values` key (a
list of donor groups, each a field-list in the same shape as a top-level
`fields` list), not its `value` key like every other field. A visit can name
more than one donor (two organisations jointly funding one trip); each donor
group is ingested as its own `declared_interest` edge, sharing the visit's
`interest_id`/dates but reading its own name/`IsPrivateIndividual`/`Value`
(see `_donor_group_field_lists`, `_counterparty_groups`). Before this was
handled, every "Visits outside the UK" interest silently extracted zero
counterparties (verified live 2026-08-04: 410/410 fell through to
`skipped_no_counterparty`) — a category-wide silent-drop, not a fetch or
pagination failure.

`totalResults` caveat: the live `/api/v1/Interests` endpoint reports a
*different* `totalResults` depending on `ExpandChildInterests` — 4,057
without it (children counted as separate flat records) vs 3,415 with it
(verified live 2026-08-04; children are nested under their parent instead of
counted separately, even though the live corpus at that moment held only one
interest with any children at all — the ~640-record gap is NOT a
parent/child-count effect and remains unexplained). `fetch_parliament_interests`
always sends `ExpandChildInterests=true`, so a denominator read without that
parameter (e.g. a coverage gate's own `totalResults` probe) is not
apples-to-apples with what this module can ever ingest — flag this to
whoever owns that comparison rather than silently reconciling the two here.

Money: register entries either give an exact `Value` (type `Decimal`, with a
`typeInfo.currencyCode`) or, for shareholdings, a text threshold band (e.g.
"(ii) Other shareholdings, valued at more than £70,000") with no exact
figure. Bands are stored verbatim in `Edge.properties["value_band"]`;
`amount_cents` is left null rather than inventing a midpoint.

Dates: `Edge.valid_from` is the interest's own `registrationDate` — when the
member registered the interest, per this phase's brief. `Edge.valid_to` is
the category-specific `EndDate` field when the API provides one (employment,
land, shareholdings, misc, family categories carry it; donations/gifts do
not). Never inferred or defaulted — left null and counted when absent.

Counterparty resolution mirrors the EC donations uniqueness guard: a company
number resolves with zero name matching (confidence 1.0); a unique exact
name match to `staging.Company` resolves at confidence 0.9; 2+ candidates
sharing a normalised name is never guessed (no edge, counted separately). If
the exact (case/whitespace-only) normalisation finds nothing, a second,
suffix/punctuation-tolerant pass (`_normalise_name_loose`) is tried before
giving up — a declared free-text name routinely differs from the Companies
House legal name by exactly a legal-form suffix ("Ltd" vs "Limited") or
punctuation ("&" vs "and"); a unique match there resolves at confidence 0.8,
method "normalised_name", and 2+ candidates is refused exactly like the
exact-match case. Verified live 2026-08-04: 22 of the 25 ever-ingested
Commons declared_interest edges resolved to a placeholder instead of an
already-known real Company for exactly this reason before this fallback
existed. Unlike EC donations, a named counterparty that doesn't resolve to a
known company is still recorded — but never as a shared node keyed on the
bare name (that would merge unrelated organisations that happen to share a
name). It is scoped to the interest that named it
(`registry_scheme="UK-PARLIAMENT-UNRESOLVED"`,
`registry_id=f"{interest_id}:{normalised_name}"`, confidence 0.5, method
"name_only") — duplication over merging is the correct outcome when
identity cannot be proven. If a company number *was* supplied but isn't
present in `staging.Company`, it is retained in
`Edge.properties["declared_company_number"]` rather than discarded — it
remains the strongest identifier we have even though we can't resolve it.

API request shape (verified against
https://interests-api.parliament.uk/swagger/v1/swagger.json on 2026-07-26):
`SortOrder` only accepts `PublishingDateDescending` or `CategoryAscending`
— `PublishingDateAscending` (previously sent) is rejected with an HTTP 400
on every request, silently breaking every fetch. Historical data
retrievability was verified live: `RegisteredFrom`/`RegisteredTo` filter
the full interests corpus by the interest's own `registrationDate`
(confirmed retrieving 75 items registered in 2020) — no `RegisterId` is
required to reach 2020 data via this path. `RegisterId` selects a specific
*published register document* instead (a periodic snapshot), which is a
different axis; `/api/v1/Registers` only lists documents back to
2024-03-18, so a "2020 register" does not exist to select — `RegisterId`
cannot be used to reach 2020 data at all. `fetch_parliament_interests`
still accepts an optional `register_id` (enumerable via
`list_registers()`) for callers who want to pin one specific published
snapshot, but the benchmark's 2020 data is reached via
`registered_from`/`registered_to`, which this module already exposed.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from django.db import transaction

from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.staging.companies_house import _normalise_name, normalise_company_number
from uncorrupt.staging.models import Company

INTERESTS_API_BASE = "https://interests-api.parliament.uk/api/v1/Interests"
REGISTERS_API_BASE = "https://interests-api.parliament.uk/api/v1/Registers"
SOURCE_NAME = "UK Parliament Register of Interests"

FAMILY_CATEGORY_MARKER = "family members"

# Verified against https://interests-api.parliament.uk/swagger/v1/swagger.json
# (2026-07-26): InterestsSortOrder only accepts these two values.
# "PublishingDateAscending" is NOT in the enum and was rejected with an
# HTTP 400 on every request.
_VALID_SORT_ORDER = "PublishingDateDescending"


@dataclass(frozen=True)
class FetchResult:
    """Provenance record for a downloaded Parliament interests JSON dump."""

    json_path: Path
    provenance_path: Path
    item_count: int
    source_url_template: str
    retrieved_at: datetime
    content_hash: str


@dataclass(frozen=True)
class RegisterInfo:
    """A published register document, as listed by `/api/v1/Registers`."""

    register_id: int
    published_date: str
    house_type: str


def list_registers(client: httpx.Client | None = None, max_retries: int = 5) -> list[RegisterInfo]:
    """Enumerate published register documents via `/api/v1/Registers`.

    Each register is a periodic snapshot publication — a different axis
    from `registered_from`/`registered_to`, which filter by the interest's
    own `registrationDate` across the whole corpus. As of 2026-07-26 the
    earliest published register listed here is 2024-03-18; there is no
    register covering 2020, so `register_id` cannot be used to reach 2020
    interests — use `registered_from`/`registered_to` for that.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        registers: list[RegisterInfo] = []
        skip = 0
        take = 100
        while True:
            params = {"Skip": skip, "Take": take}
            url = httpx.URL(REGISTERS_API_BASE, params=params)
            payload = _fetch_json_with_backoff(client, url, max_retries)
            page_items = payload.get("items") or []
            registers.extend(
                RegisterInfo(
                    register_id=item["id"],
                    published_date=item["publishedDate"],
                    house_type=item["type"],
                )
                for item in page_items
            )
            total_results = payload.get("totalResults", len(registers))
            skip += len(page_items)
            if not page_items or skip >= total_results:
                break
        return registers
    finally:
        if owns_client:
            client.close()


def fetch_parliament_interests(
    output_dir: str | Path,
    registered_from: date | None = None,
    registered_to: date | None = None,
    register_id: int | None = None,
    page_size: int = 20,
    max_retries: int = 5,
    polite_delay_seconds: float = 1.0,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Download Parliament register interests into a local JSON file.

    Paginates via `Skip`/`Take` (the API caps `Take` at 20 regardless of the
    value requested). Backs off with exponential delay on 429/5xx rather than
    hammering the service; gives up after `max_retries` consecutive failures
    on the same page. Fetches with `ExpandChildInterests=true` so payment-level
    child interests (e.g. individual employment payments) arrive nested under
    their parent in one pass.

    `registered_from`/`registered_to` filter by the interest's own
    registrationDate across the full corpus (this is how historical data,
    e.g. 2020 interests, is reached). `register_id` instead pins one
    specific published register document (enumerable via
    `list_registers()`) — a different, narrower axis; it does not reach
    further back than that register's own coverage.

    Writes the raw JSON items plus a provenance record (source URL template,
    retrieval timestamp, content hash) into `output_dir`. Callers are expected
    to point `output_dir` at a gitignored path (e.g. `experiments/`) — this
    function does not commit anything.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "parliament_interests.json"
    provenance_path = output_dir / "parliament_interests.provenance.json"

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)

    items: list[dict[str, Any]] = []
    skip = 0

    base_params: dict[str, Any] = {
        "ExpandChildInterests": "true",
        "SortOrder": _VALID_SORT_ORDER,
    }
    if registered_from is not None:
        base_params["RegisteredFrom"] = registered_from.isoformat()
    if registered_to is not None:
        base_params["RegisteredTo"] = registered_to.isoformat()
    if register_id is not None:
        base_params["RegisterId"] = register_id

    try:
        while True:
            params = {**base_params, "Skip": skip, "Take": page_size}
            url = httpx.URL(INTERESTS_API_BASE, params=params)

            page_items = _fetch_page_with_backoff(client, url, max_retries)
            items.extend(page_items)

            if len(page_items) < page_size:
                break
            skip += page_size
            time.sleep(polite_delay_seconds)
    finally:
        if owns_client:
            client.close()

    json_path.write_text(json.dumps(items, indent=2))

    content_hash = hashlib.sha256(json_path.read_bytes()).hexdigest()
    retrieved_at = datetime.now(UTC)
    source_url_template = (
        f"{INTERESTS_API_BASE}?RegisteredFrom={registered_from}&RegisteredTo={registered_to}"
        f"&RegisterId={register_id}"
    )
    provenance = {
        "source_url_template": source_url_template,
        "retrieved_at": retrieved_at.isoformat(),
        "content_hash": f"sha256:{content_hash}",
        "item_count": len(items),
        "registered_range": {
            "from": registered_from.isoformat() if registered_from else None,
            "to": registered_to.isoformat() if registered_to else None,
        },
        "register_id": register_id,
    }
    provenance_path.write_text(json.dumps(provenance, indent=2))

    return FetchResult(
        json_path=json_path,
        provenance_path=provenance_path,
        item_count=len(items),
        source_url_template=source_url_template,
        retrieved_at=retrieved_at,
        content_hash=f"sha256:{content_hash}",
    )


def _fetch_json_with_backoff(
    client: httpx.Client, url: httpx.URL, max_retries: int
) -> dict[str, Any]:
    delay = 2.0
    for _attempt in range(max_retries):
        response = client.get(url)
        if response.status_code == 200:
            result: dict[str, Any] = response.json()
            return result
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
    raise RuntimeError(f"Parliament interests fetch failed after {max_retries} retries: {url}")


def _fetch_page_with_backoff(
    client: httpx.Client, url: httpx.URL, max_retries: int
) -> list[dict[str, Any]]:
    payload = _fetch_json_with_backoff(client, url, max_retries)
    return list(payload.get("items") or [])


def _is_family_category(category_name: str) -> bool:
    return FAMILY_CATEGORY_MARKER in category_name.lower()


def _fields_by_name(fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {f["name"]: f.get("value") for f in fields}


def _raw_field(fields: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for f in fields:
        if f["name"] == name:
            return f
    return None


def _donor_group_field_lists(fields: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Extract per-donor field-lists from a `Donor[]`-typed nested field.

    "Visits outside the UK" (CategoryId 5) does not carry a flat
    `DonorName`/`PayerName`/`OrganisationName` field at all — instead a
    single `Donors` field of API type `Donor[]` has `value: null` and its
    real payload nested under its own `values` key: a list of donor groups,
    each itself a list of field dicts (`Name`, `IsPrivateIndividual`,
    `Value`, ...) in the same shape as a top-level `fields` list (verified
    live 2026-08-04; a visit can have more than one donor — e.g. two
    organisations jointly funding one trip). `_fields_by_name`/`_raw_field`
    only ever read a field's own `value`, never its nested `values` --
    without this, every "Visits outside the UK" interest silently extracts
    zero counterparties (confirmed: 410/410 fell through to
    `skipped_no_counterparty`).
    """
    donors_field = _raw_field(fields, "Donors")
    if donors_field is None or donors_field.get("type") != "Donor[]":
        return []
    return list(donors_field.get("values") or [])


def _leaf_interests(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten an interest into its ingestable leaves.

    An interest with children (e.g. an employment agreement with individual
    payments registered against it) yields one leaf per child, with the
    child's own fields taking priority over the parent's when both define the
    same field name — the parent supplies context (like `PayerName`) the
    child doesn't repeat. An interest with no children yields itself.
    """
    children = item.get("childInterests") or []
    if not children:
        return [item]

    leaves = []
    parent_fields = {f["name"]: f for f in item.get("fields") or []}
    for child in children:
        child_fields = {f["name"]: f for f in child.get("fields") or []}
        merged = {**parent_fields, **child_fields}
        leaf = dict(child)
        leaf["fields"] = list(merged.values())
        leaves.append(leaf)
    return leaves


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


_BAND_BETWEEN_RE = re.compile(r"between\s*£([\d,]+)\s*and\s*£([\d,]+)", re.IGNORECASE)
_BAND_MORE_THAN_RE = re.compile(r"more than\s*£([\d,]+)", re.IGNORECASE)
_BAND_LESS_THAN_RE = re.compile(r"less than\s*£([\d,]+)", re.IGNORECASE)


def _pounds_to_cents(value: str) -> int:
    return int((Decimal(value.replace(",", "")) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _parse_value_band_bounds(band_text: str) -> tuple[int | None, int | None]:
    """Parse a free-text value band into integer-cent bounds where it parses cleanly.

    Never invents a midpoint: "more than £X" yields a floor with no ceiling,
    "less than £X" a ceiling with no floor, "between £X and £Y" both. Bands
    that carry no monetary figure at all (e.g. a shareholding-percentage
    threshold) parse to (None, None) — the verbatim text is still kept.
    """
    between = _BAND_BETWEEN_RE.search(band_text)
    if between:
        return _pounds_to_cents(between.group(1)), _pounds_to_cents(between.group(2))
    more_than = _BAND_MORE_THAN_RE.search(band_text)
    if more_than:
        return _pounds_to_cents(more_than.group(1)), None
    less_than = _BAND_LESS_THAN_RE.search(band_text)
    if less_than:
        return None, _pounds_to_cents(less_than.group(1))
    return None, None


def _extract_money(fields: list[dict[str, Any]]) -> tuple[int | None, str | None, dict[str, Any]]:
    """Returns (amount_cents, currency, extra_properties).

    An exact `Value` field of type `Decimal` is parsed via `Decimal` into
    integer cents. A shareholding value band (`ShareholdingThreshold`) is
    stored verbatim in `extra_properties["value_band"]`, and — where it
    parses cleanly — also as structured `value_band_min_cents` /
    `value_band_max_cents` bounds. `amount_cents` is never populated from a
    band, and no midpoint is ever invented.
    """
    value_field = _raw_field(fields, "Value")
    if (
        value_field is not None
        and value_field.get("type") == "Decimal"
        and value_field.get("value") is not None
    ):
        currency = (value_field.get("typeInfo") or {}).get("currencyCode")
        try:
            cents = int(
                (Decimal(str(value_field["value"])) * 100).to_integral_value(rounding=ROUND_HALF_UP)
            )
        except InvalidOperation:
            return None, None, {}
        return cents, currency, {}

    band_field = _raw_field(fields, "ShareholdingThreshold")
    if band_field is not None and band_field.get("value"):
        band_text = band_field["value"]
        extra: dict[str, Any] = {"value_band": band_text}
        min_cents, max_cents = _parse_value_band_bounds(band_text)
        if min_cents is not None:
            extra["value_band_min_cents"] = min_cents
        if max_cents is not None:
            extra["value_band_max_cents"] = max_cents
        return None, None, extra

    return None, None, {}


# Statuses that positively identify an organisation counterparty — verified
# live against https://interests-api.parliament.uk on 2026-07-26 (CategoryId
# 3, "Donations...") and re-verified 2026-08-04 by exhaustively enumerating
# every DonorStatus value across CategoryId 3 (642/642), 4 (679/679) and 6
# (17/17): "Limited Liability Partnership" and "Registered Party" are real,
# unambiguously-organisational values this allowlist was missing (an LLP and
# a registered political party are never private individuals). Mirrors
# `ec_donations.ORGANISATION_DONOR_STATUSES`, which already allowlists the
# equivalent "Limited Liability Partnership" / "Registered Political Party"
# for the Electoral Commission's donor-status taxonomy. "Trust" (5 live
# occurrences) is deliberately NOT added: unlike an LLP or a registered
# party, a "Trust" can be a private family trust rather than an
# institutional one, and EC's own allowlist excludes it too — fail closed.
# "Other" is a real value the API returns and is deliberately excluded: it
# is not a positive organisation classification.
PARLIAMENT_ORGANISATION_DONOR_STATUSES = frozenset(
    {
        "Company",
        "Trade Union",
        "Building society",
        "Unincorporated association",
        "Friendly society",
        "Limited Liability Partnership",
        "Registered Party",
    }
)


def _extract_counterparty_name(
    values: dict[str, Any],
) -> tuple[str | None, str | None, bool, bool]:
    """Returns (name, company_number, is_private_individual, is_unclassified).

    Fail-closed (mirrors the EC donations allowlist pattern): a counterparty
    is only ever named when it is POSITIVELY classified as an organisation.
    `name=None` + both flags False means no nameable counterparty was found
    on this interest at all (e.g. land/property has none). `is_private_individual`
    signals a positively-identified individual. `is_unclassified` signals a
    named counterparty whose classification is missing/unexpected — it is
    never assumed to be an organisation just because a name was present.
    """
    donor_status = values.get("DonorStatus")

    donor_company_name = values.get("DonorCompanyName")
    if donor_company_name:
        if donor_status in PARLIAMENT_ORGANISATION_DONOR_STATUSES:
            return donor_company_name, values.get("DonorCompanyIdentifier"), False, False
        if donor_status == "Individual":
            return None, None, True, False
        return None, None, False, True

    payer_is_private = values.get("PayerIsPrivateIndividual")
    payer_name = values.get("PayerName")
    if payer_name:
        if payer_is_private is False:
            return payer_name, None, False, False
        if payer_is_private is True:
            return None, None, True, False
        return None, None, False, True

    organisation_name = values.get("OrganisationName")
    if organisation_name:
        return organisation_name, None, False, False

    donor_name = values.get("DonorName")
    if donor_name:
        if donor_status in PARLIAMENT_ORGANISATION_DONOR_STATUSES:
            return donor_name, None, False, False
        if donor_status == "Individual":
            return None, None, True, False
        return None, None, False, True

    return None, None, False, False


def _counterparty_groups(
    fields: list[dict[str, Any]], values: dict[str, Any]
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Yield one (values, money_fields) pair per counterparty on this leaf.

    Every category except "Visits outside the UK" carries at most one
    counterparty in its own flat `fields` — the existing `(values, fields)`
    pair is returned unchanged, so this is a no-op for every already-tested
    code path. A visit's donor(s) live in a nested `Donor[]` field (see
    `_donor_group_field_lists`); each donor group is normalised onto the
    `PayerName`/`PayerIsPrivateIndividual` keys `_extract_counterparty_name`
    already understands, and the donor's own field list is kept alongside it
    so `_extract_money` reads that donor's own contribution (`Value`), not
    the visit's top-level fields (which carry no `Value` of their own).
    """
    donor_groups = _donor_group_field_lists(fields)
    if not donor_groups:
        return [(values, fields)]

    groups: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for group_fields in donor_groups:
        group_values = _fields_by_name(group_fields)
        synthetic_values = {
            "PayerName": group_values.get("Name"),
            "PayerIsPrivateIndividual": group_values.get("IsPrivateIndividual"),
        }
        groups.append((synthetic_values, group_fields))
    return groups


_LOOSE_SUFFIX_RE = re.compile(r"\b(LIMITED|LTD|PLC|LLP|LP|INTERNATIONAL|UK|THE|AND|CO)\b")


def _normalise_name_loose(name: str) -> str:
    """Suffix/punctuation-tolerant normalisation for the name-only Company fallback.

    `_normalise_name` (case/whitespace only) is the primary, highest-confidence
    match — it never merges two companies that only coincidentally share a
    normalised name. But a Parliament-register free-text organisation name
    routinely differs from the Companies House legal name by exactly a legal
    form suffix ("Ltd" vs "Limited"/"PLC"), a leading "The", or punctuation
    ("&" vs "and") — e.g. the declared "DODS GROUP LTD" vs Companies House's
    real "DODS GROUP LIMITED". Verified live 2026-08-04 against the graph: 22
    of the 25 ever-ingested Commons declared_interest edges resolved to a
    UK-PARLIAMENT-UNRESOLVED placeholder instead of an already-known real
    Company for exactly this reason. Mirrors `scripts/phase_c_paths
    .normalise_name`'s discipline — duplicated here rather than imported,
    since `scripts/` imports from `src/`, never the reverse.
    """
    stripped = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    stripped = _LOOSE_SUFFIX_RE.sub(" ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _canonical_company_entity(company: Company) -> Entity:
    """The Companies House node for a company, creating it if absent.

    Mirrors ch_appointments._canonical_company_entity. A plain
    ``get_or_create(company_number=...)`` raises MultipleObjectsReturned here:
    GLEIF can hold a distinct Entity for the same company under
    registry_scheme="GLEIF-LEI" that also carries this company_number —
    those are legitimately separate claims and must never be merged
    (ADR-006, duplicate over merge). Resolving on registry_scheme="GB-COH" +
    registry_id is unique by DB constraint, so this can never be ambiguous.
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


def _resolve_counterparty_entity(
    name: str, company_number: str | None, interest_id: int
) -> tuple[Entity, float, str, dict[str, Any]] | None:
    """Resolve a named counterparty to an Entity.

    Returns `None` only for the ambiguous-name case (2+ companies share the
    normalised name) — that is a uniqueness-guard refusal to guess, never a
    fallback. Every other named counterparty resolves to *some* Entity: a
    matched `Company` (confidence 1.0 by number, 0.9 by unique exact name) or
    an entity scoped to THIS interest (confidence 0.5, "name_only") — never a
    shared node keyed on the bare name, which would merge unrelated
    organisations across different interests/members that happen to share a
    name (governing principle: duplication over merging).
    """
    if company_number:
        normalised_number = normalise_company_number(company_number)
        company = Company.objects.filter(company_number=normalised_number).first()
        if company:
            entity = _canonical_company_entity(company)
            return entity, 1.0, "identifier", {}
        entity, _ = Entity.objects.get_or_create(
            entity_type="regulated_entity",
            registry_scheme="UK-PARLIAMENT-UNRESOLVED",
            registry_id=f"{interest_id}:{_normalise_name(name)}",
            defaults={"name": name},
        )
        # The declared number didn't resolve locally, but it's the
        # strongest identifier we have — keep it rather than discarding it.
        return entity, 0.5, "name_only", {"declared_company_number": company_number}

    normalised = _normalise_name(name)
    matches = Company.objects.filter(normalised_name=normalised)
    match_count = matches.count()
    if match_count == 1:
        company = matches.get()
        entity = _canonical_company_entity(company)
        return entity, 0.9, "exact_name", {}
    if match_count >= 2:
        return None

    # The exact (case/whitespace-only) normalisation found no candidate —
    # fall back to a suffix/punctuation-tolerant comparison before giving up
    # and scoping a placeholder (see `_normalise_name_loose`). Narrowed via
    # an `icontains` pre-filter anchored on the first LOOSE-normalised word
    # (mirroring `scripts/run_commons_controls.resolve_organisation
    # _candidates`) rather than a fixed-length slice of either name: a
    # punctuation difference positioned before the slice boundary (e.g. the
    # declared "Guardian news and media" vs the real "Guardian News & Media
    # Limited" — the "&" sits before character 15 either way round) breaks a
    # same-position substring match even though both normalise identically.
    # A full-corpus scan isn't needed since a real match always shares a
    # name substring.
    loose_target = _normalise_name_loose(name)
    if loose_target:
        loose_words = loose_target.split()
        prefix = loose_words[0] if loose_words else loose_target
        loose_matches = [
            c
            for c in Company.objects.filter(company_name__icontains=prefix)[:200]
            if _normalise_name_loose(c.company_name) == loose_target
        ]
        if len(loose_matches) == 1:
            entity = _canonical_company_entity(loose_matches[0])
            return entity, 0.8, "normalised_name", {}
        if len(loose_matches) >= 2:
            return None

    entity, _ = Entity.objects.get_or_create(
        entity_type="regulated_entity",
        registry_scheme="UK-PARLIAMENT-UNRESOLVED",
        registry_id=f"{interest_id}:{normalised}",
        defaults={"name": name},
    )
    return entity, 0.5, "name_only", {}


def _role_description(member: dict[str, Any]) -> str:
    house = member.get("house")
    if house == "Commons":
        return f"MP for {member.get('memberFrom')}"
    if house == "Lords":
        return "Member of the House of Lords"
    return f"Member for {member.get('memberFrom')}"


def _get_or_create_member_entity(member: dict[str, Any]) -> Entity:
    entity, _ = Entity.objects.get_or_create(
        entity_type="person",
        registry_scheme="UK-PARLIAMENT-MEMBER",
        registry_id=str(member["id"]),
        defaults={
            "name": member.get("nameDisplayAs") or member.get("nameListAs") or "",
            "role_description": _role_description(member),
        },
    )
    return entity


def ingest_parliament_interests_json(json_path: str | Path) -> dict[str, Any]:
    """Ingest a previously-downloaded Parliament interests JSON dump.

    Returns summary stats: {matched, unmatched_counterparty, skipped_family,
    skipped_private_individual, skipped_unclassified_counterparty,
    skipped_no_counterparty, inverted_interval, total}.

    `total` counts one unit per counterparty-classification decision, not
    one per API record: a "Visits outside the UK" interest with 2 donors
    contributes 2 to `total` (and can produce 2 edges) — the same precedent
    already set by child interests, which are flattened into their own
    counted units by `_leaf_interests` before this loop ever sees them.
    """
    json_path = Path(json_path)
    items = json.loads(json_path.read_text())

    matched = 0
    unmatched_counterparty = 0
    skipped_family = 0
    skipped_private_individual = 0
    skipped_unclassified_counterparty = 0
    skipped_no_counterparty = 0
    ambiguous_company_number = 0
    inverted_interval = 0
    total = 0

    for item in items:
        category_name = (item.get("category") or {}).get("name") or ""
        member = item.get("member")
        if _is_family_category(category_name):
            # Family-member categories carry the relative's own details,
            # not the member's public-function interest (ADR-004 D1).
            total += 1
            skipped_family += 1
            continue
        if member is None:
            total += 1
            skipped_no_counterparty += 1
            continue

        member_entity = _get_or_create_member_entity(member)

        for leaf in _leaf_interests(item):
            fields = leaf.get("fields") or []
            values = _fields_by_name(fields)
            interest_id = leaf["id"]

            # Almost every leaf has exactly one counterparty group (itself);
            # "Visits outside the UK" nests 1+ donors in a `Donor[]` field
            # and yields one group per donor (see `_counterparty_groups`).
            for group_values, money_fields in _counterparty_groups(fields, values):
                total += 1

                name, company_number, is_private, is_unclassified = _extract_counterparty_name(
                    group_values
                )
                if is_private:
                    skipped_private_individual += 1
                    continue
                if is_unclassified:
                    skipped_unclassified_counterparty += 1
                    continue
                if not name:
                    skipped_no_counterparty += 1
                    continue

                # Commit per interest, not the whole dump in one transaction: a
                # giant transaction over every item/leaf holds locks for the
                # entire ingest and loses everything already processed if one
                # row (or the process) dies partway through.
                try:
                    with transaction.atomic():
                        resolved = _resolve_counterparty_entity(name, company_number, interest_id)
                        if resolved is None:
                            unmatched_counterparty += 1
                            continue
                        counterparty_entity, confidence, method, resolve_properties = resolved

                        amount_cents, currency, money_properties = _extract_money(money_fields)
                        valid_from = _parse_date(leaf.get("registrationDate"))
                        valid_to = _parse_date(values.get("EndDate"))

                        properties = {**resolve_properties, **money_properties}
                        if (
                            valid_to is not None
                            and valid_from is not None
                            and valid_to < valid_from
                        ):
                            # An inverted interval is bad data, not a real claim —
                            # never store it as if it were valid.
                            inverted_interval += 1
                            properties["end_date_before_registration_date"] = valid_to.isoformat()
                            valid_to = None

                        # Edge = THE CLAIM (no citation — spec v0.3 §7-bis)
                        edge, _ = Edge.objects.get_or_create(
                            edge_type="declared_interest",
                            source_entity=member_entity,
                            target_entity=counterparty_entity,
                            valid_from=valid_from,
                            valid_to=valid_to,
                            defaults={
                                "amount_cents": amount_cents,
                                "currency": currency,
                                "properties": properties,
                            },
                        )

                        # Attestation = THE EVIDENCE
                        Attestation.objects.get_or_create(
                            edge=edge,
                            source_name=SOURCE_NAME,
                            source_reference=str(interest_id),
                            defaults={
                                "source_url": f"{INTERESTS_API_BASE}/{interest_id}",
                                "match_confidence": confidence,
                                "match_method": method,
                            },
                        )
                except Entity.MultipleObjectsReturned:
                    # A company_number can legitimately resolve to 2+ Entity rows
                    # under different registry schemes (GB-COH, GLEIF-LEI —
                    # ADR-006 duplicate-over-merge). Count and move on rather
                    # than losing the whole run to one row.
                    ambiguous_company_number += 1
                    continue

                matched += 1

    return {
        "matched": matched,
        "unmatched_counterparty": unmatched_counterparty,
        "skipped_family": skipped_family,
        "skipped_private_individual": skipped_private_individual,
        "skipped_unclassified_counterparty": skipped_unclassified_counterparty,
        "skipped_no_counterparty": skipped_no_counterparty,
        "ambiguous_company_number": ambiguous_company_number,
        "inverted_interval": inverted_interval,
        "total": total,
    }
