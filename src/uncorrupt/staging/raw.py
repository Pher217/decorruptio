"""Immutable, content-addressed raw zone (the evidence). Write-once enforced.

Never mutate raw — this is what makes findings reproducible (ADR-000 G6).

`write_cached_fetch`/`read_cached_fetch` are the ONE shared cache-with-
provenance helper every `uncorrupt.graph` connector's fetch layer should use
(they were not: `ch_officers.py`, `ch_appointments.py` and
`overseas_entities.py` each hand-rolled their own `_cache_is_valid` +
provenance-dict-building, and `gleif.py`/`ec_donations.py`/`lords_interests.py`
hand-rolled the write side alone). Adopting `uncorrupt.core.provenance.
ProvenanceRecord` here — rather than inventing a second shape — means every
connector's cache entry is provenance-complete (source, retrieval time,
content hash, and licence/tier resolved from the source register) with one
implementation of "is this cache entry still trustworthy", not seven.

Sidecar file shape: the canonical fields (`source_url`, `retrieved_at`,
`content_hash`, `observed_at`) sit at the TOP LEVEL of the provenance JSON,
alongside whatever connector-specific fields the caller passes as `extra`
(e.g. `officer_count`, `hits`, `record_count`) — exactly the flat shape every
connector's ad hoc provenance dict already used. An existing on-disk cache
directory therefore needs no migration: `read_cached_fetch` recovers
`license`/`redistribution`/`jurisdiction`/`data_class`/`tier` fresh from the
source register rather than expecting them in the file (old files never had
them), and any key it does not recognise as canonical is bucketed into
`extra` automatically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from uncorrupt.core.provenance import ProvenanceRecord
from uncorrupt.register.models import SourceEntry

# The only keys `write_cached_fetch`/`read_cached_fetch` manage on the
# sidecar file. Everything else — old or new — is connector-specific extra
# data, round-tripped through `extra` without needing a schema change here.
_CANONICAL_KEYS = frozenset({"source_url", "retrieved_at", "content_hash", "observed_at"})


def content_hash(payload: bytes) -> str:
    return sha256(payload).hexdigest()


@dataclass(frozen=True)
class CachedFetch:
    """A raw artifact plus its provenance — either just written, or read back
    from an existing cache entry that is still fresh and hash-verified.

    `cached=False` means this call just fetched and wrote it; `cached=True`
    means it was served from disk without any network access.
    """

    payload_path: Path
    provenance_path: Path
    provenance: ProvenanceRecord
    extra: dict[str, Any]
    cached: bool


def _default_jurisdiction(source: SourceEntry, jurisdiction: str | None) -> str:
    if jurisdiction:
        return jurisdiction
    return source.jurisdictions[0] if source.jurisdictions else ""


def write_cached_fetch(
    payload: bytes,
    payload_path: Path,
    provenance_path: Path,
    *,
    source: SourceEntry,
    source_url: str,
    connector_version: str,
    observed_at: datetime | None = None,
    jurisdiction: str | None = None,
    extra: dict[str, Any] | None = None,
) -> CachedFetch:
    """Write a raw artifact + its provenance sidecar, and return both.

    `observed_at` must be the date the SOURCE itself published or captured
    this artifact — e.g. a Wayback Machine snapshot timestamp — and is left
    None only when the source genuinely carries no separate capture signal
    of its own (a live API response reflecting current register state IS its
    own "now"; there is nothing else to record). Do not pass
    `datetime.now()` or the about-to-be-computed `retrieved_at` here to fill
    the gap — that silent substitution is exactly the bug this helper exists
    to make hard to repeat (it once collapsed a historical Wayback capture's
    evidence onto today's download time). A caller with a real capture date
    (parsed from a Wayback timestamp, a CDX record, an API-declared snapshot
    date, ...) must compute and pass it explicitly.

    `extra` is merged onto the sidecar file's top level for connector-
    specific bookkeeping (`officer_count`, `hits`, ...) — see the module
    docstring for why it lives alongside the canonical keys rather than
    nested.
    """
    extra = extra or {}
    collision = _CANONICAL_KEYS & extra.keys()
    if collision:
        raise ValueError(
            f"extra field(s) collide with canonical provenance keys: {sorted(collision)}"
        )

    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(payload)
    retrieved_at = datetime.now(UTC)
    hash_value = f"sha256:{content_hash(payload)}"

    provenance = ProvenanceRecord(
        source_id=source.source_id,
        source_url=source_url,
        retrieved_at=retrieved_at,
        content_hash=hash_value,
        license=source.license,
        redistribution=source.redistribution,
        jurisdiction=_default_jurisdiction(source, jurisdiction),
        data_class=source.data_class,
        tier=source.tier,
        connector=source.source_id,
        connector_version=connector_version,
        observed_at=observed_at,
    )

    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "source_url": provenance.source_url,
        "retrieved_at": provenance.retrieved_at.isoformat(),
        "content_hash": provenance.content_hash,
        "observed_at": provenance.observed_at.isoformat() if provenance.observed_at else None,
        **extra,
    }
    provenance_path.write_text(json.dumps(record, indent=2))

    return CachedFetch(
        payload_path=payload_path,
        provenance_path=provenance_path,
        provenance=provenance,
        extra=extra,
        cached=False,
    )


def read_cached_fetch(
    payload_path: Path,
    provenance_path: Path,
    *,
    source: SourceEntry,
    connector_version: str,
    max_age_days: int | None = None,
    jurisdiction: str | None = None,
) -> CachedFetch | None:
    """Return the cached artifact if it exists, is fresh, and its hash still
    matches the file on disk — else None.

    A miss is ALWAYS a plain None, never an exception: a missing file, a
    provenance sidecar that isn't valid JSON, one missing an expected key, a
    cache entry older than `max_age_days`, or one whose stored content hash
    no longer matches the file on disk (tampered or corrupted) are all
    treated identically — the caller refetches. An unattended multi-hour
    sweep over thousands of cache entries must survive one bad file, not
    crash on it.
    """
    if not (payload_path.exists() and provenance_path.exists()):
        return None
    try:
        raw = json.loads(provenance_path.read_text())
        retrieved_at = datetime.fromisoformat(raw["retrieved_at"])
        stored_hash = raw["content_hash"]
        source_url = raw["source_url"]
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None

    if max_age_days is not None and (datetime.now(UTC) - retrieved_at).days > max_age_days:
        return None

    try:
        actual_hash = f"sha256:{content_hash(payload_path.read_bytes())}"
    except OSError:
        return None
    if actual_hash != stored_hash:
        return None

    observed_at: datetime | None = None
    raw_observed_at = raw.get("observed_at")
    if raw_observed_at:
        try:
            observed_at = datetime.fromisoformat(raw_observed_at)
        except ValueError:
            observed_at = None

    provenance = ProvenanceRecord(
        source_id=source.source_id,
        source_url=source_url,
        retrieved_at=retrieved_at,
        content_hash=stored_hash,
        license=source.license,
        redistribution=source.redistribution,
        jurisdiction=_default_jurisdiction(source, jurisdiction),
        data_class=source.data_class,
        tier=source.tier,
        connector=source.source_id,
        connector_version=connector_version,
        observed_at=observed_at,
    )
    extra = {k: v for k, v in raw.items() if k not in _CANONICAL_KEYS}

    return CachedFetch(
        payload_path=payload_path,
        provenance_path=provenance_path,
        provenance=provenance,
        extra=extra,
        cached=True,
    )
