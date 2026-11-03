import math
import re
from decimal import Decimal, InvalidOperation


def mpn_strict(s: str | None) -> str:
    return re.sub(r"\s+", "", s or "").upper()


def mpn_loose(s: str | None) -> str:
    return re.sub(r"[-/.,#()+]", "", mpn_strict(s))


def match_kind(query: str, candidate: str | None) -> str | None:
    """'exact' | 'loose' | None (not a match)."""
    if not candidate:
        return None
    if mpn_strict(query) == mpn_strict(candidate):
        return "exact"
    if mpn_loose(query) and mpn_loose(query) == mpn_loose(candidate):
        return "loose"
    return None


def parse_price(s) -> Decimal | None:
    if s is None:
        return None
    if isinstance(s, (int, float, Decimal)):
        return Decimal(str(s))
    s = re.sub(r"[^\d.,]", "", str(s)).strip(".,")
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:  # the last separator is the decimal one
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:  # exactly 3 digits after the last comma = thousands separator
        s = s.replace(",", "") if len(s.rsplit(",", 1)[1]) == 3 else s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_lead_time_weeks(s) -> int | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return math.ceil(s)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(day|week)?", str(s), re.I)
    if not m:
        return None
    n = float(m.group(1))
    return math.ceil(n / 7) if (m.group(2) or "").lower() == "day" else math.ceil(n)


def parse_int_stock(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    m = re.search(r"\d[\d,]*", str(v))
    return int(m.group().replace(",", "")) if m else 0


def to_int(v) -> int | None:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
