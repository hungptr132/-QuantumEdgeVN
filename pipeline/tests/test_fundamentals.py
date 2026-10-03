"""Fundamental metrics, and the honesty property that justifies implementing them early.

These metrics have no data source yet. They are implemented anyway, because a metric with
no implementation produces no row at all — so the fact sheet cannot say *không công bố* for
it, and a reader cannot tell "we have no statements" from "we never looked".
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc import fundamentals
from quantumedge.calc.registry import MetricContext, MissingInput, load_contexts, run_metrics
from quantumedge.db.enums import PipelineStage
from quantumedge.runs import start_run

AS_OF = date(2026, 6, 30)


@pytest.fixture
def ctx(session: Session, seed_ticker: int, fact_defs: int, metric_defs: int) -> MetricContext:
    return MetricContext(
        session=session,
        ticker_id=seed_ticker,
        symbol="TST",
        exchange_code="HOSE",
        as_of=AS_OF,
        par_value_vnd=Decimal(10000),
    )


def _price(session: Session, ticker_id: int, close: Decimal = Decimal(20000)) -> None:
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) VALUES ('t', 'market_data', 't') "
            "ON CONFLICT DO NOTHING"
        )
    )
    run_id = start_run(session, PipelineStage.INGEST, "t")
    doc = session.execute(
        text(
            "INSERT INTO raw_document (source_id, ingest_run_id, doc_type, content_hash, "
            " payload, byte_size) VALUES ((SELECT id FROM source WHERE code='t'), :r, 'p', "
            " 'h', '{}'::jsonb, 2) RETURNING id"
        ),
        {"r": run_id},
    ).scalar_one()
    session.execute(
        text(
            "INSERT INTO market_calendar (exchange_code, trade_date, is_trading_day, source) "
            "VALUES ('HOSE', :d, true, 'test') ON CONFLICT DO NOTHING"
        ),
        {"d": AS_OF},
    )
    session.execute(
        text(
            "INSERT INTO price_bar (ticker_id, trade_date, close, volume, turnover_vnd, "
            " source_raw_document_id, extract_run_id) "
            "VALUES (:t, :d, :c, 1000, :c * 1000, :src, :r)"
        ),
        {"t": ticker_id, "d": AS_OF, "c": close, "src": doc, "r": run_id},
    )


def _fact(
    session: Session,
    ticker_id: int,
    code: str,
    value: Decimal,
    *,
    period_type: str = "TTM",
    fiscal_year: int = 2026,
) -> None:
    """Insert one statement line item, creating its period if needed."""
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) VALUES ('f', 'disclosure', 'f') "
            "ON CONFLICT DO NOTHING"
        )
    )
    run_id = start_run(session, PipelineStage.EXTRACT, "f")
    doc = session.execute(
        text(
            "INSERT INTO raw_document (source_id, ingest_run_id, doc_type, content_hash, "
            " payload, byte_size) VALUES ((SELECT id FROM source WHERE code='f'), :r, 'stmt', "
            " :h, '{}'::jsonb, 2) RETURNING id"
        ),
        {"r": run_id, "h": f"h{code}{fiscal_year}{period_type}"},
    ).scalar_one()
    period = session.execute(
        text(
            "INSERT INTO financial_period (ticker_id, period_type, fiscal_year, period_end, "
            " source_raw_document_id) VALUES (:t, CAST(:pt AS period_type), :fy, :pe, :src) "
            "ON CONFLICT DO NOTHING RETURNING id"
        ),
        {
            "t": ticker_id,
            "pt": period_type,
            "fy": fiscal_year,
            "pe": date(fiscal_year, 3, 31),
            "src": doc,
        },
    ).scalar_one_or_none()
    if period is None:
        period = session.execute(
            text(
                "SELECT id FROM financial_period WHERE ticker_id = :t "
                "AND period_type = CAST(:pt AS period_type) AND fiscal_year = :fy"
            ),
            {"t": ticker_id, "pt": period_type, "fy": fiscal_year},
        ).scalar_one()
    session.execute(
        text(
            "INSERT INTO financial_fact (financial_period_id, fact_code, value, unit, "
            " source_raw_document_id, source_field_path, extractor_version, extract_run_id) "
            "VALUES (:p, :c, :v, 'VND', :src, :path, 'test/v1', :r)"
        ),
        {
            "p": period,
            "c": code,
            "v": value,
            "src": doc,
            "path": f"$.test.{code}",
            "r": run_id,
        },
    )


class TestMissingDataIsReportedNotSilent:
    """The reason these exist before their data source does."""

    def test_each_metric_names_the_fact_it_is_waiting_for(self, ctx: MetricContext) -> None:
        with pytest.raises(MissingInput, match="missing_fact:ebit"):
            fundamentals.interest_coverage(ctx)

    def test_the_runner_writes_a_row_rather_than_skipping(
        self, session: Session, seed_ticker: int, metric_defs: int, fact_defs: int
    ) -> None:
        """An absent row is indistinguishable from a metric that was never attempted. A row
        saying 'unavailable because these facts are missing' is information a reader can act
        on."""
        run_id = start_run(session, PipelineStage.CALC, "t")
        contexts = load_contexts(session, AS_OF, symbols=["TST"])
        outcome = run_metrics(
            session, run_id, fundamentals.CALC_VERSION, contexts, codes=["interest_coverage"]
        )

        assert outcome.written == 1
        assert outcome.unavailable == 1
        row = session.execute(
            text(
                "SELECT is_available, unavailable_reason FROM metric_value "
                "WHERE metric_code = 'interest_coverage'"
            )
        ).one()
        assert row.is_available is False
        # The declared inputs are pre-checked, so the reason lists them all rather than
        # whichever one happened to be read first.
        assert "ebit" in row.unavailable_reason
        assert "interest_expense" in row.unavailable_reason


class TestNonPositiveDenominators:
    """A ratio with a non-positive denominator is withheld, never printed negative."""

    def test_a_loss_making_company_has_no_pe_rather_than_a_negative_one(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """Printing -4.2x invites a reader to see a loss-maker as cheap, which inverts what
        the number means."""
        _price(session, ctx.ticker_id)
        _fact(session, ctx.ticker_id, "shares_outstanding", Decimal(1_000_000), period_type="Y")
        _fact(session, ctx.ticker_id, "net_profit_after_tax", Decimal(-5_000_000_000))

        with pytest.raises(MissingInput, match="non_positive_denominator:net_profit_after_tax"):
            fundamentals.pe(ctx)

    def test_zero_interest_expense_is_undefined_not_infinite_safety(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """A debt-free company is not infinitely safe; the ratio simply has no value."""
        _fact(session, ctx.ticker_id, "ebit", Decimal(1_000_000_000))
        _fact(session, ctx.ticker_id, "interest_expense", Decimal(0))

        with pytest.raises(MissingInput, match="no_interest_expense_ratio_undefined"):
            fundamentals.interest_coverage(ctx)

    def test_negative_equity_yields_no_roe(self, session: Session, ctx: MetricContext) -> None:
        _fact(session, ctx.ticker_id, "net_profit_after_tax", Decimal(1_000_000_000))
        _fact(session, ctx.ticker_id, "total_equity", Decimal(-2_000_000_000), period_type="Y")

        with pytest.raises(MissingInput, match="non_positive_denominator:total_equity"):
            fundamentals.roe(ctx)


class TestArithmetic:
    def test_interest_coverage(self, session: Session, ctx: MetricContext) -> None:
        _fact(session, ctx.ticker_id, "ebit", Decimal(3_000_000_000))
        _fact(session, ctx.ticker_id, "interest_expense", Decimal(2_000_000_000))
        result = fundamentals.interest_coverage(ctx)
        assert result.value == Decimal("1.50")
        # Below the 2.0 threshold, so the leverage flag would fire on this.
        # inputs record the value at the scale the column stores it, not a reformatted
        # version - the point of the field is to show exactly what was read.
        assert result.inputs["ebit"] == "3000000000.0000"

    def test_pe_uses_market_cap_over_profit(self, session: Session, ctx: MetricContext) -> None:
        _price(session, ctx.ticker_id, close=Decimal(20000))
        _fact(session, ctx.ticker_id, "shares_outstanding", Decimal(1_000_000), period_type="Y")
        _fact(session, ctx.ticker_id, "net_profit_after_tax", Decimal(2_000_000_000))
        # 20,000 x 1,000,000 = 20bn market cap over 2bn profit = 10x
        assert fundamentals.pe(ctx).value == Decimal("10.00")

    def test_roe(self, session: Session, ctx: MetricContext) -> None:
        _fact(session, ctx.ticker_id, "net_profit_after_tax", Decimal(1_500_000_000))
        _fact(session, ctx.ticker_id, "total_equity", Decimal(10_000_000_000), period_type="Y")
        assert fundamentals.roe(ctx).value == Decimal("0.1500")

    def test_net_debt_ebitda(self, session: Session, ctx: MetricContext) -> None:
        _fact(session, ctx.ticker_id, "short_term_debt", Decimal(3_000_000_000), period_type="Y")
        _fact(session, ctx.ticker_id, "long_term_debt", Decimal(5_000_000_000), period_type="Y")
        _fact(
            session, ctx.ticker_id, "cash_and_equivalents", Decimal(1_000_000_000), period_type="Y"
        )
        _fact(session, ctx.ticker_id, "ebitda", Decimal(2_000_000_000))
        # (3 + 5 - 1) / 2 = 3.5x
        assert fundamentals.net_debt_ebitda(ctx).value == Decimal("3.50")


class TestDilutionNeedsAnExactWindow:
    def test_three_year_cagr(self, session: Session, ctx: MetricContext) -> None:
        _fact(
            session,
            ctx.ticker_id,
            "shares_outstanding",
            Decimal(1_000_000),
            period_type="Y",
            fiscal_year=2023,
        )
        _fact(
            session,
            ctx.ticker_id,
            "shares_outstanding",
            Decimal(2_000_000),
            period_type="Y",
            fiscal_year=2026,
        )
        result = fundamentals.shares_cagr_3y(ctx)
        # Doubling over three years is roughly 26% a year.
        assert Decimal("0.25") < result.value < Decimal("0.27")
        assert result.inputs["fiscal_year_then"] == 2023

    def test_a_missing_window_is_refused_rather_than_approximated(
        self, session: Session, ctx: MetricContext
    ) -> None:
        """Comparing against whatever the oldest available year happens to be would change
        the metric's meaning per ticker without saying so."""
        _fact(
            session,
            ctx.ticker_id,
            "shares_outstanding",
            Decimal(2_000_000),
            period_type="Y",
            fiscal_year=2026,
        )
        _fact(
            session,
            ctx.ticker_id,
            "shares_outstanding",
            Decimal(1_800_000),
            period_type="Y",
            fiscal_year=2025,
        )

        with pytest.raises(MissingInput, match="no_shares_outstanding_for_fiscal_year:2023"):
            fundamentals.shares_cagr_3y(ctx)


class TestEveryDefinedMetricIsImplemented:
    def test_no_metric_in_the_rules_file_lacks_an_implementation(self) -> None:
        """A defined-but-unimplemented metric produces no row at all, so the fact sheet
        cannot report it as unavailable. This test is what keeps that from recurring."""
        import yaml

        from quantumedge.calc import liquidity  # noqa: F401  (registers its metrics)
        from quantumedge.calc.registry import REGISTRY
        from quantumedge.config import load_settings

        rules = yaml.safe_load(
            (load_settings().rules_dir / "metrics.yaml").read_text(encoding="utf-8")
        )
        defined = {m["code"] for m in rules["metrics"]}
        missing = sorted(defined - set(REGISTRY))
        assert not missing, f"defined in metrics.yaml but never implemented: {missing}"
