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

from uncorrupt.staging.models import Award, AwardResolution, Company, SupplierResolution

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


def _name_tier_entry(name: str) -> dict[str, Any]:
    """Resolve one supplier_name via the name tier (exact match, uniqueness-guarded).

    Returns a dict with company/company_number/confidence/method/note — never
    writes anything. Callers decide whether/how to persist it.
    """
    normalised = _normalise_name(name)
    if not normalised:
        return {
            "company": None,
            "company_number": None,
            "confidence": 0.0,
            "method": None,
            "note": None,
        }

    matches = Company.objects.filter(normalised_name=normalised)

    if matches.count() == 1:
        company = matches.get()
        return {
            "company": company,
            "company_number": company.company_number,
            "confidence": 0.9,
            "method": "exact_name",
            "note": (
                f"Uppercase + whitespace normalised. Company status: {company.company_status}"
            ),
        }
    elif matches.count() > 1:
        # Uniqueness guard: multiple companies with the same name → no match,
        # unless exactly one of them is active — then prefer that one.
        active = matches.filter(company_status="Active")
        if active.count() == 1:
            company = active.get()
            return {
                "company": company,
                "company_number": company.company_number,
                "confidence": 0.9,
                "method": "exact_name",
                "note": (
                    f"Multiple companies with this name; selected the sole active one. "
                    f"{matches.count()} total matches, 1 active."
                ),
            }
        return {
            "company": None,
            "company_number": None,
            "confidence": 0.0,
            "method": None,
            "note": (
                f"Ambiguous name: {matches.count()} companies, "
                f"{active.count()} active. No match — uniqueness guard."
            ),
        }
    else:
        return {
            "company": None,
            "company_number": None,
            "confidence": 0.0,
            "method": None,
            "note": None,
        }


def _has_own_gb_coh_id(supplier_id_scheme: str | None, supplier_id: str | None) -> bool:
    return supplier_id_scheme == "GB-COH" and bool(supplier_id)


def resolve_suppliers(source_id: str) -> dict[str, Any]:
    """Resolve every Award's supplier to Companies House, at the AWARD grain (ADR-012 D1).

    Writes exactly one `AwardResolution` per Award — never a shared row keyed by
    supplier_name — so two awards declaring different GB-COH supplier_ids under
    the same name resolve independently instead of one overwriting the other.

    Step A — name tier: build an in-memory supplier_name → resolution dict using
    ONLY suppliers from awards that do NOT carry a GB-COH id, and persist it to
    `SupplierResolution` (exact-name match, uniqueness-guarded; sole-active-among-
    namesakes at 0.9 confidence; ambiguous → no match). Identifier resolutions are
    no longer written to `SupplierResolution` — that is what removes the grain
    collision.

    Step B — per-award: for every Award in the source, write one `AwardResolution`:
    - GB-COH id present → resolved from the AWARD'S OWN id (never falls back to
      the name tier). Confidence 1.0 if found in the CH bulk snapshot, else the
      award still gets a non-null company_number (normalised, unverified) at
      confidence 0.0 — it stays in the denominator, exactly as before.
    - No GB-COH id, but a supplier_name → copied from the Step A name-tier dict.
    - Neither a GB-COH id nor a supplier_name → NO AwardResolution row is written.
      These awards stay excluded from every indicator, exactly as today. This is
      deliberate; whether such awards should ever be resolvable is a pending
      founder decision, not something this fix should silently take a stance on.

    Returns summary stats: {tier1_identifier, tier2_exact_name, unmatched, total}
    (same shape callers have always consumed).
    """
    tier1 = 0
    tier2 = 0
    unmatched = 0

    with transaction.atomic():
        # Step A: name-tier dictionary, built (and persisted to SupplierResolution)
        # only from suppliers whose awards carry no GB-COH id of their own.
        id_less_suppliers = (
            Award.objects.filter(source_id=source_id)
            .exclude(supplier_name__isnull=True)
            .values("supplier_name", "supplier_id_scheme", "supplier_id")
            .distinct()
        )

        name_tier: dict[str, dict[str, Any]] = {}
        for s in id_less_suppliers:
            name = s["supplier_name"] or ""
            scheme = s["supplier_id_scheme"] or ""
            sid = s["supplier_id"] or ""

            if _has_own_gb_coh_id(scheme, sid):
                continue  # resolved from its own award's id in Step B, not the name tier

            entry = _name_tier_entry(name)
            name_tier[name] = entry

            if entry["company_number"] is None and entry["method"] is None and not entry["note"]:
                # Blank/unnormalisable name — matches the old behaviour of not
                # persisting a SupplierResolution row for it at all.
                continue

            SupplierResolution.objects.update_or_create(
                source_id=source_id,
                supplier_name=name,
                defaults={
                    "supplier_id_scheme": scheme,
                    "supplier_id": sid,
                    "company": entry["company"],
                    "company_number": entry["company_number"],
                    "match_confidence": entry["confidence"],
                    "match_method": entry["method"],
                    "normalisation_note": entry["note"],
                },
            )

        # Step B: one AwardResolution per Award.
        company_cache: dict[str, Company | None] = {}

        def _get_company(number: str) -> Company | None:
            if number not in company_cache:
                company_cache[number] = Company.objects.filter(company_number=number).first()
            return company_cache[number]

        for award in Award.objects.filter(source_id=source_id):
            if _has_own_gb_coh_id(award.supplier_id_scheme, award.supplier_id):
                normalised_sid = normalise_company_number(award.supplier_id)
                company = _get_company(normalised_sid) if normalised_sid else None
                if company:
                    AwardResolution.objects.update_or_create(
                        award=award,
                        defaults={
                            "source_id": source_id,
                            "company": company,
                            "company_number": normalised_sid,
                            "match_confidence": 1.0,
                            "match_method": "identifier",
                            "normalisation_note": None,
                            "ch_snapshot_date": company.bulk_snapshot_date,
                        },
                    )
                    tier1 += 1
                else:
                    AwardResolution.objects.update_or_create(
                        award=award,
                        defaults={
                            "source_id": source_id,
                            "company": None,
                            "company_number": normalised_sid,
                            "match_confidence": 0.0,
                            "match_method": None,
                            "normalisation_note": (
                                f"GB-COH identifier '{award.supplier_id}' not found in "
                                f"CH bulk snapshot."
                            ),
                            "ch_snapshot_date": None,
                        },
                    )
                    unmatched += 1
            elif award.supplier_name:
                entry = name_tier.get(award.supplier_name)
                if entry is None:
                    # A name seen only on id-carrying awards is absent from the
                    # Step A dict. Resolve it once and memoise — passing this as
                    # a `dict.get` default would re-query CH for every award.
                    entry = _name_tier_entry(award.supplier_name)
                    name_tier[award.supplier_name] = entry
                company = entry["company"]
                AwardResolution.objects.update_or_create(
                    award=award,
                    defaults={
                        "source_id": source_id,
                        "company": company,
                        "company_number": entry["company_number"],
                        "match_confidence": entry["confidence"],
                        "match_method": entry["method"],
                        "normalisation_note": entry["note"],
                        "ch_snapshot_date": company.bulk_snapshot_date if company else None,
                    },
                )
                if entry["company_number"]:
                    tier2 += 1
                else:
                    unmatched += 1
            # else: no GB-COH id and no supplier_name — no AwardResolution row.
            # See the "Step B" docstring paragraph above.

    total = tier1 + tier2 + unmatched
    return {
        "tier1_identifier": tier1,
        "tier2_exact_name": tier2,
        "unmatched": unmatched,
        "total": total,
    }
