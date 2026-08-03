"""Tests for the shared cache-with-provenance helper (`uncorrupt.staging.raw`).

Verifies the core invariants every `uncorrupt.graph` connector's fetch layer
relies on:
- writing a payload records source_url/retrieved_at/content_hash, plus
  connector-specific `extra` fields at the top level of the sidecar file
- `observed_at` is never silently defaulted from `retrieved_at` -- it is
  either the caller's explicit source-capture date, or None
- a fresh, hash-verified cache entry reads back correctly (cached=True)
- a missing, corrupted, stale, or tampered cache entry is a plain miss
  (None), never an exception
- an old-shape provenance file (no `observed_at` key at all) still reads
  back correctly, with `observed_at=None` and its legacy fields in `extra`
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from uncorrupt.core.provenance import Redistribution
from uncorrupt.core.tiers import DataClass, Tier
from uncorrupt.register.models import SourceEntry
from uncorrupt.staging.raw import content_hash, read_cached_fetch, write_cached_fetch


def _source(**overrides) -> SourceEntry:
    defaults = {
        "source_id": "test_source",
        "name": "Test Source",
        "jurisdictions": ["GB"],
        "data_class": DataClass.A1,
        "tier": Tier.A,
        "license": "Open Government Licence v3.0",
        "redistribution": Redistribution.OPEN,
        "legal_basis": "test fixture",
        "access_method": "bulk-api",
        "freshness_sla_days": 7,
    }
    defaults.update(overrides)
    return SourceEntry.model_validate(defaults)


class TestWriteCachedFetch:
    def test_writes_payload_and_provenance_with_correct_hash(self, tmp_path):
        """GIVEN a payload WHEN written THEN the sidecar records the exact sha256 of
        the bytes actually on disk."""
        payload_path = tmp_path / "artifact.json"
        provenance_path = tmp_path / "artifact.provenance.json"

        result = write_cached_fetch(
            b'{"a": 1}',
            payload_path,
            provenance_path,
            source=_source(),
            source_url="https://example.org/a",
            connector_version="0.1",
        )

        assert result.cached is False
        assert payload_path.read_bytes() == b'{"a": 1}'
        assert result.provenance.content_hash == f"sha256:{content_hash(b'{"a": 1}')}"

    def test_observed_at_defaults_to_none_not_retrieved_at(self, tmp_path):
        """GIVEN no explicit observed_at WHEN written THEN observed_at is None --
        never silently backfilled from retrieved_at (the bug this helper exists to
        make hard to repeat)."""
        result = write_cached_fetch(
            b"payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            source_url="https://example.org/a",
            connector_version="0.1",
        )

        assert result.provenance.observed_at is None
        assert result.provenance.retrieved_at is not None

    def test_explicit_observed_at_is_recorded_distinctly_from_retrieved_at(self, tmp_path):
        """GIVEN an explicit observed_at (e.g. a Wayback capture date) WHEN written
        THEN it is recorded as given, distinct from the download-time retrieved_at."""
        captured = datetime(2020, 11, 30, tzinfo=UTC)

        result = write_cached_fetch(
            b"payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            source_url="https://example.org/a",
            connector_version="0.1",
            observed_at=captured,
        )

        assert result.provenance.observed_at == captured
        assert result.provenance.observed_at != result.provenance.retrieved_at
        stored = json.loads((tmp_path / "a.provenance.json").read_text())
        assert stored["observed_at"] == captured.isoformat()

    def test_extra_fields_are_merged_at_top_level_not_nested(self, tmp_path):
        """GIVEN connector-specific extra fields WHEN written THEN they appear at the
        TOP LEVEL of the sidecar JSON -- the same flat shape every connector's ad hoc
        provenance dict already used, so existing readers (e.g. a test asserting
        provenance["record_count"]) need no migration."""
        write_cached_fetch(
            b"payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            source_url="https://example.org/a",
            connector_version="0.1",
            extra={"record_count": 3, "country": "GB"},
        )

        stored = json.loads((tmp_path / "a.provenance.json").read_text())
        assert stored["record_count"] == 3
        assert stored["country"] == "GB"
        assert "extra" not in stored

    def test_extra_colliding_with_a_canonical_key_raises(self, tmp_path):
        """GIVEN an extra field named the same as a canonical key WHEN written THEN
        it raises rather than silently overwriting canonical provenance data."""
        with pytest.raises(ValueError, match="content_hash"):
            write_cached_fetch(
                b"payload",
                tmp_path / "a.bin",
                tmp_path / "a.provenance.json",
                source=_source(),
                source_url="https://example.org/a",
                connector_version="0.1",
                extra={"content_hash": "sha256:evil"},
            )

    def test_jurisdiction_defaults_to_source_first_jurisdiction(self, tmp_path):
        """GIVEN no explicit jurisdiction override WHEN written THEN the provenance
        jurisdiction is the source register entry's first declared jurisdiction."""
        result = write_cached_fetch(
            b"payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(jurisdictions=["GB"]),
            source_url="https://example.org/a",
            connector_version="0.1",
        )

        assert result.provenance.jurisdiction == "GB"

    def test_license_and_tier_are_resolved_from_the_source_register(self, tmp_path):
        """GIVEN a source register entry WHEN written THEN license/redistribution/
        data_class/tier come from the register, not duplicated by the caller."""
        result = write_cached_fetch(
            b"payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(license="CC0 1.0", tier=Tier.B, data_class=DataClass.A2),
            source_url="https://example.org/a",
            connector_version="0.1",
        )

        assert result.provenance.license == "CC0 1.0"
        assert result.provenance.tier == Tier.B
        assert result.provenance.data_class == DataClass.A2


