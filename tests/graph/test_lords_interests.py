"""Tests for the UK House of Lords Register of Interests ingest (Phase 1.5).

Verifies the core invariants:
- HTML parsing extracts member ID, name, party, and interest entries
- Counterparty resolution: company name match → confidence 0.9; no match → 0.5
- Ambiguous counterparty name (2+ companies) never guesses — no edge
- "(interest ceased ...)" text parses to valid_to
- observed_at is set from the Wayback snapshot provenance
- Attestation carries the source citation, not Edge
- Nil returns (no registrable interests) are counted, not errored
- fetch_lords_register/ingest_lords_register refuse to run without a
  sources/uk_lords_interests.yml register entry
"""

from pathlib import Path

import httpx
import pytest

import uncorrupt.graph.lords_interests as lords_interests_module
from uncorrupt.core.errors import RegisterError
from uncorrupt.graph.lords_interests import (
    _extract_counterparty,
    _parse_ceased_date,
    _parse_lords_page,
    fetch_lords_register,
    ingest_lords_register,
)
from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.staging.models import Company

# A minimal Lords register HTML page with 2 members
SAMPLE_HTML = """\
<html><body>
<div class="results">
  <div class="card-expandable">
    <a class="card card-member" href="/member/3898/contact">
      <div class="card-inner"><div class="content">
        <div class="primary-info">Lord Aberdare</div>
        <div class="secondary-info">Crossbench</div>
        <div class="secondary-info">Excepted Hereditary</div>
      </div></div>
    </a>
    <div class="expand-area"><div class="expand-area-content">
      <div class="card card-child">
        <div class="card-inner"><div class="content">
          <div class="primary-info">Category 10: Non-financial interests (a)</div>
          <ul><li>Director, F.C.M. Limited (recording rights)</li></ul>
        </div></div>
      </div>
      <div class="card card-child">
        <div class="card-inner"><div class="content">
          <div class="primary-info">Category 1: Directorships</div>
          <ul><li>Chairman, Microlink PC (UK) Ltd (computing and software)</li></ul>
        </div></div>
      </div>
    </div></div>
  </div>

  <div class="card-expandable">
    <a class="card card-member" href="/member/631/contact">
      <div class="card-inner"><div class="content">
        <div class="primary-info">Baroness Adams of Craigielea</div>
        <div class="secondary-info">Labour</div>
        <div class="secondary-info">Life peer</div>
      </div></div>
    </a>
    <div class="expand-area"><div class="expand-area-content">
      <div class="card card-child">
        <div class="card-inner"><div class="content">
          <div>Nil</div>
          <ul><li>No registrable interests</li></ul>
        </div></div>
      </div>
    </div></div>
  </div>
</div>
</body></html>
"""


SAMPLE_HTML_WITH_CEASED = """\
<html><body>
<div class="results">
  <div class="card-expandable">
    <a class="card card-member" href="/member/4304/contact">
      <div class="card-inner"><div class="content">
        <div class="primary-info">Lord Allen of Kensington</div>
        <div class="secondary-info">Labour</div>
        <div class="secondary-info">Life peer</div>
      </div></div>
    </a>
    <div class="expand-area"><div class="expand-area-content">
      <div class="card card-child">
        <div class="card-inner"><div class="content">
          <div class="primary-info">Category 1: Directorships</div>
          <ul>
            <li>Chair, British Horseracing Authority (interest ceased 3 March 2026)</li>
          </ul>
        </div></div>
      </div>
    </div></div>
  </div>
</div>
</body></html>
"""


SAMPLE_HTML_SHAREHOLDING = """\
<html><body>
<div class="results">
  <div class="card-expandable">
    <a class="card card-member" href="/member/4330/contact">
      <div class="card-inner"><div class="content">
        <div class="primary-info">Baroness Chisholm of Owlpen</div>
        <div class="secondary-info">Non-affiliated</div>
        <div class="secondary-info">Life peer</div>
      </div></div>
    </a>
    <div class="expand-area"><div class="expand-area-content">
      <div class="card card-child">
        <div class="card-inner"><div class="content">
          <div class="primary-info">Category 2: Shareholdings etc. (b)</div>
          <ul><li>Halma (safety equipment)</li></ul>
        </div></div>
      </div>
    </div></div>
  </div>
</div>
</body></html>
"""


