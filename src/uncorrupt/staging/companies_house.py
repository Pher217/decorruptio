"""Companies House bulk CSV ingestion + supplier resolution.

Ingests the "Basic Company Data" monthly bulk CSV from Companies House.
Downloads from: http://download.companieshouse.gov.uk/BasicCompanyDataAsOneFile.csv
(~5M rows, ~1.5GB, free, no API rate limits).

Company-level fields only — no officers, no PSC, no personal data (scope boundary).
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from django.db import transaction

from uncorrupt.staging.models import Award, Company, SupplierResolution

_PREFIXED_NUMBER_RE = re.compile(r"^([A-Z]+)(\d+)$")

# CH bulk CSV column names (as of 2024 snapshot — may change, verify on download)
CH_COLUMNS = {
    "companynumber": "company_number",
    "companyname": "company_name",
    "companystatus": "company_status",
    "companyincorporationdate": "incorporation_date",
    "incorporationdate": "incorporation_date",  # live CH bulk header
    "accountsaccountcategory": "accounts_category",
    "accountslastmadeupdate": "accounts_last_made_up_date",
    "siccode_sic_text_1": "sic_codes",
    "registeredofficeaddress": "registered_address",
    "company_status": "company_status",  # fallback
}


def _normalise_name(name: str) -> str:
    """Normalise a company name for tier-2 matching: uppercase, strip whitespace.

    Only case/whitespace normalisation — NO suffix stripping (Ltd/Limited/LLP).
    Suffix stripping is tier 3 (deferred) because it risks false positives.
    """
    return " ".join(name.upper().split())


def normalise_company_number(value: str | None) -> str | None:
    """Normalise a UK company number to Companies House's canonical 8-character form.

    External sources (e.g. Electoral Commission, Parliament) supply company
    numbers unpadded (`"7015428"`); Companies House itself stores them
    zero-padded to 8 characters (`"07015428"`) — verified against the CH
    bulk snapshot, where every one of 5.7M rows is exactly 8 characters.
    Without this, an exact-string join between an external identifier and
    `Company.company_number` silently misses.

    Rules:
    - Strip whitespace, uppercase.
    - Numeric-only values shorter than 8 chars are zero-padded to 8.
    - A leading alpha prefix (SC, NI, OC, ...) is split from the numeric
      remainder, which is zero-padded so prefix + digits totals 8 chars —
      never zfill the whole string, which would corrupt the prefix.
    - Already-8-character values pass through unchanged.
    - Values longer than 8 chars, or that don't fit the patterns above, are
      returned stripped/uppercased but otherwise unchanged — let the lookup
      miss rather than invent a wrong number.
    - Empty/None returns None.
    """
    if not value:
        return None
    v = value.strip().upper()
    if not v:
        return None
    if len(v) == 8:
        return v
    if len(v) > 8:
        return v
    if v.isdigit():
        return v.zfill(8)
    match = _PREFIXED_NUMBER_RE.match(v)
    if match:
        prefix, digits = match.groups()
        pad_width = 8 - len(prefix)
        if pad_width > len(digits):
            return prefix + digits.zfill(pad_width)
    return v


def ingest_ch_bulk_csv(
    csv_path: str | Path,
    snapshot_date: date | None = None,
    batch_size: int = 5000,
) -> int:
    """Ingest the Companies House Basic Company Data bulk CSV into the Company model.

    Returns the number of companies ingested.
    """
    csv_path = Path(csv_path)
    snapshot_date = snapshot_date or date.today()

    count = 0
    with open(csv_path, encoding="utf-8", errors="replace") as f:
        # Read the first line to get headers
        reader = csv.DictReader(f)
        # Build a mapping from CSV columns to our fields
        header_lower = {
            h.lower().replace(" ", "").replace("_", "").replace(".", ""): h
            for h in reader.fieldnames or []
        }

        # Map known column names (normalised) to CSV headers
        col_map: dict[str, str] = {}
        for norm, _ in CH_COLUMNS.items():
            norm_key = norm.lower().replace("_", "")
            if norm_key in header_lower:
                col_map[norm] = header_lower[norm_key]

        # Also try common variants
        for variant, target in [
            ("companynumber", "company_number"),
            ("companyname", "company_name"),
            ("companystatus", "company_status"),
            ("companyincorporationdate", "incorporation_date"),
            ("incorporationdate", "incorporation_date"),
            ("accountsaccountcategory", "accounts_category"),
            ("accountslastmadeupdate", "accounts_last_made_up_date"),
            ("siccode_sic_text_1", "sic_codes"),
            ("registeredofficeaddress", "registered_address"),
        ]:
            if target not in col_map.values():
                vkey = variant.lower().replace("_", "")
                if vkey in header_lower:
                    col_map[variant] = header_lower[vkey]

        companies_batch: list[Company] = []
        for row in reader:
            company_number = _get_col(row, col_map, "companynumber")
            if not company_number:
                continue
            company_name = _get_col(row, col_map, "companyname") or ""

            companies_batch.append(
                Company(
                    company_number=company_number.strip(),
                    company_name=company_name.strip(),
                    company_status=_get_col(row, col_map, "companystatus"),
                    incorporation_date=_parse_date(_get_col(row, col_map, "incorporationdate")),
                    accounts_category=_get_col(row, col_map, "accountsaccountcategory"),
                    accounts_last_made_up_date=_parse_date(
                        _get_col(row, col_map, "accountslastmadeupdate")
                    ),
                    sic_codes=_get_col(row, col_map, "siccode_sic_text_1"),
                    registered_address=_get_col(row, col_map, "registeredofficeaddress"),
                    normalised_name=_normalise_name(company_name),
                    bulk_snapshot_date=snapshot_date,
                )
            )
            count += 1
            if len(companies_batch) >= batch_size:
                _bulk_upsert(companies_batch)
                companies_batch.clear()

        if companies_batch:
            _bulk_upsert(companies_batch)

    print(f"  Ingested {count} companies from CH bulk CSV (snapshot {snapshot_date})")
    return count


def _get_col(row: dict[str, str], col_map: dict[str, str], key: str) -> str | None:
    csv_col = col_map.get(key)
    if not csv_col:
        return None
    val = row.get(csv_col)
    if val is None or val.strip() == "":
        return None
    return val.strip()


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            if fmt == "%Y-%m-%d":
                return date.fromisoformat(s)
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _bulk_upsert(companies: list[Company]) -> None:
    """Bulk upsert companies by primary key (company_number)."""

    # Use update_or_create in batches (safe for Postgres)
    with transaction.atomic():
        for c in companies:
            Company.objects.update_or_create(
                company_number=c.company_number,
                defaults={
                    "company_name": c.company_name,
                    "company_status": c.company_status,
                    "incorporation_date": c.incorporation_date,
                    "accounts_category": c.accounts_category,
                    "accounts_last_made_up_date": c.accounts_last_made_up_date,
                    "sic_codes": c.sic_codes,
                    "registered_address": c.registered_address,
                    "normalised_name": c.normalised_name,
                    "bulk_snapshot_date": c.bulk_snapshot_date,
                },
            )


def resolve_suppliers(source_id: str) -> dict[str, Any]:
    """Resolve all suppliers from Awards for a given source to Companies House.

    Two tiers:
    - Tier 1 (identifier): supplier_id_scheme=GB-COH, supplier_id=company_number → confidence 1.0
    - Tier 2 (exact_name): unique company name match (case/whitespace normalised) → confidence 0.9
      Uniqueness guard: if 2+ companies share the normalised name, NO match (avoid false positives).
      Active company preferred over dissolved; if only dissolved, match but note it.

    Tier 3 (normalised_name / fuzzy) is deferred — not built yet.

    Returns summary stats: {tier1_count, tier2_count, unmatched_count, total}
    """
    # Get all distinct suppliers from awards
    suppliers = (
        Award.objects.filter(source_id=source_id)
        .exclude(supplier_name__isnull=True)
        .values("supplier_name", "supplier_id_scheme", "supplier_id")
        .distinct()
    )

    tier1 = 0
    tier2 = 0
    unmatched = 0

    with transaction.atomic():
        for s in suppliers:
            name = s["supplier_name"] or ""
            scheme = s["supplier_id_scheme"] or ""
            sid = s["supplier_id"] or ""

            # Tier 1: identifier match — only count as matched if the Company
            # actually exists in our CH snapshot. If not, record the attempt but
            # count as unmatched so indicators don't silently skip it.
            if scheme == "GB-COH" and sid:
                normalised_sid = normalise_company_number(sid)
                company = Company.objects.filter(company_number=normalised_sid).first()
                if company:
                    SupplierResolution.objects.update_or_create(
                        source_id=source_id,
                        supplier_name=name,
                        defaults={
                            "supplier_id_scheme": scheme,
                            "supplier_id": sid,
                            "company": company,
                            "company_number": normalised_sid,
                            "match_confidence": 1.0,
                            "match_method": "identifier",
                            "normalisation_note": None,
                        },
                    )
                    tier1 += 1
                else:
                    SupplierResolution.objects.update_or_create(
                        source_id=source_id,
                        supplier_name=name,
                        defaults={
                            "supplier_id_scheme": scheme,
                            "supplier_id": sid,
                            "company": None,
                            "company_number": sid,
                            "match_confidence": 0.0,
                            "match_method": None,
                            "normalisation_note": (
                                f"GB-COH identifier '{sid}' not found in CH bulk snapshot."
                            ),
                        },
                    )
                    unmatched += 1
                continue  # DO NOT fall through to name match — the award carries its own ID

            # Tier 2: exact name match (uniqueness-guarded)
            normalised = _normalise_name(name)
            if not normalised:
                unmatched += 1
                continue

            matches = Company.objects.filter(normalised_name=normalised)

            if matches.count() == 1:
                company = matches.get()
                res, created = SupplierResolution.objects.update_or_create(
                    source_id=source_id,
                    supplier_name=name,
                    defaults={
                        "supplier_id_scheme": scheme,
                        "supplier_id": sid,
                        "company": company,
                        "company_number": company.company_number,
                        "match_confidence": 0.9,
                        "match_method": "exact_name",
                        "normalisation_note": (
                            f"Uppercase + whitespace normalised. "
                            f"Company status: {company.company_status}"
                        ),
                    },
                )
                tier2 += 1
            elif matches.count() > 1:
                # Uniqueness guard: multiple companies with the same name → no match
                # Prefer active company if exactly one is active
                active = matches.filter(company_status="Active")
                if active.count() == 1:
                    company = active.get()
                    res, created = SupplierResolution.objects.update_or_create(
                        source_id=source_id,
                        supplier_name=name,
                        defaults={
                            "supplier_id_scheme": scheme,
                            "supplier_id": sid,
                            "company": company,
                            "company_number": company.company_number,
                            "match_confidence": 0.9,
                            "match_method": "exact_name",
                            "normalisation_note": (
                                f"Multiple companies with this name; selected the sole active one. "
                                f"{matches.count()} total matches, 1 active."
                            ),
                        },
                    )
                    tier2 += 1
                else:
                    # Ambiguous — no match
                    SupplierResolution.objects.update_or_create(
                        source_id=source_id,
                        supplier_name=name,
                        defaults={
                            "supplier_id_scheme": scheme,
                            "supplier_id": sid,
                            "company": None,
                            "company_number": None,
                            "match_confidence": 0.0,
                            "match_method": None,
                            "normalisation_note": (
                                f"Ambiguous name: {matches.count()} companies, "
                                f"{active.count()} active. No match — uniqueness guard."
                            ),
                        },
                    )
                    unmatched += 1
            else:
                # No match at all
                unmatched += 1

    total = tier1 + tier2 + unmatched
    return {
        "tier1_identifier": tier1,
        "tier2_exact_name": tier2,
        "unmatched": unmatched,
        "total": total,
    }
