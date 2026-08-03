"""Companies House officer-appointments ingest — the second directorship hop.

`ch_officers` answers "who are the officers of company X". That alone cannot
surface a **shared** directorship: if a referrer and a supplier are connected
because one person sits on both boards, the connecting company is not in our
target list and the path stays invisible no matter how many companies we pull.
This module walks the other direction — `/officers/{officer_id}/appointments`,
"what else does this person direct" — which is what turns two disconnected
company subgraphs into a two-hop path.

Scope boundary (ADR-004 D1): only officers **already present in the graph** as
`GB-COH-OFFICER` entities are expanded. This never discovers new people; it
only completes the appointment set of people a prior, in-scope ingest already
recorded. Officers with no stable CH officer ID
(`GB-COH-OFFICER-UNRESOLVED`) are deliberately NOT expanded — without an
identifier there is no way to fetch *that* person's appointments rather than
someone else's with the same name, and guessing would merge distinct people
(governing principle: duplicate over merge).

The same field allowlist as `ch_officers` applies: date of birth, address,
nationality, occupation and country of residence are dropped before the
response touches disk.

A company on the far end of an appointment is only linked if it already exists
in `staging.Company`. An appointment to a company we cannot verify against the
register is counted (`company_unmatched`) and skipped, never turned into a
placeholder entity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from django.db import transaction

from uncorrupt.graph.ch_officers import (
    CH_API_BASE,
    DEFAULT_MAX_CACHE_AGE_DAYS,
    SOURCE_NAME,
    _cache_is_valid,
    _fetch_page_with_backoff,
    _parse_appointment_self_link,
    _parse_ch_date,
    _require_api_key,
)
from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.staging.companies_house import normalise_company_number
from uncorrupt.staging.models import Company

# Appointment items carry a different shape to officer items: the company is
# in `appointed_to`, and the person's name sits on the response envelope
# rather than on each item.
_ALLOWED_APPOINTMENT_FIELDS = (
    "appointed_to",
    "officer_role",
    "appointed_on",
    "resigned_on",
    "links",
)
_ALLOWED_APPOINTED_TO_FIELDS = ("company_number", "company_name", "company_status")

# CH allows 600 requests / 5 minutes. Stay under it.
_THROTTLE_SECONDS = 0.55

logger = logging.getLogger(__name__)


def _strip_appointment(item: dict[str, Any]) -> dict[str, Any]:
    kept = {k: item[k] for k in _ALLOWED_APPOINTMENT_FIELDS if k in item}
    appointed_to = kept.get("appointed_to")
    if isinstance(appointed_to, dict):
        kept["appointed_to"] = {
            k: appointed_to[k] for k in _ALLOWED_APPOINTED_TO_FIELDS if k in appointed_to
        }
    return kept


def fetch_officer_appointments(
    officer_ids: list[str],
    output_dir: str | Path,
    max_cache_age_days: int = DEFAULT_MAX_CACHE_AGE_DAYS,
    max_retries: int = 5,
    items_per_page: int = 50,
) -> dict[str, int]:
    """Fetch and cache each officer's full appointment list.

    Caches `{officer_id}.json` plus a `{officer_id}.provenance.json` beside
    it, so an interrupted run resumes without refetching. Returns counts:
    {fetched, cached, failed}.
    """
    api_key = _require_api_key()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    counts = {"fetched": 0, "cached": 0, "failed": 0}

    with httpx.Client(auth=(api_key, ""), timeout=30.0) as client:
        for officer_id in officer_ids:
            json_path = output_dir / f"{officer_id}.json"
            provenance_path = output_dir / f"{officer_id}.provenance.json"

            if json_path.exists() and provenance_path.exists():
                try:
                    provenance = json.loads(provenance_path.read_text())
                    if _cache_is_valid(json_path, provenance, max_cache_age_days):
                        counts["cached"] += 1
                        continue
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass  # unreadable provenance ⇒ refetch rather than trust

            source_url = f"{CH_API_BASE}/officers/{officer_id}/appointments"
            try:
                items: list[dict[str, Any]] = []
                start_index = 0
                while True:
                    params = {
                        "items_per_page": items_per_page,
                        "start_index": start_index,
                    }
                    page = _fetch_page_with_backoff(client, source_url, params, max_retries)
                    page_items = page.get("items", [])
                    items.extend(page_items)
                    total = page.get("total_results", len(items))
                    start_index += len(page_items)
                    if not page_items or start_index >= total:
                        break
            except (httpx.HTTPError, RuntimeError):
                counts["failed"] += 1
                continue

            stripped = [_strip_appointment(i) for i in items]
            payload = json.dumps(stripped, indent=2).encode()
            json_path.write_bytes(payload)
            provenance_path.write_text(
                json.dumps(
                    {
                        "officer_id": officer_id,
                        "source_url": source_url,
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "appointment_count": len(stripped),
                        "content_hash": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                    },
                    indent=2,
                )
            )
            counts["fetched"] += 1
            time.sleep(_THROTTLE_SECONDS)

    return counts


def _canonical_company_entity(company: Company) -> Entity:
    """The Companies House node for a company, creating it if absent.

    A plain `get_or_create(company_number=...)` raises MultipleObjectsReturned
    here: GLEIF publishes more than one LEI record for the same UK company
    (observed on 5 company numbers, one pair carrying different names -- a
    rename with a stale LEI record left behind). Those GLEIF entities are
    legitimately distinct claims and must NOT be merged (governing principle:
    duplicate over merge), so we cannot pick one arbitrarily.

    A Companies-House-sourced appointment belongs on the Companies House node,
    so that is what we resolve to -- creating it when only GLEIF records exist.
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


