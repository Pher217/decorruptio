"""General company-identifier alias layer: former company names -> live CH numbers.

Scope (see `ADR amendment v2.10` / the UK NO SCORE instrument-limitation
finding this closes the general half of): a company rename is common
(Royal Dutch Shell plc -> Shell plc, 2022) and a register that resolves
only current names silently loses every historical relationship. This
module builds a general, independently-sourced alias table -- former name
-> live company number -- applied uniformly to every applicable record. It
does not special-case, and must never special-case, any specific company;
if a name is not resolvable by the rule below, it does not appear here.

What was verified before writing this (2026-08-04, against the live CH
REST API and the bulk snapshot this pipeline already downloads):

1. Former NAMES are genuinely published, twice over. The live REST API
   exposes them per-company as `previous_company_names`
   (name/effective_from/ceased_on triples) -- confirmed against SHELL PLC
   (`GET /company/04366849`), whose two former names "ROYAL DUTCH SHELL
   PLC" (2004-10-27 to 2022-01-21) and "FORTHDEAL LIMITED" (its 2002 shelf
   name) round-trip exactly. The bulk "Basic Company Data" CSV -- already
   on disk at `experiments/BasicCompanyDataAsOneFile-*.csv`, no API call
   needed -- carries the same history as up to ten
   `PreviousName_{1..10}.CompanyName` / `PreviousName_{1..10}.CONDATE`
   column pairs per row (`CONDATE` is the single date that name *stopped*
   applying; the bulk file does not separately publish when it started).
   In the 2026-07-01 snapshot, 533,729 of 5,734,780 rows (9.3%) carry at
   least one former name. This module builds from the bulk CSV, not the
   API: it is deterministic, requires no network access and no rate
   limit, and covers the whole register in one pass.

2. Legacy non-file identifiers with letter prefixes (NF, FC, SF, ...) DO
   exist in the live register and DO resolve -- but they are not a
   supersession/rename mechanism. Companies House's "oversea company"
   register assigns them to a branch/establishment registration for a
   company whose primary registration is elsewhere (`type:
   "oversea-company"` on the live API). Confirmed against a real NF-prefix
   row sampled from the bulk CSV (NF003690, "A & A CLUCKIE LIMITED"):
   `GET /company/NF003690` returns
   `foreign_company_details.registration_number: "SC222690"` -- a
   cross-reference to that same legal entity's own, separate Scottish
   registration. This IS a general, published, per-record cross-reference --
   not present in the bulk CSV, so it needs one live API call per branch
   registration -- and it does not merge into THIS module's former-name
   table (a rename and a branch cross-reference are structurally different
   relationships: one supersedes an identifier, the other parallels it).
   `uncorrupt.staging.oversea_company_aliases` is the general remediation
   this finding called for: it fetches `foreign_company_details.
   registration_number` for every NF/FC/SF-prefixed record (measured
   2026-08-04 against the real bulk snapshot: 509 NF + 13,823 FC + 184 SF =
   14,516 rows -- small enough for a single bounded, rate-limited sweep,
   not the "~15,000+ ... not attempted here" open question this docstring
   used to leave unresolved), producing `CompanyAlias` rows with
   `alias_kind="legacy_identifier"` that `AliasIndex.resolve_identifier`
   below looks up. Whether the mechanism generalises to useful coverage
   (most oversea-company records actually carrying a populated
   `registration_number`, vs. mostly empty) is an empirical question that
   module's build report answers by measurement, never assumed here. Where
   an identifier does not correspond to a real CH company number, that
   module reports no alias rather than inventing one -- same discipline as
   the former-name matching below.

Ambiguity -- never guess. Over 5.7M companies and decades of history,
names get reused after dissolution. A former name is usable as an alias
only when:
  (a) exactly one company number ever carried it as a former name, AND
  (b) it does not collide with any OTHER company's *current* name.
Either condition failing drops the candidate silently -- it is simply
absent from the built table, never resolved to a best guess.

Not wired into any connector or resolver in this branch -- see
`AliasIndex` for the adoption seam a resolver can call later.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from uncorrupt.staging.companies_house import _normalise_name, normalise_company_number

SOURCE_BULK_PREVIOUS_NAMES = "companies_house_bulk_csv.previous_company_names"
BULK_CSV_SOURCE_URL = "http://download.companieshouse.gov.uk/BasicCompanyDataAsOneFile.csv"
MAX_PREVIOUS_NAME_SLOTS = 10

_COMPANY_NUMBER_COL = "CompanyNumber"
_COMPANY_NAME_COL = "CompanyName"
_CONDATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d")


@dataclass(frozen=True)
class CompanyAlias:
    """One alias row: an alias resolved to its live CH number.

    Two mechanisms feed this one shape (`alias_kind` distinguishes them so
    `AliasIndex` can route each to the right lookup):
    - `"former_name"` (default, built by `build_alias_table` below): a
      former COMPANY NAME, free-text-matched with the ambiguity guard
      documented in the module docstring.
    - `"legacy_identifier"` (built by
      `uncorrupt.staging.oversea_company_aliases.
      build_oversea_company_alias_table`): an NF/FC/SF-prefixed oversea-
      company branch registration number, cross-referenced via CH's
      `foreign_company_details.registration_number`. Keyed by a unique
      registry ID rather than free text, so it does not need the same
      "collides with another company" guard -- see that module's docstring.

    `source`/`source_url`/`retrieved_at` are carried per row (rather than
    only once at the table level) so a single alias row is independently
    auditable if the table is ever filtered, merged, or handed to another
    process.
    """

    alias_name: str
    normalised_alias_name: str
    live_company_number: str
    name_changed_on: str | None
    source: str
    source_url: str
    retrieved_at: str
    alias_kind: str = "former_name"


@dataclass(frozen=True)
class AliasBuildReport:
    """Coverage report for one build run -- every number measured, none guessed."""

    rows_scanned: int
    companies_with_any_former_name: int
    former_name_cells_seen: int
    candidate_aliases: int
    dropped_ambiguous_among_former_names: int
    dropped_collides_with_a_current_name: int
    aliases_written: int
    source: str
    snapshot_date: str


def _parse_condate(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _CONDATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _iter_bulk_csv_rows(csv_path: Path) -> Iterator[tuple[list[str], dict[str, int]]]:
    with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader)]
        idx = {name: i for i, name in enumerate(header)}
        for row in reader:
            if len(row) < len(header):
                continue
            yield row, idx


def build_alias_table(
    csv_path: str | Path,
    snapshot_date: date,
    source: str = SOURCE_BULK_PREVIOUS_NAMES,
    source_url: str = BULK_CSV_SOURCE_URL,
) -> tuple[list[CompanyAlias], AliasBuildReport]:
    """Deterministically derive former-name aliases from the CH bulk CSV.

    Single pass over the CSV builds two structures:
      - `current_owner`: normalised current name -> the one company number
        seen holding it (a name held by two different company numbers'
        *current* names is flagged ambiguous instead of picking one).
      - `former_targets`: normalised former name -> the set of company
        numbers that were ever recorded as having carried it.

    A former name becomes an alias only if its target set has exactly one
    member AND that name does not collide with a different company's
    current name (see module docstring, "Ambiguity"). Everything else is
    dropped and counted in `AliasBuildReport`, never guessed.

    Deterministic: output is sorted by (live_company_number,
    normalised_alias_name) before being returned, so re-running against an
    unchanged CSV produces byte-identical output.
    """
    csv_path = Path(csv_path)

    current_owner: dict[str, str] = {}
    current_ambiguous: set[str] = set()
    former_targets: dict[str, set[str]] = {}
    former_seen: dict[str, tuple[str, str | None]] = {}

    rows_scanned = 0
    companies_with_any_former_name = 0
    former_name_cells_seen = 0

    for row, idx in _iter_bulk_csv_rows(csv_path):
        rows_scanned += 1
        if _COMPANY_NUMBER_COL not in idx:
            continue
        company_number = normalise_company_number(row[idx[_COMPANY_NUMBER_COL]].strip())
        if not company_number:
            continue

        if _COMPANY_NAME_COL in idx:
            raw_current_name = row[idx[_COMPANY_NAME_COL]].strip()
            if raw_current_name:
                norm_current = _normalise_name(raw_current_name)
                owner = current_owner.get(norm_current)
                if owner is None:
                    current_owner[norm_current] = company_number
                elif owner != company_number:
                    current_ambiguous.add(norm_current)

        had_former_name = False
        for n in range(1, MAX_PREVIOUS_NAME_SLOTS + 1):
            name_col = f"PreviousName_{n}.CompanyName"
            date_col = f"PreviousName_{n}.CONDATE"
            if name_col not in idx:
                break
            raw_former = row[idx[name_col]].strip()
            if not raw_former:
                continue
            had_former_name = True
            former_name_cells_seen += 1
            norm_former = _normalise_name(raw_former)
            former_targets.setdefault(norm_former, set()).add(company_number)
            if norm_former not in former_seen:
                changed_on = _parse_condate(row[idx[date_col]]) if date_col in idx else None
                former_seen[norm_former] = (raw_former, changed_on)

        if had_former_name:
            companies_with_any_former_name += 1

    candidate_aliases = len(former_targets)
    dropped_ambiguous = 0
    dropped_collision = 0
    aliases: list[CompanyAlias] = []
    retrieved_at = snapshot_date.isoformat()

    for norm_former, targets in former_targets.items():
        if len(targets) > 1:
            dropped_ambiguous += 1
            continue
        (company_number,) = targets
        current_owner_of_name = current_owner.get(norm_former)
        collides = norm_former in current_ambiguous or (
            current_owner_of_name is not None and current_owner_of_name != company_number
        )
        if collides:
            dropped_collision += 1
            continue
        raw_former, changed_on = former_seen[norm_former]
        aliases.append(
            CompanyAlias(
                alias_name=raw_former,
                normalised_alias_name=norm_former,
                live_company_number=company_number,
                name_changed_on=changed_on,
                source=source,
                source_url=source_url,
                retrieved_at=retrieved_at,
            )
        )

    aliases.sort(key=lambda a: (a.live_company_number, a.normalised_alias_name))

    report = AliasBuildReport(
        rows_scanned=rows_scanned,
        companies_with_any_former_name=companies_with_any_former_name,
        former_name_cells_seen=former_name_cells_seen,
        candidate_aliases=candidate_aliases,
        dropped_ambiguous_among_former_names=dropped_ambiguous,
        dropped_collides_with_a_current_name=dropped_collision,
        aliases_written=len(aliases),
        source=source,
        snapshot_date=snapshot_date.isoformat(),
    )
    return aliases, report


def write_alias_table(aliases: list[CompanyAlias], output_path: str | Path) -> None:
    """Serialise the alias table deterministically (stable key order)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(a) for a in aliases]
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


