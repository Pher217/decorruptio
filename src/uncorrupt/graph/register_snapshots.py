"""Historical register-snapshot reconstruction — the temporal-evidence ladder.

The problem this module exists to fix: Phase C tests whether a supplier<->
official relationship existed BEFORE a public contract was awarded, but only
24 of 5,425 `declared_interest` edges carry a `valid_from` (0.4%) because the
House of Lords register publishes no start dates. The strict test
`valid_from <= award_date` is therefore nearly unsatisfiable for register
data, no matter how real the relationship is — that is a property of the
SOURCE, not a finding about relationships (see `phase_c_paths.py`,
`run_positive_controls.py`).

The insight: even when a register does not say WHEN a relationship began, a
register EDITION published before the award proves the relationship was **on
the record by that date**. House of Lords rules require members to report
changes within one month, keep ceased interests visible for a year, and make
previous register editions available — so a relationship visible in a
pre-award snapshot is positive evidence of a pre-existing relationship, even
with no exact start date. Absence from a snapshot is NOT evidence of absence
(archive coverage is patchy — see `query_wayback_cdx`); it is scored as "not
observed", never as a negative.

Evidence ladder (`EvidenceLevel`, do not collapse these):
  1. EVENT_DATED             — `Edge.valid_from <= award_date`. Strongest.
  2. PRE_AWARD_OBSERVED      — seen in a register snapshot published before
                                the award date. Supports the temporal claim.
  3. ATEMPORAL_CORROBORATION — the register currently contains the
                                relationship; timing unknown. An investigative
                                lead, NOT evidence of a pre-award conflict —
                                must never drive a CONFIRMED verdict.
  4. NO_TRACE                — no record of the relationship at all.

Snapshot acquisition: the Internet Archive Wayback Machine CDX API
(`query_wayback_cdx`) enumerates dated captures of a register page. The UK
Parliament Interests API's `publishedDate`/`updatedDates` fields were
investigated as a possible shortcut (`parliament_registration_date_coverage`)
and found NOT to encode real edition history for older interests — see that
function's docstring. Snapshot reconstruction is therefore scoped to the
Lords register (HTML, no API) via Wayback.

Schema: this module deliberately adds NO new model — `Edge` (the claim) and
`Attestation` (the evidence, via `observed_at` + `snapshot_ref`) already carry
everything a snapshot observation needs (spec v0.3 §7-bis; see
`uncorrupt.graph.models`).

Deliberate non-reuse of `lords_interests.ingest_lords_register` for the
Attestation write (see `ingest_lords_snapshot`): that function keys its
Attestation only on `(edge, source_name, interest_key)` — with no snapshot
identity in the key — and sets `observed_at` from the *fetch* time
(`provenance['retrieved_at']`, i.e. "now"), not the archived date. Re-running
it over several historical snapshots of the same still-registered interest
therefore collapses onto ONE attestation carrying today's wall-clock time,
which is useless as pre-award evidence and was the reason this module exists
rather than a one-line change to that ingest. `ingest_lords_snapshot` reuses
that module's PARSING and RESOLUTION helpers (no parallel extraction logic)
but writes its own, snapshot-keyed Attestation with the correct `observed_at`.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

import httpx
from django.core.exceptions import MultipleObjectsReturned
from django.db import transaction

from uncorrupt.graph.lords_interests import (
    _MAX_COUNTERPARTY_NAME,
    LORDS_REGISTER_URL,
    WAYBACK_PREFIX,
    LordsFetchResult,
    _extract_counterparty,
    _get_or_create_lord_entity,
    _parse_lords_page,
    _resolve_counterparty,
    fetch_lords_register,
)
from uncorrupt.graph.lords_interests import (
    SOURCE_NAME as LORDS_SOURCE_NAME,
)
from uncorrupt.graph.models import Attestation, Edge
from uncorrupt.staging.companies_house import _normalise_name

CDX_API_BASE = "http://web.archive.org/cdx/search/cdx"

# Suffix distinguishing a snapshot-sourced attestation from the live-register
# attestation `lords_interests.ingest_lords_register` writes under the plain
# `LORDS_SOURCE_NAME` — the two must never collide in
# `Attestation.source_name` or a snapshot observation and a live observation
# of the same interest would fight over one (edge, source_name,
# source_reference) slot.
SNAPSHOT_SOURCE_SUFFIX = " (Wayback archive snapshot)"


class EvidenceLevel(IntEnum):
    """The temporal evidence ladder. Lower value = stronger evidence.

    Never collapse levels: level 3 is an investigative lead, not evidence of
    a pre-award conflict, and must never be treated as equivalent to level
    1/2 when deciding a CONFIRMED verdict.
    """

    EVENT_DATED = 1
    PRE_AWARD_OBSERVED = 2
    ATEMPORAL_CORROBORATION = 3
    NO_TRACE = 4


# The only two levels that support "this relationship existed before the
# award" — level 3 supports only the weaker claim ("a formal association
# exists; when it began is not established") and must never appear here.
PRE_AWARD_ADMISSIBLE_LEVELS = frozenset(
    {EvidenceLevel.EVENT_DATED, EvidenceLevel.PRE_AWARD_OBSERVED}
)


def is_pre_award_admissible(level: EvidenceLevel) -> bool:
    """Does this evidence level support a pre-award-existence claim?

    True only for levels 1-2. Level 3 (atemporal corroboration) and level 4
    (no trace) never satisfy the strict endpoint — a passing gate must never
    be built by quietly admitting level 3 evidence.
    """
    return level in PRE_AWARD_ADMISSIBLE_LEVELS


# ---------------------------------------------------------------------------
# Wayback Machine CDX API — snapshot acquisition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WaybackCapture:
    """One dated capture of a URL, as listed by the Wayback CDX API."""

    timestamp: str  # raw 14-digit wayback timestamp, e.g. "20200617183732"
    original_url: str
    mimetype: str
    statuscode: str
    digest: str
    length: int

    @property
    def captured_at(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)

    @property
    def wayback_url(self) -> str:
        return f"{WAYBACK_PREFIX}{self.timestamp}/{self.original_url}"


def _fetch_text_with_backoff(client: httpx.Client, url: httpx.URL, max_retries: int) -> str:
    """Shared GET-with-backoff for both the CDX API and the Interests API.

    Retries on 429/5xx with exponential delay (mirrors
    `parliament_interests._fetch_json_with_backoff`); anything else raises.
    """
    delay = 2.0
    for _attempt in range(max_retries):
        response = client.get(url)
        if response.status_code == 200:
            return response.text
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
    raise RuntimeError(f"GET failed after {max_retries} retries: {url}")


def query_wayback_cdx(
    url: str,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    client: httpx.Client | None = None,
    max_retries: int = 5,
) -> list[WaybackCapture]:
    """Enumerate Wayback Machine captures of `url` via the CDX API.

    `collapse=digest` de-duplicates consecutive captures with identical
    content, so the result is distinct EDITIONS, not raw crawl frequency.
    `from_date`/`to_date` are Wayback timestamp prefixes (e.g. "2020",
    "202003"), not ISO dates.

    Returns an empty list on zero captures — this is an ARCHIVE COVERAGE
    fact, not evidence that a relationship never existed. Never treat an
    empty result as refuting anything.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    params: dict[str, Any] = {
        "url": url,
        "output": "json",
        "collapse": "digest",
        "filter": "statuscode:200",
    }
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date

    try:
        cdx_url = httpx.URL(CDX_API_BASE, params=params)
        payload = _fetch_text_with_backoff(client, cdx_url, max_retries)
    finally:
        if owns_client:
            client.close()

    rows = json.loads(payload) if payload.strip() else []
    if not rows:
        return []
    # The first row is the column header (["urlkey", "timestamp", ...]), not
    # a capture.
    return [
        WaybackCapture(
            timestamp=row[1],
            original_url=row[2],
            mimetype=row[3],
            statuscode=row[4],
            digest=row[5],
            length=int(row[6]),
        )
        for row in rows[1:]
    ]


