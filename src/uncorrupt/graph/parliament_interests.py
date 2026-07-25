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
sharing a normalised name is never guessed (no edge, counted separately).
Unlike EC donations, a named counterparty that doesn't resolve to a known
company (no number given, and either 0 exact-name matches or the interest
carries no company-number field at all) is still recorded as a generically
named organisation (`regulated_entity`, confidence 0.5, method "name_only")
— the register names it, so it is kept, just without a stronger identifier.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from django.db import transaction

from uncorrupt.graph.models import Edge, Entity
from uncorrupt.staging.companies_house import _normalise_name
from uncorrupt.staging.models import Company

INTERESTS_API_BASE = "https://interests-api.parliament.uk/api/v1/Interests"
SOURCE_NAME = "UK Parliament Register of Interests"

FAMILY_CATEGORY_MARKER = "family members"


@dataclass(frozen=True)
class FetchResult:
    """Provenance record for a downloaded Parliament interests JSON dump."""

    json_path: Path
    provenance_path: Path
    item_count: int
    source_url_template: str
    retrieved_at: datetime
    content_hash: str


def fetch_parliament_interests(
    output_dir: str | Path,
    registered_from: date | None = None,
    registered_to: date | None = None,
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
        "SortOrder": "PublishingDateAscending",
    }
    if registered_from is not None:
        base_params["RegisteredFrom"] = registered_from.isoformat()
    if registered_to is not None:
        base_params["RegisteredTo"] = registered_to.isoformat()

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


def _fetch_page_with_backoff(
    client: httpx.Client, url: httpx.URL, max_retries: int
) -> list[dict[str, Any]]:
    delay = 2.0
    for _attempt in range(max_retries):
        response = client.get(url)
        if response.status_code == 200:
            payload = response.json()
            return list(payload.get("items") or [])
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
    raise RuntimeError(f"Parliament interests fetch failed after {max_retries} retries: {url}")


def _is_family_category(category_name: str) -> bool:
    return FAMILY_CATEGORY_MARKER in category_name.lower()


def _fields_by_name(fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {f["name"]: f.get("value") for f in fields}


def _raw_field(fields: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for f in fields:
        if f["name"] == name:
            return f
    return None


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


def _extract_money(fields: list[dict[str, Any]]) -> tuple[int | None, str | None, dict[str, Any]]:
    """Returns (amount_cents, currency, extra_properties).

    An exact `Value` field of type `Decimal` is parsed via `Decimal` into
    integer cents. A shareholding value band (`ShareholdingThreshold`) is
    stored verbatim in `extra_properties["value_band"]` with `amount_cents`
    left null — no midpoint is ever invented.
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
        return None, None, {"value_band": band_field["value"]}

    return None, None, {}


def _extract_counterparty_name(
    values: dict[str, Any],
) -> tuple[str | None, str | None, bool]:
    """Returns (name, company_number, is_private_individual).

    `name=None` means no nameable counterparty was found on this interest
    (e.g. land/property has none) — the caller records no edge. A private
    individual is signalled separately so it can be counted distinctly from
    "no counterparty at all".
    """
    if values.get("DonorStatus") == "Individual":
        return None, None, True

    donor_company_name = values.get("DonorCompanyName")
    if donor_company_name:
        return donor_company_name, values.get("DonorCompanyIdentifier"), False

    if values.get("PayerIsPrivateIndividual") is True:
        return None, None, True

    payer_name = values.get("PayerName")
    if payer_name:
        return payer_name, None, False

    organisation_name = values.get("OrganisationName")
    if organisation_name:
        return organisation_name, None, False

    donor_name = values.get("DonorName")
    if donor_name:
        return donor_name, None, False

    return None, None, False


def _resolve_counterparty_entity(
    name: str, company_number: str | None
) -> tuple[Entity, float, str] | None:
    """Resolve a named counterparty to an Entity.

    Returns `None` only for the ambiguous-name case (2+ companies share the
    normalised name) — that is a uniqueness-guard refusal to guess, never a
    fallback. Every other named counterparty resolves to *some* Entity: a
    matched `Company` (confidence 1.0 by number, 0.9 by unique exact name) or
    a generically named organisation (confidence 0.5, "name_only").
    """
    if company_number:
        company = Company.objects.filter(company_number=company_number).first()
        if company:
            entity, _ = Entity.objects.get_or_create(
                entity_type="company",
                company_number=company.company_number,
                defaults={
                    "name": company.company_name,
                    "registry_scheme": "GB-COH",
                    "registry_id": company.company_number,
                },
            )
            return entity, 1.0, "identifier"
        entity, _ = Entity.objects.get_or_create(
            entity_type="regulated_entity",
            name=name,
            registry_id=None,
        )
        return entity, 0.5, "name_only"

    normalised = _normalise_name(name)
    matches = Company.objects.filter(normalised_name=normalised)
    match_count = matches.count()
    if match_count == 1:
        company = matches.get()
        entity, _ = Entity.objects.get_or_create(
            entity_type="company",
            company_number=company.company_number,
            defaults={
                "name": company.company_name,
                "registry_scheme": "GB-COH",
                "registry_id": company.company_number,
            },
        )
        return entity, 0.9, "exact_name"
    if match_count >= 2:
        return None

    entity, _ = Entity.objects.get_or_create(
        entity_type="regulated_entity",
        name=name,
        registry_id=None,
    )
    return entity, 0.5, "name_only"


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
    skipped_private_individual, skipped_no_counterparty, total}.
    """
    json_path = Path(json_path)
    items = json.loads(json_path.read_text())

    matched = 0
    unmatched_counterparty = 0
    skipped_family = 0
    skipped_private_individual = 0
    skipped_no_counterparty = 0
    total = 0

    with transaction.atomic():
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
                total += 1
                fields = leaf.get("fields") or []
                values = _fields_by_name(fields)

                name, company_number, is_private = _extract_counterparty_name(values)
                if is_private:
                    skipped_private_individual += 1
                    continue
                if not name:
                    skipped_no_counterparty += 1
                    continue

                resolved = _resolve_counterparty_entity(name, company_number)
                if resolved is None:
                    unmatched_counterparty += 1
                    continue
                counterparty_entity, confidence, method = resolved

                amount_cents, currency, extra_properties = _extract_money(fields)
                valid_from = _parse_date(leaf.get("registrationDate"))
                valid_to = _parse_date(values.get("EndDate"))
                interest_id = leaf["id"]

                Edge.objects.get_or_create(
                    edge_type="declared_interest",
                    source_entity=member_entity,
                    target_entity=counterparty_entity,
                    source_reference=str(interest_id),
                    defaults={
                        "valid_from": valid_from,
                        "valid_to": valid_to,
                        "source_name": SOURCE_NAME,
                        "source_url": f"{INTERESTS_API_BASE}/{interest_id}",
                        "match_confidence": confidence,
                        "match_method": method,
                        "amount_cents": amount_cents,
                        "currency": currency,
                        "properties": extra_properties,
                    },
                )
                matched += 1

    return {
        "matched": matched,
        "unmatched_counterparty": unmatched_counterparty,
        "skipped_family": skipped_family,
        "skipped_private_individual": skipped_private_individual,
        "skipped_no_counterparty": skipped_no_counterparty,
        "total": total,
    }
