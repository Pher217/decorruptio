"""Tests for `normalise_company_number` (padding bug fix).

External sources (Electoral Commission, Parliament, Lords register) supply
company numbers unpadded (`"7015428"`); Companies House stores them
zero-padded to 8 characters (`"07015428"`) — verified against the CH bulk
snapshot, where every one of 5.7M rows is exactly 8 characters. Without
normalisation, an exact-string join between an external identifier and
`Company.company_number` silently misses.
"""

from uncorrupt.staging.companies_house import normalise_company_number


class TestNormaliseCompanyNumber:
    def test_unpadded_numeric_is_zero_padded_to_eight_chars(self):
        """A numeric-only company number shorter than 8 chars gets zero-padded."""
        assert normalise_company_number("7015428") == "07015428"

    def test_prefixed_number_pads_only_the_numeric_part(self):
        """A letter-prefixed number pads the digits, never the whole string."""
        assert normalise_company_number("SC1234") == "SC001234"

    def test_already_canonical_eight_char_value_is_unchanged(self):
        """A value already 8 characters passes through unchanged."""
        assert normalise_company_number("08209948") == "08209948"

    def test_empty_string_returns_none(self):
        """An empty string is treated as no identifier."""
        assert normalise_company_number("") is None

    def test_none_returns_none(self):
        """None is treated as no identifier."""
        assert normalise_company_number(None) is None

    def test_whitespace_is_stripped_and_uppercased(self):
        """Surrounding whitespace is stripped and the value is uppercased."""
        assert normalise_company_number(" sc1234 ") == "SC001234"

    def test_value_longer_than_eight_chars_is_returned_unmangled(self):
        """An unparseable, over-length value is returned stripped/uppercased, never truncated."""
        assert normalise_company_number("123456789") == "123456789"

    def test_prefixed_number_already_eight_chars_is_unchanged(self):
        """A prefixed number already at canonical length is untouched."""
        assert normalise_company_number("NI059358") == "NI059358"