def nearest_capture_before(
    captures: Sequence[WaybackCapture], target_date: date
) -> WaybackCapture | None:
    """The latest capture strictly before `target_date`, or None.

    None means no register edition is available to check — a statement
    about ARCHIVE COVERAGE, not about the relationship. Never treat it as
    refuting evidence.
    """
    candidates = [c for c in captures if c.captured_at.date() < target_date]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.captured_at)


def fetch_lords_snapshot(
    capture: WaybackCapture,
    output_dir: str | Path,
    max_pages: int = 50,
    polite_delay_seconds: float = 2.0,
    client: httpx.Client | None = None,
) -> LordsFetchResult:
    """Download the Lords register HTML as it stood at `capture`'s timestamp.

    Thin wrapper over `lords_interests.fetch_lords_register` — downloading a
    live vs. an archived page differs only in the timestamp, and that
    function already handles Wayback fetches, pagination, and provenance.
    """
    return fetch_lords_register(
        output_dir,
        wayback_timestamp=capture.timestamp,
        max_pages=max_pages,
        polite_delay_seconds=polite_delay_seconds,
        client=client,
    )


# ---------------------------------------------------------------------------
# Snapshot-aware attestation — the Lords register
# ---------------------------------------------------------------------------


_PAGE_FILE_RE = re.compile(r"page_(\d+)")


