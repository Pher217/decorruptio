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

Two match tiers, because peerage names carry no forename:

    tier A  surname + first forename + compatible title   confidence 0.85
    tier B  surname + peerage title, no forename to check confidence 0.60

Tier B is deliberately weak. "Lord Smith" is not a unique person, and a
confidence of 0.6 says so rather than pretending otherwise.

Ambiguity rule: candidates that differ in forename are DIFFERENT PEOPLE and
produce no edge (Agnew has a Lord, a Lady and a Sir -- linking on surname alone
would fuse three people). Candidates that agree on name but differ only by
officer ID are the SAME person duplicated by Companies House, and all of them are
linked -- suppressing those would discard real appointments.
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
CONFIDENCE_TITLE_ONLY = 0.60

_PEERAGE_TITLES = {"lord", "lady", "baroness", "baron"}
_HONORIFICS = {"sir", "dame"}
_ALL_TITLES = _PEERAGE_TITLES | _HONORIFICS
_PREFIXES = {"the", "rt", "hon", "mr", "mrs", "ms", "dr", "prof"}


def parse_parliament_name(name: str) -> dict[str, Any]:
    """Split "Lord Agnew of Oulton" / "Sir Geoffrey Cox" / "Danny Kruger"."""
    # Territorial designation is not part of the family name.
    base = re.split(r"\s+of\s+", name or "", maxsplit=1, flags=re.IGNORECASE)[0]
    tokens = [t for t in re.split(r"[^A-Za-z'\-]+", base) if t]

    title = None
    while tokens and tokens[0].lower() in _ALL_TITLES | _PREFIXES:
        if tokens[0].lower() in _ALL_TITLES:
            title = tokens[0].lower()
        tokens.pop(0)

    if not tokens:
        return {"surname": "", "forename": None, "title": title}
    surname = tokens[-1].lower()
    forename = tokens[0].lower() if len(tokens) > 1 else None
    return {"surname": surname, "forename": forename, "title": title}


def parse_officer_name(name: str) -> dict[str, Any]:
    """Split "COX, Geoffrey Charles, Sir" into surname/forename/title."""
    parts = [p.strip() for p in (name or "").split(",")]
    if not parts or not parts[0]:
        return {"surname": "", "forename": None, "title": None}

    surname = re.sub(r"[^A-Za-z'\-]", "", parts[0]).lower()
    title = None
    forename = None

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

    return {"surname": surname, "forename": forename, "title": title}


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
        "linked_title_only": 0,
        "ambiguous_skipped": 0,
        "no_candidate": 0,
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
    members = Entity.objects.filter(entity_type="person", registry_scheme="UK-PARLIAMENT-MEMBER")

    with transaction.atomic():
        for member in members:
            stats["parliamentarians"] += 1
            parsed = parse_parliament_name(member.name)
            if not parsed["surname"]:
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
                # Peerage name: no forename to check. Only safe when every
                # candidate is the same person duplicated by Companies House.
                distinct = {op["forename"] for _, op in candidates}
                matched = candidates if len(distinct) == 1 else []
                confidence = CONFIDENCE_TITLE_ONLY
                tier = "surname_title_only"

            if not matched:
                stats["ambiguous_skipped"] += 1
                continue

            if parsed["forename"]:
                stats["linked_with_forename"] += 1
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
