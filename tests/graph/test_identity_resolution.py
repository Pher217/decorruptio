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

from datetime import UTC, datetime

import pytest

from uncorrupt.gates.binding import compute_attestation_inclusive_hash
from uncorrupt.graph.identity_resolution import (
    CONFIDENCE_TERRITORIAL,
    CONFIDENCE_TITLE_ONLY,
    CONFIDENCE_WITH_FORENAME,
    MIN_PERSISTED_FOR_DELETE_FLOOR,
    _territorial_compatible,
    _titles_compatible,
    parse_officer_name,
    parse_parliament_name,
    resolve_cross_register_identities,
)
from uncorrupt.graph.models import Attestation, Edge, Entity


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


def test_viscount_is_recognised_as_a_title_not_a_forename():
    """GIVEN a peer styled with a peerage rank word missing from the
    original title set ("Viscount Hailsham")
    WHEN parsed
    THEN "Viscount" is stripped as the title, not left to be misread as a
    bogus "forename" — the old parse gave title=None, meaning the peer
    could only ever match an untitled CH officer, exactly the rank
    confusion `_titles_compatible` exists to prevent."""
    parsed = parse_parliament_name("Viscount Hailsham")
    assert parsed["title"] == "viscount"
    assert parsed["surname"] == "hailsham"
    assert parsed["forename"] is None


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
class TestAdversarialReviewRegressions:
    """Regressions from an adversarial review that reproduced, by executing
    the shipped code, that the 0.85 territorial tier recreated the exact
    cross-assertion bug it was written to eliminate — at higher confidence."""

    def test_territorial_confirmation_is_checked_per_officer_record_not_per_group(
        self,
    ):
        """GIVEN two distinct real peers who share surname + title (a
        contested bucket) and TWO CH officer records that happen to share
        the SAME forename token ("Greville") but carry DIFFERENT, mutually
        contradicting territorial designations (found live: "HOWARD,
        Greville Patrick Charles, The Lord Howard Of Rising" and "HOWARD,
        Greville John, The Lord Howard Of Lympne")
        WHEN identities are resolved
        THEN each peer is linked ONLY to the officer record whose own
        territorial designation actually confirms it — grouping by
        forename must not let one confirmed officer drag in a
        same-forename sibling whose designation contradicts the member."""
        rising = _make_member("mp-1", "Lord Howard of Rising")
        lympne = _make_member("mp-2", "Lord Howard of Lympne")
        officer_rising = _make_officer(
            "officer-1", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising"
        )
        officer_lympne = _make_officer(
            "officer-2", "HOWARD, Greville John, The Lord Howard Of Lympne"
        )

        resolve_cross_register_identities()

        rising_edge = Edge.objects.get(edge_type="same_as", source_entity=rising)
        assert rising_edge.target_entity_id == officer_rising.id
        lympne_edge = Edge.objects.get(edge_type="same_as", source_entity=lympne)
        assert lympne_edge.target_entity_id == officer_lympne.id
        assert not Edge.objects.filter(
            edge_type="same_as", source_entity=rising, target_entity=officer_lympne
        ).exists()
        assert not Edge.objects.filter(
            edge_type="same_as", source_entity=lympne, target_entity=officer_rising
        ).exists()

    def test_bare_officer_record_in_a_confirmed_forename_group_gets_no_edge_at_any_tier(
        self,
    ):
        """GIVEN a contested bucket where a confirmed territorial match
        exists for one peer, and a SECOND officer record sharing that same
        forename but carrying NO territorial designation of its own at all
        (found live: "DAVIES, John Emlyn, Lord Davies Of Stamford" confirms
        "Lord Davies of Stamford"; "DAVIES, John Emrys, Lord" shares
        forename "John" but has no designation to confirm anything)
        WHEN identities are resolved
        THEN the confirmed peer is linked only to the confirming officer
        record, the bare officer record receives NO same_as edge from
        anyone — neither at the territorial tier nor a silent fallback to
        the weak title-only tier — and the non-matching peer gets nothing
        either."""
        stamford = _make_member("mp-1", "Lord Davies of Stamford")
        oldham = _make_member("mp-2", "Lord Davies of Oldham")
        officer_stamford = _make_officer("officer-1", "DAVIES, John Emlyn, Lord Davies Of Stamford")
        officer_bare = _make_officer("officer-2", "DAVIES, John Emrys, Lord")

        stats = resolve_cross_register_identities()

        stamford_edge = Edge.objects.get(edge_type="same_as", source_entity=stamford)
        assert stamford_edge.target_entity_id == officer_stamford.id
        assert stamford_edge.attestations.get().match_confidence == CONFIDENCE_TERRITORIAL
        assert not Edge.objects.filter(edge_type="same_as", target_entity=officer_bare).exists()
        assert not Edge.objects.filter(edge_type="same_as", source_entity=oldham).exists()
        assert stats["linked_title_only"] == 0

    def test_bucket_key_normalises_baron_lord_equivalence(self):
        """GIVEN two distinct real peers who share surname + title but one
        is styled "Lord" and the other "Baron" — the same rank written two
        ways — against a single bare CH officer record
        WHEN identities are resolved
        THEN both peers are treated as ONE contested bucket (not two
        separately "uncontested" buckets) and neither gets an edge to the
        shared officer — the original multi-peer-one-officer bug, reached
        via a title spelling variant instead of an exact string match."""
        stamford = _make_member("mp-1", "Lord Davies of Stamford")
        abersoch = _make_member("mp-2", "Baron Davies of Abersoch")
        _make_officer("officer-1", "DAVIES, Evan Mervyn, Lord")

        resolve_cross_register_identities()

        assert not Edge.objects.filter(edge_type="same_as", source_entity=stamford).exists()
        assert not Edge.objects.filter(edge_type="same_as", source_entity=abersoch).exists()

    def test_forenamed_member_contests_the_bucket_for_a_no_forename_sibling(self):
        """GIVEN a forenamed member ("Lord Quentin Davies") and a
        no-forename peerage member ("Lord Davies of Stamford") who share
        surname + title, against a single CH officer whose forename
        matches the FORENAMED member exactly
        WHEN identities are resolved
        THEN the forenamed member gets the edge (a genuine forename match)
        and the no-forename peer gets nothing — the peer's own
        contested-bucket gate must count the forenamed sibling, or the
        peer's weak uncontested shortcut hands the SAME officer to a
        second, different member."""
        quentin = _make_member("mp-1", "Lord Quentin Davies")
        stamford = _make_member("mp-2", "Lord Davies of Stamford")
        officer = _make_officer("officer-1", "DAVIES, Quentin, Lord")

        stats = resolve_cross_register_identities()

        quentin_edge = Edge.objects.get(edge_type="same_as", source_entity=quentin)
        assert quentin_edge.target_entity_id == officer.id
        assert quentin_edge.attestations.get().match_confidence == CONFIDENCE_WITH_FORENAME
        assert not Edge.objects.filter(edge_type="same_as", source_entity=stamford).exists()
        assert stats["linked_title_only"] == 0

    def test_uncontested_bucket_still_rejects_a_contradicting_territorial_designation(
        self,
    ):
        """GIVEN a surname + title shared by only ONE real parliament
        member (uncontested) whose display name carries a territorial
        designation, and the single candidate CH officer record carries a
        DIFFERENT territorial designation in its own title field
        WHEN identities are resolved
        THEN no edge is created — an uncontested bucket must not skip the
        territorial contradiction test just because there is nobody else
        to disambiguate against; the officer's own designation says it
        belongs to someone else."""
        rising = _make_member("mp-1", "Lord Howard of Rising")
        _make_officer("officer-1", "HOWARD, Someone Else, The Lord Howard Of Lympne")

        stats = resolve_cross_register_identities()

        assert not Edge.objects.filter(edge_type="same_as", source_entity=rising).exists()
        assert stats["linked_title_only"] == 0
        assert stats["ambiguous_skipped"] == 1

    def test_archbishop_functional_title_produces_no_edge(self):
        """GIVEN "The Lord Archbishop of Canterbury" — a real ex-officio
        Lords Spiritual style, not a personal peerage — and a CH officer
        literally surnamed Archbishop
        WHEN identities are resolved
        THEN no same_as edge is created — the functional-title exclusion
        must catch more than the single literal word "bishop" (found real:
        scripts/run_positive_controls.py cites "The Lord Archbishop of
        York")."""
        archbishop_member = _make_member("mp-1", "The Lord Archbishop of Canterbury")
        _make_officer("officer-1", "ARCHBISHOP, Peter James, Lord")

        stats = resolve_cross_register_identities()

        assert stats["skipped_functional_title"] == 1
        assert not Edge.objects.filter(
            edge_type="same_as", source_entity=archbishop_member
        ).exists()


