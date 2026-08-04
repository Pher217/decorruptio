"""Phase C — can the graph recover a VIP-lane supplier<->referrer relationship?

Phase C v1 asked only for a DIRECT edge between the referrer person and the
supplier company, and recovered 0 of 52. That is a necessary but very narrow
test: the relationships this cohort is about are rarely a single declared edge
between the two named parties. They are more often mediated -- a shared
directorship, a company both parties are attached to, a donation to a party the
referrer sits in.

This script widens the question to PATHS of length <= 2 while keeping every
discipline rule that made v1 credible:

  * The cohort is fixed. Rows come from `.consult/vip_lane_positives.csv`
    (the official DHSC High Priority Lane table). No row is added, dropped or
    reselected to improve the number.
  * Only PRE-AWARD evidence counts. An edge is admissible only if its
    `valid_from` is strictly before the award date. An edge with no
    `valid_from` cannot be shown to pre-date the award, so it is counted
    separately (`undated_only`) and never silently credited as a hit.
  * Resolution failures are reported, not hidden. A row whose supplier or
    referrer never resolved is `unresolved`, distinct from a resolved row with
    no path (`no_path`) -- conflating them would let poor matching masquerade
    as a negative finding.

Direction is ignored when walking (a relationship is symmetric for this
question even though the edge that records it is directed).

Usage:
    PYTHONPATH=.:src python scripts/phase_c_paths.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import defaultdict
from datetime import date

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.models import Edge, Entity  # noqa: E402

COHORT_CSV = ".consult/vip_lane_positives.csv"
VIP_CH_CACHE = "experiments/vip_ch_cache.json"

# The lane operated through 2020; awards in the cohort are 2020 or later. Rows
# carry no machine-readable award date, so we use a single conservative cutoff
# rather than inventing a per-row date we cannot source.
AWARD_CUTOFF = date(2020, 3, 1)

# GROUP and HOLDINGS are deliberately NOT in this list -- they are legally
# distinguishing words, not filler. Stripping them collapsed three real,
# separately-registered companies onto one normalised key: "CLOSE BROTHERS
# GROUP PLC" (the parent, GB-COH 00520241), "CLOSE BROTHERS LIMITED" (a
# subsidiary, 00195626), and "CLOSE BROTHERS HOLDINGS LIMITED" (another
# subsidiary, 06582618) all normalised to "CLOSE BROTHERS", turning a single
# unambiguous name into a 3-way collision (found via a positive-control run).
_SUFFIXES = re.compile(r"\b(LIMITED|LTD|PLC|LLP|LP|INTERNATIONAL|UK|THE|AND|CO)\b")


def normalise_company_number(cn: str) -> str:
    cn = (cn or "").strip().upper()
    m = re.match(r"^([A-Z]*)(\d+)$", cn)
    if not m:
        return cn
    prefix, digits = m.groups()
    return prefix + digits.zfill(8 - len(prefix))


def normalise_name(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def surname(person_name: str) -> str:
    """Last alphabetic token, lowercased.

    Peers are recorded in the register with titles ("Baroness Mone of
    Mayfair") and in the DHSC table without them, so a full-string match
    fails on almost every row. Surname is the one token both forms share.
    This is deliberately crude and OVER-matches; a hit found this way is a
    candidate, and the count it produces is a ceiling on true matches, not a
    claim about any individual.

    Peerage names carry a TERRITORIAL DESIGNATION after "of" -- "Lord Agnew of
    Oulton", "Baroness Mone of Mayfair". Taking the last token would yield
    "oulton" and "mayfair", which never match the "Agnew"/"Mone" an external
    table writes. Found by positive controls: 14 of 15 resolution failures
    were this.

    Companies House writes officers as "SURNAME, Forenames, Title", so the
    LAST token there is a forename or a title, not the family name --
    "LEWIS, John Patrick, Sir" would index under "patrick". When a comma is
    present the surname is everything before the first one.

    Post-nominals ("MP", "QC", "OBE") must also be stripped: "Matt Hancock MP"
    otherwise resolves to "mp", and every MP in a cohort collapses onto
    whichever entity happens to parse the same way. That produced four phantom
    matches in Phase C, all pointing at one unrelated officer record.
    """
    raw = person_name or ""
    if "," in raw:
        raw = raw.split(",", 1)[0]
    name = re.split(r"\s+of\s+", raw, maxsplit=1, flags=re.IGNORECASE)[0]
    tokens = [t for t in re.split(r"[^A-Za-z]+", name) if len(t) > 1]
    tokens = [
        t
        for t in tokens
        if t.lower()
        not in {
            "of",
            "the",
            "lord",
            "lady",
            "baroness",
            "baron",
            "sir",
            "dame",
            # post-nominals and honorifics that are never a family name
            "mp",
            "msp",
            "qc",
            "kc",
            "obe",
            "cbe",
            "mbe",
            "dbe",
            "kbe",
            "gbe",
            "phd",
            "prof",
            "dr",
            "mr",
            "mrs",
            "ms",
            "rt",
            "hon",
            "esq",
        }
    ]
    return tokens[-1].lower() if tokens else ""


def prefer_companies_house(candidates: list[Entity]) -> list[Entity]:
    """Disambiguate a name-matched candidate SET without merging anything.

    GLEIF publishes an LEI for organisations registered with a UK authority
    other than Companies House (e.g. a charity that is also a company limited
    by guarantee), which the ingest correctly leaves un-cross-linked
    (`company_number` NULL) -- see `graph.gleif._resolve_gb_company`. That
    surfaces at name-resolution time as two exact-name-matching Entity rows
    for one real organisation: a GB-COH record and a GLEIF-LEI record ("EDGE
    FOUNDATION", found via a positive control). Companies House is the
    authoritative national company register, so when a GB-COH candidate is
    present and unique among the matches, it wins the tie -- the GLEIF-LEI
    row is untouched in the database, just not the chosen path-search node.
    Any other ambiguity (e.g. two GB-COH candidates) is left unresolved.
    """
    if len(candidates) <= 1:
        return candidates
    gb_coh = [e for e in candidates if e.registry_scheme == "GB-COH"]
    return gb_coh if len(gb_coh) == 1 else candidates


def _resolve_by_company_number(cn: str) -> Entity | None:
    """Deterministic, GB-COH-preferring lookup for one normalised company number.

    A company number can be carried by more than one Entity row: GLEIF
    cross-links a UK company's LEI record with the same `company_number` as
    its Companies House twin (`graph.gleif._resolve_gb_company`), so
    `Entity.objects.filter(company_number=cn)` can return several rows for
    one real organisation. Django's `.first()` implicitly orders an unordered
    queryset by `pk`, so a plain `.first()` here always picks whichever
    candidate happened to get the lowest database id -- an accident of
    ingestion order (which source ran first, whether the graph was rebuilt),
    not a rule about which register is authoritative. That is a
    reproducibility hazard across graph builds, and it is liable to land on
    the GLEIF twin: `officer_of` edges attach to the GB-COH node, so
    resolving to the GLEIF twin silently yields a company with no officers,
    a recall failure indistinguishable from a genuine null.

    The GB-COH node for a company number is unique by construction
    (`unique_registry_id` constraint on (`registry_scheme`, `registry_id`),
    and every GB-COH-creating ingest path sets `registry_id` = its own
    `company_number` -- see `ch_appointments._canonical_company_entity` /
    `ch_officers._canonical_company_entity`), so looking it up directly is
    both deterministic and authoritative regardless of ingestion order. Only
    when no GB-COH row exists for this number do we fall back to
    `prefer_companies_house` over whatever `company_number`-tagged rows do
    exist, same disambiguation convention used for name matches below --
    never a second, ad hoc tie-break.

    Intentional, measured behaviour change from the old plain-`.first()`
    version: when an ambiguous `company_number` has NO GB-COH row at all
    (so the fallback's `prefer_companies_house` sees no authoritative
    candidate and stays ambiguous among 2+ rows), this now returns `None`
    where the old code returned whichever row had the lowest pk. Exactly 4
    such groups exist in the graph as measured, all with 0 edges attached
    (so nothing downstream was relying on the old pick), and one of them
    (`04867747`) holds two genuinely different companies sharing a reused
    company number, for which `None` is the more correct answer than an
    arbitrary pick.
    """
    found = Entity.objects.filter(registry_scheme="GB-COH", registry_id=cn).first()
    if found:
        return found
    candidates = prefer_companies_house(
        list(Entity.objects.filter(company_number=cn).order_by("id"))
    )
    return candidates[0] if len(candidates) == 1 else None


def resolve_supplier(name: str, ch_cache: dict, company_number: str | None = None) -> Entity | None:
    """Registry ID first, exact normalised name second. Never a fuzzy guess.

    The cohort CSV carries its own `company_number` for many rows; a registry
    identifier sourced with the row beats anything we could infer from a name,
    so it is tried before the cache and before name matching.
    """
    if company_number and company_number.strip():
        found = _resolve_by_company_number(normalise_company_number(company_number))
        if found:
            return found

    cached = ch_cache.get(name.strip())
    if cached and cached.get("company_number"):
        return _resolve_by_company_number(normalise_company_number(cached["company_number"]))

    target = normalise_name(name)
    if not target:
        return None
    # Ordered and fetched one row past the cap so truncation is detectable.
    # An unordered `[:200]` slice is DB-order-dependent, and if a genuine
    # second exact-name match happens to fall outside the window, the
    # uniqueness guard below sees only one candidate and wrongly passes it --
    # truncation manufacturing apparent uniqueness. A capped window is
    # therefore never trusted to prove uniqueness; we return None instead
    # (same precision-over-recall stance as the guard itself).
    #
    # Known over-conservatism: this guard counts rows matching the 15-char
    # SUBSTRING prefix, but the question it is standing in for is whether an
    # EXACT-name match is unique -- a check measured at one scope (substring
    # window size) and applied to a different one (exact-name uniqueness).
    # That means it can reject a resolution as unprovable even when the
    # exact-name candidate set underneath it is trivially unique. It fails
    # closed (this never produces a wrong link, only a missed one), and it is
    # unreachable in practice today: the worst observed substring window
    # across this cohort is 21 rows against a cap of 200, and 0 of 52 cohort
    # names and 0 of 300 sampled company names come anywhere near the cap.
    # Left as-is deliberately -- precision-over-recall makes the conservative
    # behaviour acceptable here -- but it is a latent recall trap if the
    # company table grows enough that ordinary substrings start landing
    # windows near 200.
    nearby = list(
        Entity.objects.filter(entity_type="company", name__icontains=name.strip()[:15]).order_by(
            "id"
        )[:201]
    )
    if len(nearby) > 200:
        return None
    candidates = prefer_companies_house([e for e in nearby if normalise_name(e.name) == target])
    # Uniqueness guard: 2+ candidates means we cannot say which, so we say none.
    return candidates[0] if len(candidates) == 1 else None


_NOT_A_PERSON = re.compile(
    r"mailbox|cabinet office|buy cell|buy team|not available|direct approach|"
    r"^\s*$|team|unit|department|dhsc|nhs|dit\b",
    re.IGNORECASE,
)
_PERSONISH = re.compile(r"\b(Lord|Baroness|Lady|Sir|Dame|MP|Mr|Mrs|Ms|Dr)\b", re.I)


def _names_a_person(value: str) -> bool:
    """Does this referrer cell name an identifiable individual?

    29 of the 52 cohort rows name a civil servant, and 11 are mailboxes or
    teams. Civil servants file no register of interests, so those rows can
    never be tested against register data no matter how good the pipeline is.
    Counting them in the denominator overstates the test's power -- which is
    what "0 of 52" did.
    """
    value = (value or "").strip()
    if not value or _NOT_A_PERSON.search(value):
        return bool(_PERSONISH.search(value))
    return True


def resolve_referrer(name: str, people_by_surname: dict) -> list[Entity]:
    sn = surname(name)
    return people_by_surname.get(sn, []) if sn else []


def build_adjacency() -> dict[int, list[Edge]]:
    adj: dict[int, list[Edge]] = defaultdict(list)
    for edge in Edge.objects.all().only(
        "id", "edge_type", "source_entity_id", "target_entity_id", "valid_from"
    ):
        adj[edge.source_entity_id].append(edge)
        adj[edge.target_entity_id].append(edge)
    return adj


def other_end(edge: Edge, entity_id: int) -> int:
    return edge.target_entity_id if edge.source_entity_id == entity_id else edge.source_entity_id


def find_paths(
    start_ids: set[int],
    goal_id: int,
    adj: dict[int, list[Edge]],
    max_hops: int,
    cutoff: date = AWARD_CUTOFF,
) -> tuple[list[list[Edge]], list[list[Edge]]]:
    """Return (pre_award_paths, undated_paths) up to `max_hops` edges.

    A path is pre-award only if EVERY edge on it has a `valid_from` strictly
    before the cutoff. A path with any undated edge is returned separately so
    it can be reported honestly rather than counted as a recovery.

    Note on why the split matters more than it looks: only 0.4% of
    `declared_interest` edges carry a `valid_from` at all (the Lords register
    publishes no start dates), against 92.3% of `officer_of`. A pre-award test
    is therefore unsatisfiable through register-of-interests data no matter how
    real the relationship is. Callers measuring *retrieval* rather than
    *temporal admissibility* should pass `cutoff=date.max`.
    """
    pre_award: list[list[Edge]] = []
    undated: list[list[Edge]] = []

    def cost(edge: Edge) -> int:
        """`same_as` is an IDENTITY assertion, not a relationship.

        Stepping from "Lord Agnew of Oulton" (parliament) to "AGNEW, Theodore
        Thomas More, Lord" (Companies House) does not put another person
        between the two ends -- it is the same human recorded twice. Charging
        it a hop would make every cross-register path cost one more than the
        relationship it actually represents, and with a 2-hop budget that
        alone would hide every shared directorship a peer has.
        """
        return 0 if edge.edge_type == "same_as" else 1

    def spent(path: list[Edge]) -> int:
        return sum(cost(e) for e in path)

    def walk(node: int, path: list[Edge], seen: set[int]) -> None:
        if spent(path) >= max_hops:
            return
        for edge in adj.get(node, ()):
            nxt = other_end(edge, node)
            if nxt in seen:
                continue
            new_path = [*path, edge]
            if nxt == goal_id:
                # An identity assertion has no temporal validity to test --
                # it is not a claim about a period. Requiring a valid_from on
                # it would reject every cross-register path outright.
                dates = [e.valid_from for e in new_path if cost(e)]
                if dates and all(d is not None and d < cutoff for d in dates):
                    pre_award.append(new_path)
                else:
                    undated.append(new_path)
                continue
            walk(nxt, new_path, seen | {nxt})

    for start in start_ids:
        walk(start, [], {start})
    return pre_award, undated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--out", default="experiments/phase_c_paths.json")
    args = parser.parse_args()

    with open(VIP_CH_CACHE, encoding="utf-8") as f:
        ch_cache = json.load(f)

    people_by_surname: dict[str, list[Entity]] = defaultdict(list)
    for person in Entity.objects.filter(entity_type="person"):
        sn = surname(person.name)
        if sn:
            people_by_surname[sn].append(person)

    adj = build_adjacency()
    print(f"graph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")
    print(f"people indexed by surname: {len(people_by_surname)} distinct surnames")

    rows, results = [], []
    with open(COHORT_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    counts = defaultdict(int)
    for row in rows:
        supplier_name = (row.get("supplier_name") or "").strip()
        # `source_of_referral` is the person who ORIGINATED the referral;
        # `actual_referrer` is the administrative channel it arrived through.
        # For PPE Medpro these are "Baroness Mone" and "Office of Lord Agnew"
        # respectively. The hypothesis is about the originator's relationship
        # to the supplier, so that column is primary — testing the channel
        # instead measures which private office handled the paperwork.
        referrer_name = (row.get("source_of_referral") or "").strip()
        referrer_field = "source_of_referral"
        if not _names_a_person(referrer_name):
            fallback = (row.get("actual_referrer") or "").strip()
            if _names_a_person(fallback):
                referrer_name, referrer_field = fallback, "actual_referrer"
        counts["total"] += 1

        if not _names_a_person(referrer_name):
            counts["referrer_not_a_person"] += 1

        supplier = resolve_supplier(supplier_name, ch_cache, row.get("company_number"))
        referrers = (
            resolve_referrer(referrer_name, people_by_surname)
            if _names_a_person(referrer_name)
            else []
        )
        if supplier:
            counts["supplier_resolved"] += 1
        if referrers:
            counts["referrer_resolved"] += 1
        if not supplier or not referrers:
            counts["unresolved"] += 1
            results.append(
                {
                    "supplier": supplier_name,
                    "referrer": referrer_name,
                    "referrer_field": referrer_field,
                    "status": "unresolved",
                    "supplier_resolved": bool(supplier),
                    "referrer_candidates": len(referrers),
                }
            )
            continue

        counts["both_resolved"] += 1
        pre_award, undated = find_paths({r.id for r in referrers}, supplier.id, adj, args.max_hops)
        if pre_award:
            counts["path_found"] += 1
            status = "path_found"
        elif undated:
            counts["undated_only"] += 1
            status = "undated_only"
        else:
            counts["no_path"] += 1
            status = "no_path"

        results.append(
            {
                "supplier": supplier_name,
                "supplier_entity": supplier.name,
                "referrer": referrer_name,
                "referrer_candidates": len(referrers),
                "status": status,
                "pre_award_paths": [
                    [f"{e.edge_type}@{e.valid_from}" for e in p] for p in pre_award[:5]
                ],
                "undated_paths": [
                    [f"{e.edge_type}@{e.valid_from}" for e in p] for p in undated[:5]
                ],
            }
        )

    print(f"\n=== PHASE C (paths, max {args.max_hops} hops) ===")
    for key in (
        "total",
        "supplier_resolved",
        "referrer_resolved",
        "both_resolved",
        "unresolved",
        "referrer_not_a_person",
        "path_found",
        "undated_only",
        "no_path",
    ):
        print(f"{key:20s}: {counts[key]}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"counts": dict(counts), "rows": results}, f, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
