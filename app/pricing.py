import math
import re
from decimal import Decimal

from .models import BetterBuy, Offer, PartResult, PricedOffer


def price_offer(offer: Offer, qty: int) -> PricedOffer:
    warnings = list(offer.warnings)
    mult = offer.multiple or 1
    order = max(qty, offer.moq or 1)
    if mult > 1 and order % mult:
        order = math.ceil(order / mult) * mult
        warnings.append("rounded_to_multiple")
    breaks = sorted(offer.price_breaks, key=lambda b: b.qty)
    base = offer.model_dump(exclude={"warnings"})
    if not breaks:
        return PricedOffer(**base, warnings=warnings, requested_qty=qty, order_qty=order,
                           in_stock=_in_stock(offer, order), excluded_from_best="no_price")
    if order < breaks[0].qty:
        order = breaks[0].qty
        if mult > 1 and order % mult:
            order = math.ceil(order / mult) * mult
        warnings.append("raised_to_first_break")
    fee = offer.extra_fee or Decimal(0)
    unit = [b for b in breaks if b.qty <= order][-1].unit_price
    extended = unit * order + fee
    better = None
    for b in breaks:
        cost = b.unit_price * b.qty + fee
        if b.qty > order and cost < extended and (better is None or cost < better.extended_price):
            better = BetterBuy(qty=b.qty, unit_price=b.unit_price, extended_price=cost)
    return PricedOffer(**base, warnings=warnings, requested_qty=qty, order_qty=order, unit_price=unit,
                       extended_price=extended, in_stock=_in_stock(offer, order), better_buy=better)


def _in_stock(offer: Offer, order: int) -> bool | None:
    return None if offer.stock is None else offer.stock >= order


def _words(s: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").casefold())


def manufacturer_matches(wanted: str | None, got: str | None) -> bool:
    a, b = _words(wanted), _words(got)
    if not a:
        return True
    return bool(b) and (a[0] in b or b[0] in a)


def _pick(pool: list[PricedOffer]) -> tuple[PricedOffer | None, str | None, list[str]]:
    if not pool:
        return None, None, []
    if len({o.currency for o in pool}) > 1:
        return None, None, ["currency_mismatch"]
    stocked = [o for o in pool if o.in_stock]
    if stocked:
        return min(stocked, key=lambda o: (o.extended_price, -(o.stock or 0))), "cheapest_in_stock", []
    return min(pool, key=lambda o: o.extended_price), "no_stock_anywhere", []


def choose_best(priced: list[PricedOffer], manufacturer: str | None):
    """Marks excluded_from_best on each offer. Returns (best, reason, best_by_provider, warnings)."""
    pool = []
    for o in priced:
        if not o.excluded_from_best:
            if o.marketplace:
                o.excluded_from_best = "marketplace"
            elif o.match == "loose":
                o.excluded_from_best = "loose_match"
            elif manufacturer and not manufacturer_matches(manufacturer, o.manufacturer):
                o.excluded_from_best = "manufacturer_mismatch"
        if not o.excluded_from_best:
            pool.append(o)
    best, reason, warnings = _pick(pool)
    by_provider = {}
    for name in dict.fromkeys(o.provider for o in priced):
        by_provider[name] = _pick([o for o in pool if o.provider == name])[0]
    return best, reason, by_provider, warnings


def _money(d) -> float:
    return float(round(Decimal(d), 5))


def bom_summary(lines: list[PartResult], providers: list[str]) -> dict:
    n = len(lines)
    best = [ln.best for ln in lines if ln.best]
    currencies = {o.currency for o in best}
    totals = {}
    for p in providers:
        covered = [ln.best_by_provider.get(p) for ln in lines if ln.best_by_provider.get(p)]
        totals[p] = {"total": _money(sum(o.extended_price for o in covered)), "lines_covered": len(covered),
                     "lines_missing": n - len(covered)}
    totals["best_mix"] = {"total": _money(sum(o.extended_price for o in best)),
                          "lines_by_provider": {p: sum(o.provider == p for o in best) for p in providers}}
    return {
        "lines": n,
        "priced": len(best),
        # not found = every provider answered and none had it; a provider error never counts as not found
        "not_found": sum(not ln.offers and all(s.ok for s in ln.provider_status) for ln in lines),
        "short_stock": sum(ln.best is not None and ln.best.in_stock is False for ln in lines),
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "totals": totals,
    }