@pytest.mark.django_db
class TestUndecidableMembersArePersisted:
    def test_ambiguous_members_are_returned_by_registry_id(self):
        """GIVEN two genuine namesake peers with no discriminating signal
        (the contested-bucket-no-territorial case)
        WHEN identities are resolved
        THEN the actual undecidable member identifiers are returned, not
        just an aggregate count — previously "185 undecidable members" was
        a subset of `ambiguous_skipped` that could not be re-derived from
        the stats dict at all."""
        stamford = _make_member("mp-1", "Lord Davies of Stamford")
        oldham = _make_member("mp-2", "Lord Davies of Oldham")
        _make_officer("officer-1", "DAVIES, Evan Mervyn, Lord")

        stats = resolve_cross_register_identities()

        undecidable_ids = {row["registry_id"] for row in stats["undecidable_members"]}
        assert undecidable_ids == {stamford.registry_id, oldham.registry_id}
        assert len(stats["undecidable_members"]) == stats["ambiguous_skipped"]

    def test_resolution_run_logs_a_summary(self, caplog):
        """GIVEN a normal resolution run
        WHEN identities are resolved
        THEN the module's own logger actually emits a record — previously
        `logger` was instantiated and never called, so nothing about a run
        was observable outside the returned stats dict."""
        import logging

        _make_member("mp-1", "Lord Agnew of Oulton")
        _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")

        with caplog.at_level(logging.INFO, logger="uncorrupt.graph.identity_resolution"):
            resolve_cross_register_identities()

        assert len(caplog.records) >= 1


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


