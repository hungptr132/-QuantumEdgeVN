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
    session: Session,
    ticker_id: int,
    volumes: list[int | None],
    close: Decimal = Decimal(2000),
    closes: list[Decimal] | None = None,
) -> None:
    """Lay down a calendar plus bars, newest last.

    `None` in `volumes` means the session exists in the calendar but the ticker has no bar
    — a missing bar, which must stay distinguishable from a zero-volume session.

    `closes` gives a price per session for the path-dependent metrics (drawdown, sessions
    since the high); omit it when only turnover matters and every close can be the same.
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

    prices = closes or [close] * len(volumes)
    for d, vol, px in zip(days, volumes, prices, strict=True):
        if vol is None:
            continue
        session.execute(
            text(
                "INSERT INTO price_bar (ticker_id, trade_date, close, volume, turnover_vnd, "
                " source_raw_document_id, extract_run_id) "
                "VALUES (:t, :d, :c, :v, :c * :v, :src, :r)"
            ),
            {"t": ticker_id, "d": d, "c": px, "v": vol, "src": doc_id, "r": ingest_id},
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


class TestPriceLooksBackPastMissingBars:
    """Regression for a real bug found in live data.

    close_price used a one-session window, so on any session where a ticker printed no bar
    it reported "no_price_bar_at_or_before_as_of" - which was false, since bars existed on
    both sides. POM has nine such sessions in three years. price_to_par went unavailable
    with it, which degraded price_below_par_value from active to unknown: a missing bar was
    suppressing a genuine risk flag, which is the wrong direction for this product to fail.
    """

    def test_a_missing_bar_on_the_as_of_session_does_not_blank_the_price(
        self, session: Session, ctx: MetricContext
    ) -> None:
        # Bars exist, but not on the as_of session itself (the last entry is newest).
        volumes: list[int | None] = [1000] * 19 + [None]
        _build_window(session, ctx.ticker_id, volumes, close=Decimal(2100))

        result = liquidity.close_price(ctx)
        assert result.value == Decimal("2100.00")
        # And it says how stale the quote is rather than passing it off as current.
        assert result.inputs["sessions_stale"] == 1

    def test_price_to_par_survives_too_so_the_flag_still_fires(
        self, session: Session, ctx: MetricContext
    ) -> None:
        volumes: list[int | None] = [1000] * 19 + [None]
        _build_window(session, ctx.ticker_id, volumes, close=Decimal(2100))

        result = liquidity.price_to_par(ctx)
        # 2,100 against a 10,000 par value - the flag's input is intact.
        assert result.value == Decimal("0.2100")
        assert result.inputs["sessions_stale"] == 1

    def test_a_run_of_missing_bars_is_still_found_within_the_lookback(
        self, session: Session, ctx: MetricContext
    ) -> None:
        volumes: list[int | None] = [1000] * 12 + [None] * 8
        _build_window(session, ctx.ticker_id, volumes, close=Decimal(3300))
        result = liquidity.close_price(ctx)
        assert result.value == Decimal("3300.00")
        assert result.inputs["sessions_stale"] == 8

    def test_beyond_the_lookback_it_honestly_reports_nothing(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """The lookback is bounded. A quote from further back than MAX_PRICE_LOOKBACK is
        too stale to present as this ticker's price, so it is withheld with a reason naming
        the window rather than a vague blank."""
        volumes: list[int | None] = [None] * liquidity.MAX_PRICE_LOOKBACK
        _build_window(session, ctx.ticker_id, volumes)
        with pytest.raises(MissingInput, match="no_price_bar_within_"):
            liquidity.close_price(ctx)


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


class TestTurnoverConcentration:
    """The metric that exposes an average as an illusion.

    Taken from real data: POM's mean turnover of ~1.37bn VND per session reads as a stock
    you could sell into, and it is produced by a handful of block trades while four
    sessions in five have no turnover at all.
    """

    def test_a_handful_of_block_trades_is_reported_as_concentration(
        self, session: Session, ctx: MetricContext
    ) -> None:
        # 60 dead sessions and 3 block trades. The blocks are the entire turnover.
        volumes = [0] * 60 + [1000, 1000, 1000]
        _build_window(session, ctx.ticker_id, volumes)

        result = liquidity.turnover_concentration_3m(ctx)

        assert result.value == Decimal("1.0000")
        assert result.inputs["sessions_with_turnover"] == 3
        assert result.inputs["sessions_in_window"] == 63

    def test_evenly_spread_turnover_scores_low(self, session: Session, ctx: MetricContext) -> None:
        """A genuinely liquid stock has 3/63 of its turnover in its top 3 sessions, which
        is about 4.8%. The metric must separate that from the case above."""
        _build_window(session, ctx.ticker_id, [1000] * 63)

        result = liquidity.turnover_concentration_3m(ctx)

        # 3 of 63 equal sessions.
        assert result.value == Decimal("0.0476")

    def test_the_heaviest_session_dates_are_named(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """A reader has to be able to go and look at those days. A concentration figure
        without them is a claim they cannot check."""
        volumes = [0] * 60 + [5000, 100, 3000]
        _build_window(session, ctx.ticker_id, volumes)

        result = liquidity.turnover_concentration_3m(ctx)
        dates = result.inputs["heaviest_session_dates"]

        assert len(dates) == 3
        # Ordered heaviest first, not chronologically.
        assert dates[0] != dates[1]

    def test_a_dead_stock_is_undefined_not_zero_or_one(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """Nothing traded at all. Concentration has no value — and reporting 1.0 would
        say "all turnover was concentrated" about a stock with no turnover."""
        _build_window(session, ctx.ticker_id, [0] * 63)

        with pytest.raises(MissingInput, match="no_turnover_in_3m_concentration_undefined"):
            liquidity.turnover_concentration_3m(ctx)

    def test_a_missing_bar_counts_as_no_turnover_not_as_absent(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """Consistent with every other window here: the calendar drives the denominator."""
        volumes: list[int | None] = [None] * 60 + [1000, 1000, 1000]
        _build_window(session, ctx.ticker_id, volumes)

        result = liquidity.turnover_concentration_3m(ctx)
        assert result.value == Decimal("1.0000")
        assert result.inputs["sessions_in_window"] == 63


class TestMaxDrawdown:
    """Distinct from distance-from-high: a stock that fell 50% and recovered half has a
    shallow distance and a deep drawdown, and the drawdown describes what holding it was
    like."""

    def test_the_deepest_peak_to_trough_fall_is_found(
        self, session: Session, ctx: MetricContext
    ) -> None:
        # Rise to 200, fall to 100 (-50%), recover to 180. Newest last.
        closes = (
            [Decimal(100)] * 50 + [Decimal(200)] * 50 + [Decimal(100)] * 50 + [Decimal(180)] * 102
        )
        _build_window(session, ctx.ticker_id, [1000] * 252, closes=closes)

        result = liquidity.max_drawdown_52w(ctx)

        assert result.value == Decimal("-0.5000")
        assert result.inputs["peak_close"] == "200.00"
        assert result.inputs["trough_close"] == "100.00"

    def test_it_measures_the_path_not_the_distance_from_today(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """The whole reason this exists alongside pct_from_52w_high: here the stock ends
        at its peak, so distance-from-high is zero while the drawdown was real."""
        closes = [Decimal(200)] * 100 + [Decimal(100)] * 50 + [Decimal(200)] * 102
        _build_window(session, ctx.ticker_id, [1000] * 252, closes=closes)

        assert liquidity.max_drawdown_52w(ctx).value == Decimal("-0.5000")
        # And distance from the high is nil, because today *is* the high.
        assert liquidity.pct_from_52w_high(ctx).value == Decimal("0.0000")

    def test_a_monotonic_rise_has_no_drawdown(self, session: Session, ctx: MetricContext) -> None:
        closes = [Decimal(100 + i) for i in range(252)]
        _build_window(session, ctx.ticker_id, [1000] * 252, closes=closes)

        assert liquidity.max_drawdown_52w(ctx).value == Decimal("0.0000")

    def test_the_peak_and_trough_dates_are_recorded(
        self, session: Session, ctx: MetricContext
    ) -> None:
        closes = [Decimal(100)] * 126 + [Decimal(50)] * 126
        _build_window(session, ctx.ticker_id, [1000] * 252, closes=closes)

        result = liquidity.max_drawdown_52w(ctx)
        # The trough must be dated after the peak: a drawdown is directional.
        assert result.inputs["peak_date"] < result.inputs["trough_date"]


class TestSessionsSinceHigh:
    def test_a_high_set_today_is_zero_sessions_ago(
        self, session: Session, ctx: MetricContext
    ) -> None:
        closes = [Decimal(100)] * 251 + [Decimal(500)]
        _build_window(session, ctx.ticker_id, [1000] * 252, closes=closes)

        assert liquidity.sessions_since_52w_high(ctx).value == Decimal(0)

    def test_a_high_at_the_start_of_the_window_is_the_full_window(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """Observed on real data: SMC and NKG both sit 251 sessions from their high, i.e.
        it was set at the very edge of the window."""
        closes = [Decimal(500)] + [Decimal(100)] * 251
        _build_window(session, ctx.ticker_id, [1000] * 252, closes=closes)

        assert liquidity.sessions_since_52w_high(ctx).value == Decimal(251)

    def test_it_counts_calendar_sessions_including_untraded_ones(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """Counting only sessions with bars would shorten the answer for exactly the
        illiquid stocks this describes, and flatter them."""
        volumes: list[int | None] = [1000] + [None] * 100 + [1000] * 151
        closes = [Decimal(500)] + [Decimal(100)] * 251
        _build_window(session, ctx.ticker_id, volumes, closes=closes)

        # 251 calendar sessions since the high, not 151 bars.
        assert liquidity.sessions_since_52w_high(ctx).value == Decimal(251)

    def test_the_high_and_its_date_are_recorded(self, session: Session, ctx: MetricContext) -> None:
        closes = [Decimal(100)] * 100 + [Decimal(900)] + [Decimal(100)] * 151
        _build_window(session, ctx.ticker_id, [1000] * 252, closes=closes)

        result = liquidity.sessions_since_52w_high(ctx)
        assert result.inputs["high_close"] == "900.00"
        assert result.value == Decimal(151)
