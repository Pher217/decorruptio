"""Analyze PPE suppliers in the 2020 COVID snapshot."""

import json

with open("experiments/snapshot_uk_covid_2020/uk_covid_ppe.json") as f:
    data = json.loads(f.read())
print(f"PPE releases: {len(data)}")

suppliers: dict[str, int] = {}
for artifact in data:
    raw = bytes.fromhex(artifact["payload_hex"]).decode("utf-8")
    release = json.loads(raw)
    for award in release.get("awards", []):
        for sup in award.get("suppliers", []):
            name = sup.get("name", "")
            if name:
                suppliers[name] = suppliers.get(name, 0) + 1

top = sorted(suppliers.items(), key=lambda x: -x[1])
print(f"Unique suppliers: {len(suppliers)}")
print()
print("Top 30 suppliers:")
for name, count in top[:30]:
    print(f"  {count:3d}  {name}")
print()
keywords = [
    "medpro",
    "pestfix",
    "crisp",
    "ayanda",
    "uniserve",
    "worldlink",
    "supply chain excellence",
    "celframe",
    "vgc",
    "mone",
    "monarch",
    "brandweneedit",
    "thecsggroup",
]
print("VIP-lane keyword matches:")
found = False
for name in suppliers:
    for kw in keywords:
        if kw in name.lower():
            print(f"  {name} ({suppliers[name]} awards) — match: {kw}")
            found = True
if not found:
    print("  (none found)")
