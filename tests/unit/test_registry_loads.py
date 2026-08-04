"""The source register loads and validates, and the OpenSanctions entry is correctly
marked non-redistributable + A2/uncleared (so the guardrails have a real case)."""

import pytest
from pydantic import ValidationError

from uncorrupt.core.provenance import Redistribution
from uncorrupt.core.tiers import DataClass
from uncorrupt.register.loader import all_sources, load_source
from uncorrupt.register.models import SourceEntry


def test_all_sources_valid():
    ids = {s.source_id for s in all_sources()}
    assert {"eu_ted", "gleif", "opensanctions"} <= ids


def test_opensanctions_is_gated():
    s = load_source("opensanctions")
    assert s.data_class is DataClass.A2
    assert s.redistribution is Redistribution.NON_COMMERCIAL
    assert s.dpia_cleared is False


def test_graph_connector_sources_are_present():
    """GIVEN the register directory WHEN all_sources() loads it THEN every graph
    (relationship-recovery) connector has a source_id, including the three wired to
    load_source() and the two documented ahead of their connector being wired."""
    ids = {s.source_id for s in all_sources()}
    assert {
        "gleif",
        "uk_ec_donations",
        "uk_lords_interests",
        "uk_parliament_interests",
        "uk_companies_house_officers",
    } <= ids


def test_gleif_declares_the_graph_connector_contract():
    """GIVEN sources/gleif.yml WHEN loaded THEN it declares connector_kind graph with
    the registry scheme, identifier field and rate limit the contract requires, and
    no locale (GLEIF is GLOBAL, not tied to one country)."""
    s = load_source("gleif")
    assert s.connector_kind == "graph"
    assert s.registry_schemes == ["GLEIF-LEI"]
    assert s.identifier_field == "lei"
    assert s.locale is None


def test_uk_ec_donations_declares_its_locale():
    """GIVEN sources/uk_ec_donations.yml WHEN loaded THEN it declares locale gb and
    the two registry schemes ec_donations.py emits."""
    s = load_source("uk_ec_donations")
    assert s.connector_kind == "graph"
    assert s.locale == "gb"
    assert s.registry_schemes == ["GB-COH", "EC-REGULATED-ENTITY"]


def test_graph_connector_missing_registry_schemes_is_rejected():
    """GIVEN a connector_kind 'graph' entry with no registry_schemes WHEN
    SourceEntry.model_validate runs THEN it raises ValidationError — a graph
    connector's contract has no optional fields."""
    payload = {
        "source_id": "incomplete_graph_source",
        "name": "Incomplete graph source",
        "jurisdictions": ["GB"],
        "data_class": "A1",
        "tier": "a",
        "license": "CC0 1.0",
        "redistribution": "open",
        "legal_basis": "test fixture",
        "access_method": "bulk-api",
        "freshness_sla_days": 7,
        "connector_kind": "graph",
        "locale": "gb",
        "identifier_field": "some_id",
        "rate_limit": "none",
        # registry_schemes omitted on purpose
    }

    with pytest.raises(ValidationError):
        SourceEntry.model_validate(payload)


def test_graph_connector_with_global_jurisdiction_does_not_require_locale():
    """GIVEN a connector_kind 'graph' entry with jurisdictions == ["GLOBAL"] and no
    locale WHEN SourceEntry.model_validate runs THEN it succeeds — GLEIF is the real
    case this protects (global reference data has no single locale)."""
    payload = {
        "source_id": "global_graph_source",
        "name": "Global graph source",
        "jurisdictions": ["GLOBAL"],
        "data_class": "A1",
        "tier": "a",
        "license": "CC0 1.0",
        "redistribution": "open",
        "legal_basis": "test fixture",
        "access_method": "open-data-dump",
        "freshness_sla_days": 7,
        "connector_kind": "graph",
        "registry_schemes": ["SOME-SCHEME"],
        "identifier_field": "some_id",
        "rate_limit": "none",
    }

    entry = SourceEntry.model_validate(payload)

    assert entry.locale is None
