"""lead_scan.py -- a reproducible, committed two-hop investigative-lead funnel.

The vault note `02 Projects/Ideas/Decorruptio/findings.md` SS6 publishes a
funnel (13,258 candidate awardee companies -> 2,912 resolved to a graph
Entity -> 165 with any 2-hop path -> 96 pre-award, 135 paths) and one named
investigative lead derived from it. That note lives in the Obsidian vault,
not this repository. No committed script produces those numbers: a
repo-wide search (`scripts/`, `experiments/`, `.consult/`, `git log --all`)
turns up nothing beyond `phase_c_paths.py` itself (a different, smaller
52-row VIP-lane cohort test) -- see `findings.md` SS4.29 in that same vault
note. The driver was a one-off, run once and never committed.

THIS SCRIPT DOES NOT REPRODUCE THAT FUNNEL. It establishes a new, independently
reproducible baseline going forward, using the exact traversal primitives
(`build_adjacency`, `find_paths`) and identity-confidence reducer
(`path_min_identity_confidence`) the project already has, committed and
importable. A later reconstruction using this same registry-ID-only resolution
method resolved ~93% of company-number candidates to a graph Entity where the
originally published figure was ~22%; the gap could not be explained because
the original driver no longer exists to diff against, and a stale-graph
explanation was directly ruled out (entity/edge counts confirmed identical
before and after the bridge fix this reconstruction ran against). Do not treat
this script's funnel numbers as comparable to, or a correction of, the
historical ones -- they measure the same QUESTION with a DIFFERENT, and this
time auditable, METHOD.

Method, deliberately mirroring `findings.md` SS6.1's own description so the
question being asked stays the same even though the driver code does not:

  1. candidates  -- distinct, NORMALISED `SupplierResolution.company_number`
     for `source_id="uk_contracts_finder"` (see `build_candidates`'s
     docstring for why normalisation is needed to avoid double-counting one
     company under two spellings). The sealed cohort is NOT excluded here
     (SS6.1 excluded 20 sealed-cohort numbers) -- excluding it would require
     reading `SEALED_COHORT_V2_COMPANY_NUMBERS` (`scripts/run_gold_benchmark.py`)
     as an input, which this script's scope forbids: it is exploratory
     analysis, not a scorer, and must never touch the sealed cohort, the
     gates, or anything resembling a score or verdict. See
     `SEALED_COHORT_OVERLAP_NOTE` for the measured, benign overlap this
     deviation produces.
  2. resolved     -- candidates that resolve to a graph `Entity` by
     `company_number` ALONE (registry-ID-only; see `resolve_candidates`'s
     docstring for why the name-fallback tier of `resolve_supplier` is
     deliberately not engaged here). As of this writing, candidates only
     cover suppliers that already have a `SupplierResolution` row at all --
     55.0% of real awardee supplier names do not (see `stage1_context` in
     the emitted report), so `resolved` is a fraction of a fraction; do not
     read `resolved / candidates` as visibility into the full awardee
     population.
  3. any_path_within_max_hops -- resolved candidates with at least one path
     (dated or not) within `max_hops` of any `UK-PARLIAMENT-MEMBER` entity.
     Named generically, not "any_2hop_path", because `max_hops` is a CLI
     argument: the key and the caveat text both describe whatever hop budget
     the run actually used (see `INVESTIGATIVE_LEAD_CAVEAT_TEMPLATE`).
  4. pre_award    -- resolved candidates with at least one path whose dated
     edges all precede that company's own earliest resolved award date. A
     resolved candidate whose only path(s) are fully dated but NOT before
     that cutoff is `dated_post_award`, not `undated_only` -- see `scan`'s
     docstring for why conflating the two would hide a real negative finding
     behind a "no data" label.

Resolution failures are reported, never silently dropped: a candidate whose
`company_number` never resolves is `unresolved`, distinct from a resolved
candidate with no path (`no_path`), distinct again from a resolved candidate
with a path but no known award date to test pre-award admissibility against
(`path_no_award_date`), distinct again from a resolved candidate whose only
path(s) are dated but fall on or after the award (`dated_post_award`) --
conflating any of these would let poor matching or missing data masquerade
as a negative finding, or a real negative masquerade as missing data.

Every person named in the output is described strictly by what the underlying
registers factually attest. A recovered path within the run's hop budget is
an investigative lead for a human, never an allegation (ADR-000). `same_as`
identity-bridge confidences are uncalibrated match-method labels, not
probabilities -- hand-verification of a prior scan found 15 of 21 checked
cross-register identity paths were namesake collisions at BOTH confidence
tiers, including an MP whose matched appointment would have made him 15
years old. See `NOT_HISTORICAL_FUNNEL_STATEMENT`,
`INVESTIGATIVE_LEAD_CAVEAT_TEMPLATE` and `CONFIDENCE_CAVEAT` below -- all
three are written into the emitted JSON artifact itself, not just this
docstring, so a reader of the JSON in isolation cannot mistake it for the
historical figures or an accusation.

Read-only by design: every DB access below is a `.filter`/`.values_list`
query; nothing in this module writes to the graph, applies a migration,
changes a threshold or gate, or reads the sealed cohort.

Reuse, not reimplementation: `build_adjacency`, `find_paths` and
`resolve_supplier` are imported unmodified from `scripts/phase_c_paths.py`
(the same traversal and matching primitives `findings.md` SS6.1 itself reused
from that file), and `path_min_identity_confidence` is imported unmodified
from `uncorrupt.graph.register_snapshots`. None of the four is reimplemented
here -- a second, divergent definition of what a "path" or an "identity
confidence" is would be exactly the class of defect this project keeps
hitting.

Deterministic and re-runnable: candidates are iterated in sorted
`company_number` order; each company's own recovered paths are sorted by
their edge-id tuple before being capped/reported, so the DB's own (unordered)
row-scan order for `Edge.objects.all()` inside `build_adjacency` can never
change which paths get reported or in what order, even though it is not
itself modified here. `UK-PARLIAMENT-MEMBER` start ids are threaded through
as a Python `set[int]`. CPython does not randomise `int` hashing the way it
randomises `str`/`bytes` (`hash(n) == n` for small non-negative ints), but
that does NOT make a `set[int]`'s iteration order insertion-order-independent
-- it only means the order is *reproducible* across runs, not that it is
*sorted* or *insertion-order-agnostic*. Two ids that collide in the same hash
bucket (e.g. 1 and 9, which both land on slot 1 of an 8-slot table -- `1 % 8
== 9 % 8 == 1`) iterate in INSERTION order, not numeric order: `{1, 9}`
iterates `[1, 9]` but `{9, 1}` iterates `[9, 1]`. The explicit
`sorted(reportable_pre_award, key=_sort_key)` call in `scan` is what actually
makes the reported path order deterministic and edge-id-ascending; the set's
own hash-driven order is not to be relied on for that.

Usage:
    PYTHONPATH=.:src python scripts/lead_scan.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import date
from typing import Any

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.phase_c_paths import (  # noqa: E402
    build_adjacency,
    find_paths,
    normalise_company_number,
    resolve_supplier,
)

from uncorrupt.graph.models import Edge, Entity  # noqa: E402
from uncorrupt.graph.register_snapshots import path_min_identity_confidence  # noqa: E402
from uncorrupt.staging.models import Award, SupplierResolution  # noqa: E402

SOURCE_ID = "uk_contracts_finder"
MEMBER_ENTITY_TYPE = "person"
MEMBER_REGISTRY_SCHEME = "UK-PARLIAMENT-MEMBER"
DEFAULT_MAX_HOPS = 2
DEFAULT_OUT = "experiments/lead_scan.json"

_FINDINGS_MD_CITATION = (
    "the vault note `02 Projects/Ideas/Decorruptio/findings.md` (not present in "
    "this repository -- it lives in the Obsidian vault, not the code repo)"
)

NOT_HISTORICAL_FUNNEL_STATEMENT = (
    "This is a NEW, independently reproducible baseline, NOT a reproduction of "
    f"{_FINDINGS_MD_CITATION} SS6's published 13,258 / 2,912 / 165 / 96 / 135 "
    "funnel. That funnel's driver script was never committed to this repo "
    "(confirmed absent via grep across scripts/, experiments/, .consult/, and "
    f"`git log --all` -- {_FINDINGS_MD_CITATION} SS4.29) and cannot be "
    "reproduced, diffed, or audited against this one. A later reconstruction "
    "using this same registry-ID-only resolution method resolved approximately "
    "93% of company-number candidates to a graph Entity where the originally "
    "published figure was approximately 22%; that discrepancy could not be "
    "explained because the original driver no longer exists, and a stale-graph "
    "explanation was directly ruled out (entity and edge counts confirmed "
    "identical throughout). Do not compare this script's funnel numbers "
    "against the historical ones as if they measured the same run -- they ask "
    "the same question with a different, this time auditable, method."
)

# {max_hops} is filled in with the actual value the run used -- see
# `_investigative_lead_caveat` below. A previous version of this text
# hardcoded "two-hop", which stayed on screen and in the emitted JSON even
# when `--max-hops` was run at a different value: a company reachable only
# via a 3-hop path (4 edges: 1 same_as + 3 real relationship edges) would be
# published under the funnel key `any_2hop_path` and the caveat "A recovered
# two-hop path is an investigative lead" while `max_hops: 3` sat right next
# to it in the same artifact -- self-contradicting about a named person.
INVESTIGATIVE_LEAD_CAVEAT_TEMPLATE = (
    "A recovered path of at most {max_hops} hop(s) is an investigative lead "
    "for a human, never an allegation (ADR-000). Every person named below is "
    "described strictly by what the underlying registers factually attest -- "
    "a same_as identity bridge, an officer_of appointment, a declared "
    "interest, or a donation -- never by any conclusion this script draws. "
    "This script is exploratory analysis, not a scorer: it does not read the "
    "sealed cohort, and it does not emit a score or a verdict."
)


def _investigative_lead_caveat(max_hops: int) -> str:
    """The investigative-lead caveat, worded for the hop budget actually used.

    Keeps the caveat text and the `any_path_within_max_hops` funnel key
    truthful for any `--max-hops` value, not just the default of 2 --
    see the template's comment above for the contradiction this closes.
    """
    return INVESTIGATIVE_LEAD_CAVEAT_TEMPLATE.format(max_hops=max_hops)


# Deliberately NOT excluded from `candidates` -- see `build_candidates`'s
# docstring for why. Measured against the live graph: of the 20 numbers in
# `SEALED_COHORT_V2_COMPANY_NUMBERS` (`scripts/run_gold_benchmark.py`), 6
# resolve and appear as rows in this script's own output, and 0 of those 6
# land in `pre_award` (all 6 are `no_path`). Benign as measured, but stated
# here so a reader of the artifact knows sealed-benchmark company numbers can
# appear among its rows -- this script does not import or read the sealed
# cohort at runtime to produce this figure; it is a hardcoded, one-time,
# manually verified fact, consistent with this module's invariant that it
# must never touch the sealed cohort, the gates, or anything score-shaped.
SEALED_COHORT_OVERLAP_NOTE = (
    "This script does NOT exclude the sealed benchmark cohort from its "
    "candidate set (a documented deviation from findings.md SS6.1, which "
    "excluded 20 sealed-cohort company numbers). As measured against the "
    "graph this caveat was written against: 6 of the 20 sealed-cohort company "
    "numbers resolve and appear as rows below, and 0 of those 6 are "
    "'pre_award' (all 6 are 'no_path') -- benign as measured, but a reader "
    "should know sealed-benchmark company numbers can appear among these rows."
)

CONFIDENCE_CAVEAT = (
    "same_as identity-bridge confidences (0.60 'surname + peerage title only', "
    "0.85 forename- or territorial-designation-verified) are UNCALIBRATED "
    "match-method labels, not probabilities. Hand-verification of a prior "
    "corrected-bridge scan found 15 of 21 individually checked cross-register "
    "identity paths were namesake collisions -- two different real humans "
    "sharing a name -- at BOTH confidence tiers: at 0.85, one match paired an "
    "MP born in 1986 with a 2002 company directorship, implying an age of 15, "
    "below the legal minimum age for a UK company director; two others carried "
    "a Companies House middle name that did not match the member's own "
    "documented name. Never read `min_identity_confidence` as "
    "P(this identity match is correct)."
)


def build_candidates() -> dict[str, date | None]:
    """distinct NORMALISED `SupplierResolution.company_number` -> earliest award date, or None.

    Mirrors `findings.md` SS6.1's own definition of the candidate set: distinct
    `SupplierResolution.company_number` for `source_id="uk_contracts_finder"`.
    Unlike SS6.1, the sealed cohort is NOT excluded -- see this module's
    docstring for why (excluding it would require reading it as an input,
    which this script's scope forbids).

    Company numbers are normalised (`phase_c_paths.normalise_company_number`)
    BEFORE they become candidate keys, not just at resolution time. Upstream
    staging stores the raw `supplier_id` on a Companies House miss and the
    normalised spelling on a hit (e.g. `"4125764"` vs `"04125764"` for the
    same real company), so two different strings can name one company. A
    company that shows up under both spellings must collapse to ONE
    candidate here -- otherwise it is counted twice in `candidates`, and if
    both spellings ever resolve, the same person and company would be
    emitted as two separate pre-award leads. This script does not touch the
    upstream staging table; it only normalises its own candidate key.

    A candidate's cutoff is the EARLIEST `Award.award_date` among every
    `supplier_name` that resolved to its `company_number` (more than one
    supplier-name string can resolve to the same company, now including
    different-spelling company-number strings that normalise to the same
    one). `None` means no dated award was found for this candidate at all --
    reported honestly via the `path_no_award_date` status downstream, never
    silently defaulted to a permissive or restrictive date.
    """
    raw_pairs = (
        SupplierResolution.objects.filter(source_id=SOURCE_ID)
        .exclude(company_number__isnull=True)
        .exclude(company_number="")
        .values_list("supplier_name", "company_number")
    )
    supplier_to_company: dict[str, str] = {
        supplier_name: normalise_company_number(company_number)
        for supplier_name, company_number in raw_pairs
    }

    cutoffs: dict[str, date] = {}
    awards = (
        Award.objects.filter(source_id=SOURCE_ID, supplier_name__in=supplier_to_company.keys())
        .exclude(award_date__isnull=True)
        .values_list("supplier_name", "award_date")
    )
    for supplier_name, award_date in awards:
        company_number = supplier_to_company[supplier_name]
        award_day = award_date.date()
        current = cutoffs.get(company_number)
        if current is None or award_day < current:
            cutoffs[company_number] = award_day

    return {cn: cutoffs.get(cn) for cn in sorted(set(supplier_to_company.values()))}


def resolve_candidates(candidates: dict[str, date | None]) -> dict[str, Entity | None]:
    """Registry-ID-only resolution: `company_number` -> graph `Entity`, or `None`.

    `resolve_supplier` accepts both a `name` and a `company_number` and, given
    a name, falls back to normalised-name matching when the identifier lookup
    fails. That fallback is deliberately NOT engaged here (`name=""`, which
    trips `resolve_supplier`'s own `if not target: return None` guard):
    every candidate here already carries a specific registry identifier, and
    falling back to a name-string match for a company number that is not
    (yet) a graph `Entity` would resolve to a DIFFERENT, merely
    similarly-named company -- exactly the identity-conflation failure mode
    ADR-004 D2 exists to rule out ("resolution by registry ID wherever
    possible, never by person name string"; the same principle extends to
    companies here). A candidate that cannot be resolved this way is reported
    as `unresolved`, never silently patched over with a name guess.
    """
    return {cn: resolve_supplier(name="", ch_cache={}, company_number=cn) for cn in candidates}


def member_entity_ids() -> set[int]:
    """Every `UK-PARLIAMENT-MEMBER` entity id -- the path-search start set."""
    return set(
        Entity.objects.filter(
            entity_type=MEMBER_ENTITY_TYPE, registry_scheme=MEMBER_REGISTRY_SCHEME
        ).values_list("id", flat=True)
    )


def _path_member_id(path: list[Edge], member_ids: set[int]) -> int:
    """Which member entity a recovered path started from.

    `find_paths` walks outward from every id in `start_ids`; the first edge on
    a returned path always touches exactly one of them, because the member and
    company entity pools are disjoint. Recoverable without modifying
    `find_paths` to also return it.
    """
    first = path[0]
    if first.source_entity_id in member_ids:
        return first.source_entity_id
    return first.target_entity_id


def _weakest_same_as_tier(path: list[Edge], min_confidence: float | None) -> str | None:
    """The `match_method` tier label of the bridge `path_min_identity_confidence` reduced to.

    A companion lookup, not a re-derivation: the confidence NUMBER itself
    comes only from `path_min_identity_confidence` (imported, not
    reimplemented). This only finds which attestation record produced that
    number, so a reader can see which tier a path's identity claim rests on
    alongside the confidence. If more than one attestation ties on the
    minimum value, the tie is broken deterministically by `(edge id,
    match_method)`.
    """
    if min_confidence is None:
        return None
    tied: list[tuple[int, str]] = []
    for edge in path:
        if edge.edge_type != "same_as":
            continue
        for confidence, method in edge.attestations.values_list("match_confidence", "match_method"):
            if confidence == min_confidence:
                tied.append((edge.id, method))
    return sorted(tied)[0][1] if tied else None


def _serialize_edge(edge: Edge) -> dict[str, Any]:
    sources = sorted(edge.attestations.values_list("source_name", flat=True).distinct())
    return {
        "edge_type": edge.edge_type,
        "valid_from": edge.valid_from.isoformat() if edge.valid_from else None,
        "attesting_sources": sources,
    }


def _sort_key(path: list[Edge]) -> tuple[int, ...]:
    """Deterministic path ordering independent of DB row-scan order -- see module docstring."""
    return tuple(edge.id for edge in path)


def _path_fully_dated(path: list[Edge]) -> bool:
    """Does every real (non-`same_as`) edge on this path carry a `valid_from`?

    `find_paths` returns a path in its `undated` bucket whenever it is NOT
    admissible pre-award -- either because some real edge has no date at all,
    OR because every real edge IS dated but on or after the cutoff. Those are
    different facts: the first is missing data, the second is a dated
    relationship that happens to have started after the award (dispositive
    evidence about timing, not an absence of evidence). This distinguishes
    them so `scan` can report the second case as `dated_post_award` instead
    of folding it into `undated_only`. A path with no real edges at all
    (same_as-only, which cannot occur since a path always ends on a
    non-same_as edge into the company) is treated as not fully dated.
    """
    real_edges = [edge for edge in path if edge.edge_type != "same_as"]
    return bool(real_edges) and all(edge.valid_from is not None for edge in real_edges)


def _serialize_path(
    path: list[Edge],
    member_ids: set[int],
    members_by_id: dict[int, Entity],
    company_entity: Entity,
) -> dict[str, Any]:
    member = members_by_id[_path_member_id(path, member_ids)]
    min_confidence = path_min_identity_confidence(path)
    return {
        "member_entity_id": member.id,
        "member_name": member.name,
        "member_registry_id": member.registry_id,
        "company_entity_id": company_entity.id,
        "company_name": company_entity.name,
        "company_registry_id": company_entity.registry_id or company_entity.company_number,
        "edges": [_serialize_edge(edge) for edge in path],
        "same_as_tier": _weakest_same_as_tier(path, min_confidence),
        "min_identity_confidence": min_confidence,
    }


def _unresolved_row(company_number: str) -> dict[str, Any]:
    return {
        "company_number": company_number,
        "company_entity_id": None,
        "company_entity": None,
        "status": "unresolved",
        "award_cutoff": None,
        "any_path_within_max_hops": False,
        "n_pre_award_paths": 0,
        "n_dated_post_award_paths": 0,
        "n_undated_paths": None,
        "pre_award_paths": [],
        "dated_post_award_paths": [],
    }


def scan(
    adj: dict[int, list[Edge]],
    candidates: dict[str, date | None],
    resolved: dict[str, Entity | None],
    member_ids: set[int],
    members_by_id: dict[int, Entity],
    max_hops: int,
) -> list[dict[str, Any]]:
    """Run the funnel for every candidate, sorted, and return one row each.

    One `find_paths` call per resolved candidate, exactly as
    `findings.md` SS6.1 describes running it. When a candidate's award cutoff
    is unknown, `date.max` is used ONLY to test structural path existence
    (`any_path_within_max_hops`) -- the resulting paths are never reported as
    `pre_award`, since there is no real cutoff to test them against.

    Status vocabulary (mutually exclusive, see module docstring):
    `unresolved` (handled by `_unresolved_row`, before this branch) /
    `no_path` / `undated_only` / `dated_post_award` / `path_no_award_date` /
    `pre_award`. `undated_only` vs `dated_post_award` is the one distinction
    computed here rather than read straight off `find_paths`: `find_paths`'
    own `undated` bucket mixes "some real edge has no date at all" with
    "every real edge is dated but on or after the cutoff" -- see
    `_path_fully_dated`. Only the first is genuinely `undated_only`; the
    second is dispositive-negative evidence (the relationship is dated, and
    it started after the award) and is reported as `dated_post_award`
    instead, never silently folded into "no data".
    """
    rows: list[dict[str, Any]] = []
    for company_number in sorted(candidates):
        cutoff = candidates[company_number]
        entity = resolved[company_number]
        if entity is None:
            rows.append(_unresolved_row(company_number))
            continue

        pre_award, undated = find_paths(
            member_ids, entity.id, adj, max_hops, cutoff if cutoff is not None else date.max
        )
        any_path_found = bool(pre_award or undated)

        reportable_pre_award: list[list[Edge]] = []
        reportable_dated_post_award: list[list[Edge]] = []
        if cutoff is None:
            status = "path_no_award_date" if any_path_found else "no_path"
        elif pre_award:
            status = "pre_award"
            reportable_pre_award = pre_award
        elif undated:
            fully_dated = [p for p in undated if _path_fully_dated(p)]
            if fully_dated:
                status = "dated_post_award"
                reportable_dated_post_award = fully_dated
            else:
                status = "undated_only"
        else:
            status = "no_path"

        sorted_pre_award = sorted(reportable_pre_award, key=_sort_key)
        sorted_dated_post_award = sorted(reportable_dated_post_award, key=_sort_key)
        rows.append(
            {
                "company_number": company_number,
                "company_entity_id": entity.id,
                "company_entity": entity.name,
                "status": status,
                "award_cutoff": cutoff.isoformat() if cutoff else None,
                "any_path_within_max_hops": any_path_found,
                "n_pre_award_paths": len(sorted_pre_award),
                "n_dated_post_award_paths": len(sorted_dated_post_award),
                "n_undated_paths": len(undated) if cutoff is not None else None,
                "pre_award_paths": [
                    _serialize_path(p, member_ids, members_by_id, entity) for p in sorted_pre_award
                ],
                "dated_post_award_paths": [
                    _serialize_path(p, member_ids, members_by_id, entity)
                    for p in sorted_dated_post_award
                ],
            }
        )
    return rows


def compute_funnel(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate row statuses into the funnel. Every resolved row lands in
    exactly one of `no_path` / `undated_only` / `dated_post_award` /
    `path_no_award_date` / `pre_award_companies` -- see `scan`'s status
    assignment -- so those five always sum to `resolved`, and
    `resolved + unresolved == candidates`.
    """
    funnel = {
        "candidates": len(rows),
        "resolved": 0,
        "unresolved": 0,
        "any_path_within_max_hops": 0,
        "no_path": 0,
        "undated_only": 0,
        "dated_post_award_companies": 0,
        "dated_post_award_paths_total": 0,
        "path_no_award_date": 0,
        "pre_award_companies": 0,
        "pre_award_paths_total": 0,
    }
    for row in rows:
        if row["status"] == "unresolved":
            funnel["unresolved"] += 1
            continue
        funnel["resolved"] += 1
        if row["any_path_within_max_hops"]:
            funnel["any_path_within_max_hops"] += 1
        if row["status"] == "pre_award":
            funnel["pre_award_companies"] += 1
            funnel["pre_award_paths_total"] += row["n_pre_award_paths"]
        elif row["status"] == "dated_post_award":
            funnel["dated_post_award_companies"] += 1
            funnel["dated_post_award_paths_total"] += row["n_dated_post_award_paths"]
        elif row["status"] == "undated_only":
            funnel["undated_only"] += 1
        elif row["status"] == "path_no_award_date":
            funnel["path_no_award_date"] += 1
        else:
            funnel["no_path"] += 1
    return funnel


def _stage1_context() -> dict[str, Any]:
    """How far downstream `candidates` starts, against the real awardee population.

    `candidates` is scoped to `SupplierResolution` rows that already exist for
    `source_id="uk_contracts_finder"` -- it says nothing about awardee supplier
    names with NO `SupplierResolution` row at all (never attempted, or attempted
    and not persisted). Measured against the live graph, 55.0% of distinct
    awardee supplier names in `Award` have no `SupplierResolution` row: a reader
    seeing `candidates: 13,124 -> resolved: ~12,200` could infer ~93% graph
    visibility into awardees, when against the real awardee population it is
    roughly 31% (resolved / total awardee names). This makes that denominator
    explicit in the artifact instead of leaving it to be inferred.
    """
    awardee_names = set(
        Award.objects.filter(source_id=SOURCE_ID).values_list("supplier_name", flat=True)
    )
    resolution_names = set(
        SupplierResolution.objects.filter(source_id=SOURCE_ID).values_list(
            "supplier_name", flat=True
        )
    )
    without_resolution = awardee_names - resolution_names
    total = len(awardee_names)
    return {
        "awardee_supplier_names_total": total,
        "awardee_supplier_names_without_supplier_resolution_row": len(without_resolution),
        "pct_awardee_supplier_names_without_supplier_resolution_row": (
            round(100 * len(without_resolution) / total, 1) if total else None
        ),
        "note": (
            "`candidates` below counts only SupplierResolution rows that already "
            "exist for this source; the percentage above is the share of real "
            "awardee supplier names (from Award) that have none at all, and are "
            "therefore not `candidates`, not `unresolved`, not any status below."
        ),
    }


def _git_commit() -> str | None:
    """Best-effort provenance stamp. `None`, never raises, if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def build_report(max_hops: int = DEFAULT_MAX_HOPS) -> dict[str, Any]:
    candidates = build_candidates()
    resolved = resolve_candidates(candidates)
    member_ids = member_entity_ids()
    members_by_id = {e.id: e for e in Entity.objects.filter(id__in=member_ids)}
    adj = build_adjacency()

    rows = scan(adj, candidates, resolved, member_ids, members_by_id, max_hops)
    funnel = compute_funnel(rows)

    return {
        "not_a_reproduction_of_historical_funnel": NOT_HISTORICAL_FUNNEL_STATEMENT,
        "investigative_lead_caveat": _investigative_lead_caveat(max_hops),
        "identity_confidence_caveat": CONFIDENCE_CAVEAT,
        "sealed_cohort_overlap_note": SEALED_COHORT_OVERLAP_NOTE,
        "stage1_context": _stage1_context(),
        "graph_state": {
            "commit": _git_commit(),
            "entities": Entity.objects.count(),
            "edges": Edge.objects.count(),
            "same_as_edges": Edge.objects.filter(edge_type="same_as").count(),
            "supplier_resolution_rows": SupplierResolution.objects.filter(
                source_id=SOURCE_ID
            ).count(),
        },
        "max_hops": max_hops,
        "funnel": funnel,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-hops", type=int, default=DEFAULT_MAX_HOPS)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    if args.max_hops < 1:
        # 0 or negative is not a smaller funnel, it is a degenerate one:
        # `find_paths`'s `walk` bails before adding a single edge (see
        # `phase_c_paths.find_paths`), so every candidate would report
        # `no_path` and the artifact would still carry the full set of
        # investigative-lead caveats over zero real connectivity. Reject it
        # up front instead of silently emitting that.
        parser.error(f"--max-hops must be a positive integer, got {args.max_hops}")

    print(f"graph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")

    report = build_report(max_hops=args.max_hops)

    print("\n=== LEAD SCAN (new baseline -- NOT the historical findings.md SS6 funnel) ===")
    for key, value in report["funnel"].items():
        print(f"{key:22s}: {value}")

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