def _make_same_as_edge(
    member: Entity,
    officer: Entity,
    match_confidence: float = 0.60,
    match_method: str = "surname_title_only",
    observed_at: datetime | None = None,
) -> Edge:
    """Persist a `same_as` edge exactly as `resolve_cross_register_identities`
    would have written it in an earlier run, for reconciliation fixtures.
    `source_reference` mirrors the resolver's own `f"{registry_id}:{registry_id}"`
    key -- without it, this fixture does not actually reproduce a real
    earlier-run row (see the staleness-fix commit that added it). `properties`
    mirrors the resolver's own `match_tier`/`parliament_name`/`officer_name`
    shape for the same reason -- production always writes it on create, so a
    fixture leaving it at the model default `{}` is structurally blind to any
    defect in how a later run corrects it (see the `Edge.properties`
    staleness fix)."""
    edge = Edge.objects.create(
        edge_type="same_as",
        source_entity=member,
        target_entity=officer,
        properties={
            "match_tier": match_method,
            "parliament_name": member.name,
            "officer_name": officer.name,
        },
    )
    Attestation.objects.create(
        edge=edge,
        source_name="Cross-register name match",
        source_reference=f"{member.registry_id}:{officer.registry_id}",
        match_confidence=match_confidence,
        match_method=match_method,
        observed_at=observed_at,
    )
    return edge


