import difflib
import math
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache


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


# --- spec values: a caller's "10nF" vs DigiKey's parametric value names ("0.01 µF", "10 nF", "10000 pF")
_PREFIX = {"p": -12, "P": -12, "n": -9, "N": -9, "u": -6, "U": -6, "µ": -6, "m": -3, "k": 3, "K": 3, "M": 6, "G": 9}
# Only m (milli) vs M (mega) is case-sensitive: Taraz codes write 10NF, 100PF, 0.1UF, 1KV. The unit's case never
# matters; R is ohms (2.2R, 0R). Known units only, so "0603 Metric" is no quantity.
_QTY = re.compile(r"(\d+(?:\.\d+)?)([pPnNuUµmkKMG]?)((?i:f|v|ohms?|ω|r|h|a|w|hz|%)?)")
_TRAILING_PAREN = re.compile(r"(.+?)\s*\(([^()]+)\)")
_SPLIT = re.compile(r",|\||(?<=[A-Za-z])/")  # "C0G, NP0", "10 Ohms|Ohms", "600V/630V"; never the 1/10 of "1/10W"


def _plain(s: str) -> str:
    return re.sub(r"\s+", "", s).replace("±", "").replace("μ", "µ")


@lru_cache(maxsize=65536)
def _text(s: str) -> str:
    # NP0 and C0G are one dielectric (DigiKey's own value is "C0G, NP0"), also written NPO/COG with a letter O
    return re.sub(r"np[0o]|c[0o]g", "c0g", _plain(s).replace("µ", "u").casefold())


@lru_cache(maxsize=65536)
def parse_quantity(s: str) -> tuple[Decimal, str, bool] | None:
    """'10nF' -> (1E-8, 'f', True), '10K' -> (1E+4, '', True), '0603' -> (603, '', False); None if no quantity.
    The bool: it had a prefix or unit. Two bare numbers compare as text, so '1005' never equals '01005'."""
    m = _QTY.fullmatch(_plain(s))
    if not m:
        return None
    unit = m[3].casefold()
    return (Decimal(m[1]).scaleb(_PREFIX.get(m[2], 0)), "ohm" if unit.startswith("ohm") or unit in ("ω", "r") else unit,
            bool(m[2] or m[3]))


@lru_cache(maxsize=65536)
def _pieces(value: str) -> tuple[str, ...]:
    """A DigiKey value, its ',' '|' 'A/B' pieces ('A, B' means the part is both), and the lead and paren of each
    'lead (paren)' piece: '0603 (1608 Metric)' -> ..., '0603', '1608 Metric'."""
    out = []
    for p in (value, *_SPLIT.split(value)):
        out.append(p := p.strip())
        if m := _TRAILING_PAREN.fullmatch(p):
            out += [m[1], m[2]]
    return tuple(out)


def _eq(want: str, piece: str) -> bool:
    w, q = parse_quantity(want), parse_quantity(piece)
    if w and q and (w[2] or q[2]):  # by value only: 10nF = 0.01 µF = 10000 pF, 500 = 500V, 1 mOhms != 1 MOhms
        return w[0] == q[0] and (not w[1] or not q[1] or w[1] == q[1])
    return _text(want) == _text(piece)  # case, spaces, '±' and µ/u ignored


def match_values(want: str, values: list[str]) -> list[str]:
    """Every DigiKey value name that is or includes `want`: 'NP0' matches 'C0G, NP0', '0.1W' matches '0.1W, 1/10W',
    '0603' matches '0603 (1608 Metric)' (and its ', 8 PC Pad' variant), never '0201 (0603 Metric)'."""
    return [v for v in values if any(_eq(want, p) for p in _pieces(v))]


def closest_values(want: str, values: list[str], n: int = 8) -> list[str]:
    """For error messages: the values nearest in quantity (same unit) when `want` is one, else by spelling."""
    w, dist = parse_quantity(want), {}
    for v in values if w and w[2] else ():
        for q in filter(None, map(parse_quantity, _pieces(v))):
            if q[2] and (not w[1] or not q[1] or w[1] == q[1]):
                dist[v] = min(abs(q[0] - w[0]), dist.get(v, abs(q[0] - w[0])))
    return sorted(dist, key=dist.get)[:n] if dist else difflib.get_close_matches(want, values, n=n, cutoff=0.3)
