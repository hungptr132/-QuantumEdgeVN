"""Agent 5 — liquidity risk. Pure code, no model, no judgement.

This lives in `calc/` rather than `agents/` because the pipeline diagram classes it as
deterministic alongside the rest of this stage, and it emits ordinary `metric_value` rows.

Framing is deliberate and fixed: these quantify how hard a position would be to EXIT. None
of them is a buy or sell signal, there is no direction, and there is no MACD or RSI here by
design. For a Vietnamese small-cap, being unable to sell is the risk retail investors
discover last.

The window convention matters more than it looks. Every window counts **trading sessions
from the calendar**, never rows of price data. Counting rows would quietly shorten the
window for exactly the illiquid stocks these metrics exist to describe — the ones with
missing bars — and make them look better than they are.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text

from quantumedge.calc.registry import MetricContext, MetricResult, MissingInput, metric

CALC_VERSION = "liquidity/v1"

SESSIONS_20 = 20
SESSIONS_3M = 63
SESSIONS_52W = 252

# How much of a session's turnover one seller may realistically take without moving the
# price. Stated in the output rather than buried: the estimate is meaningless without it.
PARTICIPATION_RATE = Decimal("0.20")

# Position sizes the exit estimate is reported for, in VND.
EXIT_POSITIONS_VND = (Decimal(100_000_000), Decimal(500_000_000), Decimal(1_000_000_000))

FRAMING_NOTE_VI = (
    "Các chỉ số dưới đây đo RỦI RO THOÁT VỊ THẾ - mức độ khó khăn khi cần bán ra. "
    "Đây không phải tín hiệu mua hoặc bán, và không dự báo giá."
)


@dataclass(frozen=True)
class Session_:
    """One trading session from the calendar, with its bar if one exists.

    `bar_missing` and `volume == 0` are different facts and both are preserved: the first
    means no data, the second means a real session in which nothing changed hands.
    """

    trade_date: date
    close: Decimal | None
    volume: int | None
    turnover: Decimal | None

    @property
    def bar_missing(self) -> bool:
        return self.close is None

    @property
    def zero_trade(self) -> bool:
        return self.volume == 0


def _window(ctx: MetricContext, sessions: int) -> list[Session_]:
    """The last N trading sessions up to as_of, left-joined to bars.

    The calendar drives the row set, so a session with no bar appears as a gap rather than
    vanishing from the window.
    """
    rows = ctx.session.execute(
        text(
            """
            SELECT mc.trade_date, pb.close, pb.volume, pb.turnover_vnd
            FROM market_calendar mc
            LEFT JOIN price_bar pb
                   ON pb.trade_date = mc.trade_date AND pb.ticker_id = :ticker_id
            WHERE mc.exchange_code = CAST(:exchange AS varchar(10))
              AND mc.is_trading_day
              AND mc.trade_date <= CAST(:as_of AS date)
            ORDER BY mc.trade_date DESC
            LIMIT :lim
            """
        ),
        {
            "ticker_id": ctx.ticker_id,
            "exchange": ctx.exchange_code,
            "as_of": ctx.as_of,
            "lim": sessions,
        },
    ).mappings()
    return [
        Session_(
            trade_date=r["trade_date"],
            close=r["close"],
            volume=r["volume"],
            turnover=r["turnover_vnd"],
        )
        for r in rows
    ]


def _require_window(ctx: MetricContext, sessions: int, label: str) -> list[Session_]:
    window = _window(ctx, sessions)
    if not window:
        raise MissingInput("no_trading_calendar")
    if len(window) < sessions:
        raise MissingInput(f"insufficient_history:{len(window)}of{sessions}_{label}")
    return window


def _traded(window: list[Session_]) -> list[Session_]:
    return [s for s in window if not s.bar_missing]


def _median_turnover(window: list[Session_]) -> Decimal:
    """Median turnover across CALENDAR sessions, counting a missing bar as zero.

    Why the median and not the mean: POM, in real data, showed 16 of 20 sessions with zero
    volume and four block trades of 3.6-11.9bn VND. The mean said 1,370mn per session and
    implied a position could be exited in under one session. There is no such session - the
    typical one has no turnover at all. The median says zero, which is the truth.
    """
    values = sorted((s.turnover or Decimal(0)) for s in window)
    n = len(values)
    if n == 0:
        return Decimal(0)
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / Decimal(2)


# A quoted price does not vanish because one session printed no bar. Look back far enough
# to find the last real trade, and record how stale it is so the page can say so.
MAX_PRICE_LOOKBACK = 20


def _latest_traded(ctx: MetricContext) -> tuple[Session_, int] | None:
    """Most recent session that actually has a bar, plus how many sessions back it was.

    Using a one-session window here was a real bug: POM has nine sessions in three years
    with no bar, and on any of those dates close_price reported
    "no_price_bar_at_or_before_as_of" - which was false, since bars existed on both sides.
    Worse, price_to_par went unavailable with it, which degraded price_below_par_value from
    active to unknown. A missing bar was suppressing a genuine risk flag.
    """
    window = _window(ctx, MAX_PRICE_LOOKBACK)
    for offset, session in enumerate(window):
        if not session.bar_missing:
            return session, offset
    return None


# ───────────────────────── price ─────────────────────────


@metric("close_price")
def close_price(ctx: MetricContext) -> MetricResult:
    found = _latest_traded(ctx)
    if found is None:
        raise MissingInput(f"no_price_bar_within_{MAX_PRICE_LOOKBACK}_sessions")
    latest, stale = found
    return MetricResult(
        value=latest.close,
        inputs={
            "trade_date": str(latest.trade_date),
            "close_vnd": str(latest.close),
            # How far back the quote came from. Zero means the as_of session itself.
            "sessions_stale": stale,
        },
    )


@metric("price_to_par")
def price_to_par(ctx: MetricContext) -> MetricResult:
    found = _latest_traded(ctx)
    if found is None:
        raise MissingInput(f"no_price_bar_within_{MAX_PRICE_LOOKBACK}_sessions")
    latest, stale = found
    if latest.close is None:
        raise MissingInput(f"no_price_bar_within_{MAX_PRICE_LOOKBACK}_sessions")
    if ctx.par_value_vnd == 0:
        raise MissingInput("par_value_is_zero")
    return MetricResult(
        value=(latest.close / ctx.par_value_vnd).quantize(Decimal("0.0001")),
        inputs={
            "close_vnd": str(latest.close),
            "par_value_vnd": str(ctx.par_value_vnd),
            "trade_date": str(latest.trade_date),
            "sessions_stale": stale,
        },
    )


# ───────────────────────── volume and turnover ─────────────────────────


@metric("avg_volume_20s")
def avg_volume_20s(ctx: MetricContext) -> MetricResult:
    window = _require_window(ctx, SESSIONS_20, "sessions")
    traded = _traded(window)
    if not traded:
        raise MissingInput("no_bars_in_20_sessions")
    total = sum(s.volume or 0 for s in traded)
    # Divided by SESSIONS, not by bars present. A stock that only printed 12 bars in 20
    # sessions genuinely averaged less volume per session, and dividing by 12 would flatter
    # exactly the illiquidity this measures.
    return MetricResult(
        value=(Decimal(total) / Decimal(SESSIONS_20)).quantize(Decimal("0.01")),
        inputs={
            "sessions": SESSIONS_20,
            "bars_present": len(traded),
            "total_volume": total,
            "window_start": str(window[-1].trade_date),
            "window_end": str(window[0].trade_date),
        },
    )


@metric("avg_turnover_20s_vnd")
def avg_turnover_20s_vnd(ctx: MetricContext) -> MetricResult:
    window = _require_window(ctx, SESSIONS_20, "sessions")
    traded = _traded(window)
    if not traded:
        raise MissingInput("no_bars_in_20_sessions")
    total = sum((s.turnover or Decimal(0)) for s in traded)
    return MetricResult(
        value=(total / Decimal(SESSIONS_20)).quantize(Decimal("0.01")),
        inputs={
            "sessions": SESSIONS_20,
            "bars_present": len(traded),
            "total_turnover_vnd": str(total),
            "window_start": str(window[-1].trade_date),
            "window_end": str(window[0].trade_date),
        },
    )


@metric("median_turnover_20s_vnd")
def median_turnover_20s_vnd(ctx: MetricContext) -> MetricResult:
    """The turnover of a typical session, which is what exit capacity actually depends on.

    Reported alongside the mean rather than instead of it: the gap between the two is
    itself the signal. When the mean is large and the median is zero, the stock does not
    trade - it occasionally changes hands in blocks, which is a different thing and not
    something a retail seller can rely on.
    """
    window = _require_window(ctx, SESSIONS_20, "sessions")
    median = _median_turnover(window)
    mean = sum((s.turnover or Decimal(0)) for s in window) / Decimal(SESSIONS_20)
    return MetricResult(
        value=median.quantize(Decimal("0.01")),
        inputs={
            "sessions": SESSIONS_20,
            "median_turnover_vnd": str(median.quantize(Decimal("0.01"))),
            "mean_turnover_vnd": str(mean.quantize(Decimal("0.01"))),
            "block_trade_distortion": str(
                (mean / median).quantize(Decimal("0.1")) if median > 0 else "mean_over_zero_median"
            ),
            "sessions_with_turnover": sum(1 for s in window if (s.turnover or Decimal(0)) > 0),
        },
    )


# ───────────────────────── the zero-trade family ─────────────────────────


@metric("zero_trade_sessions_3m")
def zero_trade_sessions_3m(ctx: MetricContext) -> MetricResult:
    window = _require_window(ctx, SESSIONS_3M, "sessions")
    zeros = [s for s in _traded(window) if s.zero_trade]
    return MetricResult(
        value=Decimal(len(zeros)),
        inputs={
            "sessions": len(window),
            "zero_trade_sessions": len(zeros),
            "example_dates": [str(s.trade_date) for s in zeros[:5]],
        },
    )


@metric("zero_trade_ratio_3m")
def zero_trade_ratio_3m(ctx: MetricContext) -> MetricResult:
    window = _require_window(ctx, SESSIONS_3M, "sessions")
    zeros = sum(1 for s in _traded(window) if s.zero_trade)
    missing = sum(1 for s in window if s.bar_missing)
    # Denominator is calendar sessions. A missing bar is not evidence of trading, so
    # excluding it would understate how often this stock does not trade.
    return MetricResult(
        value=(Decimal(zeros) / Decimal(len(window))).quantize(Decimal("0.0001")),
        inputs={
            "zero_trade_sessions": zeros,
            "missing_bars": missing,
            "calendar_sessions": len(window),
        },
    )


@metric("missing_bar_sessions_3m")
def missing_bar_sessions_3m(ctx: MetricContext) -> MetricResult:
    """Reported separately so thin trading is never confused with a failed ingest."""
    window = _require_window(ctx, SESSIONS_3M, "sessions")
    missing = [s for s in window if s.bar_missing]
    return MetricResult(
        value=Decimal(len(missing)),
        inputs={
            "calendar_sessions": len(window),
            "missing_bars": len(missing),
            "example_dates": [str(s.trade_date) for s in missing[:5]],
        },
    )


# ───────────────────────── 52-week range ─────────────────────────


@metric("price_range_52w_pct")
def price_range_52w_pct(ctx: MetricContext) -> MetricResult:
    traded = _traded(_window(ctx, SESSIONS_52W))
    if len(traded) < 2:
        raise MissingInput(f"insufficient_history:{len(traded)}_bars_52w")
    closes = [s.close for s in traded if s.close is not None]
    low, high = min(closes), max(closes)
    if low == 0:
        raise MissingInput("low_is_zero")
    return MetricResult(
        value=((high - low) / low).quantize(Decimal("0.0001")),
        inputs={"low_52w_vnd": str(low), "high_52w_vnd": str(high), "bars": len(closes)},
    )


@metric("pct_from_52w_high")
def pct_from_52w_high(ctx: MetricContext) -> MetricResult:
    traded = _traded(_window(ctx, SESSIONS_52W))
    closes = [s.close for s in traded if s.close is not None]
    if not closes:
        raise MissingInput("no_bars_52w")
    high, latest = max(closes), closes[0]
    if high == 0:
        raise MissingInput("high_is_zero")
    return MetricResult(
        value=((latest - high) / high).quantize(Decimal("0.0001")),
        inputs={"close_vnd": str(latest), "high_52w_vnd": str(high), "bars": len(closes)},
    )


# ───────────────────────── spread and exit cost ─────────────────────────


@metric("bid_ask_spread_pct")
def bid_ask_spread_pct(ctx: MetricContext) -> MetricResult:
    """Needs an intraday quote, which history endpoints do not carry.

    Until a live price-board capture exists this reports unavailable with a specific
    reason, which is the honest answer and exercises the missing-data path on every run.
    """
    row = (
        ctx.session.execute(
            text(
                "SELECT best_bid, best_ask, captured_at FROM quote_snapshot "
                "WHERE ticker_id = :t AND captured_at <= CAST(:as_of AS date) + interval '1 day' "
                "ORDER BY captured_at DESC LIMIT 1"
            ),
            {"t": ctx.ticker_id, "as_of": ctx.as_of},
        )
        .mappings()
        .first()
    )

    if row is None:
        raise MissingInput("no_intraday_snapshot")
    bid, ask = row["best_bid"], row["best_ask"]
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        raise MissingInput("incomplete_quote")

    mid = (bid + ask) / Decimal(2)
    return MetricResult(
        value=((ask - bid) / mid).quantize(Decimal("0.0001")),
        inputs={
            "best_bid": str(bid),
            "best_ask": str(ask),
            "captured_at": str(row["captured_at"]),
        },
    )


@metric("sessions_to_exit")
def sessions_to_exit(ctx: MetricContext) -> MetricResult:
    """Sessions needed to liquidate a position at a stated share of a TYPICAL session.

    Capacity is based on the MEDIAN session, not the mean. Using the mean let four block
    trades in twenty sessions imply POM could be exited in under one session, when in four
    sessions out of five nothing trades at all. If the median session has no turnover, the
    honest answer is that the position cannot be exited on any predictable schedule - not a
    small number.

    The reported value is for the smallest position; the full ladder and the participation
    assumption live in `inputs`, so the page can name the assumption instead of presenting
    a bare figure.
    """
    window = _require_window(ctx, SESSIONS_20, "sessions")
    median = _median_turnover(window)
    mean = sum((s.turnover or Decimal(0)) for s in window) / Decimal(SESSIONS_20)

    if median <= 0:
        raise MissingInput("no_turnover_in_a_typical_session_cannot_estimate_exit")

    capacity = median * PARTICIPATION_RATE
    ladder: dict[str, Any] = {}
    for position in EXIT_POSITIONS_VND:
        try:
            ladder[f"{int(position / 1_000_000)}mn_vnd"] = str(
                (position / capacity).quantize(Decimal("0.1"))
            )
        except (InvalidOperation, ZeroDivisionError):
            ladder[f"{int(position / 1_000_000)}mn_vnd"] = "unbounded"

    smallest = (EXIT_POSITIONS_VND[0] / capacity).quantize(Decimal("0.1"))
    return MetricResult(
        value=smallest,
        inputs={
            "basis": "median_session_turnover",
            "participation_rate": str(PARTICIPATION_RATE),
            "median_turnover_20s_vnd": str(median.quantize(Decimal("0.01"))),
            "mean_turnover_20s_vnd": str(mean.quantize(Decimal("0.01"))),
            "daily_capacity_vnd": str(capacity.quantize(Decimal("0.01"))),
            "sessions_to_exit": ladder,
            "framing": FRAMING_NOTE_VI,
        },
    )


# How many of the heaviest sessions the concentration metric looks at. Three, because the
# failure it exposes is a handful of block trades carrying a quarter's worth of volume -
# and in the POM data that handful was four.
TOP_SESSIONS = 3


@metric("turnover_concentration_3m")
def turnover_concentration_3m(ctx: MetricContext) -> MetricResult:
    """Share of three-month turnover that happened in the three heaviest sessions.

    This is the metric that exposes an average as an illusion. POM's mean turnover of
    ~1.37bn VND per session reads as a stock you could sell into; it is produced by about
    four block trades while four sessions in five have no turnover at all. A reader shown
    only the average draws precisely the wrong conclusion about getting out.

    A high value says the liquidity was *episodic*: it existed on particular days, for
    particular counterparties, and waiting for such a day is not a plan a retail seller
    can rely on. It is a description of how turnover arrived, not a forecast that it will
    arrive again.
    """
    window = _require_window(ctx, SESSIONS_3M, "sessions")
    turnovers = sorted((s.turnover or Decimal(0)) for s in window)
    # Seed the sum with Decimal so an empty window cannot widen the type to int.
    total = sum(turnovers, Decimal(0))

    if total <= 0:
        # Nothing traded in three months. Concentration is undefined rather than 0 or 1,
        # and `zero_trade_ratio_3m` is the metric that already says this plainly.
        raise MissingInput("no_turnover_in_3m_concentration_undefined")

    top = sum(turnovers[-TOP_SESSIONS:], Decimal(0))
    share = (top / total).quantize(Decimal("0.0001"))

    traded = [s for s in window if (s.turnover or Decimal(0)) > 0]
    return MetricResult(
        value=share,
        inputs={
            "top_sessions": TOP_SESSIONS,
            "top_sessions_turnover_vnd": str(top.quantize(Decimal("0.01"))),
            "total_turnover_3m_vnd": str(total.quantize(Decimal("0.01"))),
            "sessions_in_window": len(window),
            "sessions_with_turnover": len(traded),
            "heaviest_session_dates": [
                str(s.trade_date)
                for s in sorted(window, key=lambda x: x.turnover or Decimal(0), reverse=True)[
                    :TOP_SESSIONS
                ]
            ],
            "framing": FRAMING_NOTE_VI,
        },
    )


@metric("max_drawdown_52w")
def max_drawdown_52w(ctx: MetricContext) -> MetricResult:
    """Deepest peak-to-trough fall in closing price over 52 weeks, as a negative ratio.

    Distinct from `pct_from_52w_high`, which measures only the distance from the high to
    *today*. A stock that fell 70% and recovered half of it has a shallow distance-from-high
    and a deep drawdown, and the second is the one that describes what holding it was like.

    Peak and trough dates are recorded, so the figure can be checked against the chart
    rather than taken on trust.
    """
    window = _require_window(ctx, SESSIONS_52W, "sessions")
    # Oldest first: a drawdown is a path, so order is the computation, not a detail.
    traded = [s for s in reversed(window) if not s.bar_missing and s.close is not None]
    if len(traded) < 2:
        raise MissingInput(f"insufficient_bars_for_drawdown:{len(traded)}")

    peak = traded[0]
    peak_close = traded[0].close
    assert peak_close is not None
    worst = Decimal(0)
    worst_peak, worst_trough = peak, peak

    for session in traded[1:]:
        close = session.close
        assert close is not None
        if close > peak_close:
            peak, peak_close = session, close
            continue
        if peak_close <= 0:
            continue
        fall = (close - peak_close) / peak_close
        if fall < worst:
            worst, worst_peak, worst_trough = fall, peak, session

    return MetricResult(
        value=worst.quantize(Decimal("0.0001")),
        inputs={
            "peak_date": str(worst_peak.trade_date),
            "peak_close": str(worst_peak.close),
            "trough_date": str(worst_trough.trade_date),
            "trough_close": str(worst_trough.close),
            "sessions_in_window": len(window),
            "bars_used": len(traded),
            "framing": FRAMING_NOTE_VI,
        },
    )


@metric("sessions_since_52w_high")
def sessions_since_52w_high(ctx: MetricContext) -> MetricResult:
    """Trading sessions elapsed since the highest close of the last 52 weeks.

    Answers "how long has this been under water", which `pct_from_52w_high` cannot: a
    stock 40% off its high last month and one 40% off a high set a year ago are different
    situations. Counted in calendar trading sessions, not rows, for the same reason every
    other window here is.
    """
    window = _require_window(ctx, SESSIONS_52W, "sessions")
    traded = [s for s in window if not s.bar_missing and s.close is not None]
    if not traded:
        raise MissingInput("no_bars_in_52w")

    # `window` is newest-first, so the index of the peak IS the number of sessions since.
    best_index, best = 0, traded[0]
    best_close = best.close
    assert best_close is not None
    for index, session in enumerate(window):
        close = session.close
        if close is not None and close > best_close:
            best_index, best, best_close = index, session, close

    return MetricResult(
        value=Decimal(best_index),
        inputs={
            "high_date": str(best.trade_date),
            "high_close": str(best_close),
            "as_of": str(ctx.as_of),
            "sessions_in_window": len(window),
            "framing": FRAMING_NOTE_VI,
        },
    )
