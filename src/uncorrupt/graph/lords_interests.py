"""UK House of Lords Register of Interests ingest (Phase 1.5).

Source: https://members.parliament.uk/members/lords/interests/register-of-lords-interests
— an HTML page (server-rendered, no API). 791 entries across 40 pages (~20
per page). Each Lord has a "card" with member ID, name, party, peerage type,
then categories (1–10) with free-text interest descriptions.

Historical data: the Wayback Machine archives this page from June 2020
onward. ``fetch_lords_register`` accepts an optional ``wayback_timestamp``
(e.g. "20201130") to retrieve a point-in-time snapshot. The snapshot date
becomes ``Attestation.observed_at`` — the register entries themselves carry
no individual dates (it is a current-state register, not a temporal log).

Scope boundary (ADR-004 D1): public-function officials only. The Lords
register concerns the member's own interests (employment, directorships,
shareholdings, property, gifts, visits) — not relatives'. Category 6
(Sponsorship) entries name the organisation providing support; Category 8
(Gifts) names the donor. Individual donors are never turned into Entities
(mirrors ``ec_donations`` and ``parliament_interests``).

Dates: the Lords register has NO per-entry registration or end dates. The
only temporal signal is the Wayback snapshot date, which is set as
``Attestation.observed_at`` (transaction time — when the source recorded
the interest). ``Edge.valid_from`` is left null because we cannot know when
the interest was actually registered — only that it was registered *by*
the snapshot date. "(interest ceased DD Month YYYY)" text in entries is
parsed into ``Edge.valid_to`` where present, but the absence of a
registration date means ``valid_from`` stays null (the bitemporal gap is
explicit, not hidden).

Counterparty resolution mirrors the Parliament interests ingest: a company
number (rare in Lords entries) resolves at confidence 1.0; a unique exact
name match to ``staging.Company`` resolves at 0.9; 2+ candidates is never
guessed; a named counterparty that doesn't resolve is scoped to the
interest (``UK-LORDS-UNRESOLVED``, confidence 0.5, "name_only") —
duplication over merging.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag
from django.db import transaction

from uncorrupt.core.provenance import ProvenanceRecord
from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.register.loader import load_source
from uncorrupt.staging.companies_house import _normalise_name, normalise_company_number
from uncorrupt.staging.models import Company

logger = logging.getLogger(__name__)

LORDS_REGISTER_URL = (
    "https://members.parliament.uk/members/lords/interests/register-of-lords-interests"
)
WAYBACK_PREFIX = "https://web.archive.org/web/"
SOURCE_NAME = "UK House of Lords Register of Interests"
# sources/uk_lords_interests.yml — connector refuses to run without it (ADR-001 D5)
SOURCE_ID = "uk_lords_interests"
CONNECTOR_VERSION = "0.1"

# Longest plausible organisation name. Anything beyond this is a parse
# artefact from free-text register entries, not a real counterparty.
_MAX_COUNTERPARTY_NAME = 200

# A comma immediately followed by nothing but a bare legal suffix is part of
# the organisation's own name (US-convention "X, Inc."), not a role
# separator — splitting there produced counterparties named literally "Inc."
# Whole-string match only: "X, Inc. of Delaware" still splits normally.
_BARE_SUFFIX_RE = re.compile(
    r"^(?:Inc|Ltd|Limited|LLC|LLP|Corp|Corporation|plc|PLC|SA|AG|NV|BV|KG|SE|"
    r"ASA|SRL|SpA|GmbH|Co|L\.?P\.?|Pty)\.?$",
    re.IGNORECASE,
)

# Legal-form suffixes (UK + common overseas jurisdictions) and institutional
# nouns that mark free text as an organisation rather than a person or a
# bare place/property description. Deliberately excludes generic words that
# could plausibly appear in a person's own name.
_ORG_MARKERS_RE = re.compile(
    r"\b(Ltd|Limited|LLP|plc|PLC|CIC|Foundation|Trust|Society|Board|"
    r"Authority|Group|Association|Charity|University|College|School|"
    r"Partnership|Holdings|Capital|Fund|Enterprise|Enterprises|"
    r"Council|Committee|Commission|Institute|Institution|Agency|Bureau|"
    r"Church|Embassy|Ministry|Chambers|Programme|Federation|Alliance|"
    r"Network|Forum|Confederation|Systems|Corporation|Corp|Inc|LLC|LP|"
    r"GmbH|AG|SA|SE|NV|BV|ASA|SRL|SpA|Oy|AB|KG|Ltda|Limitada|"
    r"Company|Companies)\b",
    re.IGNORECASE,
)

# Signals that the candidate text is prose (a sentence fragment or a
# redaction notice), not an organisation name — e.g. "The member is a
# shareholder ... full details are held by the Registrar of Lords'
# Interests". Category-2 entries carrying one of these are genuinely
# unnamed, not a pattern miss.
_PROSE_MARKERS = (
    " is ",
    " are ",
    " was ",
    " were ",
    " has ",
    " have ",
    "full details",
    "the member",
    "The member",
    "registrar",
    "Registrar",
    " no shares",
    " which ",
    " who ",
    " whom ",
    ";",
    ":",
)
_NAME_CONNECTORS = {"the", "a", "an", "and", "of", "for", "in", "at", "&", "to", "on", "with"}

# Categories that name an individual (relative) rather than the member's
# own public-function interest — excluded per ADR-004 D1.
_FAMILY_MARKERS = frozenset({"family"})

# "(interest ceased 3 March 2026)" → valid_to
_CEASED_RE = re.compile(r"interest ceased (\d{1,2}\s+\w+\s+\d{4})", re.IGNORECASE)

_MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


@dataclass(frozen=True)
class LordsFetchResult:
    """Provenance record for a downloaded Lords register HTML dump."""

    html_path: Path
    provenance_path: Path
    page_count: int
    total_entries: int
    source_url: str
    retrieved_at: datetime
    content_hash: str
    wayback_timestamp: str | None


def _clean_wayback_url(url: str) -> str:
    """Remove Wayback Machine rewriting from a URL."""
    return re.sub(r"^https://web\.archive\.org/web/\d+/(?:im_|cs_)?(?:https?://)?", "", url)


# Wayback timestamps are a date-time prefix of up to 14 digits
# (YYYYMMDDhhmmss); callers may pass a shorter prefix (e.g. "20201130") and
# let Wayback resolve to the nearest capture. Longest-first so a full
# timestamp isn't mistaken for a shorter one.
_WAYBACK_TS_FORMATS = (
    (14, "%Y%m%d%H%M%S"),
    (12, "%Y%m%d%H%M"),
    (10, "%Y%m%d%H"),
    (8, "%Y%m%d"),
    (6, "%Y%m"),
    (4, "%Y"),
)


def _parse_wayback_timestamp(wayback_timestamp: str) -> datetime | None:
    """Parse a Wayback Machine timestamp into the capture's UTC instant.

    Returns None if the string doesn't match any recognised Wayback
    timestamp length/format — callers should fall back to another signal
    rather than raise, since a malformed timestamp shouldn't crash a run.
    """
    for length, fmt in _WAYBACK_TS_FORMATS:
        if len(wayback_timestamp) == length:
            try:
                return datetime.strptime(wayback_timestamp, fmt).replace(tzinfo=UTC)
            except ValueError:
                return None
    return None


def _parse_ceased_date(text: str) -> str | None:
    """Extract '(interest ceased DD Month YYYY)' from text, return ISO date."""
    m = _CEASED_RE.search(text)
    if not m:
        return None
    date_str = m.group(1)
    parts = date_str.split()
    if len(parts) != 3:
        return None
    day, month_name, year = parts
    month = _MONTH_NAMES.get(month_name.lower())
    if month is None:
        return None
    try:
        return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    except ValueError:
        return None


def _parse_member_id(card: Tag) -> str | None:
    """Extract member ID from a card's href."""
    href = card.get("href", "")
    if not isinstance(href, str):
        return None
    m = re.search(r"member/(\d+)", href)
    return m.group(1) if m else None