def _write_page(html_dir: Path, page_num: int, content: str) -> Path:
    path = html_dir / f"page_{page_num:02d}.html"
    path.write_text(content, encoding="utf-8")
    return path


def _write_provenance(
    html_dir: Path,
    content_hash: str = "abc123",
    wayback_timestamp: str | None = "20201130",
) -> Path:
    import json
    from datetime import UTC, datetime

    prov = {
        "source": "UK House of Lords Register of Interests",
        "source_url": "https://web.archive.org/web/20201130/https://members.parliament.uk/...",
        "wayback_timestamp": wayback_timestamp,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "content_hash": content_hash,
        "page_count": 1,
        "total_entries": 2,
        "pages": [],
    }
    path = html_dir / "provenance.json"
    path.write_text(json.dumps(prov, indent=2), encoding="utf-8")
    return path


@pytest.mark.django_db
class TestLordsInterestsParsing:
    def test_parse_page_extracts_members(self):
        """HTML parsing extracts member ID, name, party, and interests."""
        members = _parse_lords_page(SAMPLE_HTML)
        assert len(members) == 2
        assert members[0]["member_id"] == "3898"
        assert members[0]["name"] == "Lord Aberdare"
        assert members[0]["party"] == "Crossbench"
        assert len(members[0]["interests"]) == 2
        assert "Non-financial interests" in members[0]["interests"][0]["category"]
        assert "Directorships" in members[0]["interests"][1]["category"]

    def test_parse_page_nil_return(self):
        """A member with no registrable interests returns empty interests list."""
        members = _parse_lords_page(SAMPLE_HTML)
        assert members[1]["member_id"] == "631"
        assert members[1]["interests"] == []

    def test_ceased_date_parsed(self):
        """'(interest ceased 3 March 2026)' is parsed to '2026-03-03'."""
        result = _parse_ceased_date("Chair, BHA (interest ceased 3 March 2026)")
        assert result == "2026-03-03"

    def test_no_ceased_date_returns_none(self):
        """Text without 'interest ceased' returns None."""
        assert _parse_ceased_date("Director, Some Ltd") is None

    def test_extract_counterpany_from_role_comma_org(self):
        """'Chairman, Microlink PC (UK) Ltd (computing and software)' extracts org name."""
        name, company_number, is_private = _extract_counterparty(
            "Chairman, Microlink PC (UK) Ltd (computing and software)"
        )
        assert name == "Microlink PC (UK) Ltd"
        assert company_number is None
        assert is_private is False

    def test_extract_counterparty_no_comma_with_ltd(self):
        """'Sharetego (travel company)' — no org markers, returns None (conservative)."""
        name, _, is_private = _extract_counterparty("Sharetego (travel company)")
        # "Sharetego" has no Ltd/Trust/etc. marker — conservative return is None
        assert name is None
        assert is_private is False

    def test_extract_counterparty_no_org_marker(self):
        """Text without org markers returns None (conservative)."""
        name, _, _ = _extract_counterparty("Occasional speaker for events")
        assert name is None

    def test_extract_counterparty_strips_all_trailing_parens(self):
        """Two trailing parentheticals are both stripped, not just the last.

        Real snapshot text: stripping only the last "(...)" (the pre-2026-08
        behaviour) left "(trading as Afiniti)" glued onto the name.
        """
        name, _, is_private = _extract_counterparty(
            "SATMAP Inc (trading as Afiniti) (communications technology)",
            "Category 2: Shareholdings etc. (b)",
        )
        assert name == "SATMAP Inc"
        assert is_private is False

    def test_extract_counterparty_comma_inside_parenthetical_not_a_role_split(self):
        """A comma inside the trailing parenthetical is not treated as a role separator.

        Real snapshot text: "Unilever plc (nutrition, hygiene and personal
        care products)" — splitting on the first comma in the raw
        description (the pre-2026-08 behaviour) cut into the parenthetical
        and lost "Unilever plc" entirely, even though it carries an
        explicit "plc" marker.
        """
        name, _, is_private = _extract_counterparty(
            "Unilever plc (nutrition, hygiene and personal care products)"
        )
        assert name == "Unilever plc"
        assert is_private is False

    def test_extract_counterparty_comma_before_bare_suffix_is_not_a_role_split(self):
        """A comma immediately followed by only a legal suffix belongs to the org's own name.

        Real snapshot text: "Automatic Data Processing, Inc. (...)" — the
        pre-2026-08 behaviour split on that comma and returned "Inc." alone
        as the counterparty name.
        """
        name, _, is_private = _extract_counterparty(
            "Automatic Data Processing, Inc. (human resources data management)",
            "Category 2: Shareholdings etc. (b)",
        )
        assert name == "Automatic Data Processing, Inc."
        assert is_private is False

    def test_extract_counterparty_distinct_companies_do_not_collapse(self):
        """Two distinct companies sharing a description tail extract to distinct names.

        Real snapshot text: five different "Dawn ... Holdings Ltd" entries
        all ended "(co-investment with venture capital firm, printing)".
        The pre-2026-08 code split on the comma INSIDE that parenthetical
        and returned the shared tail ("printing)") as the name for all
        five — silently merging five distinct companies onto one
        fabricated placeholder.
        """
        name_1, _, _ = _extract_counterparty(
            "Dawn OP Holdings Ltd (co-investment with venture capital firm, printing)",
            "Category 2: Shareholdings etc. (b)",
        )
        name_2, _, _ = _extract_counterparty(
            "Dawn OP Holdings II Ltd (co-investment with venture capital firm, printing)",
            "Category 2: Shareholdings etc. (b)",
        )
        assert name_1 == "Dawn OP Holdings Ltd"
        assert name_2 == "Dawn OP Holdings II Ltd"
        assert name_1 != name_2

    def test_extract_counterparty_shareholding_category_widens_bare_name_fallback(self):
        """A Category 2 (Shareholdings) entry with no legal-form marker still extracts.

        Real snapshot text: "Halma (safety equipment)" — a Shareholdings
        entry, by the register's own category definition, names a body
        corporate (you cannot hold "shares" in a person), so the bare
        trading name is accepted when category context is supplied.
        """
        name, _, is_private = _extract_counterparty(
            "Halma (safety equipment)", "Category 2: Shareholdings etc. (b)"
        )
        assert name == "Halma"
        assert is_private is False

    def test_extract_counterparty_without_category_context_stays_conservative(self):
        """The same bare-name text with NO category context still returns None.

        The Category 2 fallback only widens extraction when the caller
        supplies category — it never applies blindly, so a caller with no
        register-structure context gets the same conservative behaviour as
        before.
        """
        name, _, _ = _extract_counterparty("Halma (safety equipment)")
        assert name is None

    def test_extract_counterparty_shareholding_prose_still_skipped(self):
        """An ambiguous, prose Category 2 entry is skipped even with category context.

        Real snapshot text — a redaction notice, not an organisation name:
        "The member is a shareholder with a controlling interest in a
        property management company; full details are held by the
        Registrar of Lords' Interests". Widening the Shareholdings fallback
        must never promote a sentence fragment into a fabricated
        counterparty.
        """
        name, _, is_private = _extract_counterparty(
            "The member is a shareholder with a controlling interest in a property "
            "management company; full details are held by the Registrar of "
            "Lords’ Interests",
            "Category 2: Shareholdings etc. (a)",
        )
        assert name is None
        assert is_private is False

    def test_extract_counterparty_date_after_comma_is_not_the_counterparty(self):
        """A trailing date after a comma is never mistaken for the organisation name.

        Real snapshot text: "Fee received from Philip Morris International
        (tobacco company) for contributing to report on better regulation
        to be submitted to the European Commission, June 2026" — the
        pre-2026-08 code split on that final comma and returned the literal
        string "June 2026" as the counterparty name.
        """
        name, _, is_private = _extract_counterparty(
            "Fee received from Philip Morris International (tobacco company) for "
            "contributing to report on better regulation to be submitted to the "
            "European Commission, June 2026",
            "Category 1: Remunerated employment etc.",
        )
        assert name is None
        assert is_private is False

    def test_extract_counterparty_marker_inside_stripped_parenthetical_not_promoted(self):
        """An org marker inside the trailing parenthetical does not promote the property text.

        Real snapshot text: "Farmland including residential properties in
        South Yorkshire (managed by LGK Farms LLP)" — the pre-2026-08 code
        searched the FULL description (including the parenthetical about to
        be stripped) for a marker, found "LLP" inside "(managed by LGK
        Farms LLP)", and then returned the PROPERTY DESCRIPTION as the
        counterparty name — the real organisation the marker came from was
        discarded. This is Category 3 (Land and property): the interest is
        the property itself, not an organisation, so the honest result is
        no counterparty at all.
        """
        name, _, is_private = _extract_counterparty(
            "Farmland including residential properties in South Yorkshire "
            "(managed by LGK Farms LLP)",
            "Category 3: Land and property",
        )
        assert name is None
        assert is_private is False

    def test_extract_counterparty_family_role_marker_is_private_individual(self):
        """A role naming a family member is scoped out as a private individual, never an entity.

        ADR-004 D1: no private-individual profiling. This applies
        regardless of category context — the Shareholdings fallback must
        never override the family-marker exclusion.
        """
        name, _, is_private = _extract_counterparty(
            "Gift from a family member, Acme Family Trust Ltd",
            "Category 6: Gifts, benefits and hospitality",
        )
        assert name is None
        assert is_private is True