@pytest.mark.django_db
class TestSameAsReconciliation:
    """`same_as` edges are produced exclusively by this resolver (module
    docstring) and are derived, regenerable output, not source data -- so
    the resolver is authoritative for the FULL persisted `same_as` edge set
    on every run: it must delete a persisted edge its current logic no
    longer proposes, not merely add new ones on top
    (`Edge.objects.get_or_create` alone is additive-only)."""

    def test_a_persisted_edge_no_longer_proposed_is_deleted(self):
        """GIVEN a `same_as` edge already persisted from an earlier run
        (as if written under different matching logic) between a peer and
        an officer whose OWN territorial designation contradicts that
        peer's, and a SECOND peer sharing the same surname+title whose
        territorial designation the officer record actually confirms
        (mirrors the live "Lord Howard of Lympne" / "Lord Howard of
        Rising" case)
        WHEN identities are resolved
        THEN the current logic correctly matches only the second (genuinely
        confirmed) peer to the officer, and the stale persisted edge from
        the first peer -- no longer among this run's proposals -- is
        deleted rather than left in place forever."""
        stale_member = _make_member("mp-stale", "Lord Howard of Lympne")
        correct_member = _make_member("mp-correct", "Lord Howard of Rising")
        officer = _make_officer(
            "officer-1", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising"
        )
        _make_same_as_edge(stale_member, officer)

        stats = resolve_cross_register_identities()

        assert not Edge.objects.filter(edge_type="same_as", source_entity=stale_member).exists()
        correct_edge = Edge.objects.get(edge_type="same_as", source_entity=correct_member)
        assert correct_edge.target_entity_id == officer.id
        assert stats["edges_deleted_stale"] == 1
        claimants = set(
            Edge.objects.filter(edge_type="same_as", target_entity=officer).values_list(
                "source_entity_id", flat=True
            )
        )
        assert claimants == {correct_member.id}

    def test_dry_run_reports_would_be_deletions_without_performing_them(self):
        """GIVEN a stale persisted `same_as` edge that this run's logic no
        longer proposes
        WHEN identities are resolved with dry_run=True
        THEN the stale edge still exists afterwards (dry_run must never
        mutate the graph) but the stats report how many deletions WOULD
        happen -- the only way this reconciliation can be validated before
        it is ever run for real."""
        stale_member = _make_member("mp-stale", "Lord Howard of Lympne")
        _make_member("mp-correct", "Lord Howard of Rising")
        officer = _make_officer(
            "officer-1", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising"
        )
        _make_same_as_edge(stale_member, officer)

        stats = resolve_cross_register_identities(dry_run=True)

        assert stats["edges_deleted_stale"] == 1
        assert Edge.objects.filter(edge_type="same_as", source_entity=stale_member).exists()
        assert Edge.objects.filter(edge_type="same_as").count() == 1

    def test_only_same_as_edges_are_ever_deleted(self):
        """GIVEN a member and officer with OTHER edge types persisted on
        them (an `officer_of` edge on the officer, a `declared_interest`
        edge on the member) alongside a stale `same_as` edge the current
        logic no longer proposes
        WHEN identities are resolved
        THEN only the stale `same_as` edge is removed -- reconciliation
        must never touch any other edge type, no matter what else is
        attached to the same entities."""
        stale_member = _make_member("mp-stale", "Lord Howard of Lympne")
        _make_member("mp-correct", "Lord Howard of Rising")
        officer = _make_officer(
            "officer-1", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising"
        )
        _make_same_as_edge(stale_member, officer)

        company = Entity.objects.create(
            entity_type="company", name="Acme Ltd", company_number="123"
        )
        officer_of_edge = Edge.objects.create(
            edge_type="officer_of", source_entity=officer, target_entity=company
        )
        declared_interest_edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=stale_member, target_entity=company
        )

        resolve_cross_register_identities()

        assert Edge.objects.filter(id=officer_of_edge.id, edge_type="officer_of").exists()
        assert Edge.objects.filter(
            id=declared_interest_edge.id, edge_type="declared_interest"
        ).exists()
        assert not Edge.objects.filter(edge_type="same_as", source_entity=stale_member).exists()

    def test_a_persisted_edge_still_proposed_is_left_untouched(self):
        """GIVEN a `same_as` edge already persisted from an earlier run that
        the current logic STILL proposes today (nothing has changed for
        this member)
        WHEN identities are resolved
        THEN the edge id is unchanged (get_or_create finds it, does not
        delete-and-recreate it) and no deletion is counted for it."""
        member = _make_member("mp-1", "Lord Agnew of Oulton")
        officer = _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")
        existing_edge = _make_same_as_edge(member, officer)

        stats = resolve_cross_register_identities()

        existing_edge.refresh_from_db()
        assert existing_edge.source_entity_id == member.id
        assert existing_edge.target_entity_id == officer.id
        assert stats["edges_deleted_stale"] == 0
        assert Edge.objects.filter(edge_type="same_as").count() == 1