def _parse_member_info(card: Tag) -> dict[str, str]:
    """Extract name, party, peer_type from a member card."""
    primary = card.find(class_="primary-info")
    name = primary.get_text(strip=True) if primary else ""
    secondary = card.find_all(class_="secondary-info")
    party = secondary[0].get_text(strip=True) if secondary else ""
    peer_type = secondary[1].get_text(strip=True) if len(secondary) > 1 else ""
    return {"name": name, "party": party, "peer_type": peer_type}


def _parse_interests(container: Tag) -> list[dict[str, Any]]:
    """Parse interest categories and entries from a member's container.

    The container is a ``card-expandable`` div that wraps both the member
    card (<a>) and the ``expand-area`` with interest entries.

    Returns a list of {category, description, ceased_date} dicts.
    """
    interests: list[dict[str, Any]] = []

    # Look for expand-area within the container
    expand_area = container.find(class_="expand-area")
    if not expand_area:
        # Maybe the container IS the expand area
        expand_area = container

    children = expand_area.find_all(class_="card card-child")
    if not children:
        return []

    for child in children:
        # Category header — may be in primary-info or as bare text
        cat_el = child.find(class_="primary-info")
        if cat_el:
            category = cat_el.get_text(strip=True)
        else:
            cat_text = child.find(string=re.compile(r"Category\s+\d+"))
            category = cat_text.strip() if cat_text else "Unknown"

        if not category or category == "Unknown":
            # Check for nil return
            nil = child.find(string=re.compile("No registrable interests"))
            if nil:
                return []
            continue

        # Interest entries
        for li in child.find_all("li"):
            text = li.get_text(strip=True)
            if not text:
                continue
            if "No registrable interests" in text:
                return []
            ceased_date = _parse_ceased_date(text)
            # Remove the "(interest ceased ...)" from the description
            clean_text = _CEASED_RE.sub("", text).strip()
            # Remove trailing punctuation
            clean_text = re.sub(r"\s*\(\s*\)\s*$", "", clean_text).strip()
            interests.append(
                {
                    "category": category,
                    "description": clean_text,
                    "ceased_date": ceased_date,
                }
            )

    return interests