@pytest.mark.django_db
class TestLordsInterestsIngest:
    def test_ingest_creates_edge_and_attestation(self, tmp_path):
        """A company-name counterparty creates an Edge + Attestation."""
        from uncorrupt.staging.companies_house import _normalise_name

        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        summary = ingest_lords_register(tmp_path)

        assert summary["total_members"] == 2
        assert summary["matched"] == 2
        assert summary["nil_returns"] == 1

        # Check edge exists
        lord = Entity.objects.get(registry_id="3898")
        edges = Edge.objects.filter(source_entity=lord, edge_type="declared_interest")
        assert edges.count() == 2

        # Check attestation
        attestations = Attestation.objects.filter(edge__in=edges)
        assert attestations.count() == 2
        att = attestations.first()
        assert att.source_name == "UK House of Lords Register of Interests"
        assert att.observed_at is not None
        assert att.snapshot_ref is not None

    def test_ingest_company_name_match_confidence_09(self, tmp_path):
        """A unique exact name match gives match_confidence=0.9."""
        from uncorrupt.staging.companies_house import _normalise_name

        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        ingest_lords_register(tmp_path)

        lord = Entity.objects.get(registry_id="3898")
        # Find the edge for Microlink
        microlink = Entity.objects.get(name="Microlink PC (UK) Ltd")
        edge = Edge.objects.get(source_entity=lord, target_entity=microlink)
        att = edge.attestations.first()
        assert att.match_confidence == 0.9
        assert att.match_method == "exact_name"

    def test_ingest_unresolved_counterparty_confidence_05(self, tmp_path):
        """An unresolved counterparty gets confidence 0.5, name_only method."""
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        ingest_lords_register(tmp_path)

        lord = Entity.objects.get(registry_id="3898")
        # F.C.M. Limited won't match any Company
        fcm = Entity.objects.filter(name="F.C.M. Limited")
        assert fcm.exists()
        entity = fcm.first()
        assert entity.registry_scheme == "UK-LORDS-UNRESOLVED"
        edge = Edge.objects.filter(source_entity=lord, target_entity=entity).first()
        assert edge is not None
        att = edge.attestations.first()
        assert att.match_confidence == 0.5
        assert att.match_method == "name_only"

    def test_ingest_passes_category_to_extraction_for_bare_shareholding_name(self, tmp_path):
        """A Category 2 entry with no legal-form marker still creates an Edge end-to-end.

        Regression test: ``ingest_lords_register`` must pass the interest's
        own category through to ``_extract_counterparty`` — without it,
        "Halma (safety equipment)" (a real bare trading name from the
        snapshot) has no legal-form marker and is silently dropped as
        skipped_no_counterparty, even though a Category 2 entry always
        names a body corporate.
        """
        _write_page(tmp_path, 1, SAMPLE_HTML_SHAREHOLDING)
        _write_provenance(tmp_path)

        summary = ingest_lords_register(tmp_path)

        assert summary["matched"] == 1
        assert summary["skipped_no_counterparty"] == 0
        lord = Entity.objects.get(registry_id="4330")
        halma = Entity.objects.get(name="Halma")
        edge = Edge.objects.filter(source_entity=lord, target_entity=halma).first()
        assert edge is not None

    def test_ingest_ambiguous_name_no_edge(self, tmp_path):
        """2+ companies with same normalised name → no edge, counted as unmatched."""
        from uncorrupt.staging.companies_house import _normalise_name

        norm = _normalise_name("Microlink PC (UK) Ltd")
        Company.objects.create(
            company_number="11111111",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=norm,
        )
        Company.objects.create(
            company_number="22222222",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=norm,
        )
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        summary = ingest_lords_register(tmp_path)

        # The Microlink entry should be unmatched (ambiguous)
        assert summary["unmatched_counterparty"] >= 1

    def test_ingest_ceased_date_becomes_valid_to(self, tmp_path):
        """'(interest ceased 3 March 2026)' sets Edge.valid_to."""
        Company.objects.create(
            company_number="99999999",
            company_name="British Horseracing Authority",
            normalised_name="british horseracing authority",
        )
        _write_page(tmp_path, 1, SAMPLE_HTML_WITH_CEASED)
        _write_provenance(tmp_path)

        ingest_lords_register(tmp_path)

        lord = Entity.objects.get(registry_id="4304")
        edges = Edge.objects.filter(source_entity=lord)
        assert edges.count() == 1
        edge = edges.first()
        assert str(edge.valid_to) == "2026-03-03"

    def test_ingest_valid_from_is_null(self, tmp_path):
        """Lords register has no registration dates — valid_from is always null."""
        from uncorrupt.staging.companies_house import _normalise_name

        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        ingest_lords_register(tmp_path)

        lord = Entity.objects.get(registry_id="3898")
        for edge in Edge.objects.filter(source_entity=lord):
            assert edge.valid_from is None

    def test_ingest_lord_entity_has_correct_role(self, tmp_path):
        """Lord entities have role_description='Member of the House of Lords'."""
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        ingest_lords_register(tmp_path)

        lord = Entity.objects.get(registry_id="3898")
        assert lord.role_description == "Member of the House of Lords"
        assert lord.entity_type == "person"
        assert lord.properties["party"] == "Crossbench"

    def test_ingest_nil_return_counted(self, tmp_path):
        """A nil return (no registrable interests) is counted, not errored."""
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        summary = ingest_lords_register(tmp_path)

        assert summary["nil_returns"] == 1
        assert summary["total_members"] == 2

    def test_ingest_reingest_is_idempotent(self, tmp_path):
        """Reingesting the same data doesn't create duplicate edges/attestations."""
        from uncorrupt.staging.companies_house import _normalise_name

        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        ingest_lords_register(tmp_path)
        ingest_lords_register(tmp_path)

        # No new edges created on reingest
        lord = Entity.objects.get(registry_id="3898")
        assert Edge.objects.filter(source_entity=lord).count() == 2
        # Attestations are also not duplicated
        total_attestations = Attestation.objects.filter(edge__source_entity=lord).count()
        assert total_attestations == 2

    def test_wayback_attestation_observed_at_is_capture_date_not_today(self, tmp_path):
        """A Wayback-sourced attestation's observed_at is the capture date, not download time.

        Regression test: observed_at was previously always set from
        provenance['retrieved_at'] (today's download time), even for a
        Wayback capture of a historical register edition — destroying the
        very evidence pre-award snapshots exist to provide.
        """
        from datetime import UTC as dt_UTC
        from datetime import datetime

        from uncorrupt.staging.companies_house import _normalise_name

        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path, wayback_timestamp="20201130")

        ingest_lords_register(tmp_path)

        lord = Entity.objects.get(registry_id="3898")
        microlink = Entity.objects.get(name="Microlink PC (UK) Ltd")
        edge = Edge.objects.get(source_entity=lord, target_entity=microlink)
        att = edge.attestations.first()
        assert att.observed_at == datetime(2020, 11, 30, tzinfo=dt_UTC)
        assert att.observed_at.date() != datetime.now(dt_UTC).date()

    def test_two_wayback_snapshots_of_same_interest_create_two_attestations(self, tmp_path):
        """Two different Wayback snapshots of the same interest yield 2 Attestations, not 1.

        Regression test: Attestation.source_reference was previously keyed
        only on interest_key (no snapshot identity), so re-ingesting a
        second historical snapshot of a still-registered interest collapsed
        onto the first attestation instead of recording separate evidence.
        """
        from uncorrupt.staging.companies_house import _normalise_name

        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )

        snapshot_1 = tmp_path / "snap1"
        snapshot_1.mkdir()
        _write_page(snapshot_1, 1, SAMPLE_HTML)
        _write_provenance(snapshot_1, content_hash="hash1", wayback_timestamp="20201130")
        ingest_lords_register(snapshot_1)

        snapshot_2 = tmp_path / "snap2"
        snapshot_2.mkdir()
        _write_page(snapshot_2, 1, SAMPLE_HTML)
        _write_provenance(snapshot_2, content_hash="hash2", wayback_timestamp="20220615")
        ingest_lords_register(snapshot_2)

        lord = Entity.objects.get(registry_id="3898")
        microlink = Entity.objects.get(name="Microlink PC (UK) Ltd")
        # Same claim -> one Edge, not duplicated across snapshots
        assert Edge.objects.filter(source_entity=lord, target_entity=microlink).count() == 1
        edge = Edge.objects.get(source_entity=lord, target_entity=microlink)
        assert edge.attestations.count() == 2
        observed_dates = sorted(a.observed_at.date().isoformat() for a in edge.attestations.all())
        assert observed_dates == ["2020-11-30", "2022-06-15"]

    def test_company_number_with_gleif_and_coh_entities_resolves_to_coh(self, tmp_path):
        """A company_number shared by GB-COH and GLEIF-LEI Entities resolves to GB-COH.

        Regression test: Entity.objects.get_or_create(entity_type="company",
        company_number=...) without registry_scheme raised
        MultipleObjectsReturned whenever GLEIF held a separate Entity for
        the same company (ADR-006: duplicate over merge, never collapsed).
        """
        from uncorrupt.staging.companies_house import _normalise_name

        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        Entity.objects.create(
            entity_type="company",
            registry_scheme="GB-COH",
            registry_id="01234567",
            name="Microlink PC (UK) Ltd",
            company_number="01234567",
        )
        Entity.objects.create(
            entity_type="company",
            registry_scheme="GLEIF-LEI",
            registry_id="529900ABCDEF1234567",
            name="Microlink PC (UK) Ltd",
            company_number="01234567",
        )
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        summary = ingest_lords_register(tmp_path)

        assert summary["ambiguous_company_number"] == 0
        coh_entity = Entity.objects.get(registry_scheme="GB-COH", registry_id="01234567")
        lord = Entity.objects.get(registry_id="3898")
        assert Edge.objects.filter(source_entity=lord, target_entity=coh_entity).exists()
        # The GLEIF entity must still exist untouched — never merged
        assert Entity.objects.filter(
            registry_scheme="GLEIF-LEI", registry_id="529900ABCDEF1234567"
        ).exists()

    def test_ambiguous_company_number_counter_increments_run_continues(self, tmp_path, monkeypatch):
        """A MultipleObjectsReturned during resolution is counted, not fatal to the run.

        Simulates the defensive catch around per-interest resolution: even
        if resolution raises Entity.MultipleObjectsReturned, the ingest
        counts it and keeps processing rather than losing the whole run to
        one bad row (81 real hits killed one prior ingest entirely).
        """
        import uncorrupt.graph.lords_interests as lords_interests_module
        from uncorrupt.staging.companies_house import _normalise_name

        Company.objects.create(
            company_number="01234567",
            company_name="Microlink PC (UK) Ltd",
            normalised_name=_normalise_name("Microlink PC (UK) Ltd"),
        )
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        def _raise_ambiguous(*args, **kwargs):
            raise Entity.MultipleObjectsReturned("simulated ambiguity")

        monkeypatch.setattr(lords_interests_module, "_resolve_counterparty", _raise_ambiguous)

        summary = ingest_lords_register(tmp_path)

        assert summary["ambiguous_company_number"] == 2
        assert summary["matched"] == 0
        assert summary["total_members"] == 2