def _page_number(page_file: Path) -> int:
    """Extract the register page number from a `page_NN.html` filename.

    `lords_interests.fetch_lords_register` names pages `page_{n:02d}.html`
    (1-based) — 0 is an impossible page number and signals a filename that
    doesn't match the expected pattern, so callers can tell it apart from a
    real page.
    """
    m = _PAGE_FILE_RE.match(page_file.stem)
    return int(m.group(1)) if m else 0


def _interest_key(member_id: str, category: str, name: str) -> str:
    """Mirrors `lords_interests.ingest_lords_register`'s own interest_key
    derivation, so a snapshot attestation's reference correlates with the
    live-register ingest's for the same interest.
    """
    body = hashlib.sha256(f"{category}:{_normalise_name(name)}".encode()).hexdigest()[:32]
    return f"{member_id}:{body}"


def ingest_lords_snapshot(
    html_dir: str | Path,
    capture: WaybackCapture,
    content_hash: str,
) -> dict[str, Any]:
    """Re-parse a downloaded historical Lords snapshot into Attestation evidence.

    Reuses `lords_interests`' own parsing (`_parse_lords_page`) and
    resolution (`_extract_counterparty`, `_resolve_counterparty`,
    `_get_or_create_lord_entity`) so a snapshot resolves counterparties
    identically to the live ingest — no parallel extraction logic. Edges are
    `get_or_create`'d on the same fields the live ingest uses
    (`edge_type="declared_interest"`, same source/target, `valid_from=None`,
    `valid_to=ceased_date`), so a snapshot never creates a duplicate claim —
    it only adds evidence to whichever Edge already represents it (creating
    one if this is the first time the relationship has been ingested at all,
    e.g. an interest later removed from the live register but present in an
    old snapshot).

    `content_hash` is the caller-supplied SHA-256 over the exact downloaded
    bytes (from `LordsFetchResult.content_hash`, produced by
    `fetch_lords_snapshot`) — matching `Attestation.snapshot_ref`'s
    documented "SHA-256 content hash" contract. The Wayback CDX API's own
    `digest` field is a different (SHA-1) hash used only for CDX-level
    dedup, not stored here.

    The Attestation is keyed on `(edge, source_name, f"{interest_key}:
    {capture.timestamp}:p{page_number}")` — distinct per snapshot AND per
    register page, so N historical snapshots of the same still-registered
    interest produce N separate, correctly-dated attestations rather than
    collapsing onto one. The embedded page number lets
    `snapshot_evidence_pages` trace which register page supplied a given
    edge's evidence — needed to quantify the register's alphabetical-
    coverage bias (see `describe_page_coverage_bias`): page 1 is archived
    far more densely than deeper pages, so lift measured from snapshots is
    confounded by a member's surname position unless this is reported.

    Each interest is written in its OWN transaction, not one atomic block
    over the whole snapshot (~40 pages, ~800 interests). Confirmed live
    against the production graph: `_resolve_counterparty` (owned by
    lords_interests.py, not editable here) can raise
    `MultipleObjectsReturned` when a company_number already has 2+ Entity
    rows — a real, pre-existing, INTENTIONAL condition (commit a8355c0,
    "MULTIPLE LEIS PER COMPANY": distinct claims that must not be merged),
    not a bug this module can fix. One ambiguous company must not roll back
    the other ~800 interests on the same snapshot, so it is caught, counted
    as `ambiguous_company_number`, and skipped — the same "count and skip
    rather than crash or guess" discipline `_resolve_counterparty` already
    applies to an ambiguous NAME match, just not (yet) to this case.
    """
    html_dir = Path(html_dir)
    page_files = sorted(html_dir.glob("page_*.html"))

    snapshot_source_url = capture.wayback_url
    source_name = f"{LORDS_SOURCE_NAME}{SNAPSHOT_SOURCE_SUFFIX}"

    total_interests = 0
    new_attestations = 0
    existing_attestations = 0
    unmatched_counterparty = 0
    skipped_private_individual = 0
    skipped_no_counterparty = 0
    skipped_implausible_name = 0
    ambiguous_company_number = 0

    for page_file in page_files:
        page_number = _page_number(page_file)
        html_content = page_file.read_text(encoding="utf-8")
        members = _parse_lords_page(html_content)

        for member in members:
            member_entity = _get_or_create_lord_entity(
                member["member_id"],
                {
                    "name": member["name"],
                    "party": member["party"],
                    "peer_type": member["peer_type"],
                },
            )

            for interest in member["interests"]:
                total_interests += 1
                description = interest["description"]
                category = interest["category"]

                name, company_number, is_private = _extract_counterparty(description)

                if name and len(name) > _MAX_COUNTERPARTY_NAME:
                    skipped_implausible_name += 1
                    continue
                if is_private:
                    skipped_private_individual += 1
                    continue
                if not name:
                    skipped_no_counterparty += 1
                    continue

                interest_key = _interest_key(member["member_id"], category, name)

                # One interest per transaction, not one atomic block over the
                # whole snapshot: `_resolve_counterparty` (owned by
                # lords_interests.py, not editable here) can raise
                # MultipleObjectsReturned when a company_number already has
                # 2+ Entity rows (a real, pre-existing condition — see
                # commit a8355c0, "MULTIPLE LEIS PER COMPANY" — intentional,
                # not a bug to merge away). A single ambiguous company must
                # not roll back the other ~800 interests on this snapshot;
                # this mirrors that same commit's other lesson ("ONE
                # TRANSACTION OVER 21K OFFICERS" -> commit per unit of work).
                try:
                    with transaction.atomic():
                        resolved = _resolve_counterparty(name, company_number, interest_key)
                        if resolved is None:
                            unmatched_counterparty += 1
                            continue
                        counterparty_entity, confidence, method, resolve_props = resolved

                        valid_to = interest.get("ceased_date")

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

                        snapshot_reference = (
                            f"{interest_key}:{capture.timestamp}:p{page_number:02d}"
                        )
                        _, created = Attestation.objects.get_or_create(
                            edge=edge,
                            source_name=source_name,
                            source_reference=snapshot_reference,
                            defaults={
                                "source_url": snapshot_source_url,
                                "observed_at": capture.captured_at,
                                "snapshot_ref": content_hash,
                                "match_confidence": confidence,
                                "match_method": method,
                            },
                        )
                except MultipleObjectsReturned:
                    ambiguous_company_number += 1
                    continue

                if created:
                    new_attestations += 1
                else:
                    existing_attestations += 1

    return {
        "capture_timestamp": capture.timestamp,
        "total_interests": total_interests,
        "new_attestations": new_attestations,
        "existing_attestations": existing_attestations,
        "unmatched_counterparty": unmatched_counterparty,
        "skipped_private_individual": skipped_private_individual,
        "skipped_no_counterparty": skipped_no_counterparty,
        "skipped_implausible_name": skipped_implausible_name,
        "ambiguous_company_number": ambiguous_company_number,
    }


