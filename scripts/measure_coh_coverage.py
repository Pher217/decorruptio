"""Measure GB-COH coverage in the UK Contracts Finder frozen snapshot.

Reports a single authoritative coverage figure using the company-eligible
denominator: non-company identifier schemes (public bodies, charities,
schools) can never hold a Companies House number, so they are excluded from
the denominator. This is the true coverage of the population the join can
reach.

Authoritative result on snapshot_2026-07-23: 45/96 = 46.9% (company-eligible).

Usage:
    uv run python scripts/measure_coh_coverage.py
    uv run python scripts/measure_coh_coverage.py --snapshot-dir experiments/snapshot_2026-07-23
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

# Schemes that are NOT companies and will never appear in Companies House:
#   GB-GOR  — Government Organisation Register (public bodies)
#   GB-LAE  — Local Authority Engagements (councils)
#   GB-CHC  — Charity Commission (registered charities)
#   GB-SRS  — UKPRN / schools register (educational institutions)
NON_COMPANY_SCHEMES = {"GB-GOR", "GB-LAE", "GB-CHC", "GB-SRS"}


def _decode_payload(art: dict) -> dict:
    payload_hex = art.get("payload_hex") or art.get("payload", "")
    raw = (
        bytes.fromhex(payload_hex)
        if isinstance(payload_hex, str) and all(c in "0123456789abcdef" for c in payload_hex[:20])
        else payload_hex
    )
    return json.loads(raw) if isinstance(raw, (bytes, str)) else raw


def measure(snapshot_dir: Path, source_id: str = "uk_contracts_finder") -> dict:
    snapshot = snapshot_dir / f"{source_id}.json"
    artifacts = json.loads(snapshot.read_text())

    total_refs = 0
    company_eligible_refs = 0
    gb_coh_refs = 0
    non_company_refs = 0
    no_scheme_refs = 0
    scheme_counts: Counter[str] = Counter()

    for art in artifacts:
        payload = _decode_payload(art)
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
                total_refs += 1
                sup_id = sup.get("id", "")
                sup_name = sup.get("name", "")

                party = party_by_id.get(sup_id) or party_by_name.get(sup_name)
                scheme = party.get("identifier", {}).get("scheme", "NONE") if party else "NONE"
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

    raw_coverage = (gb_coh_refs / total_refs * 100) if total_refs else 0.0
    eligible = (gb_coh_refs / company_eligible_refs * 100) if company_eligible_refs else 0.0

    return {
        "source_id": source_id,
        "releases": len(artifacts),
        "total_supplier_refs": total_refs,
        "company_eligible_refs": company_eligible_refs,
        "gb_coh_refs": gb_coh_refs,
        "non_company_refs": non_company_refs,
        "no_scheme_refs": no_scheme_refs,
        "scheme_counts": dict(scheme_counts),
        "raw_coverage_pct": raw_coverage,
        "eligible_coverage_pct": eligible,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=Path("experiments/snapshot_2026-07-23"),
        help="Frozen snapshot directory.",
    )
    args = parser.parse_args()

    r = measure(args.snapshot_dir)

    print("=" * 70)
    print("GB-COH COVERAGE — COMPANY-ELIGIBLE DENOMINATOR")
    print(f"Snapshot: {args.snapshot_dir}")
    print("=" * 70)
    print(f"Releases: {r['releases']}")
    print(f"Total supplier references: {r['total_supplier_refs']}")
    print()
    print("By identifier scheme:")
    for scheme, count in sorted(r["scheme_counts"].items(), key=lambda x: -x[1]):
        label = " (non-company — excluded)" if scheme in NON_COMPANY_SCHEMES else ""
        print(f"  {scheme:12s}: {count:4d}{label}")
    print()
    print(
        f"Raw coverage      (GB-COH / all):         "
        f"{r['gb_coh_refs']}/{r['total_supplier_refs']} = {r['raw_coverage_pct']:.1f}%"
    )
    print(
        f"Company-eligible  (GB-COH / eligible):     "
        f"{r['gb_coh_refs']}/{r['company_eligible_refs']} = {r['eligible_coverage_pct']:.1f}%"
    )
    print()
    print(f"Non-company entities excluded from denominator: {r['non_company_refs']}")
    print(f"Suppliers with no scheme (unknown, treated as eligible): {r['no_scheme_refs']}")
    print()
    gate = "PASS (>= 50%)" if r["eligible_coverage_pct"] >= 50 else "FAIL (< 50%)"
    print(f"50% gate (company-eligible denominator): {gate}")
    print()
    print("Authoritative figure: company-eligible coverage (non-company schemes excluded).")


if __name__ == "__main__":
    main()
