"""Load and validate the Phase C gold manifest against the pre-registration spec.

Spec (LOCKED, do not deviate): vault
`02 Projects/Ideas/Decorruptio/05 Specs/phase-c-gold-manifest-preregistration.md`,
including amendments v2.1 (unit of analysis), v2.2 (office-holding criterion)
and v2.3 (manifest schema semantics + the case key). Sections 2 and 4, and
all three amendments, are binding.

A manifest row is admissible only if it satisfies every spec SS2 criterion this
loader can decide mechanically from the manifest's own columns:

  1. `company_number` is present (SS2.2 -- the company-side registry identifier)
     AND is explicitly confirmed to be the AWARDEE, not inferred (spec A2.3.1 --
     see `awardee_confirmed` below).
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

OFFICE-HOLDING CRITERION (spec amendment v2.2, SS2.5/A2.2.2): every row must
carry an explicit `held_office_at_award` ('yes'/'no' -- a curator assertion,
never blank) AND a parseable `office_holding_start_date`. Missing/unparseable
values on either are INADMISSIBLE, same treatment as any other missing
field. But `held_office_at_award: no` (or a 'yes' the date itself
contradicts -- `office_holding_start_date` STRICTLY AFTER `award_date`) is
NOT a data defect -- it is OUT OF SCOPE (`ManifestLoadResult.out_of_scope`):
the person held no public office to influence at the time of the award (the
motivating example: a 1997 directorship, a 2000 grant, election not until
2016), so the row cannot test H1 at all regardless of how solid its sourcing
is. Out of scope is a THIRD bucket, distinct from admissible and
inadmissible -- not a data defect, not a refutation, not untestable, simply
never entering any denominator, retained only for transparency about what
was considered and excluded.

AWARDEE VS INTERMEDIARY (spec amendment v2.3, A2.3.1): parallel sourcing
produced two incompatible readings of `company_number` -- most families
recorded the awardee (the supplier receiving public money), but the
donations family recorded the donor. `company_number` means the AWARDEE,
always; a donor/linking entity goes in the separate `intermediary_company_number`
column and is part of the *path*, not the endpoint. This loader does not try
to infer which reading a row used from free text -- it REJECTS any row where
`awardee_confirmed` is not explicitly asserted true, rather than guess.

UNIT OF ANALYSIS (spec amendments v2.1 A2.1.1, narrowed by v2.3 A2.3.2): a
CASE is the distinct AWARDEE `company_number` -- not `(company_number,
award_date)` as v2.1 first set it. PPE Medpro-style companies holding
multiple separate awards from one underlying relationship would otherwise
count as multiple cases and score a single relationship twice. What actually
determines recovery is officer coverage of the awardee company, so rows
sharing an awardee are correlated, not independent. Every admissible row is
grouped into a `GoldCase` at load time (`ManifestLoadResult.cases`); the
benchmark runner scores cases, not rows, using each case's EARLIEST
qualifying award date for the temporal test (row-level counts and per-case
award counts are still reported, but only as a secondary figure -- spec
A2.1.1: "Row-level alone is forbidden as a headline.").

PSC DATA (spec amendment v2.3, A2.3.3 -- an OPEN governance question, not
settled by this loader): a row whose relationship rests only on
Persons-with-Significant-Control data records `established_by: PSC`. Such a
row is NOT rejected and NOT silently kept unflagged -- `GoldRow.is_psc_sourced`
marks it, because PSC is a label source only (never ingested for retrieval),
so it is expected to be unrecoverable by design. The benchmark runner treats
that expected non-recovery as an honest "no trace", never a refutation.

Usage:
    PYTHONPATH=.:src python scripts/load_gold_manifest.py data/gold_manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
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
# `intermediary_company_number` and `awardee_confirmed` added by spec
# amendment v2.3 (A2.3.1). `held_office_at_award` and
# `office_holding_start_date` added by spec amendment v2.2 (A2.2.2).
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
    "intermediary_company_number",
    "awardee_confirmed",
    "held_office_at_award",
    "office_holding_start_date",
)

# spec A2.3.3: the established_by value flagging a row whose relationship
# rests only on Persons-with-Significant-Control data.
PSC_LABEL_SOURCE = "PSC"

# Shared truthy/falsy vocabulary for the two explicit-assertion columns
# (`awardee_confirmed`, `held_office_at_award`) -- both follow the same rule:
# never inferred, must be an explicit 'yes'.
_TRUTHY_VALUES = {"yes", "true", "y", "1"}
_FALSY_VALUES = {"no", "false", "n", "0"}


def _parse_tri_state_bool(value: str | None) -> bool | None:
    v = (value or "").strip().lower()
    if v in _TRUTHY_VALUES:
        return True
    if v in _FALSY_VALUES:
        return False
    return None


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
    intermediary_company_number: str | None
    office_holding_start_date: date

    @property
    def is_psc_sourced(self) -> bool:
        """Spec A2.3.3: this row's relationship rests only on PSC data.

        Not rejected, not silently kept unflagged -- the benchmark runner
        treats an expected non-recovery on a PSC-sourced row as an honest
        "no trace" by design, never a refutation.
        """
        return self.established_by.strip().upper() == PSC_LABEL_SOURCE


@dataclass(frozen=True)
class InadmissibleRow:
    """A manifest row that failed admissibility, and every reason why."""

    case_id: str
    reasons: tuple[str, ...]
    raw: dict[str, str]


@dataclass(frozen=True)
class OutOfScopeRow:
    """A well-formed row excluded from H1 testing by spec A2.2.2, NOT because
    anything about it is wrong.

    The person's asserted office-holding start date post-dates the award, so
    there was no public function to influence at the time -- the row cannot
    test H1 regardless of how solid its sourcing is. This is deliberately a
    THIRD bucket alongside admissible/inadmissible: not a data defect (so not
    `InadmissibleRow`), not a refutation, not untestable (a benchmark-time
    concept) -- simply out of scope, retained here only so what was
    considered and excluded stays visible.
    """

    case_id: str
    reason: str
    raw: dict[str, str]


@dataclass(frozen=True)
class GoldCase:
    """One distinct case (spec A2.3.2): the AWARDEE `company_number` alone --
    narrowed from v2.1's `(company_number, award_date)` pair.

    Multiple admissible rows sharing this awardee -- multiple people tied to
    the company, and/or multiple separate awards to it -- collapse into ONE
    case. What determines recovery is officer coverage of the awardee
    company: if that company's officers are in the graph, most linked
    officials are findable; if not, none are. Rows sharing an awardee are
    therefore correlated, not independent (A2.3.2) -- PPE Medpro's two
    separate DHSC awards from one underlying relationship must never score
    as two recovered cases.
    """

    company_number: str
    rows: tuple[GoldRow, ...]

    @property
    def case_key(self) -> str:
        return self.company_number

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def distinct_award_dates(self) -> tuple[date, ...]:
        return tuple(sorted({row.award_date for row in self.rows}))

    @property
    def award_count(self) -> int:
        """How many distinct awards this case subsumes -- spec A2.3.2
        requires this reported per case alongside row_count, so
        concentration from either source (extra people, or extra awards to
        the same company) is visible."""
        return len(self.distinct_award_dates)

    @property
    def earliest_award_date(self) -> date:
        """The earliest qualifying award date across the case's rows.

        Spec A2.3.2: the temporal recovery test for a multi-award case uses
        this date, not any individual row's own `award_date` -- the
        strictest (hardest-to-satisfy) cutoff available, since a
        relationship pre-dating the earliest award necessarily pre-dates
        every later one from the same company too.
        """
        return min(row.award_date for row in self.rows)

    @property
    def is_concentrated(self) -> bool:
        """True if more than one row collapsed into this case -- spec
        A2.1.1 requires any such case to be listed explicitly so a reader
        can see the concentration, not just the case count."""
        return self.row_count > 1


def _group_into_cases(rows: list[GoldRow]) -> list[GoldCase]:
    """Group admissible rows into cases by the AWARDEE `company_number` alone
    (spec A2.3.2).

    Grouping order follows first appearance in the (already row-order-
    preserving) admissible list, so the result is deterministic for a given
    manifest.
    """
    grouped: dict[str, list[GoldRow]] = defaultdict(list)
    for row in rows:
        grouped[row.company_number].append(row)
    return [GoldCase(company_number=key, rows=tuple(members)) for key, members in grouped.items()]


@dataclass
class ManifestLoadResult:
    admissible: list[GoldRow] = field(default_factory=list)
    inadmissible: list[InadmissibleRow] = field(default_factory=list)
    out_of_scope: list[OutOfScopeRow] = field(default_factory=list)
    cases: list[GoldCase] = field(default_factory=list)


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
    """Validate every row of a manifest CSV against spec SS2/SS2.5 admissibility.

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

        awardee_confirmed = _parse_tri_state_bool(raw.get("awardee_confirmed"))
        if awardee_confirmed is not True:
            reasons.append(
                "company_number not confirmed as the awardee (spec A2.3.1) -- "
                "awardee_confirmed must be explicitly 'yes', never inferred from "
                "award_description or other free text"
            )

        intermediary_company_number = normalise_company_number(
            raw.get("intermediary_company_number")
        )

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

        # `held_office_at_award` is the curator's DIRECT assertion (spec
        # SS2.5), not a re-derivable duplicate of `awardee_confirmed`'s
        # pattern: an explicit 'no' means the sourcing agent has determined
        # the office post-dates the award -- that is not a data defect, it
        # is exactly the out-of-scope condition A2.2.2 describes. Only a
        # missing/unparseable value (we don't know either way) is a data
        # defect worth rejecting outright.
        held_office_at_award = _parse_tri_state_bool(raw.get("held_office_at_award"))
        if held_office_at_award is None:
            reasons.append(
                "held_office_at_award must be explicitly 'yes' or 'no', never blank "
                "(spec SS2.5) -- whether the office was held at/before the award must "
                "be an affirmative answer either way"
            )

        office_holding_start_date_raw = (raw.get("office_holding_start_date") or "").strip()
        office_holding_start_date = _parse_iso_date(office_holding_start_date_raw)
        if office_holding_start_date is None:
            reasons.append("missing or unparseable office_holding_start_date (spec SS2.5/A2.2.2)")

        if reasons:
            result.inadmissible.append(
                InadmissibleRow(case_id=case_id or "<blank>", reasons=tuple(reasons), raw=raw)
            )
            continue

        assert award_date is not None
        assert relationship_start is not None
        assert office_holding_start_date is not None
        assert held_office_at_award is not None

        # Spec A2.2.2: office post-dates the award -- out of scope, NOT
        # inadmissible (nothing about the row's own sourcing is wrong), NOT
        # a refutation, NOT untestable. Never enters the denominator. This
        # fires on EITHER signal: the curator's direct 'no', or (even if the
        # curator said 'yes') the date itself proving otherwise -- an
        # inconsistent 'yes' does not get to override the date.
        if not held_office_at_award or office_holding_start_date > award_date:
            reason = (
                f"office_holding_start_date {office_holding_start_date} post-dates "
                f"award_date {award_date}"
                if held_office_at_award
                else "held_office_at_award is 'no'"
            )
            result.out_of_scope.append(
                OutOfScopeRow(
                    case_id=case_id,
                    reason=(
                        f"{reason} -- no public function to influence at the time "
                        f"(spec A2.2.2); out of scope, not a refutation"
                    ),
                    raw=raw,
                )
            )
            continue

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
                intermediary_company_number=intermediary_company_number,
                office_holding_start_date=office_holding_start_date,
            )
        )

    result.cases = _group_into_cases(result.admissible)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="path to the gold manifest CSV (spec SS4 schema)")
    parser.add_argument(
        "--out", default=None, help="optional path to write a validation JSON report"
    )
    args = parser.parse_args()

    result = load_gold_manifest(args.manifest)
    concentrated = [c for c in result.cases if c.is_concentrated]
    psc_rows = [r for r in result.admissible if r.is_psc_sourced]

    print(f"=== GOLD MANIFEST: {args.manifest} ===")
    print(f"admissible rows : {len(result.admissible)}")
    print(
        f"distinct cases  : {len(result.cases)}  (unit of analysis -- awardee company, spec A2.3.2)"
    )
    print(f"inadmissible    : {len(result.inadmissible)}")
    print(f"out of scope    : {len(result.out_of_scope)}  (office post-dates award, spec A2.2.2)")
    for row in result.inadmissible:
        print(f"  REJECTED {row.case_id}: {'; '.join(row.reasons)}")
    for row in result.out_of_scope:
        print(f"  OUT OF SCOPE {row.case_id}: {row.reason}")
    if concentrated:
        print("\nconcentrated cases (>1 row, spec A2.1.1/A2.3.2 requires these listed explicitly):")
        for case in concentrated:
            print(
                f"  {case.case_key}: {case.row_count} rows, {case.award_count} distinct award(s) "
                f"-- {[r.case_id for r in case.rows]}"
            )
    if psc_rows:
        print(
            f"\nPSC-sourced rows (flagged, spec A2.3.3 -- expected unrecoverable by design, "
            f"never a refutation): {len(psc_rows)} -- {[r.case_id for r in psc_rows]}"
        )

    if args.out:
        payload = {
            "admissible_rows": [r.case_id for r in result.admissible],
            "psc_sourced_rows": [r.case_id for r in psc_rows],
            "out_of_scope": [
                {"case_id": r.case_id, "reason": r.reason} for r in result.out_of_scope
            ],
            "cases": [
                {
                    "case_key": c.case_key,
                    "company_number": c.company_number,
                    "row_count": c.row_count,
                    "award_count": c.award_count,
                    "earliest_award_date": c.earliest_award_date.isoformat(),
                    "row_case_ids": [r.case_id for r in c.rows],
                }
                for c in result.cases
            ],
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
