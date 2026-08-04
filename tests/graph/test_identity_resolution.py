"""Tests for cross-register identity assertion (parliament <-> CH officer).

Territorial-designation tier (2026-08): a live-graph scan found 21 CH
officer entities each carrying `same_as` edges from more than one distinct
parliament member -- e.g. one real "Evan Mervyn Davies, Lord" officer with
FIVE different "Lord Davies of ..." peers linked to it, and 22 Lords
Spiritual ("The Lord Bishop of X") all wrongly linked to one businessman
literally surnamed Bishop. These tests cover the fix: a contested
(surname, title) bucket -- more than one real parliament member -- now
requires a positively confirmed territorial-designation match before any
edge is created; the Lords Spiritual functional-title pattern is excluded
from surname matching entirely.
"""

import pytest

from uncorrupt.graph.identity_resolution import (
    CONFIDENCE_TERRITORIAL,
    CONFIDENCE_TITLE_ONLY,
    _territorial_compatible,
    _titles_compatible,
    parse_officer_name,
    parse_parliament_name,
    resolve_cross_register_identities,
)
from uncorrupt.graph.models import Edge, Entity


def test_parses_peerage_name_dropping_territorial_designation_from_surname():
    """GIVEN a peerage name with a territorial designation
    WHEN parsed
    THEN the surname is the family name, no forename is claimed, and the
    territorial designation is captured separately (not discarded)."""
    assert parse_parliament_name("Lord Agnew of Oulton") == {
        "surname": "agnew",
        "forename": None,
        "title": "lord",
        "territorial": "oulton",
        "functional_title": False,
    }


def test_parses_knighted_mp_name():
    """GIVEN an MP with an honorific
    WHEN parsed
    THEN title, forename and surname are all recovered and there is no
    territorial designation to claim."""
    assert parse_parliament_name("Sir Geoffrey Cox") == {
        "surname": "cox",
        "forename": "geoffrey",
        "title": "sir",
        "territorial": None,
        "functional_title": False,
    }


def test_parses_untitled_mp_name():
    """GIVEN an MP with no title
    WHEN parsed
    THEN title is None."""
    assert parse_parliament_name("Danny Kruger") == {
        "surname": "kruger",
        "forename": "danny",
        "title": None,
        "territorial": None,
        "functional_title": False,
    }


def test_lords_spiritual_functional_title_is_not_treated_as_a_surname():
    """GIVEN a Lords Spiritual ex-officio seat ("The Lord Bishop of X")
    WHEN parsed
    THEN no surname is claimed and functional_title is flagged — "Bishop"
    is a rotating diocesan role, not a family name, and treating it as one
    would collide every sitting bishop onto whichever CH officer happens to
    be literally surnamed Bishop (found live: 22 such wrong edges)."""
    parsed = parse_parliament_name("The Lord Bishop of Birmingham")
    assert parsed["surname"] == ""
    assert parsed["functional_title"] is True


def test_ordinary_bishop_surname_is_not_excluded():
    """GIVEN a real peer whose actual surname happens to be a common word
    WHEN parsed
    THEN the functional-title exclusion does not fire — it only matches the
    literal single-token "Bishop" pattern used by the Lords Spiritual."""
    parsed = parse_parliament_name("Lord Smith")
    assert parsed["surname"] == "smith"
    assert parsed["functional_title"] is False


def test_parses_officer_name_surname_forename_title():
    """GIVEN the Companies House "SURNAME, Forenames, Title" format
    WHEN parsed
    THEN each component is recovered and there is no territorial
    designation embedded in the bare title."""
    assert parse_officer_name("COX, Geoffrey Charles, Sir") == {
        "surname": "cox",
        "forename": "geoffrey",
        "title": "sir",
        "territorial": None,
    }


def test_parses_officer_name_without_title():
    """GIVEN an officer record with no title
    WHEN parsed
    THEN title is None and the forename is still recovered."""
    assert parse_officer_name("KRUGER, Daniel John") == {
        "surname": "kruger",
        "forename": "daniel",
        "title": None,
        "territorial": None,
    }


def test_parses_officer_name_with_embedded_territorial_designation():
    """GIVEN a CH officer title field that carries the peer's full style,
    not just the bare rank (found live: "The Lord Howard Of Rising")
    WHEN parsed
    THEN the territorial designation is extracted alongside the title."""
    parsed = parse_officer_name("HOWARD, Greville Patrick Charles, The Lord Howard Of Rising")
    assert parsed["title"] == "lord"
    assert parsed["territorial"] == "rising"