@pytest.mark.django_db
class TestAttestationConfidenceStaleness:
    """`Attestation.objects.get_or_create(..., defaults={...})` silently
    discards `defaults` once a row exists, because `source_reference` is a
    STABLE key across runs (derived from the member/officer registry-id
    pair, which cannot change for a surviving edge) -- so a pair whose match
    tier changed between runs kept its OLD confidence published forever,
    even though the resolver reconciles the persisted `same_as` Edge set to
    match every run. Measured live: 3 attestations stuck at a superseded
    surname_title_only / 0.60 after the 2026-08 territorial-tier fix
    shipped -- and now that `min_identity_confidence` is published per path
    (including via the MCP tool layer), a stale confidence is a published
    number about a named person."""

    def test_attestation_whose_tier_improved_is_updated_to_the_current_confidence(self):
        """GIVEN a `same_as` edge for "Lord Howard of Rising" already
        persisted with a STALE attestation at the old, pre-territorial-tier
        confidence (surname_title_only / 0.60) -- exactly what an earlier
        run of this resolver would have written for this pair before the
        territorial-tier fix shipped
        WHEN identities are resolved under the current logic, which now
        confirms the territorial designation and computes
        surname_title_territorial / 0.85 for this same pair
        THEN the persisted attestation is corrected in place to the current
        confidence and method, `observed_at` is left EXACTLY unchanged (a
        resolution-quality correction, not new evidence -- see the
        attestation-inclusive binding hash comment in identity_resolution.py),
        and no second attestation is created (updated, not duplicated)."""
        rising = _make_member("mp-1", "Lord Howard of Rising")
        _make_member("mp-2", "Lord Howard of Lympne")
        officer = _make_officer(
            "officer-1", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising"
        )
        stale_observed_at = datetime(2026, 1, 1, tzinfo=UTC)
        stale_edge = _make_same_as_edge(
            rising,
            officer,
            match_confidence=CONFIDENCE_TITLE_ONLY,
            match_method="surname_title_only",
            observed_at=stale_observed_at,
        )

        stats = resolve_cross_register_identities()

        assert stats["attestations_updated"] == 1
        assert Attestation.objects.filter(edge=stale_edge).count() == 1
        attestation = Attestation.objects.get(edge=stale_edge)
        assert attestation.match_confidence == CONFIDENCE_TERRITORIAL
        assert attestation.match_method == "surname_title_territorial"
        assert attestation.observed_at == stale_observed_at

    def test_attestation_whose_tier_is_unchanged_is_not_rewritten(self):
        """GIVEN a `same_as` edge already persisted with an attestation that
        already matches EXACTLY what the current logic would compute for
        this pair (uncontested surname_title_only / 0.60 -- nothing about
        the resolver's decision has changed since it was written)
        WHEN identities are resolved
        THEN the attestation's `observed_at` is left untouched -- the fix
        only corrects a STALE confidence, it does not blindly bump
        `observed_at` to "now" on every re-run, which would erase when this
        stable claim was first settled."""
        member = _make_member("mp-1", "Lord Agnew of Oulton")
        officer = _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")
        original_observed_at = datetime(2026, 1, 1, tzinfo=UTC)
        edge = _make_same_as_edge(
            member,
            officer,
            match_confidence=CONFIDENCE_TITLE_ONLY,
            match_method="surname_title_only",
            observed_at=original_observed_at,
        )

        stats = resolve_cross_register_identities()

        assert stats["attestations_updated"] == 0
        attestation = Attestation.objects.get(edge=edge)
        assert attestation.observed_at == original_observed_at

    def test_a_confidence_downgrade_is_applied_not_protected(self):
        """GIVEN a `same_as` edge already persisted with an attestation at a
        HIGH confidence (surname_title_territorial / 0.85) for a pair the
        CURRENT logic now resolves only to the weaker uncontested tier
        (surname_title_only / 0.60) -- e.g. a prior run's now-superseded
        territorial confirmation
        WHEN identities are resolved
        THEN the persisted confidence is corrected DOWN to match this run's
        decision. A stale HIGH confidence left in place would be fail-open
        (a stronger claim than the current evidence supports stays
        published) -- the fix must apply the correction in either
        direction, not special-case "only ever raise the confidence"."""
        member = _make_member("mp-1", "Lord Agnew of Oulton")
        officer = _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")
        edge = _make_same_as_edge(
            member,
            officer,
            match_confidence=CONFIDENCE_TERRITORIAL,
            match_method="surname_title_territorial",
        )

        stats = resolve_cross_register_identities()

        assert stats["attestations_updated"] == 1
        attestation = Attestation.objects.get(edge=edge)
        assert attestation.match_confidence == CONFIDENCE_TITLE_ONLY
        assert attestation.match_method == "surname_title_only"

    def test_a_same_method_confidence_downgrade_is_still_applied(self):
        """GIVEN a `same_as` edge already persisted with the SAME match
        method the current logic still computes (uncontested
        surname_title_only) but at an inflated confidence value that
        disagrees with `CONFIDENCE_TITLE_ONLY` -- isolating a confidence-only
        correction from a method change, so a fix that only re-checks
        `match_method` (or only ever raises `match_confidence`) cannot pass
        by accident
        WHEN identities are resolved
        THEN the confidence is still corrected down to what this run
        actually decided, even though the method string alone gave no
        signal that anything was stale."""
        member = _make_member("mp-1", "Lord Agnew of Oulton")
        officer = _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")
        edge = _make_same_as_edge(
            member,
            officer,
            match_confidence=0.90,
            match_method="surname_title_only",
        )

        stats = resolve_cross_register_identities()

        assert stats["attestations_updated"] == 1
        attestation = Attestation.objects.get(edge=edge)
        assert attestation.match_confidence == CONFIDENCE_TITLE_ONLY

    def test_dry_run_reports_attestation_corrections_without_writing_them(self):
        """GIVEN a stale attestation whose tier the current logic would
        correct
        WHEN identities are resolved with dry_run=True
        THEN `attestations_updated` reports the count a real run WOULD
        apply, but the persisted attestation is left completely untouched --
        the only way this correction can be validated against the live
        graph before it is ever run for real."""
        rising = _make_member("mp-1", "Lord Howard of Rising")
        _make_member("mp-2", "Lord Howard of Lympne")
        officer = _make_officer(
            "officer-1", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising"
        )
        stale_observed_at = datetime(2026, 1, 1, tzinfo=UTC)
        stale_edge = _make_same_as_edge(
            rising,
            officer,
            match_confidence=CONFIDENCE_TITLE_ONLY,
            match_method="surname_title_only",
            observed_at=stale_observed_at,
        )

        stats = resolve_cross_register_identities(dry_run=True)

        assert stats["attestations_updated"] == 1
        attestation = Attestation.objects.get(edge=stale_edge)
        assert attestation.match_confidence == CONFIDENCE_TITLE_ONLY
        assert attestation.match_method == "surname_title_only"
        assert attestation.observed_at == stale_observed_at

    def test_a_confidence_correction_does_not_move_the_attestation_inclusive_hash(self):
        """GIVEN a stale attestation the current run will correct (an
        upgrade, exercising the real save() branch this fix touches) --
        and `uncorrupt.gates.binding.compute_attestation_inclusive_hash`,
        the hash a sealed gate certificate's `GateFreezeState` is bound to,
        taken before the run
        WHEN identities are resolved and the same hash is taken again
        THEN it is EXACTLY unchanged. `compute_attestation_inclusive_hash`
        hashes `(edge_id, source_name, source_reference, observed_at,
        snapshot_ref)` and deliberately excludes `match_confidence` /
        `match_method` as "resolution-quality metadata, not new evidence"
        (binding.py docstring) -- a correction that touches only those two
        fields must never move it, or a resolution-quality re-score would
        silently unbind every sealed certificate recorded before it ran."""
        rising = _make_member("mp-1", "Lord Howard of Rising")
        _make_member("mp-2", "Lord Howard of Lympne")
        officer = _make_officer(
            "officer-1", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising"
        )
        _make_same_as_edge(
            rising,
            officer,
            match_confidence=CONFIDENCE_TITLE_ONLY,
            match_method="surname_title_only",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        hash_before = compute_attestation_inclusive_hash()

        stats = resolve_cross_register_identities()

        assert stats["attestations_updated"] == 1
        assert compute_attestation_inclusive_hash() == hash_before

    def test_edge_properties_match_tier_is_corrected_alongside_the_attestation(self):
        """GIVEN a `same_as` edge whose persisted `properties["match_tier"]`
        was written by an earlier, weaker-tier run (surname_title_only) --
        exactly what `Edge.objects.get_or_create(..., defaults={...})`
        leaves behind, since defaults are silently discarded once the edge
        row exists (the same defect class as the attestation-confidence fix,
        one statement above it in identity_resolution.py)
        WHEN identities are resolved under logic that now confirms the
        territorial designation for this same pair
        THEN `edge.properties["match_tier"]` is corrected to the current
        tier -- not left disagreeing with the attestation that now backs
        it."""
        rising = _make_member("mp-1", "Lord Howard of Rising")
        _make_member("mp-2", "Lord Howard of Lympne")
        officer = _make_officer(
            "officer-1", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising"
        )
        stale_edge = _make_same_as_edge(
            rising,
            officer,
            match_confidence=CONFIDENCE_TITLE_ONLY,
            match_method="surname_title_only",
        )
        assert stale_edge.properties["match_tier"] == "surname_title_only"

        resolve_cross_register_identities()

        stale_edge.refresh_from_db()
        assert stale_edge.properties["match_tier"] == "surname_title_territorial"

    def test_edge_properties_match_tier_is_corrected_on_a_downgrade_too(self):
        """GIVEN a `same_as` edge whose persisted `properties["match_tier"]`
        was written by an earlier run at the STRONGER territorial tier, for
        a pair the current logic now resolves only to the weaker
        uncontested tier
        WHEN identities are resolved
        THEN `edge.properties["match_tier"]` is corrected DOWN too -- a fix
        that only ever raises the persisted tier (never lowers it) would
        leave a stronger-than-supported claim on `properties`, the exact
        fail-open shape the attestation-confidence fix above was written to
        avoid."""
        member = _make_member("mp-1", "Lord Agnew of Oulton")
        officer = _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")
        edge = _make_same_as_edge(
            member,
            officer,
            match_confidence=CONFIDENCE_TERRITORIAL,
            match_method="surname_title_territorial",
        )
        assert edge.properties["match_tier"] == "surname_title_territorial"

        resolve_cross_register_identities()

        edge.refresh_from_db()
        assert edge.properties["match_tier"] == "surname_title_only"

    def test_dry_run_counts_an_already_persisted_edge_with_no_attestation_yet(self):
        """GIVEN a `same_as` edge already persisted for a pair the current
        run also proposes, but carrying NO attestation from this resolver's
        source at all (live count: 0, but reachable -- e.g. a row a
        previous partial write left behind) -- so a real run's
        `existing_attestation is None` branch would silently CREATE one
        WHEN identities are resolved with dry_run=True
        THEN `attestations_updated` still counts it, not just edges the dry
        run would newly create outright -- a dry run that reports 0 here
        while a real run silently writes a brand new confidence row would
        hide exactly the kind of change this dry run exists to preview."""
        member = _make_member("mp-1", "Lord Agnew of Oulton")
        officer = _make_officer("officer-1", "AGNEW, Theodore Thomas More, Lord")
        edge = Edge.objects.create(edge_type="same_as", source_entity=member, target_entity=officer)

        stats = resolve_cross_register_identities(dry_run=True)

        assert stats["attestations_updated"] == 1
        assert not Attestation.objects.filter(edge=edge).exists()


@pytest.mark.django_db
class TestDeleteFloor:
    """Reconciliation made this resolver destructive: it deletes persisted
    `same_as` edges its current logic no longer proposes. If an upstream
    ingest has not run -- or `registry_scheme` values drift -- the resolver
    proposes nothing and EVERY persisted edge looks stale, silently wiping
    the set. The floor refuses that, because a majority delete is never a
    routine reconciliation outcome."""

    @staticmethod
    def _persist_orphan_edges(count: int) -> None:
        """Persist `count` `same_as` edges whose officers no resolution run
        can ever propose (no matching member name), so every one of them is
        stale on the next run."""
        for i in range(count):
            member = _make_member(f"orphan-mp-{i}", f"Lord Nonesuch{i} of Nowhere{i}")
            officer = _make_officer(f"orphan-officer-{i}", f"ZZZUNMATCHED{i}, Nobody, Lord")
            _make_same_as_edge(member, officer)

    def test_deleting_a_majority_of_a_large_persisted_set_is_refused(self):
        """GIVEN more persisted `same_as` edges than the floor's minimum, all
        of which this run's logic no longer proposes
        WHEN identities are resolved without opting in to a bulk delete
        THEN it raises rather than wiping the set, and every edge survives."""
        self._persist_orphan_edges(MIN_PERSISTED_FOR_DELETE_FLOOR)

        with pytest.raises(RuntimeError, match="refusing to delete"):
            resolve_cross_register_identities()

        assert Edge.objects.filter(edge_type="same_as").count() == MIN_PERSISTED_FOR_DELETE_FLOOR

    def test_allow_bulk_delete_opts_in_to_the_majority_delete(self):
        """GIVEN the same wipe-the-set condition
        WHEN identities are resolved with allow_bulk_delete=True
        THEN the deletion proceeds, because the operator asked for it."""
        self._persist_orphan_edges(MIN_PERSISTED_FOR_DELETE_FLOOR)

        stats = resolve_cross_register_identities(allow_bulk_delete=True)

        assert stats["edges_deleted_stale"] == MIN_PERSISTED_FOR_DELETE_FLOOR
        assert Edge.objects.filter(edge_type="same_as").count() == 0

    def test_floor_does_not_fire_below_its_minimum_set_size(self):
        """GIVEN fewer persisted edges than the floor's minimum -- where a
        percentage carries no signal, since deleting 1 of 1 is 100%
        WHEN identities are resolved
        THEN the ordinary reconciliation runs and deletes them."""
        self._persist_orphan_edges(MIN_PERSISTED_FOR_DELETE_FLOOR - 1)

        stats = resolve_cross_register_identities()

        assert stats["edges_deleted_stale"] == MIN_PERSISTED_FOR_DELETE_FLOOR - 1

    def test_dry_run_reports_the_would_be_deletions_without_raising(self):
        """GIVEN the wipe-the-set condition
        WHEN identities are resolved with dry_run=True
        THEN the floor still refuses -- a dry run must surface the same
        objection a real run would, not quietly report a plan that cannot
        execute."""
        self._persist_orphan_edges(MIN_PERSISTED_FOR_DELETE_FLOOR)

        with pytest.raises(RuntimeError, match="refusing to delete"):
            resolve_cross_register_identities(dry_run=True)

        assert Edge.objects.filter(edge_type="same_as").count() == MIN_PERSISTED_FOR_DELETE_FLOOR


@pytest.mark.django_db
class TestDryRunReportsCreations:
    def test_dry_run_reports_edges_it_would_create(self):
        """GIVEN a resolvable member/officer pair with nothing persisted yet
        WHEN identities are resolved with dry_run=True
        THEN edges_created reports what a real run would create rather than
        0 -- a dry run that shows deletions but not creations invites
        approving a net loss by mistake -- and still writes nothing."""
        _make_member("mp-dry", "Lord Howard of Rising")
        _make_officer("officer-dry", "HOWARD, Greville Patrick Charles, The Lord Howard Of Rising")

        stats = resolve_cross_register_identities(dry_run=True)

        assert stats["edges_created"] == 1
        assert Edge.objects.filter(edge_type="same_as").count() == 0