def ingest_officer_appointments(
    officer_ids: list[str],
    input_dir: str | Path,
    batch_size: int = 500,
    progress_every: int = 2000,
) -> dict[str, int]:
    """Turn cached appointment lists into `officer_of` edges.

    Commits every `batch_size` officers rather than wrapping all 21k in one
    transaction. A single atomic block over ~200k get_or_create pairs holds
    locks for the whole run, grows without bound, and loses everything if the
    client dies at minute 20 -- which is exactly what happened twice. Because
    every write is a `get_or_create`, a partially-completed run is safe to
    re-run: it resumes rather than duplicating.

    Returns {edges_created, officers_processed, appointments_seen,
    company_unmatched, officer_missing, inconsistent_dates}.
    """
    input_dir = Path(input_dir)
    stats = {
        "edges_created": 0,
        "officers_processed": 0,
        "appointments_seen": 0,
        "company_unmatched": 0,
        "officer_missing": 0,
        "inconsistent_dates": 0,
    }

    for start in range(0, len(officer_ids), batch_size):
        batch = officer_ids[start : start + batch_size]
        with transaction.atomic():
            for officer_id in batch:
                json_path = input_dir / f"{officer_id}.json"
                if not json_path.exists():
                    continue

                person = Entity.objects.filter(
                    entity_type="person",
                    registry_scheme="GB-COH-OFFICER",
                    registry_id=officer_id,
                ).first()
                if person is None:
                    # Expansion is scoped to people a prior ingest already
                    # recorded; we never create a person here.
                    stats["officer_missing"] += 1
                    continue

                stats["officers_processed"] += 1
                provenance_path = input_dir / f"{officer_id}.provenance.json"
                observed_at = None
                if provenance_path.exists():
                    try:
                        observed_at = datetime.fromisoformat(
                            json.loads(provenance_path.read_text())["retrieved_at"]
                        )
                    except (json.JSONDecodeError, KeyError, ValueError):
                        observed_at = None

                for item in json.loads(json_path.read_text()):
                    stats["appointments_seen"] += 1
                    appointed_to = item.get("appointed_to") or {}
                    raw_number = appointed_to.get("company_number")
                    if not raw_number:
                        stats["company_unmatched"] += 1
                        continue

                    company_number = normalise_company_number(raw_number)
                    company = Company.objects.filter(company_number=company_number).first()
                    if company is None:
                        stats["company_unmatched"] += 1
                        continue

                    company_entity = _canonical_company_entity(company)

                    properties: dict[str, Any] = {}
                    role = (item.get("officer_role") or "").strip()
                    if role:
                        properties["officer_role"] = role

                    resigned_raw = item.get("resigned_on")
                    valid_to = _parse_ch_date(resigned_raw) if resigned_raw else None
                    if resigned_raw and valid_to is None:
                        # Present but unparseable: ended, date unknown. Never
                        # read as still serving — that would make a lapsed
                        # directorship look like a live one in a path search.
                        properties["resigned_on_unparsed"] = resigned_raw
                        properties["resignation_status"] = "ended_date_unknown"

                    valid_from = _parse_ch_date(item.get("appointed_on"))

                    # Companies House contains appointments that resigned BEFORE
                    # they were appointed (real example: appointed 1993-02-22,
                    # resigned 1993-02-02). The relationship is real -- the person
                    # WAS an officer -- but the dates are internally inconsistent,
                    # so we assert no temporal claim at all rather than guessing
                    # which date is wrong or silently swapping them. Keeping
                    # valid_from while dropping valid_to would be worse: it would
                    # read as a still-live directorship in a pre-award path search.
                    if valid_from and valid_to and valid_to < valid_from:
                        properties["appointed_on_raw"] = item.get("appointed_on")
                        properties["resigned_on_raw"] = resigned_raw
                        properties["date_status"] = "inconsistent_source_dates"
                        valid_from = None
                        valid_to = None
                        stats["inconsistent_dates"] += 1

                    edge, created = Edge.objects.get_or_create(
                        edge_type="officer_of",
                        source_entity=person,
                        target_entity=company_entity,
                        valid_from=valid_from,
                        valid_to=valid_to,
                        defaults={"properties": properties},
                    )
                    if created:
                        stats["edges_created"] += 1

                    source_reference = (
                        _parse_appointment_self_link(item) or f"{officer_id}:{company_number}"
                    )
                    att_defaults: dict[str, Any] = {
                        "source_url": f"{CH_API_BASE}/officers/{officer_id}/appointments",
                        "match_confidence": 1.0,
                        "match_method": "identifier",
                    }
                    if observed_at:
                        att_defaults["observed_at"] = observed_at
                    Attestation.objects.get_or_create(
                        edge=edge,
                        source_name=SOURCE_NAME,
                        source_reference=source_reference,
                        defaults=att_defaults,
                    )

        if stats["officers_processed"] and (
            stats["officers_processed"] % progress_every < batch_size
        ):
            logger.info(
                "appointments: %d officers, %d edges",
                stats["officers_processed"],
                stats["edges_created"],
            )
            print(
                f"  {stats['officers_processed']:,} officers / {stats['edges_created']:,} edges",
                flush=True,
            )

    return stats
