"""Phase 1: Measure GB-COH coverage in UK Contracts Finder frozen snapshot."""

from __future__ import annotations

import json
from pathlib import Path

snapshot = Path("experiments/snapshot_2026-07-23/uk_contracts_finder.json")
artifacts = json.loads(snapshot.read_text())

total_supplier_refs = 0
gb_coh_from_award_supplier = 0
gb_coh_from_party = 0
gb_coh_any = 0

# Track unique supplier names
supplier_names_with_coh: set[str] = set()
supplier_names_without_coh: set[str] = set()

# Also check what schemes are present
scheme_counts: dict[str, int] = {}

for art in artifacts:
    payload_hex = art.get("payload_hex") or art.get("payload", "")
    raw = (
        bytes.fromhex(payload_hex)
        if isinstance(payload_hex, str) and all(c in "0123456789abcdef" for c in payload_hex[:20])
        else payload_hex
    )
    payload = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    parties = payload.get("parties", [])

    # Build party lookup by id and name
    party_by_id: dict[str, dict] = {}
    party_by_name: dict[str, dict] = {}
    for p in parties:
        pid = p.get("identifier", {})
        scheme = pid.get("scheme", "NONE")
        scheme_counts[scheme] = scheme_counts.get(scheme, 0) + 1
        if p.get("id"):
            party_by_id[p["id"]] = p
        if p.get("name"):
            party_by_name[p["name"]] = p

    for award in payload.get("awards", []):
        for sup in award.get("suppliers", []):
            total_supplier_refs += 1
            name = sup.get("name", "")
            sup_id = sup.get("id", "")

            # Check award.suppliers[].identifier directly
            award_identifier = sup.get("identifier", {})
            if award_identifier.get("scheme") == "GB-COH" and award_identifier.get("id"):
                gb_coh_from_award_supplier += 1

            has_coh = False

            # Check via party reference (OCDS convention: suppliers reference parties)
            party = party_by_id.get(sup_id) or party_by_name.get(name)
            if party:
                pid = party.get("identifier", {})
                if pid.get("scheme") == "GB-COH" and pid.get("id"):
                    gb_coh_from_party += 1
                    has_coh = True

            if award_identifier.get("scheme") == "GB-COH" and award_identifier.get("id"):
                has_coh = True

            if has_coh:
                supplier_names_with_coh.add(name)
            else:
                supplier_names_without_coh.add(name)

total_unique_suppliers = len(supplier_names_with_coh) + len(supplier_names_without_coh)
coverage_pct = (
    (len(supplier_names_with_coh) / total_unique_suppliers * 100)
    if total_unique_suppliers > 0
    else 0
)

print("=" * 60)
print("PHASE 1 — GB-COH COVERAGE MEASUREMENT")
print("=" * 60)
print(f"Total artifacts (releases): {len(artifacts)}")
print(f"Total supplier references (award→supplier): {total_supplier_refs}")
print(f"Unique supplier names: {total_unique_suppliers}")
print(f"  With GB-COH: {len(supplier_names_with_coh)}")
print(f"  Without GB-COH: {len(supplier_names_without_coh)}")
print(f"COVERAGE: {coverage_pct:.1f}%")
print()
print(f"GB-COH found via award.suppliers[].identifier: {gb_coh_from_award_supplier}")
print(f"GB-COH found via parties[].identifier: {gb_coh_from_party}")
print()
print("All identifier schemes found in parties[]:")
for scheme, count in sorted(scheme_counts.items(), key=lambda x: -x[1]):
    print(f"  {scheme}: {count}")
print()
print("Decision threshold: >= 50% → proceed with identifier join")
print(f"Result: {'PROCEED' if coverage_pct >= 50 else 'STOP — name normalisation required'}")
