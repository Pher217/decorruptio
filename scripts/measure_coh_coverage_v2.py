"""Phase 1: Measure GB-COH coverage with company-eligible denominator.

Non-company schemes (public bodies, charities) can never have a CH number.
Excluding them gives the true coverage of the company-eligible population.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

# Schemes that are NOT companies and will never appear in Companies House:
#   GB-GOR  — Government Organisation Register (public bodies)
#   GB-LAE  — Local Authority Engagements (councils)
#   GB-CHC  — Charity Commission (registered charities)
#   GB-SRS  — UKPRN / schools register (educational institutions)
NON_COMPANY_SCHEMES = {"GB-GOR", "GB-LAE", "GB-CHC", "GB-SRS"}

snapshot = Path("experiments/snapshot_2026-07-23/uk_contracts_finder.json")
artifacts = json.loads(snapshot.read_text())

total_supplier_refs = 0
company_eligible_refs = 0
gb_coh_refs = 0
non_company_refs = 0
no_scheme_refs = 0

scheme_counts: Counter[str] = Counter()

for art in artifacts:
    payload_hex = art.get("payload_hex") or art.get("payload", "")
    raw = (
        bytes.fromhex(payload_hex)
        if isinstance(payload_hex, str) and all(c in "0123456789abcdef" for c in payload_hex[:20])
        else payload_hex
    )
    payload = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    parties = payload.get("parties", [])

    party_by_id: dict[str, dict] = {}
    party_by_name: dict[str, dict] = {}
    for p in parties:
        if p.get("id"):
            party_by_id[p["id"]] = p
        if p.get("name"):
            party_by_name[p["name"]] = p

    for award in payload.get("awards", []):
        for sup in award.get("suppliers", []):
            total_supplier_refs += 1
            sup_id = sup.get("id", "")
            sup_name = sup.get("name", "")

            party = party_by_id.get(sup_id) or party_by_name.get(sup_name)
            scheme = "NONE"
            if party:
                scheme = party.get("identifier", {}).get("scheme", "NONE")
            scheme_counts[scheme] += 1

            if scheme == "GB-COH":
                gb_coh_refs += 1
                company_eligible_refs += 1
            elif scheme in NON_COMPANY_SCHEMES:
                non_company_refs += 1
            elif scheme == "NONE":
                no_scheme_refs += 1
                company_eligible_refs += 1  # unknown → could be a company
            else:
                company_eligible_refs += 1  # any other scheme → could be a company

raw_coverage = (gb_coh_refs / total_supplier_refs * 100) if total_supplier_refs > 0 else 0
eligible_coverage = (gb_coh_refs / company_eligible_refs * 100) if company_eligible_refs > 0 else 0

print("=" * 70)
print("GB-COH COVERAGE — CORRECTED MEASUREMENT")
print("=" * 70)
print(f"Total supplier references: {total_supplier_refs}")
print()
print("By identifier scheme:")
for scheme, count in scheme_counts.most_common():
    label = " (non-company — excluded)" if scheme in NON_COMPANY_SCHEMES else ""
    print(f"  {scheme:12s}: {count:4d}{label}")
print()
print(
    f"Raw coverage (GB-COH / all suppliers):         {gb_coh_refs}/{total_supplier_refs} = {raw_coverage:.1f}%"  # noqa: E501
)
print(
    f"Company-eligible coverage (GB-COH / eligible): {gb_coh_refs}/{company_eligible_refs} = {eligible_coverage:.1f}%"  # noqa: E501
)
print()
print(f"Non-company entities excluded from denominator: {non_company_refs}")
print(f"Suppliers with no scheme (unknown, treated as eligible): {no_scheme_refs}")
print()
gate = "PASS (>= 50%)" if eligible_coverage >= 50 else "FAIL (< 50%)"
print(f"50% gate (company-eligible denominator): {gate}")
print()
print("Note: sample is 100 releases / 97 supplier refs — indicative, not precise.")
