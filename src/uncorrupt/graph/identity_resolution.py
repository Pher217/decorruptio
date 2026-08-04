"""Cross-register identity — link a parliamentarian to their Companies House officer record.

The officer-appointment ingest added 382,333 `officer_of` edges. **Zero** of them
attach to a parliamentarian: all 406,720 sit on `GB-COH-OFFICER` nodes, while
Phase C's referrers are `UK-PARLIAMENT-MEMBER` nodes. The two sets are disjoint,
so the same human exists twice, unlinked --

    Lord Agnew of Oulton               UK-PARLIAMENT-MEMBER   21 edges
    AGNEW, Theodore Thomas More, Lord  GB-COH-OFFICER         32 edges

-- and a path from a peer into the officer graph is impossible. This module
builds the missing link.

**It asserts, it does not merge.** Entities stay separate and a `same_as` Edge
carries the claim, with an Attestation recording the evidence and confidence.
That matters for three reasons: the claim is attributable, it is reversible, and
the benchmark can see it is weaker than a registry identifier. Merging would
also be wrong on the facts -- Companies House issues multiple officer IDs to one
person (Lord Feldman has three), so a merge would assert an identity the registry
itself does not.

Name formats differ by register:

    parliament : "Sir Geoffrey Cox" | "Lord Agnew of Oulton" | "Danny Kruger"
    officer    : "COX, Geoffrey Charles, Sir" (SURNAME, Forenames, Title)

Three match tiers, because peerage names carry no forename:

    tier A  surname + first forename + compatible title        confidence 0.85
    tier B  surname + title + matching territorial designation  confidence 0.85
    tier C  surname + peerage title, nothing else to check      confidence 0.60

Tier C is deliberately weak. "Lord Smith" is not a unique person, and a
confidence of 0.6 says so rather than pretending otherwise.

Ambiguity rule: candidates that differ in forename are DIFFERENT PEOPLE and
produce no edge (Agnew has a Lord, a Lady and a Sir -- linking on surname alone
would fuse three people). Candidates that agree on name but differ only by
officer ID are the SAME person duplicated by Companies House -- in the
forename tier (the member's own forename is known) every such duplicate is
still linked, unchanged from the original rule. In the peerage-name tiers
below (no forename to check) that is no longer universally true: a
duplicate officer record is linked only if its OWN title field carries a
confirming territorial designation (see "Adversarial review" below) -- a
second, otherwise-identical CH record for the same real person that
happens to carry no designation of its own (e.g. a bare second "MORGAN,
Sally, Baroness" record beside a confirmed one) gets no edge. This is a
deliberate recall cost, not an oversight: the alternative -- letting a
confirmed sibling's territorial match vouch for an unconfirmed one -- is
the exact adversarial-review bug this tier exists to prevent.

Territorial designation as a tier-B signal (2026-08): a live-graph scan found
CH officer titles occasionally carry the peer's full peerage style, not just
the bare rank -- "The Lord Howard Of Rising", "Lord Sainsbury Of Preston
Candover Kg". House of Lords Standing Orders require the territorial
designation ("of Rising", "of Preston Candover") to be unique among peers of
the same surname and rank -- that is its entire constitutional purpose, so a
confirmed match is at least as strong evidence as a forename match, and it is
available for the peers forename verification cannot reach at all.

The same scan also found the failure this exists to prevent, already live in
the graph: a single real "Evan Mervyn Davies, Lord" officer record carried
`same_as` edges from FIVE different real peers -- Lord Davies of Stamford,
Abersoch, Gower, Brixton, and Oldham -- because tier C's old rule only checked
whether the CANDIDATE officers agreed with each other, never whether more than
one real PARLIAMENT member shared that surname+title. When a (surname, title)
bucket contains more than one distinct parliament member (a "contested"
bucket), tier C's old shortcut is unsafe -- it produces the SAME edge from
every contested member, when at most one can be right. A contested member now
gets an edge only via a confirmed territorial match (tier B); otherwise no
edge at all, matching the module's precision-over-recall stance (a missed
link costs a null; a wrong link puts a real person's name on someone else's
relationship).

A separate, structural bug shared the same root: "The Lord Bishop of
Birmingham" and 21 other sitting Lords Spiritual all parsed to surname
"bishop" (the word "Bishop" is a functional role marker in the ex-officio
seat's title, not a family name) and all matched the one CH officer who is
literally surnamed Bishop ("BISHOP, Michael David, Baron Glendonbrook") --
22 real bishops wrongly asserted as one businessman. `_FUNCTIONAL_TITLE_WORDS`
excludes this pattern (and "Archbishop"/"Speaker", found by the same class
of bug) before it ever reaches surname matching.

Adversarial review (2026-08) found the tier-B fix above recreated the exact
bug it eliminated, at the *higher* 0.85 confidence: territorial confirmation
was checked with `any()` over a whole same-forename group but then applied
to the ENTIRE group, so one officer with a confirmed match dragged in every
same-forename sibling, including ones whose own designation contradicted
the member's, or carried none at all ("HOWARD ... Of Rising" and "HOWARD
... Of Lympne" share the forename "Greville" and were both linking to both
peers). The contested-bucket key was also built from the raw parsed title,
so a Baron/Lord spelling variant reopened the original bug, and it only
counted no-forename members, so a forenamed sibling ("Lord Quentin Davies")
never contested the bucket a peerage-name sibling ("Lord Davies of
Stamford") relied on. Fixes: territorial confirmation is now checked and
applied per INDIVIDUAL officer record; the bucket key is normalised through
the same baron/lord equivalence `_titles_compatible` uses, and every member
with a matching surname+title counts toward it; the uncontested path also
now rejects a contradicting territorial designation, not only the contested
path. A post-pass guardrail additionally enforces, at runtime, that no CH
officer ever receives `same_as` from more than one distinct member --
catching the residual case (two members with a genuinely identical full
name) that no per-tier rule defends against.

Signals investigated and NOT used: date of birth (`ch_officers.py` strips it
at ingest -- `_ALLOWED_OFFICER_FIELDS` never includes `date_of_birth`, and
ADR-004's privacy scope keeps it that way; a signal the pipeline does not
hold is not a signal this module can use). Company co-occurrence (cross-
referencing a peer's declared-interest companies against a candidate
officer's `officer_of` companies) resolved every one of the 14 remaining
undecidable clusters found in the live-graph scan when tested by hand, but is
deliberately NOT implemented here: it is a probabilistic corroboration, not a
structurally-guaranteed-unique identifier like a territorial designation, and
using it safely needs cross-member collision checking (would member X's
overlap also match a sibling contested member?) and protection against
generic/large-company overlaps that this pass has not built or tested to the
same precision bar. Left as a scoped follow-up, not shipped half-vetted into
a precision-critical resolver.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from django.db import transaction

from uncorrupt.graph.models import Attestation, Edge, Entity

logger = logging.getLogger(__name__)

SOURCE_NAME = "Cross-register name match"

CONFIDENCE_WITH_FORENAME = 0.85
CONFIDENCE_TERRITORIAL = 0.85
CONFIDENCE_TITLE_ONLY = 0.60

# Earl/Countess/Viscount/Viscountess/Duke/Duchess/Marquess/Marchioness are
# personal hereditary peerage ranks exactly like Baron/Lord -- omitting them
# left e.g. "Viscount Hailsham" parsing as forename "viscount", title None,
# so the peer could only ever match an untitled officer (the rank confusion
# `_titles_compatible` exists to prevent). Including them here also means a
# bare "The Earl of Snowdon"-style name (title + territorial, no separate
# surname token) correctly yields no surname instead of misreading the rank
# word as one.
_PEERAGE_TITLES = {
    "lord",
    "lady",
    "baroness",
    "baron",
    "earl",
    "countess",
    "viscount",
    "viscountess",
    "duke",
    "duchess",
    "marquess",
    "marchioness",
}
_HONORIFICS = {"sir", "dame"}
_ALL_TITLES = _PEERAGE_TITLES | _HONORIFICS
_PREFIXES = {"the", "rt", "hon", "mr", "mrs", "ms", "dr", "prof"}

_TITLE_EQUIVALENCES = {"baron": "lord", "baroness": "lady"}


def _normalize_title(title: str | None) -> str | None:
    """Collapse baron/baroness into lord/lady so bucket keys built from raw
    parsed titles agree with `_titles_compatible` -- otherwise "Lord Davies
    of Stamford" and "Baron Davies of Abersoch" land in two separately
    "uncontested" buckets that both assert onto the same officer at 0.60."""
    if title is None:
        return None
    return _TITLE_EQUIVALENCES.get(title, title)


# Ex-officio / functional role nouns that are never a family surname, even
# though they are the only token left once title/prefix stripping removes
# everything else: "The Lord Bishop of Birmingham" and "The Lord Archbishop
# of Canterbury" leave "bishop"/"archbishop"; "The Speaker" leaves
# "speaker". The 26 Lords Spiritual sit ex officio by diocese (a rotating
# functional seat, not a personal peerage) and the Speaker is a single
# elected office, not a family name -- treating either as a surname
# collides every holder of the role onto whichever CH officer happens to be
# literally surnamed that word (found live: 22 sitting bishops wrongly
# linked to one businessman, "BISHOP, Michael David, Baron Glendonbrook").
# A literal-word regex matching only "bishop" failed open for every other
# functional title (found real: "The Lord Archbishop of Canterbury/York",
# scripts/run_positive_controls.py:63-67) -- this is a deliberately curated,
# fail-closed set, checked regardless of whether a territorial designation
# is present (a functional role must not fall through to "assert" just
# because it lacks the diocesan "of X" clause bishops happen to carry).
#
# Known gap, not fixed here: the set itself is still a blacklist, not a
# structural test for "is this a functional role, not a surname" -- any
# functional title word not listed here (Dean, Provost, Chancellor, Earl
# Marshal, Lord Mayor, Chief Rabbi, Moderator are all real UK ex-officio /
# functional styles) falls through to ordinary surname matching, fail-open,
# exactly as "bishop" and "archbishop" once did. Zero reachability on the
# current member set (none of those words appear as a bare parsed surname
# today) is why this is noted rather than redesigned here -- a real
# occurrence would reproduce the same class of bug this set was built to
# close.
_FUNCTIONAL_TITLE_WORDS = {"bishop", "archbishop", "speaker"}

# Territorial designation: the "of X" clause in a peerage style ("of
# Oulton", "of Preston Candover"). Extracted separately from the surname/
# title split above because it is the discriminating signal, not noise --
# see module docstring.
_TERRITORIAL_RE = re.compile(r"\bof\s+(.+)$", re.IGNORECASE)


def _extract_territorial(text: str) -> str | None:
    """Pull the normalised territorial designation out of a peerage style.

    Shared by both sides: `parse_parliament_name` sees "Lord Agnew of
    Oulton" directly; `parse_officer_name` sees it embedded in a CH title
    field that sometimes carries the full style ("The Lord Howard Of
    Rising"). Trailing post-nominal letters ("Kg", "Kt") on the CH side are
    not stripped here -- `_territorial_compatible` matches on a prefix
    instead, which handles them without guessing at every possible
    post-nominal abbreviation.
    """
    match = _TERRITORIAL_RE.search(text or "")
    if not match:
        return None
    cleaned = re.sub(r"[^A-Za-z&\s]", "", match.group(1)).strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def _territorial_compatible(
    member_territorial: str | None, officer_territorial: str | None
) -> bool:
    """True unless the two territorial designations provably disagree.

    CH officer titles routinely omit the territorial designation even when
    the real peerage has one -- absence on the officer side is not evidence
    either way, so it passes through. Parliament's own display name is
    treated as complete: if it shows no "of X" the peer genuinely has no
    territorial designation, so an officer record that carries one belongs
    to someone else. A prefix match (rather than exact equality) absorbs
    CH's trailing post-nominal letters ("Preston Candover" vs "Preston
    Candover Kg") without needing to enumerate every post-nominal style.
    """
    if officer_territorial is None:
        return True
    if member_territorial is None:
        return False
    return officer_territorial == member_territorial or officer_territorial.startswith(
        member_territorial + " "
    )


def parse_parliament_name(name: str) -> dict[str, Any]:
    """Split "Lord Agnew of Oulton" / "Sir Geoffrey Cox" / "Danny Kruger"."""
    territorial = _extract_territorial(name)
    # The territorial designation is not part of the family name.
    base = re.split(r"\s+of\s+", name or "", maxsplit=1, flags=re.IGNORECASE)[0]
    tokens = [t for t in re.split(r"[^A-Za-z'\-]+", base) if t]

    title = None
    while tokens and tokens[0].lower() in _ALL_TITLES | _PREFIXES:
        if tokens[0].lower() in _ALL_TITLES:
            title = tokens[0].lower()
        tokens.pop(0)

    if not tokens:
        return {
            "surname": "",
            "forename": None,
            "title": title,
            "territorial": None,
            "functional_title": False,
        }

    if len(tokens) == 1 and tokens[0].lower() in _FUNCTIONAL_TITLE_WORDS:
        # "The Lord Bishop of X" / "The Lord Archbishop of X" / "The
        # Speaker" -- a functional role, not a personal peerage. The word is
        # a role marker here, not a surname.
        return {
            "surname": "",
            "forename": None,
            "title": title,
            "territorial": None,
            "functional_title": True,
        }

    surname = tokens[-1].lower()
    forename = tokens[0].lower() if len(tokens) > 1 else None
    return {
        "surname": surname,
        "forename": forename,
        "title": title,
        "territorial": territorial,
        "functional_title": False,
    }


def parse_officer_name(name: str) -> dict[str, Any]:
    """Split "COX, Geoffrey Charles, Sir" into surname/forename/title.

    CH's title field occasionally carries the peer's whole style rather than
    the bare rank ("The Lord Howard Of Rising", "Lord Sainsbury Of Preston
    Candover Kg") -- when it does, `territorial` picks up the "of X" clause
    from that same trailing segment (see module docstring).
    """
    parts = [p.strip() for p in (name or "").split(",")]
    if not parts or not parts[0]:
        return {"surname": "", "forename": None, "title": None, "territorial": None}

    surname = re.sub(r"[^A-Za-z'\-]", "", parts[0]).lower()
    title = None
    forename = None
    territorial = _extract_territorial(parts[-1]) if len(parts) >= 2 else None

    if len(parts) >= 3 and parts[2].lower() in _ALL_TITLES:
        title = parts[2].lower()
    if len(parts) >= 2 and parts[1]:
        first = [t for t in re.split(r"[^A-Za-z'\-]+", parts[1]) if t]
        # Some records put the title in the forename slot.
        first = [t for t in first if t.lower() not in _ALL_TITLES]
        if first:
            forename = first[0].lower()
        if title is None and len(parts) >= 2:
            trailing = [
                t for t in re.split(r"[^A-Za-z'\-]+", parts[-1]) if t.lower() in _ALL_TITLES
            ]
            if trailing:
                title = trailing[0].lower()

    return {"surname": surname, "forename": forename, "title": title, "territorial": territorial}


def _titles_compatible(parliament_title: str | None, officer_title: str | None) -> bool:
    """A peer must match a peer; an untitled MP must not match a titled officer.

    Baron/Lord and Baroness/Lady are the same rank written differently.
    """
    if parliament_title is None:
        return officer_title is None
    if officer_title is None:
        return False
    return _normalize_title(parliament_title) == _normalize_title(officer_title)


_MEMBER_SCHEME = "UK-PARLIAMENT-MEMBER"

# Refuse to delete more than this fraction of the persisted `same_as` set in
# one run. 83 of 351 (~24%) was the real reconciliation that motivated this
# code; a run proposing almost nothing means an upstream ingest is missing,
# not that the graph is genuinely stale.
MAX_DELETE_FRACTION = 0.50

# Below this many persisted edges the fraction carries no signal (1 of 1 is
# 100%), so the floor does not apply. Well under the live set of 351.
MIN_PERSISTED_FOR_DELETE_FLOOR = 20


def resolve_cross_register_identities(
    dry_run: bool = False, allow_bulk_delete: bool = False
) -> dict[str, Any]:
    """Reconcile the persisted `same_as` edge set to match this run's decisions.

    This resolver is authoritative for `same_as` -- it is the only writer
    of that edge type in the codebase (verified by grep) and the edges are
    derived, regenerable output, not source data (ADR-006: this asserts
    identity, it never merges an Entity). So every run computes the FULL
    target set of `same_as` edges and reconciles the persisted graph to
    match it exactly: edges no longer proposed are deleted, missing ones
    are created. `Edge.objects.get_or_create` alone is additive-only and
    was found live to leave stale wrong edges in place forever (see
    "Phase 2.5" below) -- reporting `collision_dropped: 0` while the
    persisted graph still held real invariant violations.
    """
    stats: dict[str, Any] = {
        "parliamentarians": 0,
        "linked_with_forename": 0,
        "linked_territorial": 0,
        "linked_title_only": 0,
        "ambiguous_skipped": 0,
        "no_candidate": 0,
        "skipped_functional_title": 0,
        "collision_dropped": 0,
        "collision_dropped_partial_members": 0,
        "collision_dropped_partial_records": 0,
        "proposed_edges": 0,
        "edges_created": 0,
        "edges_deleted_stale": 0,
        "attestations_updated": 0,
        "undecidable_members": [],
    }

    officers = list(
        Entity.objects.filter(entity_type="person", registry_scheme="GB-COH-OFFICER").only(
            "id", "name"
        )
    )
    by_surname: dict[str, list[tuple[Entity, dict[str, Any]]]] = {}
    for officer in officers:
        parsed = parse_officer_name(officer.name)
        if parsed["surname"]:
            by_surname.setdefault(parsed["surname"], []).append((officer, parsed))

    observed_at = datetime.now(UTC)
    members = list(Entity.objects.filter(entity_type="person", registry_scheme=_MEMBER_SCHEME))

    # "Lord Smith" is not a unique parliament member either: a (surname,
    # title) bucket containing more than one real peer is "contested" -- the
    # single-candidate-officer shortcut below would hand every contested
    # member the SAME edge, when at most one can be right (found live: one
    # real "Evan Mervyn Davies, Lord" officer with five different "Lord
    # Davies of ..." peers same_as'd onto it). Contested members need a
    # confirmed territorial match; uncontested members keep the original
    # single-candidate rule. The key is normalised through the same
    # baron/lord equivalence `_titles_compatible` uses (otherwise "Lord
    # Davies of Stamford" and "Baron Davies of Abersoch" land in two
    # separately-uncontested buckets that both assert onto the same
    # officer), and EVERY member with this surname+title counts toward the
    # bucket, not just the ones with no forename -- a forenamed member
    # ("Lord Quentin Davies") can claim the same officer a no-forename peer
    # ("Lord Davies of Stamford") would otherwise take via the uncontested
    # shortcut.
    bucket_member_counts: dict[tuple[str, str | None], int] = {}
    for member in members:
        parsed = parse_parliament_name(member.name)
        if parsed["surname"]:
            key = (parsed["surname"], _normalize_title(parsed["title"]))
            bucket_member_counts[key] = bucket_member_counts.get(key, 0) + 1
    contested_buckets = {key for key, count in bucket_member_counts.items() if count > 1}

    # Phase 1: decide a match (or non-match) for every member without
    # writing anything yet -- the officer-ownership guardrail below needs
    # every member's decision in hand before any edge is created.
    decisions: list[tuple[Entity, list[tuple[Entity, dict[str, Any]]], float, str]] = []

    for member in members:
        stats["parliamentarians"] += 1
        parsed = parse_parliament_name(member.name)
        if not parsed["surname"]:
            if parsed["functional_title"]:
                stats["skipped_functional_title"] += 1
            else:
                stats["no_candidate"] += 1
            continue

        candidates = [
            (officer, op)
            for officer, op in by_surname.get(parsed["surname"], [])
            if _titles_compatible(parsed["title"], op["title"])
        ]
        if not candidates:
            stats["no_candidate"] += 1
            continue

        if parsed["forename"]:
            # Known gap, not fixed here: unlike the peerage-name branches
            # below, this tier never checks territorial compatibility --
            # a genuine forename match on a contested surname+title bucket
            # is trusted outright. Reachable under adversarial fuzzing
            # (198 violations) but 0 on the current real member set, and
            # identical to the pre-fix behaviour this module already
            # shipped with -- noted as a scoped follow-up, not redesigned
            # here.
            matched = [(o, op) for o, op in candidates if op["forename"] == parsed["forename"]]
            confidence = CONFIDENCE_WITH_FORENAME
            tier = "surname_forename_title"
        else:
            # Peerage name: no forename to check. Group candidates by
            # forename first -- candidates sharing (surname, title,
            # forename) are the SAME person duplicated by Companies
            # House (module docstring), never different people.
            groups: dict[str | None, list[tuple[Entity, dict[str, Any]]]] = {}
            for officer, op in candidates:
                groups.setdefault(op["forename"], []).append((officer, op))

            bucket_key = (parsed["surname"], _normalize_title(parsed["title"]))
            if bucket_key in contested_buckets:
                # Multiple real parliament members share this surname +
                # title: the single-candidate-officer shortcut cannot tell
                # them apart. Only a positively confirmed territorial
                # designation breaks the tie -- and it must be confirmed on
                # the INDIVIDUAL officer record, not merely somewhere in its
                # forename group, or a group containing one officer with a
                # confirmed match drags in every same-forename sibling,
                # including ones whose own designation contradicts the
                # member's (found live: "HOWARD ... Of Rising" and "HOWARD
                # ... Of Lympne" share the forename "Greville" and would
                # otherwise both link to both peers).
                territorial_groups = [
                    group
                    for group in groups.values()
                    if any(
                        op["territorial"] is not None
                        and _territorial_compatible(parsed["territorial"], op["territorial"])
                        for _, op in group
                    )
                ]
                if parsed["territorial"] and len(territorial_groups) == 1:
                    matched = [
                        (o, op)
                        for o, op in territorial_groups[0]
                        if op["territorial"] is not None
                        and _territorial_compatible(parsed["territorial"], op["territorial"])
                    ]
                    confidence = CONFIDENCE_TERRITORIAL
                    tier = "surname_title_territorial"
                else:
                    matched = []
                    confidence = CONFIDENCE_TITLE_ONLY
                    tier = "surname_title_only"
            else:
                # Not contested: only one real parliament member could
                # possibly be this surname+title, so the single-candidate
                # rule is safe from a cross-member collision -- but an
                # officer record whose OWN territorial designation
                # contradicts this member's (or is present when the member
                # has none) still belongs to someone else, and that check
                # must not be skipped just because the bucket happens to be
                # uncontested (found live: sole member "Lord Howard of
                # Rising" against an officer titled "...Of Lympne").
                if len(groups) == 1:
                    sole_group = next(iter(groups.values()))
                    matched = [
                        (o, op)
                        for o, op in sole_group
                        if _territorial_compatible(parsed["territorial"], op["territorial"])
                    ]
                else:
                    matched = []
                confidence = CONFIDENCE_TITLE_ONLY
                tier = "surname_title_only"

        if not matched:
            stats["ambiguous_skipped"] += 1
            stats["undecidable_members"].append(
                {"registry_id": member.registry_id, "name": member.name, "reason": "ambiguous"}
            )
            continue

        decisions.append((member, matched, confidence, tier))

    # Phase 2: guardrail -- no CH officer may end up with `same_as` claims
    # from more than one distinct parliament member. If it would, drop
    # every claim on that officer rather than guess which member is right.
    # This is the invariant whose absence let one real officer collect
    # `same_as` edges from five different peers before this module existed
    # -- enforced here as a runtime post-pass (not merely scattered
    # per-tier logic) because a genuine full-name collision bypasses the
    # per-tier contested-bucket and territorial defenses entirely.
    officer_claimants: dict[int, set[int]] = {}
    for member, matched, _confidence, _tier in decisions:
        for officer, _op in matched:
            officer_claimants.setdefault(officer.id, set()).add(member.id)
    colliding_officer_ids = {
        officer_id for officer_id, claimants in officer_claimants.items() if len(claimants) > 1
    }

    final_decisions: list[tuple[Entity, list[tuple[Entity, dict[str, Any]]], float, str]] = []
    for member, matched, confidence, tier in decisions:
        surviving = [(o, op) for o, op in matched if o.id not in colliding_officer_ids]
        if not surviving:
            stats["ambiguous_skipped"] += 1
            stats["collision_dropped"] += 1
            stats["undecidable_members"].append(
                {
                    "registry_id": member.registry_id,
                    "name": member.name,
                    "reason": "officer_collision",
                }
            )
            continue
        if len(surviving) < len(matched):
            # A PARTIAL drop: the member keeps at least one officer record
            # (so it never enters undecidable_members, which only tracks
            # members who end up with nothing) but loses one or more CH
            # duplicate records to the guardrail. Previously unrecorded
            # anywhere -- the exact "computed but not reported" defect
            # class the Phase 2.5 reconciliation below closes for the
            # persisted graph; recorded here so it is not silently true of
            # this run's own decisions too.
            stats["collision_dropped_partial_members"] += 1
            stats["collision_dropped_partial_records"] += len(matched) - len(surviving)
        final_decisions.append((member, surviving, confidence, tier))

    # Phase 2.5: reconcile against the persisted graph. `same_as` edges are
    # produced exclusively by this resolver (module docstring) and are
    # derived, regenerable resolver output, not source data -- so this
    # resolver is authoritative for the FULL persisted `same_as` edge set
    # on every run, not merely additive to it. Without this, a stale edge
    # from an earlier (buggy) run of this module survives forever --
    # `Edge.objects.get_or_create` below only ever adds, it has no path
    # that removes an edge the current logic no longer proposes (found
    # live: 83 persisted edges the fixed logic no longer proposes,
    # including the 22-Lords-Spiritual-onto-one-businessman and
    # 5-peers-onto-one-officer cases this module exists to prevent, both
    # silently left in place by an additive-only run that reports
    # collision_dropped: 0).
    #
    # Seeding the officer-ownership guardrail above (Phase 2) from
    # persisted edges INSTEAD of doing this would be a worse bug, not a
    # fix: it would make the guardrail refuse to add a correct new edge
    # because the officer already holds a stale wrong one, leaving the
    # wrong edge in place AND blocking the right one.
    desired_pairs: set[tuple[int, int]] = set()
    for member, matched, _confidence, _tier in final_decisions:
        for officer, _op in matched:
            desired_pairs.add((member.id, officer.id))
    stats["proposed_edges"] = len(desired_pairs)

    # `desired_pairs` is about to become the ENTIRE persisted same_as edge
    # set this resolver owns -- it must be collision-free. It already
    # passed the intra-run guardrail above by construction (every officer
    # in colliding_officer_ids was subtracted out of every `matched` list),
    # so this can never fire in practice; it is a hard runtime check, not
    # a silent trust, because a violation here would defeat the
    # guardrail's entire purpose at the moment its output is written.
    officer_to_members: dict[int, set[int]] = {}
    for member_id, officer_id in desired_pairs:
        officer_to_members.setdefault(officer_id, set()).add(member_id)
    resulting_violations = {
        officer_id: members
        for officer_id, members in officer_to_members.items()
        if len(members) > 1
    }
    if resulting_violations:
        raise RuntimeError(
            "identity_resolution: officer-ownership guardrail invariant "
            f"violated after reconciliation for officer id(s) "
            f"{sorted(resulting_violations)} -- refusing to write or delete "
            "same_as edges"
        )

    # Phase 3: write.
    with transaction.atomic():
        persisted = list(
            Edge.objects.filter(edge_type="same_as").values_list(
                "id", "source_entity_id", "target_entity_id"
            )
        )
        persisted_pairs_to_edge_id = {(m, o): eid for eid, m, o in persisted}

        # This resolver is the sole writer of these attestations (module
        # docstring), and there is only ever one per edge: the edge's own
        # identity IS the (member, officer) pair `source_reference` is
        # derived from, so it cannot change across runs for a surviving
        # edge. `edge_id`, scoped to this resolver's `source_name`, is
        # therefore a safe, simpler stand-in for the full (edge, source_name,
        # source_reference) uniqueness key. Fetched once here (not per-pair
        # inside the loop below) so both the dry-run report and the real
        # write can look up "what's already there" without N+1 queries.
        persisted_attestations = {
            att.edge_id: att
            for att in Attestation.objects.filter(
                edge__edge_type="same_as", source_name=SOURCE_NAME
            )
        }

        stale_edge_ids = [
            edge_id
            for edge_id, member_id, officer_id in persisted
            if (member_id, officer_id) not in desired_pairs
        ]
        stats["edges_deleted_stale"] = len(stale_edge_ids)

        # Delete floor. This function was additive-only until reconciliation
        # was added; it can now destroy the whole persisted set. If an
        # upstream ingest has not run, or officer/member `registry_scheme`
        # values have drifted, resolution proposes nothing and every
        # persisted edge looks stale -- a silent wipe. Deleting most of the
        # set is never a routine outcome, so refuse and make the operator
        # opt in explicitly rather than discovering it afterwards.
        # The fraction is only meaningful once the persisted set is big enough
        # for it to mean anything: deleting 1 of 1 is 100% and says nothing.
        # Small sets are normal in tests and in a freshly-seeded graph, so the
        # floor applies only above a size where a majority-delete is genuinely
        # anomalous. Live is 351.
        if len(persisted) >= MIN_PERSISTED_FOR_DELETE_FLOOR and not allow_bulk_delete:
            delete_fraction = len(stale_edge_ids) / len(persisted)
            if delete_fraction > MAX_DELETE_FRACTION:
                raise RuntimeError(
                    "identity_resolution: refusing to delete "
                    f"{len(stale_edge_ids)} of {len(persisted)} persisted "
                    f"same_as edges ({delete_fraction:.1%} > "
                    f"{MAX_DELETE_FRACTION:.0%} floor). This usually means an "
                    "upstream ingest did not run or registry_scheme values "
                    "drifted, not that the edges are genuinely stale. Re-run "
                    "with dry_run=True to inspect, then pass "
                    "allow_bulk_delete=True if the deletion is intended."
                )

        if stale_edge_ids and not dry_run:
            # Deleting the Edge cascades to its Attestation rows
            # (Attestation.edge is on_delete=CASCADE) -- an Attestation
            # with no Edge to attest is meaningless, never orphaned data
            # worth keeping, so this is the intended, documented cascade,
            # not an incidental side effect.
            #
            # Attestation.derived_from is ALSO a self-FK with CASCADE, so an
            # attestation on a surviving edge that derives from one of these
            # would be destroyed too. Nothing in production sets
            # `derived_from` (live non-null count is 0; only test fixtures
            # populate it), so this is unreachable today -- recorded because
            # it stops being unreachable the moment provenance chaining ships.
            Edge.objects.filter(
                id__in=stale_edge_ids,
                # Scope the delete to the edges this resolver owns rather than
                # relying on `same_as` having exactly one writer. That is true
                # today (verified by grep) but is an unenforced invariant, and
                # a company<->company same_as written by a future connector
                # would otherwise be deleted here as "stale".
                source_entity__registry_scheme=_MEMBER_SCHEME,
            ).delete()

        for member, matched, confidence, tier in final_decisions:
            if tier == "surname_forename_title":
                stats["linked_with_forename"] += 1
            elif tier == "surname_title_territorial":
                stats["linked_territorial"] += 1
            else:
                stats["linked_title_only"] += 1

            if dry_run:
                # Report what a real run WOULD create or correct, rather
                # than 0. A dry run that tells you what it deletes and
                # creates but not what confidence it would silently leave
                # stale invites publishing a corrected 0.85 as the old 0.60
                # (or vice versa) unreviewed -- the whole point of the dry
                # run is to see the full effect before committing.
                for officer, _op in matched:
                    edge_id = persisted_pairs_to_edge_id.get((member.id, officer.id))
                    if edge_id is None:
                        stats["edges_created"] += 1
                        continue
                    existing = persisted_attestations.get(edge_id)
                    # `existing is None` here means the edge is already
                    # persisted but carries no attestation from this
                    # resolver's source (live count: 0) -- the real-write
                    # branch below would silently add one via
                    # `Attestation.objects.create(...)` with no trace in
                    # `stats` at all. Counting it here keeps the dry run's
                    # preview honest about everything a real run would
                    # write, not just corrections to an attestation that
                    # already existed.
                    if existing is None or (
                        existing.match_confidence != confidence or existing.match_method != tier
                    ):
                        stats["attestations_updated"] += 1
                continue

            for officer, _op in matched:
                # `get_or_create`'s `defaults` are silently discarded once a
                # row exists -- the same defect class fixed below for the
                # Attestation confidence (see its comment): `edge.properties`
                # is derived fresh from `tier`/`member.name`/`officer.name`
                # on every run, but only ever WRITTEN on the run that first
                # created the edge. Verified live: after an attestation is
                # corrected from surname_title_only to
                # surname_title_territorial, `edge.properties["match_tier"]`
                # still reads the superseded `surname_title_only` --
                # `compute_graph_hash` covers only `(edge_type, source,
                # target, valid_from)`, never `properties`, so repairing this
                # does not move the graph hash. Nothing publishes
                # `properties` today (the MCP tool layer never serialises
                # it), so this is not a currently-published claim, but it is
                # the surface an auditor would check the published
                # confidence against, so it must not silently disagree with
                # the attestation that actually backs it.
                desired_properties = {
                    "match_tier": tier,
                    "parliament_name": member.name,
                    "officer_name": officer.name,
                }
                edge, created = Edge.objects.get_or_create(
                    edge_type="same_as",
                    source_entity=member,
                    target_entity=officer,
                    valid_from=None,
                    valid_to=None,
                    defaults={"properties": desired_properties},
                )
                if created:
                    stats["edges_created"] += 1
                elif edge.properties != desired_properties:
                    edge.properties = desired_properties
                    edge.save(update_fields=["properties"])

                # `get_or_create`'s `defaults` are silently discarded once a
                # row exists -- the exact bug class Phase 2.5 fixed for the
                # Edge itself (see module docstring): a match tier that
                # changed between runs (e.g. the 2026-08 territorial-tier
                # fix) left the PUBLISHED confidence frozen at whatever the
                # very first run computed, forever (measured live: 3
                # attestations stuck at the superseded surname_title_only /
                # 0.60 tier). Bring the persisted confidence/method to match
                # this run's decision -- in either direction, a corrected
                # upgrade or a corrected downgrade -- but only when it
                # actually changed.
                #
                # `observed_at` is deliberately left UNTOUCHED by this
                # correction, even though it is transaction time -- when the
                # source recorded the claim (Attestation docstring). This
                # resolver's own attestations are read by
                # `uncorrupt.gates.binding.compute_attestation_inclusive_hash`,
                # which hashes every attestation on `(edge_id, source_name,
                # source_reference, observed_at, snapshot_ref)` -- fields
                # that identify WHICH evidence exists -- and its own
                # docstring deliberately EXCLUDES `match_confidence` /
                # `match_method` from that hash as "resolution-quality
                # metadata, not new evidence". Bumping `observed_at` on a
                # confidence-only correction would route exactly that
                # excluded class of change into a hash meant to be blind to
                # it, moving a sealed gate certificate's binding hash for a
                # run that changed no evidence -- unbinding
                # `GateFreezeState.matches_recorded()` from a certificate it
                # should still match. It also buys nothing: no other reader
                # depends on `observed_at` for a resolver-written `same_as`
                # attestation -- `edge_evidence_level` and
                # `snapshot_evidence_pages` (register_snapshots.py) both
                # require `snapshot_ref` to be set, which these attestations
                # never are, and `path_evidence_level` excludes `same_as`
                # edges from temporal evidence entirely.
                #
                # Updating in place (rather than writing a second, distinct
                # observation) is deliberate: this is not new real-world
                # evidence arriving at a new date -- like a fresh Wayback
                # register snapshot would be (see register_snapshots.py) --
                # it is the same fixed input names re-scored by improved
                # logic, so there is nothing bitemporal to preserve by
                # keeping the superseded row around. What IS lost is the
                # record that a stale confidence was PUBLISHED about a named
                # person until this run corrected it -- `created_at`
                # (`auto_now_add`, untouched here) still preserves
                # first-appearance, but recovering "what did we publish, and
                # until when" needs an append-only correction log, not a
                # second attestation row on this edge -- out of scope for
                # this fix.
                existing_attestation = persisted_attestations.get(edge.id)
                if existing_attestation is None:
                    Attestation.objects.create(
                        edge=edge,
                        source_name=SOURCE_NAME,
                        source_reference=f"{member.registry_id}:{officer.registry_id}",
                        match_confidence=confidence,
                        match_method=tier,
                        observed_at=observed_at,
                    )
                elif (
                    existing_attestation.match_confidence != confidence
                    or existing_attestation.match_method != tier
                ):
                    existing_attestation.match_confidence = confidence
                    existing_attestation.match_method = tier
                    existing_attestation.save(update_fields=["match_confidence", "match_method"])
                    stats["attestations_updated"] += 1

    logger.info(
        "resolve_cross_register_identities%s: %d parliamentarians, %d "
        "proposed same_as edges (%d created, %d deleted as stale), %d "
        "undecidable (%d dropped by the officer-collision guardrail, %d "
        "more partially dropped across %d members)",
        " (dry run)" if dry_run else "",
        stats["parliamentarians"],
        stats["proposed_edges"],
        stats["edges_created"],
        stats["edges_deleted_stale"],
        len(stats["undecidable_members"]),
        stats["collision_dropped"],
        stats["collision_dropped_partial_records"],
        stats["collision_dropped_partial_members"],
    )
    return stats
