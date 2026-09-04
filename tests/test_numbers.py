"""Phone numbers, as people write them."""

from __future__ import annotations

import pytest

from njiwa_shopify import numbers


class TestToMsisdn:
    @pytest.mark.parametrize(
        "raw",
        ["254712345678", "+254 712 345 678", "0712345678", "(0712) 345-678", "00254712345678"],
    )
    def test_the_documented_forms_agree(self, raw):
        assert numbers.to_msisdn(raw, "KE") == "254712345678"

    def test_a_plus_stops_the_country_having_a_say(self):
        assert numbers.to_msisdn("+44 7911 123456", "KE") == "447911123456"

    def test_a_local_number_gets_its_country(self):
        assert numbers.to_msisdn("07911 123456", "GB") == "447911123456"
        assert numbers.to_msisdn("(212) 555-1234", "US") == "12125551234"

    def test_a_us_number_that_already_has_its_one(self):
        assert numbers.to_msisdn("1 212 555 1234", "US") == "12125551234"

    def test_no_country_passes_it_through(self):
        assert numbers.to_msisdn("0712345678", "") == "0712345678"
        assert numbers.to_msisdn("0712345678", None) == "0712345678"

    def test_nothing_usable_is_empty(self):
        assert numbers.to_msisdn("", "KE") == ""
        assert numbers.to_msisdn(None, "KE") == ""
        assert numbers.to_msisdn("call me", "KE") == ""
        assert numbers.to_msisdn("12345", "KE") == ""  # a typo, not a short Kenyan number
        assert numbers.to_msisdn("+1234567890123456", "KE") == ""  # sixteen digits


class TestParseList:
    def test_comma_space_and_newline_separated(self):
        assert numbers.parse_list("254700000001, 254700000002\n+254 700 000 003") == [
            "254700000001",
            "254700000002",
            "254700000003",
        ]

    def test_duplicates_collapse(self):
        assert numbers.parse_list("254700000001, 254700000001") == ["254700000001"]

    def test_a_group_address_is_dropped_whole(self):
        assert numbers.parse_list("120363028712345678@g.us") == []
        assert numbers.rejected_from_list("120363028712345678@g.us, 254700000001") == [
            "120363028712345678@g.us"
        ]

    def test_too_short_is_dropped(self):
        assert numbers.parse_list("12345, 254700000001") == ["254700000001"]
        assert numbers.rejected_from_list("12345, 254700000001") == ["12345"]

    def test_empty(self):
        assert numbers.parse_list("") == []
        assert numbers.parse_list(None) == []
