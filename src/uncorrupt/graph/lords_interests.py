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

# Longest plausible organisation name. Anything beyond this is a parse
# artefact from free-text register entries, not a real counterparty.
_MAX_COUNTERPARTY_NAME = 200

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


def _extract_counterparty(description: str) -> tuple[str | None, str | None, bool]:
    """Extract counterparty name and company number from an interest description.

    Lords register entries are free-text like:
        "Chairman, Microlink PC (UK) Ltd (computing and software)"
        "Director, Leadership in Mind Ltd (business activities)"

    Returns (name, company_number, is_private_individual).
    Company numbers are rarely present in Lords entries — most return None.
    """
    company_number = None

    # Try to extract the organisation name — typically after a role and comma
    # "Chairman, Microlink PC (UK) Ltd (computing and software)"
    # → "Microlink PC (UK) Ltd"
    parts = description.split(",", 1)
    if len(parts) < 2:
        # No comma — the whole description might be the organisation
        # e.g. "Sharetego (travel company)"
        if re.search(r"\b(Ltd|Limited|LLP|plc|CIC)\b", description, re.IGNORECASE):
            # Extract name before last parenthetical description
            name = re.split(r"\s*\([^)]*\)\s*$", description)[0].strip()
            return name, company_number, False
        # Check for org markers without comma
        if re.search(
            r"\b(Ltd|Limited|LLP|plc|CIC|Foundation|Trust|Society|Board|"
            r"Authority|Group|Association|Charity|University|College)\b",
            description,
            re.IGNORECASE,
        ):
            name = re.split(r"\s*\([^)]*\)\s*$", description)[0].strip()
            return name, company_number, False
        return None, company_number, False

    role = parts[0].strip().lower()
    rest = parts[1].strip()

    # Check if this is a family-related entry
    if any(marker in role for marker in _FAMILY_MARKERS):
        return None, company_number, True

    # Extract organisation name (before the LAST parenthetical description)
    # "Microlink PC (UK) Ltd (computing and software)" → "Microlink PC (UK) Ltd"
    name = re.split(r"\s*\([^)]*\)\s*$", rest)[0].strip()

    # Check if it looks like an organisation vs a person
    org_markers = re.search(
        r"\b(Ltd|Limited|LLP|plc|CIC|Foundation|Trust|Society|Board|"
        r"Authority|Group|Association|Charity|University|College|"
        r"Partnership|Holdings|Capital|Fund|Enterprise)\b",
        name,
        re.IGNORECASE,
    )
    if not org_markers:
        # Could be a person name or an institution without markers
        if re.search(r"\b(company|corporation|firm)\b", description, re.IGNORECASE):
            return name, company_number, False
        return None, company_number, False

    return name, company_number, False


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
            entity, _ = Entity.objects.get_or_create(
                entity_type="company",
                company_number=company.company_number,
                defaults={
                    "name": company.company_name,
                    "registry_scheme": "GB-COH",
                    "registry_id": company.company_number,
                },
            )
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
        entity, _ = Entity.objects.get_or_create(
            entity_type="company",
            company_number=company.company_number,
            defaults={
                "name": company.company_name,
                "registry_scheme": "GB-COH",
                "registry_id": company.company_number,
            },
        )
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
    load_source(SOURCE_ID)  # refuses to run without sources/uk_lords_interests.yml
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
        provenance = {
            "source": SOURCE_NAME,
            "source_url": base_url,
            "wayback_timestamp": wayback_timestamp,
            "retrieved_at": retrieved_at.isoformat(),
            "content_hash": content_hash,
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
            source_url=base_url,
            retrieved_at=retrieved_at,
            content_hash=content_hash,
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
    observed_at: datetime | None = None
    snapshot_ref: str | None = None
    if provenance_path.exists():
        prov = json.loads(provenance_path.read_text())
        observed_at = datetime.fromisoformat(prov["retrieved_at"])
        snapshot_ref = prov.get("content_hash")
        if wayback_timestamp is None:
            wayback_timestamp = prov.get("wayback_timestamp")

    matched = 0
    unmatched_counterparty = 0
    skipped_private_individual = 0
    skipped_no_counterparty = 0
    skipped_implausible_name = 0
    nil_returns = 0
    total_interests = 0
    total_members = 0

    # Collect all HTML pages
    page_files = sorted(html_dir.glob("page_*.html"))

    with transaction.atomic():
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

                    name, company_number, is_private = _extract_counterparty(description)

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

                    Attestation.objects.get_or_create(
                        edge=edge,
                        source_name=SOURCE_NAME,
                        source_reference=interest_key,
                        defaults={
                            "source_url": source_url,
                            **att_defaults,
                        },
                    )
                    matched += 1

    return {
        "matched": matched,
        "unmatched_counterparty": unmatched_counterparty,
        "skipped_private_individual": skipped_private_individual,
        "skipped_no_counterparty": skipped_no_counterparty,
        "skipped_implausible_name": skipped_implausible_name,
        "nil_returns": nil_returns,
        "total_interests": total_interests,
        "total_members": total_members,
    }
