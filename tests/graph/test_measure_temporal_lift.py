"""Regression tests for measure_temporal_lift.py's cohort selection.

A live run against the real DB caught that `classify_positive_controls`
originally read `.consult/vip_lane_positives.csv` (the DHSC VIP-lane
referral cohort — formally ruled an INVALID positive set) instead of
`run_positive_controls.py`'s real 30-edge cohort (sampled directly from the
graph: `declared_interest` edges with a UK-PARLIAMENT-MEMBER source and a
GB-COH target). This project's single most repeated defect is testing the
wrong cohort / wrong denominator, so this file exists specifically to
prevent that regression from recurring silently.
"""

from __future__ import annotations

from datetime import date

import pytest
from scripts.measure_temporal_lift import (
    classify_negative_controls,
    classify_positive_controls,
    classify_vip_lane_cohort,
    report_page_bias,
)

from uncorrupt.graph.models import Edge, Entity
from uncorrupt.graph.register_snapshots import EvidenceLevel


@pytest.mark.django_db
class TestClassifyPositiveControlsCohort:
    def test_never_reads_the_vip_lane_csv(self, monkeypatch, tmp_path):
        """classify_positive_controls must not open any CSV/cache file at
        all — it samples the graph directly, exactly like
        run_positive_controls.py does. Pointing COHORT_CSV/VIP_CH_CACHE at
        paths that don't exist must not raise."""
        import scripts.measure_temporal_lift as module

        monkeypatch.setattr(module, "COHORT_CSV", str(tmp_path / "does-not-exist.csv"))
        monkeypatch.setattr(module, "VIP_CH_CACHE", str(tmp_path / "does-not-exist.json"))

        person = Entity.objects.create(
            entity_type="person",
            name="Baroness Test of Somewhere",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="1",
        )
        company = Entity.objects.create(
            entity_type="company",
            name="Widgets Ltd",
            company_number="01234567",
            registry_scheme="GB-COH",
            registry_id="01234567",
        )
        Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )

        rows = classify_positive_controls({}, {}, max_hops=2, award_cutoff=date(2020, 3, 1))

        assert len(rows) == 1

    def test_selects_the_same_edges_run_positive_controls_selects(self):
        """The candidate query matches run_positive_controls.py:92-99
        exactly: declared_interest edges, GB-COH target, UK-PARLIAMENT-MEMBER
        source, ordered by edge id. An edge with the WRONG registry scheme
        on either end must be excluded."""
        person = Entity.objects.create(
            entity_type="person",
            name="Lord Test",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="2",
        )
        matching_company = Entity.objects.create(
            entity_type="company",
            name="Match Ltd",
            company_number="11111111",
            registry_scheme="GB-COH",
            registry_id="11111111",
        )
        wrong_scheme_company = Entity.objects.create(
            entity_type="company",
            name="Wrong Ltd",
            company_number="22222222",
            registry_scheme="UK-LORDS-UNRESOLVED",
            registry_id="somekey",
        )
        Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=matching_company
        )
        Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=wrong_scheme_company
        )

        people_by_surname = {"test": [person]}
        rows = classify_positive_controls(
            {}, people_by_surname, max_hops=2, award_cutoff=date(2020, 3, 1)
        )

        assert len(rows) == 1
        assert rows[0]["company"] == "Match Ltd"

    def test_resolves_via_as_published_not_the_sampled_entity_directly(self):
        """The register name is re-expressed (title stripped) and re-resolved
        by surname — a person whose published surname can't be found in
        `people_by_surname` is unresolved, even though the sampled Entity
        itself obviously exists."""
        person = Entity.objects.create(
            entity_type="person",
            name="Baroness Mone of Mayfair",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="3",
        )
        company = Entity.objects.create(
            entity_type="company",
            name="PPE Supplies Ltd",
            company_number="33333333",
            registry_scheme="GB-COH",
            registry_id="33333333",
        )
        Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )

        # people_by_surname keyed on "mone" (as_published strips "Baroness"
        # and "of Mayfair") -- if this weren't string-re-resolved and instead
        # just reused the sampled entity, the empty dict below wouldn't matter.
        rows = classify_positive_controls({}, {}, max_hops=2, award_cutoff=date(2020, 3, 1))

        assert rows[0]["status"] == "unresolved"


@pytest.mark.django_db
class TestClassifyVipLaneCohortIsSeparate:
    def test_is_a_distinct_function_from_the_real_positive_controls(self):
        """classify_vip_lane_cohort and classify_positive_controls must stay
        two separate functions -- the VIP-lane cohort is an invalid positive
        set and must never be reachable under the "positive controls" name."""
        assert classify_vip_lane_cohort is not classify_positive_controls