# ---------------------------------------------------------------------------
# Alphabetical-coverage bias — the register is sorted alphabetically and
# Wayback's archival density is NOT uniform across it
# ---------------------------------------------------------------------------

_SNAPSHOT_REFERENCE_PAGE_RE = re.compile(r":p(\d+)$")


def snapshot_evidence_pages(edge: Edge, award_date: date | None = None) -> list[int]:
    """Which register page(s) supplied `edge`'s snapshot evidence.

    Parses the page number `ingest_lords_snapshot` embeds in
    `Attestation.source_reference`. If `award_date` is given, only
    attestations observed strictly before it are considered (matching
    PRE_AWARD_OBSERVED's own definition); otherwise every snapshot-sourced
    attestation on the edge is included.

    This exists to quantify the register's ALPHABETICAL-COVERAGE BIAS:
    verified live (2026-08-02) that the register is sorted alphabetically
    (page 1 starts "Lord Aberdare"/"Baroness Adams..."; page 2 "Lord
    Arbuthnot..."; page 20 "Lord Hunt..."; page 40 "Baroness Warsi...") and
    that Wayback's archival density falls off sharply with page depth (see
    `describe_page_coverage_bias`). A member early in the alphabet has far
    more chances to be caught by a pre-award snapshot than one late in it —
    independent of whether their relationship is real — so any lift number
    must be reported broken down by page, not averaged across the cohort.
    """
    qs = edge.attestations.filter(snapshot_ref__isnull=False)
    if award_date is not None:
        award_datetime = datetime.combine(award_date, datetime.min.time(), tzinfo=UTC)
        qs = qs.filter(observed_at__lt=award_datetime)

    pages: set[int] = set()
    for source_reference in qs.values_list("source_reference", flat=True):
        if not source_reference:
            continue
        m = _SNAPSHOT_REFERENCE_PAGE_RE.search(source_reference)
        if m:
            pages.add(int(m.group(1)))
    return sorted(pages)


