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
officer ID are the SAME person duplicated by Companies House, and all of them are
linked -- suppressing those would discard real appointments.

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
22 real bishops wrongly asserted as one businessman. `_LORDS_SPIRITUAL_RE`
excludes this pattern before it ever reaches surname matching.

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

_PEERAGE_TITLES = {"lord", "lady", "baroness", "baron"}
_HONORIFICS = {"sir", "dame"}
_ALL_TITLES = _PEERAGE_TITLES | _HONORIFICS
_PREFIXES = {"the", "rt", "hon", "mr", "mrs", "ms", "dr", "prof"}

# "The Lord Bishop of Birmingham" etc. -- the 26 Lords Spiritual sit ex
# officio by diocese, a functional/rotating seat, not a personal peerage.
# "Bishop" here is a role marker, not a surname: treating it as one collides
# every sitting bishop onto any CH officer who happens to be literally
# surnamed Bishop (found live in the graph -- 22 wrong `same_as` edges onto
# one businessman, "BISHOP, Michael David, Baron Glendonbrook").
_LORDS_SPIRITUAL_RE = re.compile(r"^bishop$")

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

    if len(tokens) == 1 and _LORDS_SPIRITUAL_RE.match(tokens[0].lower()) and territorial:
        # "The Lord Bishop of X" -- an ex-officio Lords Spiritual seat, not a
        # personal peerage. "Bishop" is a role marker here, not a surname.
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
    equivalences = {"baron": "lord", "baroness": "lady"}
    return equivalences.get(parliament_title, parliament_title) == equivalences.get(
        officer_title, officer_title
    )


def resolve_cross_register_identities(dry_run: bool = False) -> dict[str, int]:
    """Create `same_as` edges from parliament entities to CH officer entities."""
    stats = {
        "parliamentarians": 0,
        "linked_with_forename": 0,
        "linked_territorial": 0,
        "linked_title_only": 0,
        "ambiguous_skipped": 0,
        "no_candidate": 0,
        "skipped_functional_title": 0,
        "edges_created": 0,
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
    members = list(
        Entity.objects.filter(entity_type="person", registry_scheme="UK-PARLIAMENT-MEMBER")
    )

    # "Lord Smith" is not a unique parliament member either: a (surname,
    # title) bucket containing more than one real peer is "contested" -- the
    # old single-candidate-officer shortcut below would hand every contested
    # member the SAME edge, when at most one can be right (found live: one
    # real "Evan Mervyn Davies, Lord" officer with five different "Lord
    # Davies of ..." peers same_as'd onto it). Contested members need a
    # confirmed territorial match; uncontested members keep the original
    # single-candidate rule.
    bucket_member_counts: dict[tuple[str, str | None], int] = {}
    for member in members:
        parsed = parse_parliament_name(member.name)
        if parsed["surname"] and not parsed["forename"]:
            key = (parsed["surname"], parsed["title"])
            bucket_member_counts[key] = bucket_member_counts.get(key, 0) + 1
    contested_buckets = {key for key, count in bucket_member_counts.items() if count > 1}

    with transaction.atomic():
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

                bucket_key = (parsed["surname"], parsed["title"])
                if bucket_key in contested_buckets:
                    # Multiple real parliament members share this surname +
                    # title: the single-candidate-officer shortcut cannot
                    # tell them apart, and a bare/no-territorial officer
                    # candidate cannot be safely attributed to any one of
                    # them. Only a positively confirmed territorial
                    # designation breaks the tie.
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
                        matched = territorial_groups[0]
                        confidence = CONFIDENCE_TERRITORIAL
                        tier = "surname_title_territorial"
                    else:
                        matched = []
                        confidence = CONFIDENCE_TITLE_ONLY
                        tier = "surname_title_only"
                else:
                    # Not contested: only one real parliament member could
                    # possibly be this surname+title, so the original
                    # single-distinct-officer rule is safe.
                    matched = candidates if len(groups) == 1 else []
                    confidence = CONFIDENCE_TITLE_ONLY
                    tier = "surname_title_only"

            if not matched:
                stats["ambiguous_skipped"] += 1
                continue

            if tier == "surname_forename_title":
                stats["linked_with_forename"] += 1
            elif tier == "surname_title_territorial":
                stats["linked_territorial"] += 1
            else:
                stats["linked_title_only"] += 1

            if dry_run:
                continue

            for officer, _op in matched:
                edge, created = Edge.objects.get_or_create(
                    edge_type="same_as",
                    source_entity=member,
                    target_entity=officer,
                    valid_from=None,
                    valid_to=None,
                    defaults={
                        "properties": {
                            "match_tier": tier,
                            "parliament_name": member.name,
                            "officer_name": officer.name,
                        }
                    },
                )
                if created:
                    stats["edges_created"] += 1
                Attestation.objects.get_or_create(
                    edge=edge,
                    source_name=SOURCE_NAME,
                    source_reference=f"{member.registry_id}:{officer.registry_id}",
                    defaults={
                        "match_confidence": confidence,
                        "match_method": tier,
                        "observed_at": observed_at,
                    },
                )

    return stats
