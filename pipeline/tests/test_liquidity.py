"""Agent 5 tests, against hand-computed windows with known answers.

The arithmetic here is simple enough to check by hand, which is the point: these are the
numbers a retail investor would act on, so they are verified rather than trusted.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc import liquidity
from quantumedge.calc.registry import (
    MetricContext,
    MissingInput,
    load_contexts,
    run_metrics,
)
from quantumedge.db.enums import PipelineStage
from quantumedge.runs import start_run

AS_OF = date(2026, 6, 30)


def _build_window(
    session: Session, ticker_id: int, volumes: list[int | None], close: Decimal = Decimal(2000)
) -> None:
    """Lay down a calendar plus bars, newest last.

    `None` means the session exists in the calendar but the ticker has no bar — a missing
    bar, which must stay distinguishable from a zero-volume session.
    """
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) VALUES ('t', 'market_data', 't') "
            "ON CONFLICT DO NOTHING"
        )
    )
    ingest_id = start_run(session, PipelineStage.INGEST, "t")
    doc_id = session.execute(
        text(
            "INSERT INTO raw_document (source_id, ingest_run_id, doc_type, content_hash, "
            " payload, byte_size) VALUES ((SELECT id FROM source WHERE code='t'), :r, 'p', "
            " 'h', '{}'::jsonb, 2) RETURNING id"
        ),
        {"r": ingest_id},
    ).scalar_one()

    # Sessions run backwards from AS_OF, skipping weekends so the dates look plausible.
    days: list[date] = []
    cursor = AS_OF
    while len(days) < len(volumes):
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()

    for d in days:
        session.execute(
            text(
                "INSERT INTO market_calendar (exchange_code, trade_date, is_trading_day, source) "
                "VALUES ('HOSE', :d, true, 'test') ON CONFLICT DO NOTHING"
            ),
            {"d": d},
        )

    for d, vol in zip(days, volumes, strict=True):
        if vol is None:
            continue
        session.execute(
            text(
                "INSERT INTO price_bar (ticker_id, trade_date, close, volume, turnover_vnd, "
                " source_raw_document_id, extract_run_id) "
                "VALUES (:t, :d, :c, :v, :c * :v, :src, :r)"
            ),
            {"t": ticker_id, "d": d, "c": close, "v": vol, "src": doc_id, "r": ingest_id},
        )


@pytest.fixture
def ctx(session: Session, seed_ticker: int) -> MetricContext:
    return MetricContext(
        session=session,
        ticker_id=seed_ticker,
        symbol="TST",
        exchange_code="HOSE",
        as_of=AS_OF,
        par_value_vnd=Decimal(10000),
    )


class TestWindowsCountSessionsNotRows:
    def test_average_divides_by_sessions_not_bars_present(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """A stock printing 10 bars in 20 sessions genuinely averaged less per session.
        Dividing by bars present would flatter exactly the illiquidity this measures."""
        volumes: list[int | None] = [1000] * 10 + [None] * 10
        _build_window(session, ctx.ticker_id, volumes)

        result = liquidity.avg_volume_20s(ctx)
        # 10 bars x 1000 shares over 20 calendar sessions = 500, not 1000.
        assert result.value == Decimal("500.00")
        assert result.inputs["bars_present"] == 10
        assert result.inputs["sessions"] == 20

    def test_short_history_is_refused_with_a_specific_reason(
        self, session: Session, ctx: MetricContext
    ) -> None:
        _build_window(session, ctx.ticker_id, [1000] * 5)
        with pytest.raises(MissingInput, match="insufficient_history"):
            liquidity.avg_volume_20s(ctx)


class TestZeroTradeVersusMissingBar:
    def test_the_two_are_counted_separately(self, session: Session, ctx: MetricContext) -> None:
        """63 sessions: 20 zero-volume, 10 absent, 33 traded. These are three different
        facts and conflating any two would misreport liquidity."""
        volumes: list[int | None] = [1000] * 33 + [0] * 20 + [None] * 10
        _build_window(session, ctx.ticker_id, volumes)

        zeros = liquidity.zero_trade_sessions_3m(ctx)
        missing = liquidity.missing_bar_sessions_3m(ctx)
        ratio = liquidity.zero_trade_ratio_3m(ctx)

        assert zeros.value == Decimal(20)
        assert missing.value == Decimal(10)
        # Denominator is calendar sessions: a missing bar is not evidence of trading.
        assert ratio.value == (Decimal(20) / Decimal(63)).quantize(Decimal("0.0001"))
        assert ratio.inputs["missing_bars"] == 10


class TestMedianBasisForExitEstimate:
    """Regression for a real defect found in live data.

    POM showed 16 of 20 sessions with zero volume and four block trades. The mean said
    1,370mn turnover per session and implied the position could be exited in under one
    session. The median says zero, which is the truth.
    """

    def test_block_trades_do_not_create_phantom_liquidity(
        self, session: Session, ctx: MetricContext
    ) -> None:
        # Sixteen dead sessions, four enormous ones - POM's actual shape.
        volumes: list[int | None] = [0] * 16 + [1_000_000] * 4
        _build_window(session, ctx.ticker_id, volumes)

        mean_metric = liquidity.avg_turnover_20s_vnd(ctx)
        median_metric = liquidity.median_turnover_20s_vnd(ctx)

        assert mean_metric.value is not None and mean_metric.value > 0
        assert median_metric.value == Decimal("0.00")
        assert median_metric.inputs["sessions_with_turnover"] == 4
        assert median_metric.inputs["block_trade_distortion"] == "mean_over_zero_median"

    def test_exit_estimate_is_withheld_when_a_typical_session_is_dead(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """Refusing to answer is the correct behaviour. A small number here would be worse
        than no number: it would tell a holder they can get out when they cannot."""
        volumes: list[int | None] = [0] * 16 + [1_000_000] * 4
        _build_window(session, ctx.ticker_id, volumes)

        with pytest.raises(MissingInput, match="cannot_estimate_exit"):
            liquidity.sessions_to_exit(ctx)

    def test_exit_estimate_is_computed_for_a_normally_traded_stock(
        self, session: Session, ctx: MetricContext
    ) -> None:
        # Every session turns over 1,000 shares at 2,000 VND = 2,000,000 VND.
        _build_window(session, ctx.ticker_id, [1000] * 20)
        result = liquidity.sessions_to_exit(ctx)

        # Capacity = 2,000,000 x 0.20 = 400,000 VND/session.
        # A 100,000,000 VND position therefore needs 250 sessions.
        assert result.value == Decimal("250.0")
        assert result.inputs["basis"] == "median_session_turnover"
        assert result.inputs["participation_rate"] == "0.20"
        # Every reported size, so the page can show the ladder rather than one number.
        assert set(result.inputs["sessions_to_exit"]) == {
            "100mn_vnd",
            "500mn_vnd",
            "1000mn_vnd",
        }

    def test_the_participation_assumption_is_always_disclosed(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """The estimate is meaningless without it, so it travels with the answer."""
        _build_window(session, ctx.ticker_id, [1000] * 20)
        result = liquidity.sessions_to_exit(ctx)
        assert "participation_rate" in result.inputs
        assert "RỦI RO" in result.inputs["framing"]


class TestPriceMetrics:
    def test_price_to_par_detects_sub_par_trading(
        self, session: Session, ctx: MetricContext
    ) -> None:
        _build_window(session, ctx.ticker_id, [1000] * 20, close=Decimal(2100))
        result = liquidity.price_to_par(ctx)
        # 2,100 against a 10,000 par value.
        assert result.value == Decimal("0.2100")

    def test_52_week_range_needs_at_least_two_bars(
        self, session: Session, ctx: MetricContext
    ) -> None:
        _build_window(session, ctx.ticker_id, [1000])
        with pytest.raises(MissingInput, match="insufficient_history"):
            liquidity.price_range_52w_pct(ctx)


class TestSpreadIsHonestlyUnavailable:
    def test_no_intraday_snapshot_is_reported_with_its_reason(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """History endpoints carry no bid/ask. Saying so plainly exercises the missing-data
        path on every run rather than leaving it untested until it matters."""
        _build_window(session, ctx.ticker_id, [1000] * 20)
        with pytest.raises(MissingInput, match="no_intraday_snapshot"):
            liquidity.bid_ask_spread_pct(ctx)


class TestRunnerPersistsEveryOutcome:
    def test_unavailable_metrics_are_still_written_with_a_reason(
        self, session: Session, seed_ticker: int, metric_defs: int
    ) -> None:
        """An absent row is indistinguishable from a metric never attempted. A row saying
        'unavailable, because X' is information."""
        _build_window(session, seed_ticker, [1000] * 20)
        run_id = start_run(session, PipelineStage.CALC, "t")
        contexts = load_contexts(session, AS_OF, symbols=["TST"])
        outcome = run_metrics(
            session,
            run_id,
            liquidity.CALC_VERSION,
            contexts,
            codes=["avg_volume_20s", "bid_ask_spread_pct"],
        )

        assert outcome.written == 2
        assert outcome.available == 1
        assert outcome.unavailable == 1

        row = session.execute(
            text(
                "SELECT is_available, unavailable_reason, value FROM metric_value "
                "WHERE metric_code = 'bid_ask_spread_pct'"
            )
        ).one()
        assert row.is_available is False
        assert row.unavailable_reason == "no_intraday_snapshot"
        assert row.value is None

    def test_rerunning_updates_rather_than_duplicating(
        self, session: Session, seed_ticker: int, metric_defs: int
    ) -> None:
        _build_window(session, seed_ticker, [1000] * 20)
        contexts = load_contexts(session, AS_OF, symbols=["TST"])
        for _ in range(2):
            run_id = start_run(session, PipelineStage.CALC, "t")
            run_metrics(session, run_id, liquidity.CALC_VERSION, contexts, codes=["avg_volume_20s"])
        count = session.execute(
            text("SELECT count(*) FROM metric_value WHERE metric_code = 'avg_volume_20s'")
        ).scalar_one()
        assert count == 1

    def test_every_value_records_the_inputs_that_produced_it(
        self, session: Session, seed_ticker: int, metric_defs: int
    ) -> None:
        """Invariant: any figure on a page must be recomputable by hand from its own row."""
        _build_window(session, seed_ticker, [1000] * 20)
        run_id = start_run(session, PipelineStage.CALC, "t")
        contexts = load_contexts(session, AS_OF, symbols=["TST"])
        run_metrics(
            session, run_id, liquidity.CALC_VERSION, contexts, codes=["avg_turnover_20s_vnd"]
        )
        inputs = session.execute(
            text("SELECT inputs FROM metric_value WHERE metric_code = 'avg_turnover_20s_vnd'")
        ).scalar_one()
        assert {"sessions", "bars_present", "total_turnover_vnd"} <= set(inputs)
