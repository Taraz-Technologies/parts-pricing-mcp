from decimal import Decimal

import pytest

from app.normalize import match_kind, mpn_loose, mpn_strict, parse_int_stock, parse_lead_time_weeks, parse_price


@pytest.mark.parametrize("raw,want", [
    ("$0.54", "0.54"), ("0,54 €", "0.54"), ("1.234,56 €", "1234.56"), ("$1,234.56", "1234.56"),
    ("PKR 120.00", "120.00"), ("1,234", "1234"), ("12,5", "12.5"), ("$0.00012", "0.00012"),
    ("", None), (None, None), (0.42, "0.42"),
])
def test_parse_price(raw, want):
    assert parse_price(raw) == (Decimal(want) if want else None)


@pytest.mark.parametrize("raw,want", [
    ("42 Days", 6), ("45 Days", 7), ("10 Weeks", 10), ("52", 52), ("", None), (None, None), ("N/A", None),
])
def test_parse_lead_time(raw, want):
    assert parse_lead_time_weeks(raw) == want


@pytest.mark.parametrize("raw,want", [
    (12345, 12345), ("12345", 12345), ("1,234 In Stock", 1234), ("None", 0), ("On Order", 0), ("", 0), (None, None),
])
def test_parse_int_stock(raw, want):
    assert parse_int_stock(raw) == want


def test_mpn():
    assert mpn_strict(" lm358 dr ") == "LM358DR"
    assert mpn_loose("LM1117IMPX-3.3/NOPB") == "LM1117IMPX33NOPB"
    assert mpn_loose("B2B-XH-A(LF)(SN)") == "B2BXHALFSN"
    assert match_kind("lm358dr", "LM358DR") == "exact"
    assert match_kind("74HC595D,118", "74HC595D118") == "loose"
    assert match_kind("LM358DR", "LM358D") is None
    assert match_kind("LM358DR", None) is None
