"""Tests for peerage name resolution in the Phase C matcher.

These exist because a positive-control run found that 14 of 15 resolution
failures came from one bug: taking the last token of a peerage name yields the
territorial designation, not the family name.
"""

from scripts.phase_c_paths import surname


def test_territorial_designation_is_not_the_surname():
    """GIVEN a peerage name with a territorial designation
    WHEN the surname is extracted
    THEN it is the family name, not the place.

    "Baroness Mone of Mayfair" must resolve to "mone" — an external table
    writes "Baroness Mone", so matching on "mayfair" can never succeed.
    """
    assert surname("Baroness Mone of Mayfair") == "mone"


def test_territorial_designation_stripped_for_lords():
    """GIVEN other peers written with 'of <place>'
    WHEN the surname is extracted
    THEN the place is discarded."""
    assert surname("Lord Agnew of Oulton") == "agnew"
    assert surname("Lord Allan of Hallam") == "allan"


def test_honorific_is_not_the_surname():
    """GIVEN a knighthood honorific
    WHEN the surname is extracted
    THEN the family name is returned, not the title."""
    assert surname("Sir Gavin Williamson") == "williamson"


def test_plain_name_is_unchanged():
    """GIVEN an MP written without any title
    WHEN the surname is extracted
    THEN the last name is returned."""
    assert surname("Kim Leadbeater") == "leadbeater"


def test_empty_name_returns_empty_string():
    """GIVEN no name
    WHEN the surname is extracted
    THEN the result is empty rather than an exception."""
    assert surname("") == ""
    assert surname(None) == ""


def test_single_token_name_survives():
    """GIVEN a name that is already just a surname
    WHEN extracted
    THEN it is returned lowercased."""
    assert surname("Adebowale") == "adebowale"


def test_post_nominal_is_not_the_surname():
    """GIVEN an MP written with a post-nominal
    WHEN the surname is extracted
    THEN it is the family name, not "mp".

    Every MP in a cohort otherwise collapses onto the same key — this produced
    four phantom matches in Phase C, all pointing at one unrelated officer.
    """
    assert surname("Matt Hancock MP") == "hancock"
    assert surname("Dr Julian Lewis MP") == "lewis"


def test_companies_house_comma_format_uses_the_leading_surname():
    """GIVEN the CH "SURNAME, Forenames, Title" format
    WHEN the surname is extracted
    THEN it is the part before the first comma, not the last token.

    "LEWIS, John Patrick, Sir" indexed under "patrick" before this fix, so all
    21,082 officer entities were keyed by a forename.
    """
    assert surname("LEWIS, John Patrick, Sir") == "lewis"
    assert surname("AGNEW, Theodore Thomas More, Lord") == "agnew"
