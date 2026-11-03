from datetime import datetime, timezone
from decimal import Decimal as D

from app.models import Offer, PriceBreak
from app.pricing import choose_best, manufacturer_matches, price_offer

BREAKS = [(1, "0.42"), (10, "0.297"), (100, "0.186")]


def offer(breaks=BREAKS, **kw):
    kw.setdefault("moq", 1)
    kw.setdefault("multiple", 1)
    return Offer(provider=kw.pop("provider", "digikey"), mpn="LM358DR", fetched_at=datetime.now(timezone.utc),
                 price_breaks=[PriceBreak(qty=q, unit_price=D(p)) for q, p in breaks], currency=kw.pop("currency", "USD"),
                 **kw)


def test_qty_50():
    p = price_offer(offer(), 50)
    assert (p.order_qty, p.unit_price, p.extended_price, p.better_buy) == (50, D("0.297"), D("14.85"), None)


def test_qty_90_better_buy():
    p = price_offer(offer(), 90)
    assert (p.unit_price, p.extended_price) == (D("0.297"), D("26.73"))
    assert (p.better_buy.qty, p.better_buy.unit_price, p.better_buy.extended_price) == (100, D("0.186"), D("18.60"))


def test_reel_moq_and_multiple():
    p = price_offer(offer([(2500, "0.104")], moq=2500, multiple=2500), 100)
    assert (p.order_qty, p.extended_price) == (2500, D("260.00"))


def test_multiple_rounding():
    p = price_offer(offer(multiple=4), 10)
    assert p.order_qty == 12 and "rounded_to_multiple" in p.warnings


def test_raised_to_first_break():
    p = price_offer(offer([(10, "0.3")], moq=None), 5)
    assert p.order_qty == 10 and "raised_to_first_break" in p.warnings


def test_short_stock():
    assert price_offer(offer(stock=30), 50).in_stock is False
    assert price_offer(offer(stock=None), 50).in_stock is None


def test_no_breaks():
    p = price_offer(offer([]), 5)
    assert p.unit_price is None and p.extended_price is None and p.excluded_from_best == "no_price"


def test_extra_fee_added():
    p = price_offer(offer(extra_fee=D("7")), 50)
    assert p.extended_price == D("21.85")


def test_best_prefers_in_stock():
    cheap_no_stock = price_offer(offer([(1, "0.10")], stock=0, provider="mouser"), 50)
    pricier_stocked = price_offer(offer(stock=1000), 50)
    best, reason, by_p, w = choose_best([cheap_no_stock, pricier_stocked], None)
    assert best is pricier_stocked and reason == "cheapest_in_stock" and w == []
    assert by_p["mouser"] is cheap_no_stock


def test_best_tie_higher_stock_wins():
    a, b = price_offer(offer(stock=100), 50), price_offer(offer(stock=900, provider="mouser"), 50)
    assert choose_best([a, b], None)[0] is b


def test_no_stock_anywhere():
    a, b = price_offer(offer(stock=0), 50), price_offer(offer([(1, "0.1")], stock=0, provider="mouser"), 50)
    best, reason, _, _ = choose_best([a, b], None)
    assert best is b and reason == "no_stock_anywhere"


def test_exclusions():
    mp = price_offer(offer([(1, "0.01")], stock=999, marketplace=True), 1)
    loose = price_offer(offer([(1, "0.02")], stock=999, match="loose"), 1)
    wrong_mfr = price_offer(offer([(1, "0.03")], stock=999, manufacturer="ON Semiconductor"), 1)
    good = price_offer(offer(stock=999, manufacturer="Texas Instruments Incorporated"), 1)
    best, *_ = choose_best([mp, loose, wrong_mfr, good], "Texas Instruments")
    assert best is good
    assert (mp.excluded_from_best, loose.excluded_from_best, wrong_mfr.excluded_from_best) == (
        "marketplace", "loose_match", "manufacturer_mismatch")


def test_currency_mismatch():
    a, b = price_offer(offer(stock=9), 1), price_offer(offer(stock=9, currency="EUR", provider="mouser"), 1)
    best, reason, by_p, w = choose_best([a, b], None)
    assert best is None and w == ["currency_mismatch"] and by_p["digikey"] is a


def test_manufacturer_matches():
    assert manufacturer_matches("Texas Instruments", "texas instruments inc")
    assert manufacturer_matches("Microchip Technology", "Microchip")
    assert not manufacturer_matches("Texas Instruments", "ON Semiconductor")
    assert not manufacturer_matches("Texas Instruments", None)
    assert manufacturer_matches(None, "anything")
