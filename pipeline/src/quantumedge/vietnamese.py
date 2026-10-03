"""Vietnamese number conventions, in one place.

Two things that are easy to get wrong and expensive to get wrong in public:

**The separators are swapped relative to English.** Vietnamese writes `1.234.567,89` —
dot for thousands, comma for the decimal. A page that prints `1,234,567.89` to a
Vietnamese reader is not merely unidiomatic; `1,5` and `1.5` mean different things, so it
is ambiguous about the actual value.

**The scale words are short-scale and used constantly.** `nghìn` (10³), `triệu` (10⁶),
`tỷ` (10⁹). A reader sees "1,37 tỷ", never "1,369,641,865", and a liquidity figure in full
digits is harder to compare at a glance, which defeats the point of showing it.

This module exists so that a threshold written once in `rules/flags.yaml` can be rendered
into the prose that describes it, rather than being typed twice and drifting apart — see
`db/seed.py`. That drift was a real bug: raising a threshold from 1 tỷ to 2 tỷ left nine
pages still saying "dưới 1 tỷ" while testing against 2 tỷ.
"""

from decimal import Decimal

# Short-scale words, largest first.
_SCALES: tuple[tuple[Decimal, str], ...] = (
    (Decimal(1_000_000_000_000), "nghìn tỷ"),
    (Decimal(1_000_000_000), "tỷ"),
    (Decimal(1_000_000), "triệu"),
    (Decimal(1_000), "nghìn"),
)


def vn_decimal(value: Decimal | int | float | str, places: int = 0) -> str:
    """Plain Vietnamese digit grouping: dot for thousands, comma for the decimal."""
    d = Decimal(str(value))
    # Format in English conventions, then swap. Doing it directly would mean
    # reimplementing grouping, and the swap is unambiguous because the intermediate
    # string only ever contains one comma-role and one dot-role.
    english = f"{d:,.{places}f}"
    return english.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def vn_scaled(value: Decimal | int | float | str, places: int = 1) -> str:
    """A magnitude a reader can take in: 1369641865 → "1,4 tỷ".

    Trailing zeros after the comma are dropped, so a round number reads as "2 tỷ" rather
    than "2,0 tỷ". Values below a thousand keep their digits, since scaling them would
    lose precision for no readability gain.
    """
    d = Decimal(str(value))
    sign = "-" if d < 0 else ""
    magnitude = abs(d)

    for factor, word in _SCALES:
        if magnitude >= factor:
            scaled = magnitude / factor
            text = vn_decimal(scaled, places)
            # "2,0 tỷ" says nothing "2 tỷ" does not.
            if "," in text:
                text = text.rstrip("0").rstrip(",")
            return f"{sign}{text} {word}"

    return f"{sign}{vn_decimal(magnitude, 0)}"


def vn_money(value: Decimal | int | float | str, places: int = 1) -> str:
    """A scaled figure with the currency: "1,4 tỷ đồng"."""
    return f"{vn_scaled(value, places)} đồng"
