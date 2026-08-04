"""Tests for the oversea-company branch cross-reference resolver.

Never touches the live network (`httpx.MockTransport` only) and never
touches the two control fixtures (NF002699/NF001553) reserved for the
sealed cohort -- all company numbers here are synthetic. See
`uncorrupt.staging.oversea_company_aliases` for what this resolves and why.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import uncorrupt.staging.oversea_company_aliases as oversea_module
from uncorrupt.core.errors import RegisterError
from uncorrupt.staging.aliases import CompanyAlias
from uncorrupt.staging.models import Company
from uncorrupt.staging.oversea_company_aliases import (
    API_KEY_ENV_VAR,
    SOURCE_OVERSEA_CROSS_REFERENCE,
    OverseaCompanyAliasReport,
    build_oversea_company_alias_table,
    fetch_oversea_company_cross_references,
    oversea_company_legacy_numbers,
)

RAW_PROFILE_WITH_CROSS_REFERENCE = {
    "company_number": "NF003690",
    "company_name": "A & A EXAMPLE LIMITED",  # not allowlisted -- must not survive to disk
    "type": "oversea-company",
    "jurisdiction": "united-kingdom",  # not allowlisted either
    "foreign_company_details": {
        "registration_number": "SC222690",
        "business_activity": "dormant",  # kept as-is inside the sub-object, harmless
    },
}


def _write_fetch_cache(
    tmp_path: Path,
    legacy_id: str,
    body: dict,
    retrieved_at: str = "2026-08-04T00:00:00+00:00",
    source_url: str | None = None,
) -> None:
    """Write a cache pair matching what `fetch_oversea_company_cross_references`
    itself would have written -- for tests of `build_oversea_company_alias_table`,
    which only ever reads from disk (no network, no `read_cached_fetch`
    hash-verification)."""
    (tmp_path / f"{legacy_id}.json").write_text(json.dumps(body))
    (tmp_path / f"{legacy_id}.provenance.json").write_text(
        json.dumps(
            {
                "legacy_company_number": legacy_id,
                "status": body.get("status"),
                "source_url": source_url or f"{oversea_module.CH_API_BASE}/company/{legacy_id}",
                "retrieved_at": retrieved_at,
                "content_hash": "sha256:deadbeef",
            }
        )
    )


class TestOverseaCompanyLegacyNumbers:
    @pytest.mark.django_db
    def test_only_nf_fc_sf_prefixed_numbers_are_selected(self):
        """GIVEN a mix of company_number prefixes WHEN the universe helper runs THEN
        only NF/FC/SF-prefixed numbers are returned -- an unrelated alpha prefix (SC,
        the ordinary Scottish register, not the oversea-company scheme) is excluded."""
        Company.objects.create(company_number="NF003690", company_name="NF CO")
        Company.objects.create(company_number="FC000071", company_name="FC CO")
        Company.objects.create(company_number="SF000001", company_name="SF CO")
        Company.objects.create(company_number="SC222690", company_name="ORDINARY SCOTTISH CO")
        Company.objects.create(company_number="00000001", company_name="ORDINARY CO")

        numbers = oversea_company_legacy_numbers()

        assert numbers == ["FC000071", "NF003690", "SF000001"]

    @pytest.mark.django_db
    def test_empty_staging_returns_empty_list(self):
        """GIVEN no Company rows at all WHEN the universe helper runs THEN it returns
        an empty list rather than raising."""
        assert oversea_company_legacy_numbers() == []


class TestFetchOverseaCompanyCrossReferences:
    def test_ok_response_is_cached_with_allowlisted_fields_only(self, tmp_path, monkeypatch):
        """GIVEN a 200 response with fields beyond the allowlist WHEN fetched THEN the
        cached body keeps only company_number/type/foreign_company_details -- company_name
        and jurisdiction never touch disk."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=RAW_PROFILE_WITH_CROSS_REFERENCE)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        counts = fetch_oversea_company_cross_references(
            ["NF003690"], tmp_path, client=client, polite_delay_seconds=0
        )

        assert counts == {"fetched": 1, "cached": 0, "not_found": 0, "failed": 0}
        cached = json.loads((tmp_path / "NF003690.json").read_text())
        assert cached["status"] == "ok"
        assert cached["type"] == "oversea-company"
        assert cached["foreign_company_details"]["registration_number"] == "SC222690"
        assert "company_name" not in cached
        assert "jurisdiction" not in cached

    def test_fetch_is_resumable_and_skips_cached_identifier(self, tmp_path, monkeypatch):
        """GIVEN an identifier already cached WHEN fetched again THEN no second HTTP
        request is made."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json=RAW_PROFILE_WITH_CROSS_REFERENCE)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        fetch_oversea_company_cross_references(
            ["NF003690"], tmp_path, client=client, polite_delay_seconds=0
        )
        assert call_count == 1

        counts = fetch_oversea_company_cross_references(
            ["NF003690"], tmp_path, client=client, polite_delay_seconds=0
        )
        assert call_count == 1
        assert counts == {"fetched": 0, "cached": 1, "not_found": 0, "failed": 0}

    def test_404_is_cached_as_typed_not_found_outcome(self, tmp_path, monkeypatch):
        """GIVEN CH returns 404 for a legacy identifier WHEN fetched THEN a cache file
        IS written recording a typed not_found status -- never silently dropped, never
        conflated with 'no cross-reference'."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        counts = fetch_oversea_company_cross_references(
            ["NF999999"], tmp_path, client=client, polite_delay_seconds=0
        )

        assert counts == {"fetched": 0, "cached": 0, "not_found": 1, "failed": 0}
        assert (tmp_path / "NF999999.json").exists()
        cached = json.loads((tmp_path / "NF999999.json").read_text())
        assert cached == {"status": "not_found"}

    def test_cached_not_found_is_recounted_on_resumed_run(self, tmp_path, monkeypatch):
        """GIVEN a previously-cached not_found outcome WHEN fetched again THEN it is
        served from cache (no new HTTP request) and still counted as not_found, not
        silently reclassified as resolved-nothing."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetch_oversea_company_cross_references(
            ["NF999999"], tmp_path, client=client, polite_delay_seconds=0
        )
        assert call_count == 1

        counts = fetch_oversea_company_cross_references(
            ["NF999999"], tmp_path, client=client, polite_delay_seconds=0
        )
        assert call_count == 1
        assert counts["not_found"] == 1
        assert counts["cached"] == 1

    def test_429_is_retried_then_succeeds(self, tmp_path, monkeypatch):
        """GIVEN CH returns 429 once then 200 WHEN fetched THEN the request is retried
        (not treated as a failure) and the eventual success is cached normally."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        monkeypatch.setattr(oversea_module.time, "sleep", lambda *_a, **_k: None)
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json=RAW_PROFILE_WITH_CROSS_REFERENCE)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        counts = fetch_oversea_company_cross_references(
            ["NF003690"], tmp_path, client=client, polite_delay_seconds=0
        )

        assert call_count == 2
        assert counts == {"fetched": 1, "cached": 0, "not_found": 0, "failed": 0}

    def test_exhausted_retries_write_no_cache_file(self, tmp_path, monkeypatch):
        """GIVEN CH returns 500 on every attempt WHEN retries are exhausted THEN the
        identifier is counted as failed and NO cache file is written -- so it is
        retried (not silently marked done) on the next invocation."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        monkeypatch.setattr(oversea_module.time, "sleep", lambda *_a, **_k: None)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        counts = fetch_oversea_company_cross_references(
            ["NF999998"],
            tmp_path,
            client=client,
            polite_delay_seconds=0,
            max_retries=2,
        )

        assert counts == {"fetched": 0, "cached": 0, "not_found": 0, "failed": 1}
        assert not (tmp_path / "NF999998.json").exists()
        assert not (tmp_path / "NF999998.provenance.json").exists()

    def test_missing_api_key_raises_clear_error(self, tmp_path, monkeypatch):
        """GIVEN no COMPANIES_HOUSE_API_KEY set WHEN fetching THEN a clear RuntimeError
        is raised before any HTTP request is attempted."""
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)

        with pytest.raises(RuntimeError, match=API_KEY_ENV_VAR):
            fetch_oversea_company_cross_references(["NF003690"], tmp_path)

    def test_fetch_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/uk_companies_house_oversea_company.yml cannot be resolved
        WHEN fetching THEN it raises RegisterError before any HTTP request is made."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        monkeypatch.setattr(oversea_module, "SOURCE_ID", "does_not_exist_xyz")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make an HTTP request")

        client = httpx.Client(transport=httpx.MockTransport(handler))

        with pytest.raises(RegisterError):
            fetch_oversea_company_cross_references(["NF003690"], tmp_path, client=client)


class TestBuildOverseaCompanyAliasTable:
    def test_oversea_record_with_cross_reference_yields_an_alias(self, tmp_path):
        """GIVEN an oversea-company record carrying a foreign_company_details.
        registration_number WHEN the alias table is built THEN it resolves to that
        live company number."""
        _write_fetch_cache(
            tmp_path,
            "NF003690",
            {
                "status": "ok",
                "company_number": "NF003690",
                "type": "oversea-company",
                "foreign_company_details": {"registration_number": "SC222690"},
            },
        )

        aliases, report = build_oversea_company_alias_table(["NF003690"], tmp_path)

        assert len(aliases) == 1
        assert aliases[0].alias_name == "NF003690"
        assert aliases[0].live_company_number == "SC222690"
        assert aliases[0].alias_kind == "legacy_identifier"
        assert report.resolved == 1
        assert report.aliases_written == 1

    def test_oversea_record_without_cross_reference_yields_no_alias(self, tmp_path):
        """GIVEN an oversea-company record with no registration_number at all WHEN the
        alias table is built THEN no alias is produced -- reported, never guessed."""
        _write_fetch_cache(
            tmp_path,
            "NF000001",
            {
                "status": "ok",
                "company_number": "NF000001",
                "type": "oversea-company",
                "foreign_company_details": {},
            },
        )

        aliases, report = build_oversea_company_alias_table(["NF000001"], tmp_path)

        assert aliases == []
        assert report.no_cross_reference == 1
        assert report.aliases_written == 0

    def test_missing_foreign_company_details_yields_no_alias(self, tmp_path):
        """GIVEN an oversea-company record where the foreign_company_details object
        itself is entirely absent WHEN built THEN it is treated identically to an
        empty one -- no alias, counted as no_cross_reference."""
        _write_fetch_cache(
            tmp_path,
            "NF000002",
            {"status": "ok", "company_number": "NF000002", "type": "oversea-company"},
        )

        aliases, report = build_oversea_company_alias_table(["NF000002"], tmp_path)

        assert aliases == []
        assert report.no_cross_reference == 1

    def test_not_found_record_yields_no_alias_and_is_counted(self, tmp_path):
        """GIVEN a legacy identifier CH returned 404 for WHEN built THEN no alias is
        produced and it is counted under not_found, distinct from no_cross_reference."""
        _write_fetch_cache(tmp_path, "NF999999", {"status": "not_found"})

        aliases, report = build_oversea_company_alias_table(["NF999999"], tmp_path)

        assert aliases == []
        assert report.not_found == 1
        assert report.no_cross_reference == 0

    def test_non_oversea_company_type_yields_no_alias_and_is_counted(self, tmp_path):
        """GIVEN a cached profile whose type is NOT oversea-company (defensive: the
        universe selects by prefix, but the type is verified, never assumed) WHEN
        built THEN no alias is produced and it is counted separately."""
        _write_fetch_cache(
            tmp_path,
            "NF000003",
            {
                "status": "ok",
                "company_number": "NF000003",
                "type": "ltd",
                "foreign_company_details": {"registration_number": "SC000001"},
            },
        )

        aliases, report = build_oversea_company_alias_table(["NF000003"], tmp_path)

        assert aliases == []
        assert report.not_oversea_company == 1

    def test_self_referential_target_yields_no_alias(self, tmp_path):
        """GIVEN a registration_number that normalises to the SAME identifier as the
        legacy number itself WHEN built THEN it is dropped as degenerate -- a company
        cannot be its own live cross-reference."""
        _write_fetch_cache(
            tmp_path,
            "NF003690",
            {
                "status": "ok",
                "company_number": "NF003690",
                "type": "oversea-company",
                "foreign_company_details": {"registration_number": "NF003690"},
            },
        )

        aliases, report = build_oversea_company_alias_table(["NF003690"], tmp_path)

        assert aliases == []
        assert report.self_referential == 1

    def test_unfetched_identifier_is_counted_and_not_treated_as_resolved(self, tmp_path):
        """GIVEN a legacy identifier with no cache file at all (never fetched, or a
        prior fetch exhausted its retries) WHEN built THEN it contributes no alias
        and is counted as unfetched -- distinct from every outcome that required a
        completed fetch."""
        aliases, report = build_oversea_company_alias_table(["NF000004"], tmp_path)

        assert aliases == []
        assert report.unfetched == 1
        assert report.legacy_ids_considered == 1

    def test_builder_is_deterministic_across_runs(self, tmp_path):
        """GIVEN a fixed cache directory WHEN the alias table is built twice THEN the
        two results are exactly equal, including order."""
        _write_fetch_cache(
            tmp_path,
            "NF003690",
            {
                "status": "ok",
                "company_number": "NF003690",
                "type": "oversea-company",
                "foreign_company_details": {"registration_number": "SC222690"},
            },
        )
        _write_fetch_cache(
            tmp_path,
            "FC000071",
            {
                "status": "ok",
                "company_number": "FC000071",
                "type": "oversea-company",
                "foreign_company_details": {"registration_number": "00000010"},
            },
        )

        legacy_numbers = ["NF003690", "FC000071"]
        aliases_first, report_first = build_oversea_company_alias_table(legacy_numbers, tmp_path)
        aliases_second, report_second = build_oversea_company_alias_table(legacy_numbers, tmp_path)

        assert aliases_first == aliases_second
        assert report_first == report_second

    def test_alias_row_carries_source_and_retrieval_date_from_provenance(self, tmp_path):
        """GIVEN a cached fetch with a specific retrieved_at WHEN built THEN the
        resulting alias row carries that exact source/source_url/retrieved_at --
        auditable per row, read from the real per-identifier fetch time, not a
        single date for the whole run."""
        _write_fetch_cache(
            tmp_path,
            "NF003690",
            {
                "status": "ok",
                "company_number": "NF003690",
                "type": "oversea-company",
                "foreign_company_details": {"registration_number": "SC222690"},
            },
            retrieved_at="2026-08-04T12:34:56+00:00",
        )

        aliases, _ = build_oversea_company_alias_table(["NF003690"], tmp_path)

        assert len(aliases) == 1
        row = aliases[0]
        assert row.source == SOURCE_OVERSEA_CROSS_REFERENCE
        assert row.retrieved_at == "2026-08-04T12:34:56+00:00"
        assert row.source_url == f"{oversea_module.CH_API_BASE}/company/NF003690"

    def test_report_is_a_frozen_dataclass_with_all_buckets_summing_correctly(self, tmp_path):
        """GIVEN a mixed batch (resolved, no cross-reference, not found, unfetched)
        WHEN built THEN every legacy id lands in exactly one bucket and the buckets
        sum to legacy_ids_considered."""
        _write_fetch_cache(
            tmp_path,
            "NF000010",
            {
                "status": "ok",
                "company_number": "NF000010",
                "type": "oversea-company",
                "foreign_company_details": {"registration_number": "SC000010"},
            },
        )
        _write_fetch_cache(
            tmp_path,
            "NF000011",
            {
                "status": "ok",
                "company_number": "NF000011",
                "type": "oversea-company",
                "foreign_company_details": {},
            },
        )
        _write_fetch_cache(tmp_path, "NF000012", {"status": "not_found"})

        legacy_numbers = ["NF000010", "NF000011", "NF000012", "NF000013"]
        aliases, report = build_oversea_company_alias_table(legacy_numbers, tmp_path)

        assert isinstance(report, OverseaCompanyAliasReport)
        assert report.legacy_ids_considered == 4
        assert (
            report.resolved
            + report.no_cross_reference
            + report.not_oversea_company
            + report.not_found
            + report.self_referential
            + report.unfetched
        ) == 4
        assert len(aliases) == report.aliases_written == 1


class TestAliasIndexResolvesLegacyIdentifiers:
    """End-to-end: fetch cache -> build -> AliasIndex.resolve_identifier."""

    def test_resolve_identifier_after_build_returns_live_number(self, tmp_path):
        """GIVEN a built oversea-company alias table WHEN loaded into an AliasIndex
        THEN resolve_identifier resolves the legacy branch number to its live number."""
        _write_fetch_cache(
            tmp_path,
            "NF003690",
            {
                "status": "ok",
                "company_number": "NF003690",
                "type": "oversea-company",
                "foreign_company_details": {"registration_number": "SC222690"},
            },
        )
        aliases, _ = build_oversea_company_alias_table(["NF003690"], tmp_path)

        from uncorrupt.staging.aliases import AliasIndex

        index = AliasIndex(aliases)

        assert index.resolve_identifier("NF003690") == "SC222690"
        assert index.resolve_identifier("nf003690") == "SC222690"

    def test_unresolved_legacy_identifier_returns_none(self, tmp_path):
        """GIVEN an index built with no matching alias WHEN resolve_identifier is
        called for an unknown legacy id THEN it returns None, not a guess."""
        aliases, _ = build_oversea_company_alias_table([], tmp_path)

        from uncorrupt.staging.aliases import AliasIndex

        index = AliasIndex(aliases)

        assert index.resolve_identifier("NF999999") is None


def test_company_alias_default_alias_kind_is_former_name():
    """GIVEN a CompanyAlias constructed without alias_kind (as aliases.py's own
    build_alias_table already does) WHEN inspected THEN it defaults to
    'former_name' -- existing former-name rows are unaffected by this module."""
    alias = CompanyAlias(
        alias_name="OLD NAME LIMITED",
        normalised_alias_name="OLD NAME LIMITED",
        live_company_number="00000001",
        name_changed_on=None,
        source="x",
        source_url="y",
        retrieved_at="2026-01-01",
    )
    assert alias.alias_kind == "former_name"
