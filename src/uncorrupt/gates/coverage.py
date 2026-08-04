"""Spec A2.4.2 coverage-gate measurement -- the fail-closed 100% standard.

An independent review was explicit that the 90% threshold used elsewhere in
this project's control battery (`CONTROLS_PASS_FRACTION` in
`scripts/run_gold_benchmark.py`) is defensible only for the pre-registered
>=9/10 EXTERNAL CONTROL sample, never for census-style ingestion coverage:
*"if 10% is missing, the missingness may be systematically concentrated
among precisely the difficult suppliers."* `CoverageMeasurement.passed`
below therefore requires 100% of the frozen denominator accounted for --
not 90% -- where "accounted for" means every record reached a terminal,
auditable state: ingested (including a genuine zero-result response), or
explicitly failed with a recorded reason. A record that was simply never
attempted is neither, and fails this gate regardless of how small a
fraction it is.

NOTE ON `run_gold_benchmark.CoverageGate` (out of scope, not edited here):
that class independently computes its own `covered/total >= 90%` from the
two raw counts this module writes into `experiments/coverage_gate.json`.
This module's OWN `passed` property is the correct, spec-A2.4.2 standard;
`CoverageMeasurement.to_gate_dict()`'s docstring and `scripts/
measure_coverage_gate.py`'s no-score certificate keep that distinction
explicit rather than silently reconciling two different thresholds.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from uncorrupt.graph import ch_officers
from uncorrupt.graph.lords_interests import _extract_counterparty, _parse_lords_page
from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.graph.parliament_interests import SOURCE_NAME as COMMONS_SOURCE_NAME

DEFAULT_CH_OUTPUT_DIR = "experiments/ch_officers"
DEFAULT_COMMONS_TAKE = 1
# scripts/ingest_parliament_interests.py's own DEFAULT_OUTPUT_DIR +
# fetch_parliament_interests's fixed provenance filename -- where the LIVE
# ingest that populated `ingested` below wrote down which RegisteredFrom/
# RegisteredTo window (if any) it actually fetched.
DEFAULT_COMMONS_INGEST_PROVENANCE_PATH = (
    "experiments/parliament_interests/parliament_interests.provenance.json"
)
LORDS_MEMBER_REGISTRY_SCHEME = "UK-PARLIAMENT-MEMBER"


@dataclass(frozen=True)
class CoverageMeasurement:
    """One coverage gate's measured state (spec A2.4.2).

    `ingested`             -- reached a terminal SUCCESS state (fetched
                               and/or resolved, including a genuine
                               zero-result response).
    `explicitly_failed`    -- reached a terminal FAILURE state with a
                               recorded reason (a real fetch error, or a
                               predeclared structural exclusion).
    `not_attempted`        -- never reached any terminal state at all --
                               the missingness the independent review
                               warned could be non-random.
    `total`                -- the frozen/advertised denominator, NOT the
                               current graph count, so a silently-dropped
                               record still counts against coverage.

    `accounted_for`/`passed` implement the 100%-accounted standard (module
    docstring): every record must reach `ingested` OR `explicitly_failed`.
    """

    name: str
    ingested: int
    explicitly_failed: int
    not_attempted: int
    total: int
    failure_manifest: tuple[str, ...] = ()
    known_limits: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ingested < 0 or self.explicitly_failed < 0 or self.not_attempted < 0:
            raise ValueError(
                f"{self.name}: ingested/explicitly_failed/not_attempted must each be >= 0 -- "
                "a negative component means the measured counts exceed the declared total, "
                "a real data inconsistency that must be investigated, never silently clamped."
            )
        counted = self.ingested + self.explicitly_failed + self.not_attempted
        if self.total < 0 or counted != self.total:
            raise ValueError(
                f"{self.name}: ingested+explicitly_failed+not_attempted ({counted}) must "
                f"equal total ({self.total}) -- a malformed count must never silently "
                "produce a wrong ratio."
            )

    @property
    def accounted_for(self) -> int:
        return self.ingested + self.explicitly_failed

    @property
    def passed(self) -> bool:
        """spec v2.4 §A2.4.2, corrected: 100% of `total` accounted for --
        NOT a 90% ratio. See module docstring."""
        return self.total > 0 and self.accounted_for == self.total

    def to_gate_dict(self, covered_key: str, total_key: str) -> dict[str, int]:
        """The two raw fields `run_gold_benchmark.CoverageGate` reads.

        `covered` is deliberately the SUCCESS count only (`ingested`), never
        `accounted_for` -- crediting an explicitly-failed record toward
        `covered` would let a batch of honestly-logged failures also pass
        the downstream (out-of-scope, unedited) 90% ratio check. This
        module's own `passed` above is the authoritative spec-A2.4.2
        determination; `to_gate_dict()` only supplies the raw counts the
        immutable consumer's contract requires.
        """
        return {covered_key: self.ingested, total_key: self.total}


# ---------------------------------------------------------------------------
# Companies House officer-roster coverage
# ---------------------------------------------------------------------------


def _read_ch_run_manifest(output_dir: Path) -> set[str]:
    """Every company_number that appears in a "selected" run-manifest batch.

    Best-effort: `run_manifest.jsonl` is written by `ingest_ch_officers.py`'s
    CLI (`ch_officers.append_run_manifest`) -- a cache directory populated
    another way, or one whose manifest was not preserved, simply has none.
    Absence does not change PASS/FAIL: every company without a cache file is
    `not_attempted` either way unless the manifest proves an attempt was
    made, in which case it is reclassified to `explicitly_failed` for a more
    informative failure manifest.
    """
    manifest_path = output_dir / "run_manifest.jsonl"
    if not manifest_path.exists():
        return set()
    attempted: set[str] = set()
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("phase") == "selected":
            attempted.update(record.get("selected_companies") or [])
    return attempted


def measure_ch_officer_coverage(
    output_dir: str | Path = DEFAULT_CH_OUTPUT_DIR,
    max_failure_manifest: int = 500,
) -> CoverageMeasurement:
    """Companies House officer-roster coverage over the procurement-supplier
    universe (spec A2.4.2; packet: 12,227 verified suppliers).

    Reuses `ch_officers.procurement_supplier_universe()` for the universe
    and `ch_officers.procurement_universe_coverage_report()` for a secondary
    graph-level diagnostic (`extra["graph_tiers"]`) -- both REUSED, not
    reimplemented, per the delegation packet. The GATE ITSELF, however, is
    measured from the raw per-company cache
    (`{output_dir}/{company_number}.provenance.json`, written by
    `ch_officers.fetch_company_officers`), because the graph-level report
    cannot distinguish a valid zero-officer response from a company that was
    never queried at all -- both produce zero `officer_of` edges, hence
    identical (zero) graph signal. The cache file is the only durable,
    per-company terminal-state evidence: written on every SUCCESSFUL fetch
    (`officer_count` >= 0), never written on a failed one
    (`fetch_company_officers`'s own docstring: "no cache files are written
    for it either").
    """
    output_dir = Path(output_dir)
    universe = ch_officers.procurement_supplier_universe()
    universe_set = set(universe)

    ingested_set = {
        company_number
        for company_number in universe
        if (output_dir / f"{company_number}.provenance.json").exists()
    }

    attempted = _read_ch_run_manifest(output_dir)
    explicitly_failed_list = sorted((attempted & universe_set) - ingested_set)
    accounted = ingested_set | set(explicitly_failed_list)
    not_attempted_list = sorted(universe_set - accounted)

    known_limits = []
    if not (output_dir / "run_manifest.jsonl").exists():
        known_limits.append(
            f"no run_manifest.jsonl found at {output_dir} -- 'attempted but failed' cannot "
            "be distinguished from 'never attempted'; every company without a cache file is "
            "conservatively counted as not_attempted (fail closed, ADR-008)."
        )

    graph_tiers = ch_officers.procurement_universe_coverage_report()

    return CoverageMeasurement(
        name="companies_house_officer_roster",
        ingested=len(ingested_set),
        explicitly_failed=len(explicitly_failed_list),
        not_attempted=len(not_attempted_list),
        total=len(universe),
        failure_manifest=tuple(
            (explicitly_failed_list + not_attempted_list)[:max_failure_manifest]
        ),
        known_limits=tuple(known_limits),
        extra={
            "graph_tiers": graph_tiers,
            "graph_tiers_note": (
                "reused ch_officers.procurement_universe_coverage_report() verbatim -- "
                "informational only, never used to decide 'passed' (see module docstring)."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Commons (UK Parliament Register of Interests) ingest completeness
# ---------------------------------------------------------------------------


def _commons_totals_query_params(
    registered_from: date | None = None,
    registered_to: date | None = None,
) -> dict[str, Any]:
    """The exact query shape `fetch_parliament_interests` issues (minus
    pagination) -- the single source of truth both `fetch_commons_total_results`
    and `measure_commons_coverage`'s provenance note read, so the two can never
    independently drift apart the way they did before this fix.

    `ExpandChildInterests=true` is included DELIBERATELY: the live
    `/api/v1/Interests` endpoint reports a *different* `totalResults`
    depending on this flag -- verified live 4,057 without it vs. 3,415 with
    it (see `parliament_interests.py`'s own documented "totalResults
    caveat"). `fetch_parliament_interests` always sends
    `ExpandChildInterests=true`, so a denominator read without it is not
    apples-to-apples with what this repository can ever ingest -- that
    mismatch (4,057 vs. the real 3,415 corpus) previously went unnoticed for
    hours. Never drop this parameter here without also confirming the fetch
    no longer sends it.

    `registered_from`/`registered_to` mirror `fetch_parliament_interests`'s
    OWN optional date-window params, added the same way it adds them
    (ISO date strings, only when not `None`) -- a second, independently
    discovered axis on which the denominator can silently stop being
    apples-to-apples with the fetch: `ExpandChildInterests` alone is not
    sufficient if the actual ingest was date-windowed and this query is not.
    See `measure_commons_coverage`'s `ingest_provenance_path` for how the
    caller is expected to supply these rather than guessing.
    """
    params: dict[str, Any] = {
        "Take": DEFAULT_COMMONS_TAKE,
        "SortOrder": "PublishingDateDescending",
        "ExpandChildInterests": "true",
    }
    if registered_from is not None:
        params["RegisteredFrom"] = registered_from.isoformat()
    if registered_to is not None:
        params["RegisteredTo"] = registered_to.isoformat()
    return params


def fetch_commons_total_results(
    client: httpx.Client | None = None,
    max_retries: int = 5,
    registered_from: date | None = None,
    registered_to: date | None = None,
) -> int:
    """Live `totalResults` from the Commons Interests API, queried with the
    SAME shape (`ExpandChildInterests=true`, plus the same date window when
    given) the real fetch actually used (packet: "the interests API reports
    totalResults" -- but see `_commons_totals_query_params`'s docstring for
    why the query shape must match, not just the endpoint).

    A single `Take=1` request reads only the pagination envelope, never the
    corpus itself (that remains `parliament_interests.py`'s job). This
    reading, taken at measurement time and recorded with a UTC timestamp
    into the gate artifact this produces, IS the freeze for Commons: unlike
    Lords, there is no separately-captured HTML snapshot file to hash --
    the measurement's own recorded reading is the frozen denominator (spec
    A2.4.5's "raw-artifact hashes and source snapshot dates").
    """
    from uncorrupt.graph.parliament_interests import INTERESTS_API_BASE, _fetch_json_with_backoff

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        params = _commons_totals_query_params(
            registered_from=registered_from, registered_to=registered_to
        )
        url = httpx.URL(INTERESTS_API_BASE, params=params)
        payload = _fetch_json_with_backoff(client, url, max_retries)
    finally:
        if owns_client:
            client.close()
    total = payload.get("totalResults")
    if not isinstance(total, int) or total < 0:
        raise RuntimeError(
            f"Commons Interests API returned no usable totalResults ({total!r}) -- cannot "
            "measure coverage without a real denominator (fail closed)."
        )
    return total


def _read_commons_ingest_date_window(
    provenance_path: str | Path | None,
) -> tuple[date | None, date | None, str]:
    """Best-effort read of the ACTUAL `RegisteredFrom`/`RegisteredTo` window
    the currently-ingested Commons dump was fetched with, from
    `fetch_parliament_interests`'s own `parliament_interests.provenance.json`
    -- so the coverage denominator can be windowed to match what was
    actually fetchable, rather than silently assuming an unwindowed fetch
    (the same "read the fetch's own query shape" principle the
    `ExpandChildInterests` fix above applied one axis up, per an independent
    review: an ingest run with `--registered-from 2019-01-01
    --registered-to 2021-12-31` produces `item_count: 130`, but an unwindowed
    `totalResults` denominator (3,415, the full corpus) is not apples-to-
    apples with what that windowed fetch could ever have reached).

    Returns `(registered_from, registered_to, description)` -- `description`
    is always populated, on every path (found, not found, or unwindowed),
    so the caller can record an honest `known_limits` entry regardless of
    which branch fired; this function never silently succeeds or fails.
    """
    if provenance_path is None:
        return (
            None,
            None,
            "no ingest provenance path supplied -- denominator queried UNWINDOWED. If the "
            "live ingest was date-windowed, this denominator is not apples-to-apples with "
            "what was actually fetched (flagged, not resolved).",
        )
    path = Path(provenance_path)
    if not path.exists():
        return (
            None,
            None,
            f"no ingest provenance file found at {path} -- denominator queried UNWINDOWED. If "
            "the live ingest was date-windowed, this denominator is not apples-to-apples with "
            "what was actually fetched (flagged, not resolved).",
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    window = data.get("registered_range") or {}
    from_raw, to_raw = window.get("from"), window.get("to")
    if from_raw is None and to_raw is None:
        return (
            None,
            None,
            f"ingest provenance at {path} recorded an UNWINDOWED fetch (no RegisteredFrom/"
            "RegisteredTo) -- denominator queried unwindowed to match.",
        )
    return (
        date.fromisoformat(from_raw) if from_raw else None,
        date.fromisoformat(to_raw) if to_raw else None,
        f"ingest provenance at {path} recorded RegisteredFrom={from_raw}/RegisteredTo={to_raw} "
        "-- denominator queried with the SAME window, so it is apples-to-apples with what the "
        "live ingest actually fetched.",
    )


_COMMONS_UNEXPANDED_GAP_LIMIT = (
    "the ExpandChildInterests=true total used here as the denominator (3,415, verified live "
    "2026-08-04) is ~640 records SMALLER than the same query without that flag (4,057) -- "
    "parliament_interests.py's own module docstring calls this gap 'unexplained', and it "
    "cannot be attributed to parent/child collapsing alone (the live corpus held only ONE "
    "interest with children at capture time, which could not itself produce a 640-record "
    "swing). A smaller denominator flatters the reported coverage percentage. This module "
    "keeps ExpandChildInterests=true because that is the query shape the real fetch actually "
    "issues (the fix this module exists for) -- reverting to 4,057 would reintroduce that "
    "defect, comparing ingested records against a total the fetch can never reach. If the "
    "gap instead reflects records NOT reachable via ExpandChildInterests=true, THIS "
    "denominator understates the true corpus -- flagged here, not resolved."
)


def measure_commons_coverage(
    total_results: int | None = None,
    client: httpx.Client | None = None,
    ingest_provenance_path: str | Path | None = DEFAULT_COMMONS_INGEST_PROVENANCE_PATH,
) -> CoverageMeasurement:
    """UK Parliament (Commons) register ingest completeness (spec A2.4.2;
    packet: "the interests API reports totalResults; last measured 130 of
    4,057").

    `total_results`: pass explicitly to skip the live network call (tests,
    offline runs, or pinning a specific prior reading); omitted, this
    performs one live request via `fetch_commons_total_results`.

    `ingest_provenance_path`: where to look for the CURRENT ingest's own
    `parliament_interests.provenance.json` (written by
    `fetch_parliament_interests`) so the live `totalResults` query can be
    windowed to the SAME `RegisteredFrom`/`RegisteredTo` the ingest actually
    used, rather than silently assuming an unwindowed fetch -- see
    `_read_commons_ingest_date_window`'s docstring for the defect class this
    closes (an independent review found the real ingest that produced the
    live `ingested` count ran windowed `2019-01-01..2021-12-31`, while this
    denominator was reading the full unwindowed corpus). Pass `None` to
    skip the provenance read outright (documented, not silently assumed).
    Ignored when `total_results` is given explicitly -- no live query is
    made at all in that branch.

    `ingested` is `Attestation.objects.filter(source_name=COMMONS_SOURCE_NAME
    ).count()`: `parliament_interests.ingest_parliament_interests_json`
    keys each Commons interest's Attestation on
    `source_reference=str(interest_id)`, a 1:1 correspondence with the API's
    own record identifiers, so this count is directly comparable to
    `totalResults` without re-deriving interest identity.

    KNOWN LIMIT (fail closed, not reimplemented): `parliament_interests.py`
    does not persist a per-record fetch-failure reason the way
    `ch_officers.py`'s cache files do -- a page that failed to fetch simply
    produces fewer `items`, with no durable trace of which records were
    missed. Every non-ingested record is therefore counted as
    `not_attempted`, never `explicitly_failed`.

    `extra["total_source"]`/`extra["ingested_source"]` record HOW each half
    of the ratio was obtained (generalising the fix for the query-shape
    mismatch above: a coverage ratio is meaningless if its two halves came
    from incomparable queries, so both must always be traceable, not just
    the denominator). The ExpandChildInterests permissiveness gap (see
    `_COMMONS_UNEXPANDED_GAP_LIMIT`) is recorded in `known_limits` on every
    live measurement, not just when things look wrong.
    """
    known_limits: list[str] = []
    if total_results is None:
        registered_from, registered_to, window_note = _read_commons_ingest_date_window(
            ingest_provenance_path
        )
        total_results = fetch_commons_total_results(
            client=client, registered_from=registered_from, registered_to=registered_to
        )
        total_source = (
            f"live query, same shape as fetch_parliament_interests: params="
            f"{_commons_totals_query_params(registered_from, registered_to)} -- "
            "ExpandChildInterests=true matches what the real fetch always sends, so this "
            "total is apples-to-apples with what `ingested` can ever reach (see "
            f"_commons_totals_query_params's docstring). {window_note}"
        )
        known_limits.append(window_note)
        known_limits.append(_COMMONS_UNEXPANDED_GAP_LIMIT)
    else:
        total_source = (
            f"explicitly provided by the caller (total_results={total_results}) -- no live "
            "query made this call; the caller is responsible for having obtained this from "
            "the same ExpandChildInterests=true query shape fetch_parliament_interests uses."
        )

    ingested = Attestation.objects.filter(source_name=COMMONS_SOURCE_NAME).count()
    not_attempted = total_results - ingested

    known_limits.append(
        "no per-record fetch-failure evidence is persisted by parliament_interests.py -- "
        "every non-ingested record is counted as not_attempted, never explicitly_failed."
    )

    return CoverageMeasurement(
        name="commons_register",
        ingested=ingested,
        explicitly_failed=0,
        not_attempted=not_attempted,
        total=total_results,
        known_limits=tuple(known_limits),
        extra={
            "total_source": total_source,
            "ingested_source": (
                f"Attestation.objects.filter(source_name={COMMONS_SOURCE_NAME!r}).count() -- "
                "1:1 with the API's own record identifiers via "
                "source_reference=str(interest_id)."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Lords frozen-snapshot coverage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LordsSnapshotIntegrity:
    """Per-page SHA-256 verification against the snapshot's own provenance.json."""

    total_pages: int
    verified_pages: int
    mismatched_pages: tuple[str, ...]

    @property
    def intact(self) -> bool:
        return self.total_pages > 0 and self.verified_pages == self.total_pages


def verify_lords_snapshot_integrity(snapshot_dir: str | Path) -> LordsSnapshotIntegrity:
    """Re-hash every `page_NN.html` against `provenance.json`'s recorded SHA-256.

    Fail closed: a snapshot whose bytes do not match their own recorded
    hash is not a trustworthy frozen source snapshot (spec A2.4.5) and must
    never be silently used for a coverage measurement.
    """
    snapshot_dir = Path(snapshot_dir)
    provenance = json.loads((snapshot_dir / "provenance.json").read_text(encoding="utf-8"))
    page_hashes: dict[str, str] = provenance.get("page_hashes_sha256", {})
    mismatched: list[str] = []
    verified = 0
    for filename, expected in page_hashes.items():
        page_path = snapshot_dir / filename
        if not page_path.exists():
            mismatched.append(filename)
            continue
        actual = hashlib.sha256(page_path.read_bytes()).hexdigest()
        if actual == expected:
            verified += 1
        else:
            mismatched.append(filename)
    return LordsSnapshotIntegrity(
        total_pages=len(page_hashes),
        verified_pages=verified,
        mismatched_pages=tuple(mismatched),
    )


@dataclass(frozen=True)
class LordsSnapshotCoverage:
    """Both halves of spec A2.4.2's Lords coverage question -- members AND
    interests, per the delegation packet -- plus the integrity check they
    are conditional on."""

    integrity: LordsSnapshotIntegrity
    members: CoverageMeasurement
    interests: CoverageMeasurement


def measure_lords_snapshot_coverage(snapshot_dir: str | Path) -> LordsSnapshotCoverage:
    """Lords members and interests ingested vs. the frozen browser-captured
    snapshot (spec A2.4.2 "Lords source coverage"; packet: 789 members).

    Refuses (raises) if the snapshot fails `verify_lords_snapshot_integrity`
    -- a coverage number measured against tampered or corrupted pages is
    worse than none. Re-parses every page via
    `lords_interests._parse_lords_page` (reused, not reimplemented) for the
    snapshot's own member/interest counts, computed fresh every call rather
    than hardcoded, so this stays correct if a different snapshot capture
    is substituted later.

    Each parsed interest is classified via the pure, side-effect-free
    `lords_interests._extract_counterparty` (reused) into:
      * structurally excluded -- private-individual or no extractable
        counterparty name. These can NEVER produce a graph edge by design
        (mirrors `ingest_lords_snapshot`'s own `skipped_private_individual`/
        `skipped_no_counterparty` counters) -- a predeclared, terminal
        exclusion, counted toward `explicitly_failed`, never
        `not_attempted`.
      * extractable -- checked against the graph: `ingested` if an Edge
        exists from that member's Entity carrying this exact
        `properties["description"]` (the field both the live and
        snapshot-specific Lords ingest paths write identically -- see
        `lords_interests.ingest_lords_register` and
        `register_snapshots.ingest_lords_snapshot`), else `not_attempted`.

    KNOWN LIMIT: an extractable interest with no matching edge is reported
    as `not_attempted` even where the true cause might be an ambiguous
    company-name match correctly refused by `_resolve_counterparty`
    (ADR-006 duplicate-over-merge discipline) rather than a genuine gap.
    Distinguishing those would require re-deriving `_resolve_counterparty`'s
    resolution logic without its mutating side effects (it `get_or_create`s
    placeholder Entities) -- this read-only measurement deliberately does
    not do that. Fail-closed default: an unproven "correctly skipped" is
    counted against coverage, never credited to it.
    """
    integrity = verify_lords_snapshot_integrity(snapshot_dir)
    if not integrity.intact:
        raise ValueError(
            f"Lords snapshot at {snapshot_dir} failed integrity verification -- "
            f"{len(integrity.mismatched_pages)}/{integrity.total_pages} page(s) do not match "
            f"provenance.json's recorded hash: {list(integrity.mismatched_pages)}. Refusing to "
            "measure coverage against an unverified snapshot (spec A2.4.5)."
        )

    snapshot_dir = Path(snapshot_dir)
    page_files = sorted(snapshot_dir.glob("page_*.html"))

    member_ids: set[str] = set()
    interests_total = 0
    interests_excluded = 0
    extractable_by_member: dict[str, list[str]] = {}

    for page_file in page_files:
        members = _parse_lords_page(page_file.read_text(encoding="utf-8"))
        for member in members:
            member_ids.add(member["member_id"])
            for interest in member["interests"]:
                interests_total += 1
                name, _company_number, is_private = _extract_counterparty(interest["description"])
                if is_private or not name:
                    interests_excluded += 1
                else:
                    extractable_by_member.setdefault(member["member_id"], []).append(
                        interest["description"]
                    )

    members_ingested = Entity.objects.filter(
        entity_type="person",
        registry_scheme=LORDS_MEMBER_REGISTRY_SCHEME,
        registry_id__in=member_ids,
    ).count()

    ingested_pairs = 0
    for member_id, descriptions in extractable_by_member.items():
        existing_descriptions = set(
            Edge.objects.filter(
                edge_type="declared_interest",
                source_entity__registry_scheme=LORDS_MEMBER_REGISTRY_SCHEME,
                source_entity__registry_id=member_id,
            ).values_list("properties__description", flat=True)
        )
        ingested_pairs += sum(1 for d in descriptions if d in existing_descriptions)

    interests_extractable_count = sum(len(v) for v in extractable_by_member.values())
    interests_not_attempted = interests_extractable_count - ingested_pairs

    member_measurement = CoverageMeasurement(
        name="lords_members",
        ingested=members_ingested,
        explicitly_failed=0,
        not_attempted=len(member_ids) - members_ingested,
        total=len(member_ids),
    )
    interest_measurement = CoverageMeasurement(
        name="lords_interests",
        ingested=ingested_pairs,
        explicitly_failed=interests_excluded,
        not_attempted=interests_not_attempted,
        total=interests_total,
        known_limits=(
            "an extractable interest with no matching edge is counted as not_attempted even "
            "if the true cause is a correctly-refused ambiguous company match (ADR-006) -- "
            "fail closed, not credited.",
        ),
        extra={"structurally_excluded_private_or_unnamed": interests_excluded},
    )
    return LordsSnapshotCoverage(
        integrity=integrity, members=member_measurement, interests=interest_measurement
    )