def _strip_trailing_parens(text: str) -> str:
    """Strip every consecutive trailing "(...)" annotation, not just the last one.

    "SATMAP Inc (trading as Afiniti) (communications technology)" carries two
    trailing parentheticals; stripping only the last one left "(trading as
    Afiniti)" glued onto the name.

    Depth-counted rather than a `\\([^)]*\\)` regex: register text nests
    parens ("...see category 2(a))"), and a regex that stops at the first
    ")" fails to match the true trailing group at all on nested text —
    leaving it un-stripped and full of sentence punctuation.
    """
    text = text.rstrip()
    while text.endswith(")"):
        depth = 0
        i = len(text) - 1
        while i >= 0:
            if text[i] == ")":
                depth += 1
            elif text[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        if i < 0 or depth != 0:
            break
        text = text[:i].rstrip()
    return text


def _looks_like_entity_name(text: str) -> bool:
    """Plausibility gate for a candidate with no explicit legal-form marker.

    Used only where the surrounding structure already establishes that a
    named entity (not a person) is expected — a Category 2 shareholding, or
    a description that independently mentions "company"/"corporation"/
    "firm". Requires a short, capitalised, non-prose token sequence: real
    organisation names in this register are short and Title Case even
    without a legal suffix ("Halma", "The Terrapin Group", "Barclays");
    prose fragments ("the member is a shareholder with...") and dates
    ("18 December 2025") are rejected so this never promotes a sentence
    fragment or a redaction notice into a fabricated counterparty.
    """
    text = text.strip()
    if not text or len(text) > 100:
        return False
    if any(marker in text for marker in _PROSE_MARKERS):
        return False
    if not text[0].isalnum():
        return False
    words = text.split()
    if not words or len(words) > 9:
        return False
    if words[0][0].islower():
        return False
    if any(w.strip(",.-()").lower() in _MONTH_NAMES for w in words):
        return False
    significant = [
        w for w in words if w.lower().strip(",.-'()") not in _NAME_CONNECTORS and w[:1].isalpha()
    ]
    if not significant:
        return False
    upper_count = sum(1 for w in significant if w[0].isupper())
    return (upper_count / len(significant)) >= 0.6


def _extract_counterparty(
    description: str, category: str | None = None
) -> tuple[str | None, str | None, bool]:
    """Extract counterparty name and company number from an interest description.

    Lords register entries are free-text like:
        "Chairman, Microlink PC (UK) Ltd (computing and software)"
        "Director, Leadership in Mind Ltd (business activities)"

    ``category`` (e.g. "Category 2: Shareholdings etc. (b)") is optional
    context, not a requirement — it only widens the plausibility fallback
    below for the one category where a named-but-unmarked entity is
    virtually certain (see the Category 2 branch).

    Two defects in the previous, marker-anywhere-in-description approach:
    trailing parentheticals routinely contain their OWN commas ("Unilever
    plc (nutrition, hygiene and personal care products)"), so splitting on
    the first comma in the raw description split mid-parenthetical and
    silently corrupted the role/organisation boundary — for a family of
    entries this collapsed several distinct companies onto one fabricated
    placeholder name built from the shared tail of their description (e.g.
    five different "Dawn ... Holdings Ltd" companies all resolving to a
    counterparty literally named "printing)"). Parentheses are now stripped
    FIRST, and a comma is only treated as a role separator when the text
    after it isn't itself just the organisation's own legal suffix ("X,
    Inc.").

    Returns (name, company_number, is_private_individual).
    Company numbers are rarely present in Lords entries — most return None.
    """
    company_number = None
    core = _strip_trailing_parens(description)
    if not core:
        return None, company_number, False

    # A comma is a role/organisation separator UNLESS the text after it is
    # nothing but a bare legal suffix ("Automatic Data Processing, Inc."),
    # in which case the comma belongs to the organisation's own name.
    comma_idx = core.find(",")
    role: str | None = None
    candidate = core
    if comma_idx != -1:
        after = core[comma_idx + 1 :].strip()
        if not _BARE_SUFFIX_RE.match(after):
            role = core[:comma_idx].strip().lower()
            candidate = after

    if role is not None and any(marker in role for marker in _FAMILY_MARKERS):
        return None, company_number, True

    if not candidate:
        return None, company_number, False

    # "Company"/"Companies" in _ORG_MARKERS_RE below is a real legal-form
    # marker for names like "Walt Disney Company" — but it is also an
    # ordinary English noun, so an unguarded marker search matches prose
    # too: "...a property management company; full details are held by the
    # Registrar..." is not a role/organisation split at all, just a
    # sentence that happens to contain the word "company". Reject prose
    # before any marker match, strong or fallback.
    if any(marker in candidate for marker in _PROSE_MARKERS):
        return None, company_number, False

    if _ORG_MARKERS_RE.search(candidate):
        return candidate, company_number, False

    # No explicit legal-form marker on the candidate. Two contexts still
    # make a named (non-person) entity plausible enough to extract:
    #  - Category 2 (Shareholdings): the register's own category definition
    #    means the entry NAMES a body corporate, never a person — you
    #    cannot hold "shares" in an individual. "Halma", "Barclays",
    #    "Sharetego" (trading names with no legal suffix) sit here.
    #  - a "Role, X" entry (comma present) whose description independently
    #    says "company"/"corporation"/"firm" (mirrors the previous
    #    with-comma-only fallback, now gated on the candidate itself
    #    looking like a name rather than blindly returning whatever
    #    followed the comma).
    is_shareholding = category is not None and category.startswith("Category 2")
    mentions_org_word = role is not None and bool(
        re.search(r"\b(company|corporation|firm)\b", description, re.IGNORECASE)
    )
    if (is_shareholding or mentions_org_word) and _looks_like_entity_name(candidate):
        return candidate, company_number, False

    return None, company_number, False


def _scoped_registry_id(scope: str, name: str) -> str:
    """Bounded, deterministic registry_id for an unresolved placeholder.

    The name component is hashed rather than embedded: Lords interest text is
    free-form and unbounded, which overflowed registry_id (DataError: value too
    long). Hashing keeps the key a fixed length while remaining deterministic
    (idempotent re-ingest) and scoped to the individual claim, so unresolved
    counterparties never merge across interests.
    """
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    return f"{scope[:64]}:{digest}"


def _canonical_company_entity(company: Company) -> Entity:
    """The Companies House node for a company, creating it if absent.

    Mirrors ch_appointments._canonical_company_entity. A plain
    ``get_or_create(company_number=...)`` raises MultipleObjectsReturned here:
    GLEIF can hold a distinct Entity for the same company under
    registry_scheme="GLEIF-LEI" that also carries this company_number —
    those are legitimately separate claims and must never be merged
    (ADR-006, duplicate over merge). Resolving on registry_scheme="GB-COH" +
    registry_id is unique by DB constraint, so this can never be ambiguous.
    """
    coh = Entity.objects.filter(
        entity_type="company",
        registry_scheme="GB-COH",
        registry_id=company.company_number,
    ).first()
    if coh:
        return coh
    entity, _ = Entity.objects.get_or_create(
        entity_type="company",
        registry_scheme="GB-COH",
        registry_id=company.company_number,
        defaults={
            "name": company.company_name,
            "company_number": company.company_number,
        },
    )
    return entity


def _resolve_counterparty(
    name: str, company_number: str | None, interest_key: str
) -> tuple[Entity, float, str, dict[str, Any]] | None:
    """Resolve a named counterparty to an Entity.

    Mirrors parliament_interests._resolve_counterparty_entity.
    Returns None for ambiguous-name case (2+ companies).
    """
    if company_number:
        normalised_number = normalise_company_number(company_number)
        company = Company.objects.filter(company_number=normalised_number).first()
        if company:
            entity = _canonical_company_entity(company)
            return entity, 1.0, "identifier", {}
        entity, _ = Entity.objects.get_or_create(
            entity_type="regulated_entity",
            registry_scheme="UK-LORDS-UNRESOLVED",
            registry_id=_scoped_registry_id(interest_key, _normalise_name(name)),
            defaults={"name": name},
        )
        return entity, 0.5, "name_only", {"declared_company_number": company_number}

    normalised = _normalise_name(name)
    matches = Company.objects.filter(normalised_name=normalised)
    match_count = matches.count()
    if match_count == 1:
        company = matches.get()
        entity = _canonical_company_entity(company)
        return entity, 0.9, "exact_name", {}
    if match_count >= 2:
        return None

    entity, _ = Entity.objects.get_or_create(
        entity_type="regulated_entity",
        registry_scheme="UK-LORDS-UNRESOLVED",
        registry_id=_scoped_registry_id(interest_key, normalised),
        defaults={"name": name},
    )
    return entity, 0.5, "name_only", {}


def _get_or_create_lord_entity(member_id: str, info: dict[str, str]) -> Entity:
    """Create or get a Lord entity by Parliament member ID."""
    entity, _ = Entity.objects.get_or_create(
        entity_type="person",
        registry_scheme="UK-PARLIAMENT-MEMBER",
        registry_id=member_id,
        defaults={
            "name": info["name"],
            "role_description": "Member of the House of Lords",
            "properties": {
                "party": info["party"],
                "peer_type": info["peer_type"],
            },
        },
    )
    return entity


def fetch_lords_register(
    output_dir: str | Path,
    wayback_timestamp: str | None = None,
    max_pages: int = 50,
    polite_delay_seconds: float = 2.0,
    client: httpx.Client | None = None,
) -> LordsFetchResult:
    """Download the Lords register HTML pages into a local directory.

    If ``wayback_timestamp`` is given (e.g. "20201130"), fetches from the
    Wayback Machine snapshot for that date. Otherwise fetches the live page.

    Stores each page as ``page_NN.html`` and a provenance JSON sidecar.
    """
    source = load_source(SOURCE_ID)  # refuses to run without sources/uk_lords_interests.yml
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    owns_client = client is None
    client = client or httpx.Client(
        timeout=30.0,
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
    )

    try:
        if wayback_timestamp:
            base_url = f"{WAYBACK_PREFIX}{wayback_timestamp}/{LORDS_REGISTER_URL}"
        else:
            base_url = LORDS_REGISTER_URL

        pages: list[str] = []
        total_entries = 0

        for page_num in range(1, max_pages + 1):
            url = f"{base_url}?page={page_num}" if page_num > 1 else base_url
            r = client.get(url)
            # A 404 past page 1 means we have run off the end of the register, or
            # (with Wayback) that this page was never archived. Either way it is
            # the end of what is available — stop and keep the pages already
            # fetched rather than discarding a successful partial crawl.
            if r.status_code == 404 and page_num > 1:
                logger.warning(
                    "lords register: page %d returned 404, stopping after %d pages",
                    page_num,
                    len(pages),
                )
                break
            r.raise_for_status()
            html_content = r.text

            page_path = output_dir / f"page_{page_num:02d}.html"
            page_path.write_text(html_content, encoding="utf-8")
            pages.append(str(page_path))

            # Count entries on this page
            soup = BeautifulSoup(html_content, "html.parser")
            cards = soup.find_all(class_="card card-member")
            total_entries += len(cards)

            if not cards:
                break

            time.sleep(polite_delay_seconds)

        # Compute content hash of all pages
        hasher = hashlib.sha256()
        for pg in sorted(pages):
            hasher.update(Path(pg).read_bytes())
        content_hash = hasher.hexdigest()

        retrieved_at = datetime.now(UTC)
        # observed_at is when the SOURCE published/captured this page set --
        # the Wayback snapshot timestamp -- never today's download time. This
        # is the multi-page analogue of `uncorrupt.staging.raw`'s shared
        # cache-with-provenance helper (that helper assumes one payload file;
        # a Lords register fetch is N page files sharing one provenance
        # record, so the `ProvenanceRecord` shape is adopted directly here
        # rather than forcing a single-payload API onto a multi-file
        # artifact). A live (non-Wayback) fetch has no capture date of its
        # own and leaves this None; `ingest_lords_register` still falls back
        # to retrieved_at for that case (unchanged).
        observed_at = _parse_wayback_timestamp(wayback_timestamp) if wayback_timestamp else None
        provenance_record = ProvenanceRecord(
            source_id=source.source_id,
            source_url=base_url,
            retrieved_at=retrieved_at,
            content_hash=content_hash,
            license=source.license,
            redistribution=source.redistribution,
            jurisdiction=source.jurisdictions[0] if source.jurisdictions else "",
            data_class=source.data_class,
            tier=source.tier,
            connector=source.source_id,
            connector_version=CONNECTOR_VERSION,
            observed_at=observed_at,
        )
        provenance = {
            "source": SOURCE_NAME,
            "source_url": provenance_record.source_url,
            "wayback_timestamp": wayback_timestamp,
            "retrieved_at": provenance_record.retrieved_at.isoformat(),
            "content_hash": provenance_record.content_hash,
            "observed_at": (
                provenance_record.observed_at.isoformat() if provenance_record.observed_at else None
            ),
            "page_count": len(pages),
            "total_entries": total_entries,
            "pages": pages,
        }
        provenance_path = output_dir / "provenance.json"
        provenance_path.write_text(json.dumps(provenance, indent=2), encoding="utf-8")

        return LordsFetchResult(
            html_path=output_dir,
            provenance_path=provenance_path,
            page_count=len(pages),
            total_entries=total_entries,
            source_url=provenance_record.source_url,
            retrieved_at=provenance_record.retrieved_at,
            content_hash=provenance_record.content_hash,
            wayback_timestamp=wayback_timestamp,
        )
    finally:
        if owns_client:
            client.close()


def _parse_lords_page(html_content: str) -> list[dict[str, Any]]:
    """Parse a single Lords register HTML page into member records.

    Returns a list of {member_id, name, party, peer_type, interests} dicts.
    Each interest is {category, description, ceased_date}.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    cards = soup.find_all(class_="card card-member")
    members: list[dict[str, Any]] = []

    for card in cards:
        member_id = _parse_member_id(card)
        if not member_id:
            continue
        info = _parse_member_info(card)
        if not info["name"]:
            continue

        # The card (<a>) is inside a card-expandable div that also
        # contains the expand-area with interest entries.
        container = card.parent
        interests = _parse_interests(container) if container else []

        members.append(
            {
                "member_id": member_id,
                "name": info["name"],
                "party": info["party"],
                "peer_type": info["peer_type"],
                "interests": interests,
            }
        )

    return members


def ingest_lords_register(
    html_dir: str | Path,
    wayback_timestamp: str | None = None,
) -> dict[str, Any]:
    """Ingest previously-downloaded Lords register HTML pages.

    Returns summary stats: {matched, unmatched_counterparty,
    skipped_private_individual, skipped_no_counterparty, nil_returns,
    total_interests, total_members}.
    """
    load_source(SOURCE_ID)  # refuses to run without sources/uk_lords_interests.yml
    html_dir = Path(html_dir)
    provenance_path = html_dir / "provenance.json"

    # Parse observed_at from provenance
    retrieved_at: datetime | None = None
    snapshot_ref: str | None = None
    if provenance_path.exists():
        prov = json.loads(provenance_path.read_text())
        retrieved_at = datetime.fromisoformat(prov["retrieved_at"])
        snapshot_ref = prov.get("content_hash")
        if wayback_timestamp is None:
            wayback_timestamp = prov.get("wayback_timestamp")

    # observed_at is when the SOURCE published or was captured, never when we
    # downloaded it. For a Wayback snapshot that is the capture timestamp, not
    # today's retrieval time — using retrieved_at here silently destroyed the
    # historical value of re-ingested snapshots (every snapshot of a
    # still-registered interest collapsed onto one attestation stamped
    # today). A live (non-Wayback) fetch has no capture date of its own, so it
    # keeps falling back to retrieved_at.
    observed_at = _parse_wayback_timestamp(wayback_timestamp) if wayback_timestamp else None
    if observed_at is None:
        if wayback_timestamp:
            logger.warning(
                "lords register: unparseable wayback_timestamp %r, falling back to retrieved_at",
                wayback_timestamp,
            )
        observed_at = retrieved_at

    matched = 0
    unmatched_counterparty = 0
    skipped_private_individual = 0
    skipped_no_counterparty = 0
    skipped_implausible_name = 0
    ambiguous_company_number = 0
    nil_returns = 0
    total_interests = 0
    total_members = 0

    # Collect all HTML pages
    page_files = sorted(html_dir.glob("page_*.html"))

    for page_file in page_files:
        html_content = page_file.read_text(encoding="utf-8")
        members = _parse_lords_page(html_content)

        for member in members:
            total_members += 1
            member_entity = _get_or_create_lord_entity(
                member["member_id"],
                {
                    "name": member["name"],
                    "party": member["party"],
                    "peer_type": member["peer_type"],
                },
            )

            if not member["interests"]:
                nil_returns += 1
                continue

            for interest in member["interests"]:
                total_interests += 1
                description = interest["description"]
                category = interest["category"]

                name, company_number, is_private = _extract_counterparty(description, category)

                # A counterparty "name" longer than this is an extraction
                # failure, not an organisation — the Lords register is free
                # text and the extractor sometimes captures a whole sentence.
                # Creating an Entity from it would both overflow Entity.name
                # (varchar 500) and pollute the graph with junk nodes, so
                # skip and count instead. Conservative extraction: prefer
                # missing a counterparty over inventing one.
                if name and len(name) > _MAX_COUNTERPARTY_NAME:
                    skipped_implausible_name += 1
                    continue
                if is_private:
                    skipped_private_individual += 1
                    continue
                if not name:
                    skipped_no_counterparty += 1
                    continue

                # Bounded at construction: category and counterparty name are free-form
                # Lords register text. interest_key feeds BOTH registry_id and
                # Attestation.source_reference (varchar 200), so hash the
                # variable part rather than embedding it — deterministic, so
                # re-ingest stays idempotent, and still unique per interest.
                _key_body = hashlib.sha256(
                    f"{category}:{_normalise_name(name)}".encode()
                ).hexdigest()[:32]
                interest_key = f"{member['member_id']}:{_key_body}"

                # Commit per interest, not the whole run in one transaction: a
                # giant transaction over every member/interest holds locks for
                # the entire ingest and loses everything already processed if
                # one row (or the process) dies partway through.
                try:
                    with transaction.atomic():
                        resolved = _resolve_counterparty(name, company_number, interest_key)
                        if resolved is None:
                            unmatched_counterparty += 1
                            continue
                        counterparty_entity, confidence, method, resolve_props = resolved

                        valid_to = interest.get("ceased_date")

                        # Edge = THE CLAIM (no citation — spec v0.3 §7-bis)
                        edge, _ = Edge.objects.get_or_create(
                            edge_type="declared_interest",
                            source_entity=member_entity,
                            target_entity=counterparty_entity,
                            valid_from=None,
                            valid_to=valid_to,
                            defaults={
                                "properties": {
                                    "category": category,
                                    "description": description,
                                    **resolve_props,
                                },
                            },
                        )

                        # Attestation = THE EVIDENCE
                        att_defaults: dict[str, Any] = {
                            "match_confidence": confidence,
                            "match_method": method,
                        }
                        if observed_at:
                            att_defaults["observed_at"] = observed_at
                        if snapshot_ref:
                            att_defaults["snapshot_ref"] = snapshot_ref

                        source_url = LORDS_REGISTER_URL
                        if wayback_timestamp:
                            source_url = f"{WAYBACK_PREFIX}{wayback_timestamp}/{LORDS_REGISTER_URL}"

                        # A snapshot identifier on the reference, not just the
                        # interest: without it, re-ingesting several Wayback
                        # snapshots of the same still-registered interest
                        # collapses onto a single Attestation row (get_or_create
                        # matches on source_reference), destroying the very
                        # evidence of when each snapshot recorded the interest.
                        attestation_reference = interest_key
                        if wayback_timestamp:
                            attestation_reference = f"{interest_key}:{wayback_timestamp}"

                        Attestation.objects.get_or_create(
                            edge=edge,
                            source_name=SOURCE_NAME,
                            source_reference=attestation_reference,
                            defaults={
                                "source_url": source_url,
                                **att_defaults,
                            },
                        )
                except Entity.MultipleObjectsReturned:
                    # A company_number can legitimately resolve to 2+ Entity
                    # rows under different registry schemes (GB-COH,
                    # GLEIF-LEI — ADR-006 duplicate-over-merge). Count and
                    # move on rather than losing the whole run to one row.
                    ambiguous_company_number += 1
                    continue

                matched += 1

    return {
        "matched": matched,
        "unmatched_counterparty": unmatched_counterparty,
        "skipped_private_individual": skipped_private_individual,
        "skipped_no_counterparty": skipped_no_counterparty,
        "skipped_implausible_name": skipped_implausible_name,
        "ambiguous_company_number": ambiguous_company_number,
        "nil_returns": nil_returns,
        "total_interests": total_interests,
        "total_members": total_members,
    }
