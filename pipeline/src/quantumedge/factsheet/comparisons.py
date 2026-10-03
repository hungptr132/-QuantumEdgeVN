"""Turn stored benchmarks into the comparison block a reader sees.

Two jobs, kept apart from `build.py` because both are judgement-shaped and deserve to be
read on their own:

1. **Band a figure** against its sector's interquartile range, and against its own
   trailing range.
2. **Say so in Vietnamese**, positionally.

The banding rule is the only real decision here, and it is deliberately conservative.
A figure counts as `above` only if it exceeds **p75**, and `below` only under **p25**;
anything between is `in_line`. Comparing against the median alone would announce a figure
one dong above the midpoint as "above the sector" — with a sample of three peers, that is
noise reported as a finding.

What this module must never do is evaluate. `above` means a larger number, not a better
company. Turnover above the sector median is more liquid; a price above par is neither
good nor bad. Several metrics here are ones where "higher" is plainly worse
(`zero_trade_ratio_3m`, `sessions_to_exit`), and the vocabulary has to stay honest across
all of them, so it describes position and stops.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.factsheet.schema import BenchmarkRef, Comparison, Provenance

NOT_DISCLOSED = "không công bố"

# Where a stock sits in its own trailing range, as a fraction of (max - min).
NEAR_EDGE = Decimal("0.25")

SECTOR_LABELS = {
    "above": "Cao hơn nhóm ngành",
    "below": "Thấp hơn nhóm ngành",
    "in_line": "Tương đương nhóm ngành",
    "unknown": NOT_DISCLOSED,
}

RANGE_LABELS = {
    "near_high": "Gần mức cao nhất của khoảng",
    "near_low": "Gần mức thấp nhất của khoảng",
    "mid_range": "Ở giữa khoảng",
    "unknown": NOT_DISCLOSED,
}

WINDOW_LABELS = {
    "current": "Trung vị nhóm ngành",
    "3y": "Khoảng 3 năm của chính mã này",
    "1y": "Khoảng 1 năm của chính mã này",
}

# Why a benchmark is absent, in the reader's language. An unexplained blank invites the
# assumption that the comparison was unfavourable and withheld.
REASON_LABELS = {
    "sample_below_minimum": "Số mã trong nhóm ngành chưa đủ để tính trung vị",
    "no_sample": "Chưa có dữ liệu so sánh",
}


def _position_vs_sector(value: Decimal, bench: dict[str, Any]) -> str:
    p25, p75 = bench["p25"], bench["p75"]
    if p25 is None or p75 is None:
        return "unknown"
    if value > Decimal(str(p75)):
        return "above"
    if value < Decimal(str(p25)):
        return "below"
    return "in_line"


def _position_in_range(value: Decimal, bench: dict[str, Any]) -> str:
    low, high = bench["min_value"], bench["max_value"]
    if low is None or high is None:
        return "unknown"
    low_d, high_d = Decimal(str(low)), Decimal(str(high))
    span = high_d - low_d
    if span <= 0:
        # The metric has not moved across the window. Saying "near the high" of a flat
        # line would imply movement that did not happen.
        return "mid_range"
    fraction = (value - low_d) / span
    if fraction >= 1 - NEAR_EDGE:
        return "near_high"
    if fraction <= NEAR_EDGE:
        return "near_low"
    return "mid_range"


def _to_ref(
    bench: dict[str, Any],
    *,
    formatter: Any,
    members: list[str],
) -> BenchmarkRef:
    available = bool(bench["is_available"])
    median = bench["median"]
    low, high = bench["min_value"], bench["max_value"]

    return BenchmarkRef(
        scope=bench["scope"],
        window_label=bench["window_label"],
        label_vi=WINDOW_LABELS.get(bench["window_label"], bench["window_label"]),
        median=median,
        p25=bench["p25"],
        p75=bench["p75"],
        min_value=low,
        max_value=high,
        display_median=formatter(median) if available else NOT_DISCLOSED,
        display_range=(
            f"{formatter(low)} – {formatter(high)}"
            if available and low is not None
            else NOT_DISCLOSED
        ),
        sample_size=int(bench["sample_size"]),
        available=available,
        unavailable_reason=(
            REASON_LABELS.get(bench["unavailable_reason"], bench["unavailable_reason"])
            if not available
            else None
        ),
        members=members,
        provenance=[
            Provenance(
                kind="benchmark",
                ref=f"metric_benchmark:{bench['id']}",
                label=(
                    f"{bench['metric_code']} · {bench['scope']}/{bench['window_label']} · "
                    f"n={bench['sample_size']}"
                ),
            )
        ]
        if available
        else [],
    )


def load_comparisons(
    session: Session,
    ticker_id: int,
    as_of: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Every stored benchmark relevant to this ticker, keyed by metric code.

    One query for all of them rather than one per figure: a sheet has ~18 metrics and
    three benchmarks each, and per-figure queries would make building a sheet noticeably
    slower for no gain in clarity.
    """
    rows = session.execute(
        text(
            """
            SELECT b.id, b.metric_code, b.scope::text AS scope, b.window_label,
                   b.median, b.p25, b.p75, b.min_value, b.max_value,
                   b.sample_size, b.is_available, b.unavailable_reason,
                   b.member_ticker_ids
            FROM metric_benchmark b
            LEFT JOIN company c ON c.sector_id = b.sector_id
            WHERE b.as_of_date = CAST(:d AS date)
              AND (
                    b.ticker_id = :t
                    OR (b.scope = 'sector' AND c.id = (
                          SELECT company_id FROM ticker WHERE id = :t))
                  )
            ORDER BY b.metric_code, b.scope, b.window_label
            """
        ),
        {"t": ticker_id, "d": as_of},
    ).mappings()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["metric_code"], []).append(dict(row))
    return grouped


def _symbols(session: Session, ticker_ids: list[int]) -> list[str]:
    if not ticker_ids:
        return []
    return list(
        session.execute(
            text("SELECT symbol FROM ticker WHERE id = ANY(:ids) ORDER BY symbol"),
            {"ids": ticker_ids},
        ).scalars()
    )


def build_comparison(
    session: Session,
    *,
    value: Decimal | None,
    benchmarks: list[dict[str, Any]],
    formatter: Any,
) -> Comparison | None:
    """Assemble one figure's comparison block, or None if there is nothing to compare to.

    An unavailable *figure* still gets its comparison when benchmarks exist: knowing the
    sector median while this company's own number is missing is exactly the kind of gap
    worth showing, rather than hiding both.
    """
    if not benchmarks:
        return None

    sector_ref: BenchmarkRef | None = None
    history: list[BenchmarkRef] = []
    position_sector = "unknown"
    position_range = "unknown"

    for bench in benchmarks:
        members = _symbols(session, list(bench["member_ticker_ids"] or []))
        ref = _to_ref(bench, formatter=formatter, members=members)

        if bench["scope"] == "sector":
            sector_ref = ref
            if value is not None and bench["is_available"]:
                position_sector = _position_vs_sector(value, bench)
        else:
            history.append(ref)
            # The 3-year window is the headline one; 1y is shown but does not set the
            # label, so the descriptor means one fixed thing on every page.
            if bench["window_label"] == "3y" and value is not None and bench["is_available"]:
                position_range = _position_in_range(value, bench)

    return Comparison(
        sector=sector_ref,
        own_history=history,
        position_vs_sector=position_sector,  # type: ignore[arg-type]
        position_vs_sector_label_vi=SECTOR_LABELS[position_sector],
        position_vs_own_range=position_range,  # type: ignore[arg-type]
        position_vs_own_range_label_vi=RANGE_LABELS[position_range],
    )