def describe_page_coverage_bias(
    page_numbers: Sequence[int],
    client: httpx.Client | None = None,
) -> dict[int, int]:
    """Wayback capture count for each requested Lords-register page number.

    Live numbers checked 2026-08-02 (`collapse=digest`, 2020-06 to
    2026-07): page 1 has 51 unique captures, page 2 has 14, page 5 has 7,
    page 20 has 4, page 40 has 6. Page 1 is archived roughly 3-10x more
    densely than any deeper page — deeper pages are all in the same thin
    4-14 range, not a smooth decline. Combined with the register's
    alphabetical ordering (see `snapshot_evidence_pages`), this means
    snapshot-derived pre-award evidence is structurally easier to find for
    members whose surname sorts early — a confound on any lift measurement,
    not a footnote.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        result: dict[int, int] = {}
        for page in page_numbers:
            url = LORDS_REGISTER_URL if page == 1 else f"{LORDS_REGISTER_URL}?page={page}"
            captures = query_wayback_cdx(url, client=client)
            result[page] = len(captures)
        return result
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# UK Parliament Interests API — investigated as a possible shortcut
# ---------------------------------------------------------------------------


def parliament_registration_date_coverage(
    registered_from: date,
    registered_to: date,
    client: httpx.Client | None = None,
    max_retries: int = 5,
    page_size: int = 20,
) -> dict[str, Any]:
    """Assess whether the Commons Interests API's date metadata gives real
    edition/snapshot history, as an alternative to Wayback scraping.

    Checked live against https://interests-api.parliament.uk on 2026-08-02:
    for interests registered in 2019-2020, `publishedDate` clusters around
    2024-03 to 2024-07 regardless of the interest's own `registrationDate`
    (e.g. one item: registrationDate=2020-01-08, publishedDate=2024-07-31).
    That matches `/api/v1/Registers`' earliest listed document (2024-03-18,
    per `parliament_interests.py`'s own docstring) — `publishedDate` is the
    date this record was migrated onto the CURRENT interests-api platform,
    not a real per-item publication date. It gives no edition history.

    `updatedDates` occasionally carries a genuine intermediate amendment
    date (proof the record already existed as of that date) but only adds
    signal when `registrationDate` itself is null — most Commons interests
    already carry `registrationDate`, which `parliament_interests.py` already
    uses as `Edge.valid_from` (level 1 on the evidence ladder, no snapshot
    needed).

    Conclusion: this API gives no additional snapshot/edition signal beyond
    what `parliament_interests.py` already ingests. Snapshot reconstruction
    work is scoped to the Lords register (HTML, no API) via Wayback.

    Returns real counts from a live sample so this conclusion is checked,
    not just asserted.
    """
    from uncorrupt.graph.parliament_interests import INTERESTS_API_BASE

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        params = {
            "ExpandChildInterests": "true",
            "SortOrder": "PublishingDateDescending",
            "RegisteredFrom": registered_from.isoformat(),
            "RegisteredTo": registered_to.isoformat(),
            "Take": page_size,
        }
        url = httpx.URL(INTERESTS_API_BASE, params=params)
        payload = _fetch_text_with_backoff(client, url, max_retries)
    finally:
        if owns_client:
            client.close()

    data = json.loads(payload)
    items = data.get("items") or []

    total = len(items)
    with_registration_date = 0
    without_registration_date = 0
    with_nonempty_updated_dates = 0
    migration_artifact_published_dates = 0

    for item in items:
        if item.get("registrationDate"):
            with_registration_date += 1
        else:
            without_registration_date += 1
        if item.get("updatedDates"):
            with_nonempty_updated_dates += 1
        published = item.get("publishedDate")
        if published and published >= "2024-03-18":
            migration_artifact_published_dates += 1

    return {
        "total": total,
        "with_registration_date": with_registration_date,
        "without_registration_date": without_registration_date,
        "with_nonempty_updated_dates": with_nonempty_updated_dates,
        "published_date_looks_like_migration_artifact": migration_artifact_published_dates,
    }


# ---------------------------------------------------------------------------
# The evidence ladder — classifying edges, paths, and relationships
# ---------------------------------------------------------------------------


def edge_evidence_level(edge: Edge, award_date: date) -> EvidenceLevel:
    """Classify one Edge's evidence for "existed before `award_date`".

    Never returns anything weaker than ATEMPORAL_CORROBORATION for an edge
    that exists — there is no "refuted" level. Absence of a pre-award
    snapshot attestation does not demote an edge; it just means level 2
    isn't reached.
    """
    if edge.valid_from is not None and edge.valid_from <= award_date:
        return EvidenceLevel.EVENT_DATED

    award_datetime = datetime.combine(award_date, datetime.min.time(), tzinfo=UTC)
    has_pre_award_snapshot = edge.attestations.filter(
        snapshot_ref__isnull=False,
        observed_at__lt=award_datetime,
    ).exists()
    if has_pre_award_snapshot:
        return EvidenceLevel.PRE_AWARD_OBSERVED

    return EvidenceLevel.ATEMPORAL_CORROBORATION


def path_evidence_level(path: Sequence[Edge], award_date: date) -> EvidenceLevel | None:
    """The weakest evidence level among a path's temporally-meaningful edges.

    `same_as` edges assert identity, not a relationship in time — they carry
    no temporal claim to weaken and are excluded, mirroring
    `phase_c_paths.find_paths`' treatment of `same_as` as a zero-cost hop
    exempt from the date test. That exclusion is correct and must stay: an
    identity assertion cannot weaken a temporal level it never contributed
    to.

    Returns `None` — explicitly, never an `EvidenceLevel` member — when the
    path has NO temporally-meaningful edge at all (empty path, or every edge
    on it is `same_as`). This used to return `EvidenceLevel.EVENT_DATED`
    (level 1, the STRONGEST level) for that case, on the theory that "no
    temporal edge on the path" means "nothing to weaken it" — but that is
    fail-open: it asserts affirmative event-dated support that does not
    exist, in a codebase whose architecture (ADR-008) is fail-closed
    everywhere else. `None` is the honest representation of "no temporal
    claim was made"; `EvidenceLevel` has no member for that, and inventing
    one would misrepresent it as either evidence (weak or strong) or as
    `NO_TRACE` (which specifically means "no path was found at all" — see
    `relationship_evidence_level`), neither of which is true here: a path
    WAS found, it just says nothing about timing. `relationship_evidence_level`
    is the only caller and is written to handle `None` explicitly (see its
    docstring for how); any new caller must do the same rather than compare
    the result to an `EvidenceLevel` directly.
    """
    levels = [edge_evidence_level(edge, award_date) for edge in path if edge.edge_type != "same_as"]
    if not levels:
        return None
    return max(levels)


def path_min_identity_confidence(path: Sequence[Edge]) -> float | None:
    """The weakest `same_as` identity-bridge confidence on a path.

    STRICTLY POST-HOC, EXPLORATORY, NON-GATING DIAGNOSTIC METADATA. This
    value must NEVER alter inclusion, scoring, path selection, any gate, the
    sealed cohort, or the verdict — it may only be *reported* alongside a
    path's evidence level. No consumer of this project currently reads
    `Attestation.match_confidence` on a `same_as` edge for any gating
    purpose (`gates/binding.py` deliberately excludes it — see its ADR-008
    docstring); this function must not become the first.

    The `same_as` confidence values it reads (0.60 "surname + peerage title
    only" / 0.85 forename- or territorial-designation-verified — see
    `identity_resolution.py`'s `CONFIDENCE_*` constants) are UNCALIBRATED
    estimates, not probabilities. Hand-verification found 15 of 21 checked
    cross-register identity paths were namesake collisions — two different
    real humans sharing a name — at BOTH tiers: at 0.85, one match paired an
    MP born 1986 with a 2002 directorship (age 15, impossible), and two
    others carried the wrong middle name. Never read this value as "the
    probability this identity match is correct." Reporting it fixes
    OBSERVABILITY — a path bridged by a coin-flip identity guess no longer
    reports identical strength to one bridged by a registry identifier — it
    does NOT fix VALIDITY. Displaying this number does not make any
    person-level claim on the path defensible.

    Returns `None` when the path uses no identity bridge at all (no
    `same_as` edge on it), and also when a `same_as` edge is present but
    carries no confidence-bearing attestation — both mean "nothing to warn
    on" to a caller and are deliberately not distinguished in the return
    value.
    """
    same_as_edges = [edge for edge in path if edge.edge_type == "same_as"]
    if not same_as_edges:
        return None

    confidences = [
        confidence
        for edge in same_as_edges
        for confidence in edge.attestations.values_list("match_confidence", flat=True)
    ]
    if not confidences:
        return None

    return min(confidences)


def find_all_paths(
    start_ids: set[int],
    goal_id: int,
    adj: dict[int, list[Edge]],
    max_hops: int,
) -> list[list[Edge]]:
    """Every path (dated or not) from any of `start_ids` to `goal_id`.

    Mirrors `phase_c_paths.find_paths`' walk/cost/adjacency semantics exactly
    (same_as costs 0, everything else costs 1, budget `max_hops`) but is
    date-agnostic by design — date filtering is the caller's job via
    `path_evidence_level`, so this can serve the widened evidence ladder
    instead of the strict `valid_from`-only test.
    """
    paths: list[list[Edge]] = []

    def cost(edge: Edge) -> int:
        return 0 if edge.edge_type == "same_as" else 1

    def spent(path: list[Edge]) -> int:
        return sum(cost(e) for e in path)

    def other_end(edge: Edge, entity_id: int) -> int:
        return (
            edge.target_entity_id if edge.source_entity_id == entity_id else edge.source_entity_id
        )

    def walk(node: int, path: list[Edge], seen: set[int]) -> None:
        if spent(path) >= max_hops:
            return
        for edge in adj.get(node, ()):
            nxt = other_end(edge, node)
            if nxt in seen:
                continue
            new_path = [*path, edge]
            if nxt == goal_id:
                paths.append(new_path)
                continue
            walk(nxt, new_path, seen | {nxt})

    for start in start_ids:
        walk(start, [], {start})
    return paths


def relationship_evidence_level(
    start_ids: set[int],
    goal_id: int,
    adj: dict[int, list[Edge]],
    max_hops: int,
    award_date: date,
) -> EvidenceLevel:
    """Classify a person<->company relationship on the evidence ladder.

    The best (lowest-numbered) level among every path found — if ANY path is
    pre-award-observed or event-dated, the relationship is admissible at
    that level, even if other paths between the same two entities are
    weaker. No path at all is NO_TRACE, never a "refuted" level.

    A path can carry NO temporal evidence at all — `path_evidence_level`
    returns `None` for a path made entirely of `same_as` identity hops (see
    its docstring). Such paths are excluded from the min-reduction: they say
    nothing about timing, so they must never be compared against — or win
    over — a path that does. If every path found is like this, there is no
    real `EvidenceLevel` member for "a structural path exists but asserts no
    temporal claim." This function's only consumer
    (`scripts/measure_temporal_lift.py`) always needs a concrete
    `EvidenceLevel` back (it calls `int(level)` / `level.name`
    unconditionally), so — unlike `path_evidence_level`, whose only caller is
    this function and can be given the explicit `None` — this function
    cannot return `None` and falls back to `EvidenceLevel.ATEMPORAL_CORROBORATION`:
    the same floor `edge_evidence_level` already uses for "this exists, but
    corroboration/timing is unclear" (its docstring: "never returns anything
    weaker than ATEMPORAL_CORROBORATION for an edge that exists"). This is
    deliberately NOT `NO_TRACE` — that level means "no path was found at
    all" (the branch immediately below), and a real path here WAS found, so
    reporting `NO_TRACE` would be its own false claim. It is also never a
    dated level (`EVENT_DATED`/`PRE_AWARD_OBSERVED`), which no path here
    earned.
    """
    paths = find_all_paths(start_ids, goal_id, adj, max_hops)
    if not paths:
        return EvidenceLevel.NO_TRACE
    levels = [
        level for level in (path_evidence_level(p, award_date) for p in paths) if level is not None
    ]
    if not levels:
        return EvidenceLevel.ATEMPORAL_CORROBORATION
    return min(levels)


# ---------------------------------------------------------------------------
# Confidence intervals — never report a bare rate, especially a bare zero
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion, as (lower, upper) in [0, 1].

    Used so a 0/n rate is never reported as a bare zero — e.g. 0/200 has a
    Wilson 95% upper bound of ~1.9%, not 0%. `z=1.96` is the default 95%
    confidence level.
    """
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z**2 / n
    centre = phat + z**2 / (2 * n)
    spread = z * ((phat * (1 - phat) / n + z**2 / (4 * n**2)) ** 0.5)
    lower = (centre - spread) / denom
    upper = (centre + spread) / denom
    return (max(0.0, lower), min(1.0, upper))
