"""No CH officer entity may end up asserted as the SAME identity as more
than one distinct parliament member. This is the exact invariant whose
absence let one real officer collect `same_as` edges from five different
real peers before `identity_resolution.py` existed -- and it must hold as a
runtime post-pass, not merely as scattered per-tier logic, because a genuine
full-name collision (two distinct members sharing surname + forename +
title) bypasses the per-tier contested-bucket and territorial defenses
entirely."""

import pytest

from uncorrupt.graph.identity_resolution import resolve_cross_register_identities
from uncorrupt.graph.models import Attestation, Edge, Entity


def _make_member(registry_id: str, name: str) -> Entity:
    return Entity.objects.create(
        entity_type="person",
        registry_scheme="UK-PARLIAMENT-MEMBER",
        registry_id=registry_id,
        name=name,
    )


def _make_officer(registry_id: str, name: str) -> Entity:
    return Entity.objects.create(
        entity_type="person",
        registry_scheme="GB-COH-OFFICER",
        registry_id=registry_id,
        name=name,
    )


@pytest.mark.django_db
def test_no_officer_ever_receives_same_as_from_more_than_one_distinct_member():
    """GIVEN two distinct real parliament members who happen to share the
    exact same surname, forename and title (a genuine full-name collision --
    e.g. two different people both recorded as "Sir John Smith"), a single
    CH officer record matching that full name, and -- pre-seeded before
    resolution runs -- a `same_as` edge from the FIRST member onto that
    officer, as if an earlier run had already asserted it
    WHEN identities are resolved
    THEN a scan for the invariant -- the same scan that originally found the
    five-peers-one-officer bug -- finds no officer with `same_as` claims
    from more than one distinct member; the intra-run collision is dropped
    rather than resolved by guessing (forename-tier matching has no other
    defence against two members who share an identical full name), AND the
    pre-seeded edge is purged too, not merely left in place because it
    predates this run -- an officer-collision guardrail whose write path
    never re-examines what is already persisted only ever catches
    collisions this run itself proposes, not ones already sitting in the
    graph, so a real fixture is needed here for the assertion below to mean
    anything (an empty starting graph makes it trivially true)."""
    first = _make_member("mp-1", "Sir John Smith")
    second = _make_member("mp-2", "Sir John Smith")
    officer = _make_officer("officer-1", "SMITH, John Robert, Sir")
    pre_existing_edge = Edge.objects.create(
        edge_type="same_as", source_entity=first, target_entity=officer
    )
    Attestation.objects.create(
        edge=pre_existing_edge,
        source_name="Cross-register name match",
        match_confidence=0.85,
        match_method="surname_forename_title",
    )

    resolve_cross_register_identities()

    claimants_by_officer: dict[int, set[int]] = {}
    for edge in Edge.objects.filter(edge_type="same_as"):
        claimants_by_officer.setdefault(edge.target_entity_id, set()).add(edge.source_entity_id)

    violations = {
        officer_id: members
        for officer_id, members in claimants_by_officer.items()
        if len(members) > 1
    }
    assert violations == {}
    assert not Edge.objects.filter(edge_type="same_as", target_entity=officer).exists()
    assert not Edge.objects.filter(edge_type="same_as", source_entity=first).exists()
    assert not Edge.objects.filter(edge_type="same_as", source_entity=second).exists()


@pytest.mark.django_db
def test_distinct_members_matching_distinct_officers_are_unaffected():
    """GIVEN two distinct real members who resolve to two DIFFERENT officer
    records (no collision at all)
    WHEN identities are resolved
    THEN both edges are created normally -- the ownership guardrail must
    only drop claims that actually collide on one officer, not every edge
    a contested-looking name happens to produce."""
    cox = _make_member("mp-1", "Sir Geoffrey Cox")
    agnew = _make_member("mp-2", "Lord Agnew of Oulton")
    officer_cox = _make_officer("officer-1", "COX, Geoffrey Charles, Sir")
    officer_agnew = _make_officer("officer-2", "AGNEW, Theodore Thomas More, Lord")

    resolve_cross_register_identities()

    cox_edge = Edge.objects.get(edge_type="same_as", source_entity=cox)
    assert cox_edge.target_entity_id == officer_cox.id
    agnew_edge = Edge.objects.get(edge_type="same_as", source_entity=agnew)
    assert agnew_edge.target_entity_id == officer_agnew.id