class AliasIndex:
    """Read-only lookup: a former name OR legacy identifier -> its live CH
    company number.

    Two independent lookup tables, kept separate rather than merged into one
    dict: former company names and legacy oversea-company branch identifiers
    are different keyspaces (free text vs. a registry ID) built by different
    mechanisms with different ambiguity guards (see `CompanyAlias`'s
    docstring for the `alias_kind` distinction). Loading both former-name and
    legacy-identifier rows into one `AliasIndex` (concatenate the two lists
    before constructing) makes both `resolve` and `resolve_identifier`
    available together.

    Usage (once a resolver chooses to adopt it -- not wired in this branch):

        >>> index = AliasIndex.load("data/company_aliases.json")
        >>> index.resolve("Royal Dutch Shell plc")
        '04366849'
        >>> index.resolve("a name that was never anyone's") is None
        True
        >>> oversea = AliasIndex.load("data/oversea_company_aliases.json")
        >>> oversea.resolve_identifier("NF003690")
        'SC222690'
    """

    def __init__(self, aliases: list[CompanyAlias]):
        self._by_name: dict[str, str] = {}
        self._by_identifier: dict[str, str] = {}
        for a in aliases:
            if a.alias_kind == "legacy_identifier":
                self._by_identifier[a.normalised_alias_name] = a.live_company_number
            else:
                self._by_name[a.normalised_alias_name] = a.live_company_number

    @classmethod
    def load(cls, path: str | Path) -> AliasIndex:
        raw: list[dict[str, Any]] = json.loads(Path(path).read_text())
        return cls([CompanyAlias(**row) for row in raw])

    def resolve(self, name_or_identifier: str | None) -> str | None:
        """Return the live company number for a former name, or None.

        Looks up by normalised former COMPANY NAME only -- former names come
        from `previous_company_names`/`PreviousName_N` (see module
        docstring, point 1). A miss returns None, never a guess. For an
        NF/FC/SF-prefixed legacy identifier, use `resolve_identifier`
        instead -- that is a different mechanism (point 2) with its own
        keyspace, never merged into this lookup.
        """
        if not name_or_identifier:
            return None
        return self._by_name.get(_normalise_name(name_or_identifier))

    def resolve_identifier(self, company_number: str | None) -> str | None:
        """Return the live company number for a legacy identifier, or None.

        Looks up by normalised company-number IDENTIFIER (e.g. an
        NF/FC/SF-prefixed oversea-company branch registration) -- built from
        `uncorrupt.staging.oversea_company_aliases`, the cross-reference
        resolver the module docstring's point 2 finding called for. A miss
        returns None, never a guess, and never falls back to `resolve`'s
        name-keyed table (a company number is never a company name).
        """
        if not company_number:
            return None
        normalised = normalise_company_number(company_number)
        if not normalised:
            return None
        return self._by_identifier.get(normalised)

    def __len__(self) -> int:
        return len(self._by_name) + len(self._by_identifier)
