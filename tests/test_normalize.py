from decimal import Decimal

import pytest

from app.normalize import (closest_values, match_kind, match_values, mpn_loose, mpn_strict, parse_int_stock,
                           parse_lead_time_weeks, parse_price, parse_quantity)


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


@pytest.mark.parametrize("raw,want", [
    ("10nF", ("1E-8", "f")), ("0.01 µF", ("1E-8", "f")), ("10000 pF", ("1E-8", "f")), ("0.1μF", ("1E-7", "f")),
    ("±10%", ("10", "%")), ("50V", ("50", "v")), ("10K", ("1E+4", "")), ("4.7 kOhms", ("4700", "ohm")),
    ("2.2Ω", ("2.2", "ohm")), ("1 MOhms", ("1E+6", "ohm")), ("1 mOhms", ("0.001", "ohm")), ("100mA", ("0.1", "a")),
    ("0603 Metric", None), ("-20%", None), ("1/10W", None), ("50VAC", None), ("X7R", None),
    # Taraz part-number spellings: TZCC10NF50V..., TZCC10PF500V..., TZCC100NF1KV..., TZR2.2R1%..., TZR0R...
    ("10NF", ("1E-8", "f")), ("10PF", ("1E-11", "f")), ("0.1UF", ("1E-7", "f")), ("1KV", ("1000", "v")),
    ("2.2R", ("2.2", "ohm")), ("0R", ("0", "ohm")), ("1MF", ("1E+6", "f")),  # M is mega, never micro
])
def test_parse_quantity(raw, want):
    assert parse_quantity(raw) == ((Decimal(want[0]), want[1], True) if want else None)


def test_bare_numbers_are_flagged():  # no prefix, no unit: compared as text, so 1005 != 01005
    assert parse_quantity("0603") == (Decimal(603), "", False) and parse_quantity("01005")[2] is False


# Real DigiKey value names (Ceramic Capacitors: fixtures/digikey_keyword_cat60_options.json; resistors: live 2026-11-25)
CAP = ["9100 pF", "0.01 µF", "10 nF", "10000 pF", "0.012 µF", "0.1 µF", "100 nF", "1 µF"]
PKG = ["0201 (0603 Metric)", "0402 (1005 Metric)", "0603 (1608 Metric)", "0603 (1608 Metric), 8 PC Pad",
       "0805 (2012 Metric)", "01005 (0402 Metric)", "1005 (2512 Metric)", "2-SMD", "2-SMD, Gull Wing"]
TC = ["C0G, NP0", "C0G, NP0 (1B)", "NP0", "X5R", "X7R", "X7R (2R1)", "X7S", "Y5V (F)"]
VOLT = ["6.3V", "50V", "500V", "500VAC", "600V/630V", "300VAC/440VAC", "1000V (1kV)", "2000V (2kV)"]
TOL = ["-20%, +80%", "±0.25pF", "±1%", "±10%", "±20%"]
RES = ["1 mOhms", "10 Ohms|Ohms", "10 Ohms", "100 Ohms", "1 kOhms", "10 kOhms", "10000 Ohms", "100 kOhms", "1 MOhms",
       "1000000 Ohms"]
POWER = ["0.06W", "0.063W, 1/16W", "0.07W", "0.1W, 1/10W", "0.125W, 1/8W", "10W"]


@pytest.mark.parametrize("want,values,hits", [
    ("10nF", CAP, ["0.01 µF", "10 nF", "10000 pF"]),  # one capacitance, three DigiKey spellings
    ("10NF", CAP, ["0.01 µF", "10 nF", "10000 pF"]),  # as written in TZCC10NF50V10%X7R0603
    ("100n", CAP, ["0.1 µF", "100 nF"]), ("47nF", CAP, []),
    ("0603", PKG, ["0603 (1608 Metric)", "0603 (1608 Metric), 8 PC Pad"]),  # never 0201 (0603 Metric)
    ("1608 metric", PKG, ["0603 (1608 Metric)", "0603 (1608 Metric), 8 PC Pad"]),
    ("1005", PKG, ["1005 (2512 Metric)"]), ("01005", PKG, ["01005 (0402 Metric)"]),  # bare codes compare as text
    ("2-SMD", PKG, ["2-SMD", "2-SMD, Gull Wing"]),
    ("NP0", TC, ["C0G, NP0", "C0G, NP0 (1B)", "NP0"]), ("c0g", TC, ["C0G, NP0", "C0G, NP0 (1B)", "NP0"]),
    ("NPO", TC, ["C0G, NP0", "C0G, NP0 (1B)", "NP0"]), ("COG", TC, ["C0G, NP0", "C0G, NP0 (1B)", "NP0"]),  # letter O
    ("X7R", TC, ["X7R", "X7R (2R1)"]),
    ("50V", VOLT, ["50V"]), ("1kV", VOLT, ["1000V (1kV)"]), ("500", VOLT, ["500V"]),
    ("630V", VOLT, ["600V/630V"]), ("440VAC", VOLT, ["300VAC/440VAC"]),
    ("10%", TOL, ["±10%"]), ("±1%", TOL, ["±1%"]), ("0.25pF", TOL, ["±0.25pF"]), ("80%", TOL, []),
    ("0.1W", POWER, ["0.1W, 1/10W"]), ("10W", POWER, ["10W"]),  # "1/10W" is never split into "10W"
    ("10K", RES, ["10 kOhms", "10000 Ohms"]), ("10R", RES, ["10 Ohms|Ohms", "10 Ohms"]),
    ("1M", RES, ["1 MOhms", "1000000 Ohms"]), ("1 MOhms", RES, ["1 MOhms", "1000000 Ohms"]),  # never milliohms
    ("1 mOhms", RES, ["1 mOhms"]), ("120R", ["12 Ohms", "120 Ohms", "1.2 kOhms"], ["120 Ohms"]),
    ("0R", ["0 Ohms", "0 Ohms|Ohms", "0.1 Ohms"], ["0 Ohms", "0 Ohms|Ohms"]),
])
def test_match_values(want, values, hits):
    assert match_values(want, values) == hits


@pytest.mark.parametrize("want,values,first", [
    ("0.0625W", POWER, ["0.063W, 1/16W", "0.06W", "0.07W"]),  # by value: 1/16 W, not by spelling
    ("10.5nF", CAP, ["0.01 µF", "10 nF", "10000 pF"]),
    ("Y5F", TC, ["Y5V (F)"]),  # not a quantity: by spelling
])
def test_closest_values(want, values, first):
    assert closest_values(want, values)[:len(first)] == first