class TestReadCachedFetch:
    def test_fresh_valid_cache_returns_a_hit(self, tmp_path):
        """GIVEN a freshly written cache entry WHEN read back THEN it is returned as
        a hit (cached=True) with the same provenance fields."""
        write_cached_fetch(
            b"payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            source_url="https://example.org/a",
            connector_version="0.1",
            extra={"record_count": 3},
        )

        result = read_cached_fetch(
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            connector_version="0.1",
        )

        assert result is not None
        assert result.cached is True
        assert result.extra["record_count"] == 3

    def test_missing_payload_file_is_a_miss(self, tmp_path):
        """GIVEN no payload file on disk WHEN read THEN the result is None, not an
        exception."""
        result = read_cached_fetch(
            tmp_path / "missing.bin",
            tmp_path / "missing.provenance.json",
            source=_source(),
            connector_version="0.1",
        )

        assert result is None

    def test_missing_provenance_file_is_a_miss(self, tmp_path):
        """GIVEN a payload file but no provenance sidecar WHEN read THEN the result
        is None."""
        (tmp_path / "a.bin").write_bytes(b"payload")

        result = read_cached_fetch(
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            connector_version="0.1",
        )

        assert result is None

    def test_corrupted_provenance_json_is_a_miss_not_an_exception(self, tmp_path):
        """GIVEN a provenance sidecar that is not valid JSON WHEN read THEN the
        result is a plain miss (None) -- a corrupted cache entry must never crash an
        unattended multi-hour sweep."""
        (tmp_path / "a.bin").write_bytes(b"payload")
        (tmp_path / "a.provenance.json").write_text("{not valid json")

        result = read_cached_fetch(
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            connector_version="0.1",
        )

        assert result is None

    def test_provenance_missing_a_required_key_is_a_miss(self, tmp_path):
        """GIVEN a provenance sidecar missing content_hash WHEN read THEN the result
        is None rather than raising KeyError."""
        (tmp_path / "a.bin").write_bytes(b"payload")
        (tmp_path / "a.provenance.json").write_text(
            json.dumps(
                {"source_url": "https://example.org/a", "retrieved_at": "2026-01-01T00:00:00+00:00"}
            )
        )

        result = read_cached_fetch(
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            connector_version="0.1",
        )

        assert result is None

    def test_hash_mismatch_is_a_miss_not_an_error(self, tmp_path):
        """GIVEN a payload file whose bytes were tampered with after caching (the
        stored content_hash no longer matches) WHEN read THEN the result is None,
        never an exception -- a corrupted cache entry is treated as a miss."""
        write_cached_fetch(
            b"original payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            source_url="https://example.org/a",
            connector_version="0.1",
        )
        (tmp_path / "a.bin").write_bytes(b"tampered payload")

        result = read_cached_fetch(
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            connector_version="0.1",
        )

        assert result is None

    def test_stale_cache_beyond_max_age_is_a_miss(self, tmp_path):
        """GIVEN a cache entry older than max_age_days WHEN read THEN the result is
        None -- a cache entry is not trusted forever."""
        write_cached_fetch(
            b"payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            source_url="https://example.org/a",
            connector_version="0.1",
        )
        stale = json.loads((tmp_path / "a.provenance.json").read_text())
        stale["retrieved_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        (tmp_path / "a.provenance.json").write_text(json.dumps(stale))

        result = read_cached_fetch(
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            connector_version="0.1",
            max_age_days=30,
        )

        assert result is None

    def test_fresh_cache_within_max_age_is_a_hit(self, tmp_path):
        """GIVEN a cache entry within max_age_days WHEN read THEN it is still trusted
        (sanity check paired with the staleness test above)."""
        write_cached_fetch(
            b"payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            source_url="https://example.org/a",
            connector_version="0.1",
        )

        result = read_cached_fetch(
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            connector_version="0.1",
            max_age_days=30,
        )

        assert result is not None
        assert result.cached is True

    def test_legacy_provenance_file_with_no_observed_at_key_still_reads(self, tmp_path):
        """GIVEN a pre-migration sidecar file (written before `observed_at` existed,
        e.g. by an old fetch run) WHEN read THEN it still succeeds -- observed_at is
        None and its old connector-specific fields (e.g. officer_count) land in
        `extra`. Existing on-disk cache directories need no migration."""
        payload = b'[{"name": "SMITH, John"}]'
        (tmp_path / "a.bin").write_bytes(payload)
        (tmp_path / "a.provenance.json").write_text(
            json.dumps(
                {
                    "company_number": "12410514",
                    "source_url": "https://api.company-information.service.gov.uk/company/12410514/officers",
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "content_hash": f"sha256:{content_hash(payload)}",
                    "officer_count": 1,
                }
            )
        )

        result = read_cached_fetch(
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            connector_version="0.1",
        )

        assert result is not None
        assert result.provenance.observed_at is None
        assert result.extra["officer_count"] == 1
        assert result.extra["company_number"] == "12410514"

    def test_observed_at_round_trips_through_write_then_read(self, tmp_path):
        """GIVEN an explicit observed_at at write time WHEN read back THEN the exact
        same observed_at is returned -- the capture date survives a cache hit, not
        just the initial write."""
        captured = datetime(2020, 11, 30, 18, 37, 32, tzinfo=UTC)
        write_cached_fetch(
            b"payload",
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            source_url="https://example.org/a",
            connector_version="0.1",
            observed_at=captured,
        )

        result = read_cached_fetch(
            tmp_path / "a.bin",
            tmp_path / "a.provenance.json",
            source=_source(),
            connector_version="0.1",
        )

        assert result is not None
        assert result.provenance.observed_at == captured