@pytest.mark.django_db
class TestNoTemporalClaimHandledAtEveryCallSite:
    """`relationship_evidence_level` can now return `None` (a structural
    path exists but carries no temporal evidence at all -- every path found
    is same_as-only). Each of this file's three call sites must handle it
    without crashing on an unconditional `int(level)`/`level.name`, and
    without silently reintroducing the one-rung-lower ATEMPORAL_CORROBORATION
    fallback the fix removed. All three reuse this file's existing
    `"level": None` convention rather than a new one."""

    def test_classify_positive_controls_reports_no_temporal_claim(self):
        """A positive-control row whose ONLY path to its re-resolved target
        company is an identity (same_as) chain classifies as
        'classified_no_temporal_claim' with `level: None` -- not a crash,
        and not ATEMPORAL_CORROBORATION."""
        person = Entity.objects.create(
            entity_type="person",
            name="Lord Test",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="1",
        )
        company = Entity.objects.create(
            entity_type="company",
            name="Match Ltd",
            company_number="11111111",
            registry_scheme="GB-COH",
            registry_id="11111111",
        )
        Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )
        # The traversal adjacency carries ONLY an identity bridge between
        # person and company -- deliberately excludes the declared_interest
        # edge above (which is only used for source_edge_level/pages), so
        # relationship_evidence_level's search sees an identity-only path.
        same_as_edge = Edge.objects.create(
            edge_type="same_as", source_entity=person, target_entity=company
        )
        adj = {person.id: [same_as_edge], company.id: [same_as_edge]}
        people_by_surname = {"test": [person]}

        rows = classify_positive_controls(
            adj, people_by_surname, max_hops=2, award_cutoff=date(2020, 3, 1)
        )

        assert len(rows) == 1
        assert rows[0]["status"] == "classified_no_temporal_claim"
        assert rows[0]["level"] is None
        assert "level_name" not in rows[0]

    def test_classify_vip_lane_cohort_reports_no_temporal_claim(self, monkeypatch, tmp_path):
        """The same handling for the VIP-lane cohort's classification call site."""
        import scripts.measure_temporal_lift as module

        cohort_csv = tmp_path / "cohort.csv"
        cohort_csv.write_text(
            "supplier_name,source_of_referral,actual_referrer,company_number\n"
            "Match Ltd,Lord Test,,\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(module, "COHORT_CSV", str(cohort_csv))

        referrer = Entity.objects.create(entity_type="person", name="Lord Test")
        supplier = Entity.objects.create(
            entity_type="company",
            name="Match Ltd",
            company_number="11111111",
            registry_scheme="GB-COH",
            registry_id="11111111",
        )
        same_as_edge = Edge.objects.create(
            edge_type="same_as", source_entity=referrer, target_entity=supplier
        )
        adj = {referrer.id: [same_as_edge], supplier.id: [same_as_edge]}
        people_by_surname = {"test": [referrer]}

        rows = module.classify_vip_lane_cohort(
            adj, people_by_surname, {}, max_hops=2, award_cutoff=date(2020, 3, 1)
        )

        assert len(rows) == 1
        assert rows[0]["status"] == "classified_no_temporal_claim"
        assert rows[0]["level"] is None
        assert "level_name" not in rows[0]

    def test_classify_negative_controls_reports_no_temporal_claim(self):
        """The same handling for the negative-control classification call
        site. The identity bridge runs through an intermediate node (not a
        direct person->company edge) so the pre-existing-edge exclusion
        check does not remove this pair from the sample."""
        person = Entity.objects.create(
            entity_type="person", name="A", registry_scheme="UK-PARLIAMENT-MEMBER"
        )
        twin = Entity.objects.create(entity_type="person", name="A (CH record)")
        company = Entity.objects.create(entity_type="company", name="B", registry_scheme="GB-COH")
        same_as_1 = Edge.objects.create(
            edge_type="same_as", source_entity=person, target_entity=twin
        )
        same_as_2 = Edge.objects.create(
            edge_type="same_as", source_entity=twin, target_entity=company
        )
        adj = {
            person.id: [same_as_1],
            twin.id: [same_as_1, same_as_2],
            company.id: [same_as_2],
        }

        rows = classify_negative_controls(adj, n=1, max_hops=2, award_cutoff=date(2020, 3, 1))

        assert len(rows) == 1
        assert rows[0]["status"] == "classified_no_temporal_claim"
        assert rows[0]["level"] is None
        assert "level_name" not in rows[0]


class TestReportPageBias:
    def test_no_promoted_rows_reports_nothing_to_break_down(self, capsys):
        """No PRE_AWARD_OBSERVED source edges means no page breakdown to
        print, and this must not error."""
        rows = [
            {
                "status": "classified",
                "level": int(EvidenceLevel.ATEMPORAL_CORROBORATION),
                "source_edge_level": int(EvidenceLevel.ATEMPORAL_CORROBORATION),
                "source_edge_pages": [],
            }
        ]

        report_page_bias(rows, describe_live=False)

        captured = capsys.readouterr()
        assert "nothing to break down" in captured.out

    def test_promotions_are_broken_down_by_page(self, capsys):
        """A promoted row's page number is counted and printed, and an
        all-page-1-or-2 warning fires when every promotion is that shallow."""
        rows = [
            {
                "status": "classified",
                "level": int(EvidenceLevel.PRE_AWARD_OBSERVED),
                "source_edge_level": int(EvidenceLevel.PRE_AWARD_OBSERVED),
                "source_edge_pages": [1],
            },
            {
                "status": "classified",
                "level": int(EvidenceLevel.ATEMPORAL_CORROBORATION),
                "source_edge_level": int(EvidenceLevel.ATEMPORAL_CORROBORATION),
                "source_edge_pages": [],
            },
        ]

        report_page_bias(rows, describe_live=False)

        captured = capsys.readouterr()
        assert "page  1: 1" in captured.out
        assert "every promotion came from page 1-2" in captured.out