def test_parses_officer_name_territorial_with_trailing_post_nominal():
    """GIVEN a CH title field with a territorial designation followed by
    post-nominal letters (found live: "Lord Sainsbury Of Preston Candover
    Kg")
    WHEN parsed
    THEN the territorial designation still includes the trailing text —
    `_territorial_compatible` handles the post-nominal via a prefix match,
    not by trying to strip it here."""
    parsed = parse_officer_name("SAINSBURY, John Davan, Lord Sainsbury Of Preston Candover Kg")
    assert parsed["territorial"] == "preston candover kg"


def test_untitled_member_does_not_match_titled_officer():
    """GIVEN an untitled MP and a titled officer
    WHEN titles are compared
    THEN they are incompatible — a peer is not an untitled namesake."""
    assert _titles_compatible(None, "lord") is False
    assert _titles_compatible("lord", None) is False


def test_baron_and_lord_are_the_same_rank():
    """GIVEN the same rank written two ways
    WHEN titles are compared
    THEN they are compatible."""
    assert _titles_compatible("baron", "lord") is True
    assert _titles_compatible("baroness", "lady") is True


def test_different_peerage_ranks_are_incompatible():
    """GIVEN a Lord and a Lady of the same surname
    WHEN titles are compared
    THEN they are incompatible — this is what stops Lord Agnew being fused
    with Lady Agnew."""
    assert _titles_compatible("lord", "lady") is False


def test_territorial_compatible_when_officer_omits_it():
    """GIVEN a member with a known territorial designation and an officer
    record that carries no territorial text at all
    WHEN compared
    THEN they are compatible — CH routinely omits it, so absence is not
    evidence of a mismatch."""
    assert _territorial_compatible("oulton", None) is True


def test_territorial_incompatible_when_member_has_none_but_officer_does():
    """GIVEN a member whose display name carries no territorial designation
    (Parliament's own naming is treated as complete) and an officer record
    that does carry one
    WHEN compared
    THEN they are incompatible — an officer with a specific territorial
    designation belongs to a different, more specific peer."""
    assert _territorial_compatible(None, "crudwell & dingwall") is False


def test_territorial_compatible_with_trailing_post_nominal():
    """GIVEN a member territorial designation that is a prefix of the
    officer's (the officer's carries trailing post-nominal letters)
    WHEN compared
    THEN they are compatible."""
    assert _territorial_compatible("preston candover", "preston candover kg") is True


def test_territorial_incompatible_on_prefix_word_boundary():
    """GIVEN two territorial designations where one is a naive substring of
    the other but not a whole-word prefix ("chester" vs "chesterton")
    WHEN compared
    THEN they are incompatible — a bare substring match would wrongly treat
    "of Chester" as compatible with "of Chesterton"."""
    assert _territorial_compatible("chester", "chesterton") is False


# --- resolve_cross_register_identities: contested-bucket / territorial tier ---


def _make_member(registry_id: str, name: str) -> Entity:
    return Entity.objects.create(
        entity_type="person",
        registry_scheme="UK-PARLIAMENT-MEMBER",
        registry_id=registry_id,
        name=name,
    )


def _make_officer(registry_id: str, name: str, properties: dict | None = None) -> Entity:
    return Entity.objects.create(
        entity_type="person",
        registry_scheme="GB-COH-OFFICER",
        registry_id=registry_id,
        name=name,
        properties=properties or {},
    )


