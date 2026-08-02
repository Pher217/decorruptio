"""Tests for cross-register identity assertion (parliament <-> CH officer)."""

from uncorrupt.graph.identity_resolution import (
    _titles_compatible,
    parse_officer_name,
    parse_parliament_name,
)


def test_parses_peerage_name_dropping_territorial_designation():
    """GIVEN a peerage name with a territorial designation
    WHEN parsed
    THEN the surname is the family name and no forename is claimed."""
    assert parse_parliament_name("Lord Agnew of Oulton") == {
        "surname": "agnew",
        "forename": None,
        "title": "lord",
    }


def test_parses_knighted_mp_name():
    """GIVEN an MP with an honorific
    WHEN parsed
    THEN title, forename and surname are all recovered."""
    assert parse_parliament_name("Sir Geoffrey Cox") == {
        "surname": "cox",
        "forename": "geoffrey",
        "title": "sir",
    }


def test_parses_untitled_mp_name():
    """GIVEN an MP with no title
    WHEN parsed
    THEN title is None."""
    assert parse_parliament_name("Danny Kruger") == {
        "surname": "kruger",
        "forename": "danny",
        "title": None,
    }


def test_parses_officer_name_surname_forename_title():
    """GIVEN the Companies House "SURNAME, Forenames, Title" format
    WHEN parsed
    THEN each component is recovered."""
    assert parse_officer_name("COX, Geoffrey Charles, Sir") == {
        "surname": "cox",
        "forename": "geoffrey",
        "title": "sir",
    }


def test_parses_officer_name_without_title():
    """GIVEN an officer record with no title
    WHEN parsed
    THEN title is None and the forename is still recovered."""
    assert parse_officer_name("KRUGER, Daniel John") == {
        "surname": "kruger",
        "forename": "daniel",
        "title": None,
    }


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
