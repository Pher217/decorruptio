"""Phase 1b: Deeper GB-COH coverage analysis — check party cross-reference potential."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

snapshot = Path("experiments/snapshot_2026-07-23/uk_contracts_finder.json")
artifacts = json.loads(snapshot.read_text())

total_supplier_refs = 0
matched_by_id = 0
matched_by_name = 0
unmatched = 0

# What schemes do the parties that match suppliers have?
matched_schemes: Counter[str] = Counter()

# Suppliers that have NO party match at all
unmatched_names: list[str] = []

# Suppliers matched to a party but party has no GB-COH
matched_no_coh: list[str] = []

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
            name = sup.get("name", "")
            sup_id = sup.get("id", "")

            party = party_by_id.get(sup_id) or party_by_name.get(name)

            if party:
                pid = party.get("identifier", {})
                scheme = pid.get("scheme", "NONE")
                matched_schemes[scheme] += 1
                if scheme == "GB-COH" and pid.get("id"):
                    matched_by_id += 1
                else:
                    matched_no_coh.append(f"{name} (scheme={scheme})")
            else:
                unmatched += 1
                unmatched_names.append(name)

print("=" * 60)
print("PHASE 1b — DEEP COVERAGE ANALYSIS")
print("=" * 60)
print(f"Total supplier refs: {total_supplier_refs}")
print(f"Matched to party by id or name: {matched_by_id + len(matched_no_coh)}")
print(f"  Of which GB-COH: {matched_by_id}")
print(f"  Of which non-GB-COH: {len(matched_no_coh)}")
print(f"Unmatched (no party found): {unmatched}")
print()
print("Matched party schemes:")
for scheme, count in matched_schemes.most_common():
    print(f"  {scheme}: {count}")
print()

# If we improve ingest to cross-reference parties, what's the max possible?
max_coh = matched_by_id
pct_of_total = (max_coh / total_supplier_refs * 100) if total_supplier_refs > 0 else 0
print(
    f"Max GB-COH coverage (with party cross-ref): {max_coh}/{total_supplier_refs} = {pct_of_total:.1f}%"  # noqa: E501
)
print()

# Show some unmatched supplier names
print("Sample unmatched supplier names (first 10):")
for name in unmatched_names[:10]:
    print(f"  {name}")
print()
print("Sample matched-but-no-COH (first 10):")
for name in matched_no_coh[:10]:
    print(f"  {name}")