@pytest.mark.django_db
class TestContestedBucketRequiresTerritorialConfirmation:
    def test_two_genuine_namesakes_with_no_discriminating_signal_get_no_high_confidence_edge(
        self,
    ):
        """GIVEN two distinct real peers who share surname + title (a
        contested bucket) and a single CH officer record that carries no
        territorial designation at all
        WHEN identities are resolved
        THEN neither peer gets a same_as edge — the officer record cannot
        be safely attributed to either one of them (mirrors the live
        "Evan Mervyn Davies, Lord" case: 5 different real "Lord Davies of
        ..." peers, one bare-titled officer record, none resolvable)."""
        stamford = _make_member("mp-1", "Lord Davies of Stamford")
        oldham = _make_member("mp-2", "Lord Davies of Oldham")
        _make_officer("officer-1", "DAVIES, Evan Mervyn, Lord")

        stats = resolve_cross_register_identities()

        assert stats["linked_territorial"] == 0
        assert stats["linked_title_only"] == 0
        assert stats["ambiguous_skipped"] == 2
        assert not Edge.objects.filter(edge_type="same_as", source_entity=stamford).exists()
        assert not Edge.objects.filter(edge_type="same_as", source_entity=oldham).exists()

    def test_peer_distinguished_by_territorial_designation_gets_high_confidence_edge(self):
        """GIVEN two distinct real peers who share surname + title (a
        contested bucket) and a CH officer record whose title field embeds
        the territorial designation matching only ONE of them (found live:
        "The Lord Howard Of Rising")
        WHEN identities are resolved
        THEN the matching peer gets a same_as edge at the territorial
        confidence tier, and the non-matching peer gets none."""
        rising = _make_member("mp-1", "Lord Howard of Rising")
        lympne = _make_member("mp-2", "Lord Howard of Lympne")
        officer = _make_officer(
            "officer-1", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising"
        )

        stats = resolve_cross_register_identities()

        assert stats["linked_territorial"] == 1
        assert stats["ambiguous_skipped"] == 1

        rising_edge = Edge.objects.get(edge_type="same_as", source_entity=rising)
        assert rising_edge.target_entity_id == officer.id
        assert rising_edge.attestations.get().match_confidence == CONFIDENCE_TERRITORIAL
        assert rising_edge.attestations.get().match_method == "surname_title_territorial"

        assert not Edge.objects.filter(edge_type="same_as", source_entity=lympne).exists()

    def test_uncontested_bucket_keeps_original_weak_tier_confidence(self):
        """GIVEN a surname + title shared by only ONE real parliament member
        (uncontested) and a single-candidate CH officer record
        WHEN identities are resolved
        THEN the existing weak title-only tier still applies — the
        contested-bucket gate must not regress the safe, non-colliding
        majority of matches."""
        member = _make_member("mp-1", "Lord Agnew of Oulton")
        officer = _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")

        stats = resolve_cross_register_identities()

        assert stats["linked_title_only"] == 1
        edge = Edge.objects.get(edge_type="same_as", source_entity=member)
        assert edge.target_entity_id == officer.id
        assert edge.attestations.get().match_confidence == CONFIDENCE_TITLE_ONLY

    def test_lords_spiritual_functional_title_produces_no_edge(self):
        """GIVEN a Lords Spiritual ex-officio member ("The Lord Bishop of
        Birmingham") and a CH officer literally surnamed Bishop
        WHEN identities are resolved
        THEN no same_as edge is created — "Bishop" is the diocesan seat's
        functional role marker, not a personal surname (found live: 22
        sitting bishops wrongly linked to one businessman before this fix)."""
        bishop_member = _make_member("mp-1", "The Lord Bishop of Birmingham")
        _make_officer("officer-1", "BISHOP, Michael David, Baron Glendonbrook")

        stats = resolve_cross_register_identities()

        assert stats["skipped_functional_title"] == 1
        assert not Edge.objects.filter(edge_type="same_as", source_entity=bishop_member).exists()


@pytest.mark.django_db
class TestIdentityAssertionNeverMerges:
    def test_resolution_never_merges_entities(self):
        """GIVEN a matched member and officer
        WHEN identities are resolved
        THEN both Entity rows still exist independently afterwards, under
        their own registry identifiers — the module asserts a same_as edge,
        it never deletes, merges, or repoints either Entity row (ADR-006)."""
        member = _make_member("mp-1", "Lord Agnew of Oulton")
        officer = _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")
        entity_count_before = Entity.objects.count()

        resolve_cross_register_identities()

        assert Entity.objects.count() == entity_count_before
        member.refresh_from_db()
        officer.refresh_from_db()
        assert member.registry_scheme == "UK-PARLIAMENT-MEMBER"
        assert officer.registry_scheme == "GB-COH-OFFICER"
        assert member.id != officer.id

    def test_dry_run_creates_no_edges(self):
        """GIVEN dry_run=True
        WHEN identities are resolved
        THEN stats are still computed but no Edge or Attestation rows are
        written — dry_run must never mutate the graph."""
        _make_member("mp-1", "Lord Agnew of Oulton")
        _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")

        stats = resolve_cross_register_identities(dry_run=True)

        assert stats["linked_title_only"] == 1
        assert Edge.objects.filter(edge_type="same_as").count() == 0


@pytest.mark.django_db
class TestPersonalFieldsNeverSurface:
    def test_edge_properties_never_carry_entity_properties(self):
        """GIVEN an officer Entity whose properties JSON happens to carry a
        personal field (simulating an upstream leak — this module never
        requests or needs one)
        WHEN a same_as edge is created
        THEN the edge's properties contain only the three name/tier fields
        this module writes — no personal data from Entity.properties is
        ever copied onto the claim."""
        _make_member("mp-1", "Lord Agnew of Oulton")
        _make_officer(
            "officer-1",
            "AGNEW, Theodore Thomas More, Lord",
            properties={"date_of_birth": "1955-06", "nationality": "British"},
        )

        resolve_cross_register_identities()

        edge = Edge.objects.get(edge_type="same_as")
        assert set(edge.properties.keys()) == {"match_tier", "parliament_name", "officer_name"}
        assert "date_of_birth" not in edge.properties
        assert "nationality" not in edge.properties
