"""Load and validate the Phase C gold manifest against the pre-registration spec.

Spec (LOCKED, do not deviate): vault
`02 Projects/Ideas/Decorruptio/05 Specs/phase-c-gold-manifest-preregistration.md`.
Sections 2 (admissibility) and 4 (schema) are binding here.

A manifest row is admissible only if it satisfies every spec SS2 criterion this
loader can decide mechanically from the manifest's own columns:

  1. `company_number` is present (SS2.2 -- the company-side registry identifier).
  2. `label_source_url` is present (SS2.1 -- how the relationship was
     independently established must trace to a primary document).
  3. `award_date` parses as an ISO date (SS2.3 -- pre-award must be decidable
     per row, not by a single blanket cutoff).
  4. `relationship_start` parses as an ISO date STRICTLY BEFORE `award_date`
     (SS2.4). A blank value or the literal string "unknown" makes pre-award
     UNDECIDABLE for that row -- which is exactly the ambiguity SS2.4 rules
     out, so it is rejected here rather than passed downstream with the
     question deferred.

(SS2.1's "independently established" and SS2.2's registry-identifier
requirement on the person side are asserted by whoever curated the row --
`established_by` and `person_registry_id` are recorded but not re-derived or
graded here.)

A row that fails any check is reported as INADMISSIBLE with the specific
reason(s) -- never silently dropped, never coerced to a best guess.

Company numbers are normalised with the project's canonical Companies House
normaliser (`uncorrupt.staging.companies_house.normalise_company_number`)
rather than re-implementing the zero-padding rule a second time.

Usage:
    PYTHONPATH=.:src python scripts/load_gold_manifest.py data/gold_manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.staging.companies_house import normalise_company_number  # noqa: E402

# Spec SS4 schema -- exact column names. Two of Phase C v1's four defects were
# wrong column names; failing loudly on a header mismatch is the "head -1"
# check SS7.3 requires, done in code instead of by hand.
REQUIRED_COLUMNS = (
    "case_id",
    "person_name",
    "person_registry_id",
    "company_name",
    "company_number",
    "relationship_type",
    "established_by",
    "label_source_url",
    "award_date",
    "relationship_start",
    "excluded_from_retrieval",
)


@dataclass(frozen=True)
class GoldRow:
    """One admissible gold-manifest row (spec SS4), ready for the benchmark runner."""

    case_id: str
    person_name: str
    person_registry_id: str | None
    company_name: str
    company_number: str
    relationship_type: str
    established_by: str
    label_source_url: str
    award_date: date
    relationship_start: date
    excluded_from_retrieval: tuple[str, ...]


@dataclass(frozen=True)
class InadmissibleRow:
    """A manifest row that failed admissibility, and every reason why."""

    case_id: str
    reasons: tuple[str, ...]
    raw: dict[str, str]


@dataclass
class ManifestLoadResult:
    admissible: list[GoldRow] = field(default_factory=list)
    inadmissible: list[InadmissibleRow] = field(default_factory=list)


def _parse_iso_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _split_sources(value: str | None) -> tuple[str, ...]:
    return tuple(s.strip() for s in (value or "").split(";") if s.strip())


def load_gold_manifest(path: str | Path) -> ManifestLoadResult:
    """Validate every row of a manifest CSV against spec SS2 admissibility.

    Raises ValueError before reading a single row if the header is missing a
    spec SS4 column, so a malformed manifest cannot silently produce zero
    admissible rows with no explanation.
    """
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise ValueError(
                f"{path}: manifest is missing required column(s) {missing} "
                f"(found {header}) -- spec SS4 schema / SS7.3 'check the input columns'"
            )
        raw_rows = list(reader)

    result = ManifestLoadResult()
    seen_case_ids: set[str] = set()

    for raw in raw_rows:
        case_id = (raw.get("case_id") or "").strip()
        reasons: list[str] = []

        if not case_id:
            reasons.append("missing case_id")
        elif case_id in seen_case_ids:
            reasons.append(f"duplicate case_id {case_id!r}")
        seen_case_ids.add(case_id)

        company_number = normalise_company_number(raw.get("company_number"))
        if not company_number:
            reasons.append("missing company_number (spec SS2.2)")

        label_source_url = (raw.get("label_source_url") or "").strip()
        if not label_source_url:
            reasons.append("missing label_source_url (spec SS2.1)")

        award_date = _parse_iso_date(raw.get("award_date"))
        if award_date is None:
            reasons.append("missing or unparseable award_date (spec SS2.3)")

        relationship_start_raw = (raw.get("relationship_start") or "").strip()
        relationship_start: date | None = None
        if not relationship_start_raw:
            reasons.append("missing relationship_start (spec SS2.4)")
        elif relationship_start_raw.lower() == "unknown":
            reasons.append(
                "relationship_start is 'unknown' -- pre-award is undecidable (spec SS2.4)"
            )
        else:
            relationship_start = _parse_iso_date(relationship_start_raw)
            if relationship_start is None:
                reasons.append(
                    f"relationship_start {relationship_start_raw!r} is not a valid ISO date"
                )
            elif award_date is not None and relationship_start >= award_date:
                reasons.append(
                    f"relationship_start {relationship_start} does not pre-date "
                    f"award_date {award_date} (spec SS2.4)"
                )

        if reasons:
            result.inadmissible.append(
                InadmissibleRow(case_id=case_id or "<blank>", reasons=tuple(reasons), raw=raw)
            )
            continue

        assert award_date is not None
        assert relationship_start is not None
        result.admissible.append(
            GoldRow(
                case_id=case_id,
                person_name=(raw.get("person_name") or "").strip(),
                person_registry_id=(raw.get("person_registry_id") or "").strip() or None,
                company_name=(raw.get("company_name") or "").strip(),
                company_number=company_number,
                relationship_type=(raw.get("relationship_type") or "").strip(),
                established_by=(raw.get("established_by") or "").strip(),
                label_source_url=label_source_url,
                award_date=award_date,
                relationship_start=relationship_start,
                excluded_from_retrieval=_split_sources(raw.get("excluded_from_retrieval")),
            )
        )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="path to the gold manifest CSV (spec SS4 schema)")
    parser.add_argument(
        "--out", default=None, help="optional path to write a validation JSON report"
    )
    args = parser.parse_args()

    result = load_gold_manifest(args.manifest)

    print(f"=== GOLD MANIFEST: {args.manifest} ===")
    print(f"admissible  : {len(result.admissible)}")
    print(f"inadmissible: {len(result.inadmissible)}")
    for row in result.inadmissible:
        print(f"  REJECTED {row.case_id}: {'; '.join(row.reasons)}")

    if args.out:
        payload = {
            "admissible": [r.case_id for r in result.admissible],
            "inadmissible": [
                {"case_id": r.case_id, "reasons": list(r.reasons)} for r in result.inadmissible
            ],
        }
        Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")

    if not result.admissible:
        print(
            "\nWARNING: zero admissible rows -- nothing for the benchmark to test.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
