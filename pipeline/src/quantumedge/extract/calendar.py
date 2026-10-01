"""Derive the trading calendar from observed bars.

Why this exists at all: with only per-ticker price data, a date missing from one ticker is
ambiguous — the exchange may have been closed, or that one stock may simply not have
traded. Agent 5's entire purpose is measuring the second case, so the two must be
separable.

The rule: a date is a trading day if **any** covered ticker recorded a bar on it. With
nine tickers across three sectors, a date where the market was open but not one of them
traded is implausible enough to accept; a public holiday shows up as a date nobody has.

Consequence, which is the payoff: once the calendar exists, a ticker with no bar on a
known trading day is a genuine *absence* (`missing_bar_sessions_3m`), while a bar with
`volume = 0` is a genuine *zero-trade session*. Those are different facts about liquidity
and the page reports them differently.
"""

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.extract.base import ExtractResult

log = structlog.get_logger(__name__)

CALENDAR_SOURCE = "derived_from_observed_bars"


def rebuild_calendar(session: Session, exchange_code: str = "HOSE") -> ExtractResult:
    """Mark every date on which any covered ticker of this exchange traded."""
    result = ExtractResult()

    inserted = (
        session.execute(
            text(
                """
            INSERT INTO market_calendar (exchange_code, trade_date, is_trading_day, source)
            -- Casts are required: these parameters appear both in the SELECT list and
            -- in the WHERE clause below, and Postgres cannot deduce one type for them.
            SELECT CAST(:exchange AS varchar(10)), pb.trade_date, true, CAST(:source AS text)
            FROM price_bar pb
            JOIN ticker t ON t.id = pb.ticker_id
            WHERE t.exchange_code = CAST(:exchange AS varchar(10)) AND t.is_covered
            GROUP BY pb.trade_date
            ON CONFLICT (exchange_code, trade_date) DO UPDATE
               SET is_trading_day = true, source = EXCLUDED.source
            RETURNING trade_date
            """
            ),
            {"exchange": exchange_code, "source": CALENDAR_SOURCE},
        )
        .scalars()
        .all()
    )

    result.rows_written = len(inserted)
    span = session.execute(
        text(
            "SELECT min(trade_date), max(trade_date), count(*) "
            "FROM market_calendar WHERE exchange_code = :e AND is_trading_day"
        ),
        {"e": exchange_code},
    ).one()
    result.notes.append(f"{exchange_code}: {span[2]} trading days, {span[0]} to {span[1]}")
    log.info(
        "extract.calendar",
        exchange=exchange_code,
        days=span[2],
        first=str(span[0]),
        last=str(span[1]),
    )
    return result


def session_count(
    session: Session, exchange_code: str, start: str | None = None, end: str | None = None
) -> int:
    """Trading sessions in a window — the denominator for every liquidity ratio."""
    return int(
        session.execute(
            text(
                # Every parameter is cast: Postgres cannot deduce a type for a bare
                # parameter in an IS NULL test any more than in a SELECT list.
                "SELECT count(*) FROM market_calendar "
                "WHERE exchange_code = CAST(:e AS varchar(10)) AND is_trading_day "
                "  AND (CAST(:start AS date) IS NULL OR trade_date >= CAST(:start AS date)) "
                "  AND (CAST(:end AS date) IS NULL OR trade_date <= CAST(:end AS date))"
            ),
            {"e": exchange_code, "start": start, "end": end},
        ).scalar_one()
    )