class TestLordsInterestsFetchProvenance:
    def test_wayback_fetch_records_observed_at_as_the_capture_date(self, tmp_path):
        """GIVEN a wayback_timestamp WHEN fetch_lords_register runs THEN the
        provenance sidecar's observed_at is the parsed capture date -- distinct from
        retrieved_at (today's download time). This is the fetch-side half of the
        regression covered at ingest time by
        test_wayback_attestation_observed_at_is_capture_date_not_today: the capture
        date is now computed and recorded once, at write time, rather than only
        ever re-derived at ingest time."""
        import json

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, text=SAMPLE_HTML)
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = fetch_lords_register(tmp_path, wayback_timestamp="20201130", client=client)

        provenance = json.loads(result.provenance_path.read_text())
        assert provenance["observed_at"] == "2020-11-30T00:00:00+00:00"
        assert provenance["observed_at"] != provenance["retrieved_at"]

    def test_live_fetch_with_no_wayback_timestamp_leaves_observed_at_null(self, tmp_path):
        """GIVEN no wayback_timestamp (a live, non-archival fetch) WHEN
        fetch_lords_register runs THEN observed_at is null -- never silently
        backfilled from retrieved_at at write time."""
        import json

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, text=SAMPLE_HTML)
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = fetch_lords_register(tmp_path, client=client)

        provenance = json.loads(result.provenance_path.read_text())
        assert provenance["observed_at"] is None


class TestLordsInterestsRegisterContract:
    def test_ingest_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/uk_lords_interests.yml cannot be resolved (its source_id is
        absent from the register) WHEN ingest_lords_register is called THEN it raises
        RegisterError and writes nothing to the database."""
        monkeypatch.setattr(lords_interests_module, "SOURCE_ID", "does_not_exist_xyz")
        _write_page(tmp_path, 1, SAMPLE_HTML)
        _write_provenance(tmp_path)

        with pytest.raises(RegisterError):
            ingest_lords_register(tmp_path)

    def test_fetch_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/uk_lords_interests.yml cannot be resolved WHEN
        fetch_lords_register is called THEN it raises RegisterError before making any
        HTTP request."""
        monkeypatch.setattr(lords_interests_module, "SOURCE_ID", "does_not_exist_xyz")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("fetch_lords_register must not make an HTTP request")

        client = httpx.Client(transport=httpx.MockTransport(handler))

        with pytest.raises(RegisterError):
            fetch_lords_register(tmp_path, client=client)
