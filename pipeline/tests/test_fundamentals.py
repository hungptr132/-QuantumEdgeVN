"""Fundamental metrics: the arithmetic, and the honesty properties that constrain it.

Two things are being protected here.

**That a metric exists at all before its data does.** A metric with no implementation
produces no row, so the fact sheet cannot say *không công bố* for it, and a reader cannot
tell "we have no statements" from "we never looked".

**That TTM is actually resolved.** These metrics used to read `period_type = 'TTM'` rows
that nothing in the pipeline writes, so every one of them was unavailable no matter how
many quarters had been extracted. The inputs now come from `calc/periods.py`, and
`TestQuarterlyFactsAreEnough` is the regression test for the original bug.

Every expected figure below is computed by hand in a comment. A test that asserts whatever
the code returned would pass for an arithmetic error too.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc import fundamentals
from quantumedge.calc.periods import BASIS_AUDITED_ANNUAL
from quantumedge.calc.registry import MetricContext, MissingInput, load_contexts, run_metrics
from quantumedge.db.enums import PipelineStage
from quantumedge.runs import start_run

AS_OF = date(2026, 6, 30)

BILLION = Decimal(1_000_000_000)
MILLION = Decimal(1_000_000)

# The four quarters ending on AS_OF, oldest first, with their filing dates.
WINDOW = ((2025, 3), (2025, 4), (2026, 1), (2026, 2))
WINDOW_LABEL = "2025Q3-2026Q2"

_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


class Facts:
    """Statement rows for one ticker, inserted the way `extract/statements.py` does.

    A flow is spread evenly across the four quarters of the window so the TTM sum is the
    stated total; a stock is written at the window's closing quarter, which is the only
    date a point-in-time figure is read from. Both halves matter: a test that wrote a
    balance to all four quarters would pass whether or not the code summed it.
    """

    def __init__(self, session: Session, ticker_id: int, *, window: tuple[tuple[int, int], ...]):
        self.session = session
        self.ticker_id = ticker_id
        self.window = window
        session.execute(
            text(
                "INSERT INTO source (code, kind, name) VALUES ('stmt', 'disclosure', 'stmt') "
                "ON CONFLICT DO NOTHING"
            )
        )
        self.run_id = start_run(session, PipelineStage.EXTRACT, "stmt")
        self.quarter_ids = {
            (fiscal_year, quarter): self._quarter(fiscal_year, quarter)
            for fiscal_year, quarter in window
        }

    def _document(self, key: str) -> int:
        return int(
            self.session.execute(
                text(
                    "INSERT INTO raw_document (source_id, ingest_run_id, ticker_id, symbol, "
                    " doc_type, period_key, content_hash, payload, byte_size) "
                    "VALUES ((SELECT id FROM source WHERE code='stmt'), :r, :t, 'TST', "
                    " 'statement', :pk, :h, '{}'::jsonb, 2) RETURNING id"
                ),
                {"r": self.run_id, "t": self.ticker_id, "pk": key, "h": f"hash-{key}"},
            ).scalar_one()
        )

    def _period(
        self,
        *,
        period_type: str,
        fiscal_year: int,
        fiscal_quarter: int | None,
        period_end: date,
        is_audited: bool | None,
    ) -> int:
        key = f"{fiscal_year}-{period_type}-{fiscal_quarter or 0}"
        return int(
            self.session.execute(
                text(
                    "INSERT INTO financial_period (ticker_id, period_type, fiscal_year, "
                    " fiscal_quarter, period_end, is_audited, source_raw_document_id) "
                    "VALUES (:t, CAST(:pt AS period_type), :fy, :fq, :pe, :audited, :src) "
                    "RETURNING id"
                ),
                {
                    "t": self.ticker_id,
                    "pt": period_type,
                    "fy": fiscal_year,
                    "fq": fiscal_quarter,
                    "pe": period_end,
                    "audited": is_audited,
                    "src": self._document(key),
                },
            ).scalar_one()
        )

    def _quarter(self, fiscal_year: int, quarter: int) -> int:
        month, day = _QUARTER_END[quarter]
        return self._period(
            period_type="Q",
            fiscal_year=fiscal_year,
            fiscal_quarter=quarter,
            period_end=date(fiscal_year, month, day),
            is_audited=None,
        )

    def _fact(self, period_id: int, code: str, value: Decimal) -> None:
        self.session.execute(
            text(
                "INSERT INTO financial_fact (financial_period_id, fact_code, value, unit, "
                " source_raw_document_id, source_field_path, extractor_version, "
                " extract_run_id) "
                "VALUES (:p, :c, :v, CAST(:u AS unit_code), "
                " (SELECT source_raw_document_id FROM financial_period WHERE id = :p), "
                " :path, 'test/v1', :r)"
            ),
            {
                "p": period_id,
                "c": code,
                "v": value,
                "u": "share" if code == "shares_outstanding" else "VND",
                "path": f"$.test.{code}",
                "r": self.run_id,
            },
        )

    def flow(self, code: str, ttm_total: Decimal) -> None:
        """Spread a twelve-month total evenly over the window's four quarters."""
        per_quarter = ttm_total / 4
        assert per_quarter * 4 == ttm_total, f"{ttm_total} does not divide into four quarters"
        for key in self.window:
            self._fact(self.quarter_ids[key], code, per_quarter)

    def stock(self, code: str, value: Decimal) -> None:
        """A balance, at the window's closing quarter only."""
        self._fact(self.quarter_ids[self.window[-1]], code, value)

    def stock_every_quarter(self, code: str, value: Decimal) -> None:
        """The same balance at all four quarter ends — what a stable balance sheet looks
        like, and the shape that would expose a figure summed four times."""
        for key in self.window:
            self._fact(self.quarter_ids[key], code, value)

    def annual(
        self, fiscal_year: int, facts: dict[str, Decimal], *, is_audited: bool | None = True
    ) -> int:
        period_id = self._period(
            period_type="Y",
            fiscal_year=fiscal_year,
            fiscal_quarter=None,
            period_end=date(fiscal_year, 12, 31),
            is_audited=is_audited,
        )
        for code, value in facts.items():
            self._fact(period_id, code, value)
        return period_id


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


@pytest.fixture
def facts(ctx: MetricContext) -> Facts:
    return Facts(ctx.session, ctx.ticker_id, window=WINDOW)


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


def _market_cap_inputs(session: Session, facts: Facts, ctx: MetricContext) -> None:
    """20.000 đ x 1.000.000 cổ phiếu = 20 tỷ market cap, used by pe, pb and ev_ebitda."""
    _price(session, ctx.ticker_id, close=Decimal(20000))
    facts.stock("shares_outstanding", MILLION)


class TestQuarterlyFactsAreEnough:
    """The original bug: TTM metrics read a period type nothing ever creates.

    Before this, four extracted quarters produced `missing_facts:...` forever. The page
    said *không công bố* while the data sat in the database, which is the failure mode the
    product exists to avoid.
    """

    def test_four_quarters_make_a_ttm_metric_available(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        # 750 triệu x 4 = 3 tỷ EBIT; 500 triệu x 4 = 2 tỷ lãi vay; 3 / 2 = 1,50 lần.
        facts.flow("ebit", 3 * BILLION)
        facts.flow("interest_expense", 2 * BILLION)

        result = fundamentals.interest_coverage(ctx)
        assert result.value == Decimal("1.50")
        assert result.inputs["ttm_window"] == WINDOW_LABEL
        assert result.inputs["ebit"] == "3000000000.0000"
        # Invariant 2: four quarters summed means four fact ids named.
        assert len(result.inputs["ebit_fact_ids"]) == 4

    def test_a_stock_input_is_not_summed_over_the_window(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """The 4x bug, at the metric level: a balance repeated at four quarter ends must
        produce the balance, not four times it. 2 / 40 = 5%, not 2 / 160 = 1,25%."""
        facts.flow("net_profit_after_tax", 2 * BILLION)
        facts.stock_every_quarter("total_assets", 40 * BILLION)

        result = fundamentals.roa(ctx)
        assert result.value == Decimal("0.0500")
        assert result.value != Decimal("0.0125")
        assert result.inputs["total_assets"] == "40000000000.0000"
        assert len(result.inputs["total_assets_fact_ids"]) == 1

    def test_an_audited_annual_is_preferred_and_recorded(
        self, session: Session, seed_ticker: int, fact_defs: int, metric_defs: int
    ) -> None:
        """A full fiscal year of quarters plus the audited annual report: the audited
        figure is the one used, and the row says so."""
        as_of = date(2025, 12, 31)
        ctx = MetricContext(
            session=session,
            ticker_id=seed_ticker,
            symbol="TST",
            exchange_code="HOSE",
            as_of=as_of,
            par_value_vnd=Decimal(10000),
        )
        facts = Facts(session, seed_ticker, window=((2025, 1), (2025, 2), (2025, 3), (2025, 4)))
        facts.flow("net_profit_after_tax", 2 * BILLION)
        facts.flow("revenue", 20 * BILLION)
        # The audit cut profit to 1,8 tỷ. 1,8 / 20 = 9%, against 10% from the quarters.
        facts.annual(2025, {"net_profit_after_tax": Decimal("1.8") * BILLION}, is_audited=True)

        result = fundamentals.net_margin(ctx)
        assert result.value == Decimal("0.0900")
        assert result.inputs["net_profit_after_tax_basis"] == BASIS_AUDITED_ANNUAL
        assert result.inputs["net_profit_after_tax_audited_annual_preferred"] is True


class TestMissingDataIsReportedNotSilent:
    def test_with_no_statements_the_reason_names_the_window(self, ctx: MetricContext) -> None:
        """Distinct from a missing line item: nothing has been filed at all."""
        with pytest.raises(MissingInput, match="no_quarterly_period_on_or_before:2026-06-30"):
            fundamentals.interest_coverage(ctx)

    def test_with_quarters_filed_the_reason_names_the_fact_and_the_quarter(
        self, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.flow("interest_expense", 2 * BILLION)

        # Names `profit_before_tax`, not `ebit`: EBIT is derived in calc, so the line
        # someone has to go and find is the transcribed one. A reason naming a derived
        # code would send the reader looking for something no filing contains.
        with pytest.raises(MissingInput, match="missing_fact_in_quarter:profit_before_tax:2025Q3"):
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
        assert "profit_before_tax" in row.unavailable_reason
        assert "interest_expense" in row.unavailable_reason

    def test_every_fundamental_writes_a_row_even_with_no_statements(
        self, session: Session, seed_ticker: int, metric_defs: int, fact_defs: int
    ) -> None:
        """Breadth is only worth anything if each new metric also reports its own absence.
        A metric added to the registry but left out of FUNDAMENTAL_CODES would never be
        attempted, and a metric never attempted cannot say *không công bố*."""
        run_id = start_run(session, PipelineStage.CALC, "t")
        contexts = load_contexts(session, AS_OF, symbols=["TST"])
        outcome = run_metrics(
            session,
            run_id,
            fundamentals.CALC_VERSION,
            contexts,
            codes=list(fundamentals.FUNDAMENTAL_CODES),
        )

        assert outcome.written == len(fundamentals.FUNDAMENTAL_CODES)
        assert outcome.unavailable == len(fundamentals.FUNDAMENTAL_CODES)
        blank = session.execute(
            text(
                "SELECT count(*) FROM metric_value WHERE NOT is_available AND "
                "unavailable_reason IS NULL"
            )
        ).scalar_one()
        assert blank == 0


class TestNonPositiveDenominators:
    """A ratio with a non-positive denominator is withheld, never printed negative.

    Several covered tickers are loss-makers, so this is the ordinary path. Each case is
    listed explicitly rather than looped over the registry: the point is that *this* metric
    refuses *this* denominator, and a loop would still pass if one of them stopped checking.
    """

    def test_a_loss_making_company_has_no_pe_rather_than_a_negative_one(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """Printing -4.2x invites a reader to see a loss-maker as cheap, which inverts what
        the number means."""
        _market_cap_inputs(session, facts, ctx)
        facts.flow("net_profit_after_tax", -5 * BILLION)

        with pytest.raises(MissingInput, match="non_positive_denominator:net_profit_after_tax"):
            fundamentals.pe(ctx)

    def test_negative_equity_yields_no_pb(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        _market_cap_inputs(session, facts, ctx)
        facts.stock("total_equity", -2 * BILLION)

        with pytest.raises(MissingInput, match="non_positive_denominator:total_equity"):
            fundamentals.pb(ctx)

    def test_negative_equity_yields_no_roe(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.flow("net_profit_after_tax", BILLION)
        facts.stock("total_equity", -2 * BILLION)

        with pytest.raises(MissingInput, match="non_positive_denominator:total_equity"):
            fundamentals.roe(ctx)

    def test_negative_equity_yields_no_debt_to_equity(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.stock("short_term_debt", 3 * BILLION)
        facts.stock("long_term_debt", 5 * BILLION)
        facts.stock("total_equity", -BILLION)

        with pytest.raises(MissingInput, match="non_positive_denominator:total_equity"):
            fundamentals.debt_to_equity(ctx)

    def test_zero_revenue_yields_no_margin(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """A shell with no revenue has no margin; 0/0 and x/0 are both undefined."""
        facts.flow("revenue", Decimal(0))
        facts.flow("net_profit_after_tax", -BILLION)
        facts.flow("gross_profit", -BILLION)

        with pytest.raises(MissingInput, match="non_positive_denominator:revenue"):
            fundamentals.net_margin(ctx)
        with pytest.raises(MissingInput, match="non_positive_denominator:revenue"):
            fundamentals.gross_margin(ctx)

    def test_zero_assets_yields_no_roa_and_no_related_party_ratio(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.flow("net_profit_after_tax", BILLION)
        facts.stock("total_assets", Decimal(0))
        facts.stock("related_party_receivables", BILLION)

        with pytest.raises(MissingInput, match="non_positive_denominator:total_assets"):
            fundamentals.roa(ctx)
        with pytest.raises(MissingInput, match="non_positive_denominator:total_assets"):
            fundamentals.related_party_receivables_to_assets(ctx)

    def test_a_loss_making_company_has_no_cash_conversion_ratio(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """The question the ratio answers — was the reported profit collected? — does not
        arise when there was no profit."""
        facts.flow("cfo", -BILLION)
        facts.flow("net_profit_after_tax", -2 * BILLION)

        with pytest.raises(MissingInput, match="non_positive_denominator:net_profit_after_tax"):
            fundamentals.cfo_to_net_profit(ctx)

    def test_negative_ebitda_yields_no_multiple(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        _market_cap_inputs(session, facts, ctx)
        facts.stock("short_term_debt", 3 * BILLION)
        facts.stock("long_term_debt", 5 * BILLION)
        facts.stock("cash_and_equivalents", BILLION)
        facts.flow("ebitda", -2 * BILLION)

        with pytest.raises(MissingInput, match="non_positive_denominator:ebitda"):
            fundamentals.ev_ebitda(ctx)
        with pytest.raises(MissingInput, match="non_positive_denominator:ebitda"):
            fundamentals.net_debt_ebitda(ctx)

    def test_a_zero_share_count_yields_no_per_share_figure(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.flow("net_profit_after_tax", 2 * BILLION)
        facts.stock("total_equity", 10 * BILLION)
        facts.stock("shares_outstanding", Decimal(0))

        with pytest.raises(MissingInput, match="non_positive_denominator:shares_outstanding"):
            fundamentals.eps(ctx)
        with pytest.raises(MissingInput, match="non_positive_denominator:shares_outstanding"):
            fundamentals.book_value_per_share(ctx)

    def test_zero_interest_expense_is_undefined_not_infinite_safety(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """A debt-free company is not infinitely safe; the ratio simply has no value."""
        facts.flow("ebit", BILLION)
        facts.flow("interest_expense", Decimal(0))

        with pytest.raises(MissingInput, match="no_interest_expense_ratio_undefined"):
            fundamentals.interest_coverage(ctx)


class TestValuationArithmetic:
    def test_pe_uses_market_cap_over_ttm_profit(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        _market_cap_inputs(session, facts, ctx)
        facts.flow("net_profit_after_tax", 2 * BILLION)
        # 20 tỷ market cap / 2 tỷ lợi nhuận = 10 lần
        assert fundamentals.pe(ctx).value == Decimal("10.00")

    def test_pb_uses_market_cap_over_equity(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        _market_cap_inputs(session, facts, ctx)
        facts.stock("total_equity", 10 * BILLION)
        # 20 tỷ / 10 tỷ = 2 lần
        assert fundamentals.pb(ctx).value == Decimal("2.00")

    def test_ev_ebitda_adds_net_debt_to_market_cap(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        _market_cap_inputs(session, facts, ctx)
        facts.stock("short_term_debt", 3 * BILLION)
        facts.stock("long_term_debt", 5 * BILLION)
        facts.stock("cash_and_equivalents", BILLION)
        facts.flow("ebitda", 2 * BILLION)
        # (20 + 3 + 5 - 1) / 2 = 13,5 lần
        result = fundamentals.ev_ebitda(ctx)
        assert result.value == Decimal("13.50")
        assert result.inputs["net_debt_vnd"] == "7000000000.0000"

    def test_eps_is_ttm_profit_per_share(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.flow("net_profit_after_tax", 2 * BILLION)
        facts.stock("shares_outstanding", MILLION)
        # 2.000.000.000 / 1.000.000 = 2.000 đồng
        assert fundamentals.eps(ctx).value == Decimal("2000.00")

    def test_eps_is_published_negative(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """Unlike P/E: a loss per share reads as bad news, where a negative P/E reads as a
        low one. Withholding it would hide the loss, not the ambiguity."""
        facts.flow("net_profit_after_tax", -2 * BILLION)
        facts.stock("shares_outstanding", MILLION)
        assert fundamentals.eps(ctx).value == Decimal("-2000.00")

    def test_book_value_per_share_is_equity_per_share(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.stock("total_equity", 10 * BILLION)
        facts.stock("shares_outstanding", MILLION)
        # 10.000.000.000 / 1.000.000 = 10.000 đồng, exactly par value
        assert fundamentals.book_value_per_share(ctx).value == Decimal("10000.00")

    def test_book_value_per_share_goes_negative_when_equity_does(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """What the `negative_equity` flag reads. The share count is guarded positive, so
        the sign comes entirely from equity."""
        facts.stock("total_equity", -3 * BILLION)
        facts.stock("shares_outstanding", MILLION)
        assert fundamentals.book_value_per_share(ctx).value == Decimal("-3000.00")


class TestLeverageArithmetic:
    def test_interest_coverage(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        facts.flow("ebit", 3 * BILLION)
        facts.flow("interest_expense", 2 * BILLION)
        result = fundamentals.interest_coverage(ctx)
        assert result.value == Decimal("1.50")
        # Below the 2.0 threshold, so the leverage flag would fire on this.
        # inputs record the value at the scale the column stores it, not a reformatted
        # version - the point of the field is to show exactly what was read.
        assert result.inputs["ebit"] == "3000000000.0000"

    def test_net_debt_ebitda(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        facts.stock("short_term_debt", 3 * BILLION)
        facts.stock("long_term_debt", 5 * BILLION)
        facts.stock("cash_and_equivalents", BILLION)
        facts.flow("ebitda", 2 * BILLION)
        # (3 + 5 - 1) / 2 = 3.5x
        assert fundamentals.net_debt_ebitda(ctx).value == Decimal("3.50")

    def test_debt_to_equity_counts_only_borrowings(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.stock("short_term_debt", 3 * BILLION)
        facts.stock("long_term_debt", 5 * BILLION)
        facts.stock("total_equity", 10 * BILLION)
        # (3 + 5) / 10 = 0,8 lần
        result = fundamentals.debt_to_equity(ctx)
        assert result.value == Decimal("0.80")
        assert result.inputs["total_debt_vnd"] == "8000000000.0000"


class TestProfitabilityArithmetic:
    def test_roe(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        facts.flow("net_profit_after_tax", Decimal("1.5") * BILLION)
        facts.stock("total_equity", 10 * BILLION)
        assert fundamentals.roe(ctx).value == Decimal("0.1500")

    def test_roa(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        facts.flow("net_profit_after_tax", 2 * BILLION)
        facts.stock("total_assets", 40 * BILLION)
        # 2 / 40 = 5%
        assert fundamentals.roa(ctx).value == Decimal("0.0500")

    def test_net_margin(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        facts.flow("net_profit_after_tax", 2 * BILLION)
        facts.flow("revenue", 20 * BILLION)
        # 2 / 20 = 10%
        assert fundamentals.net_margin(ctx).value == Decimal("0.1000")

    def test_gross_margin(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        facts.flow("gross_profit", 5 * BILLION)
        facts.flow("revenue", 20 * BILLION)
        # 5 / 20 = 25%
        assert fundamentals.gross_margin(ctx).value == Decimal("0.2500")

    def test_gross_margin_is_published_negative(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """Selling below cost is the plain reading of a negative gross margin, and it is
        the opposite of flattering — there is nothing to protect a reader from."""
        facts.flow("gross_profit", -2 * BILLION)
        facts.flow("revenue", 20 * BILLION)
        assert fundamentals.gross_margin(ctx).value == Decimal("-0.1000")

    def test_cfo_to_net_profit(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        facts.flow("cfo", Decimal("1.6") * BILLION)
        facts.flow("net_profit_after_tax", 2 * BILLION)
        # 1,6 / 2 = 0,8 lần: bốn phần năm lợi nhuận đã về thành tiền
        assert fundamentals.cfo_to_net_profit(ctx).value == Decimal("0.80")

    def test_cfo_negative_against_a_real_profit_is_the_flagged_case(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """What `cfo_negative_while_profit_positive` reads: a negative ratio can only mean
        a positive profit (the denominator is guarded) with cash going out."""
        facts.flow("cfo", -BILLION)
        facts.flow("net_profit_after_tax", 2 * BILLION)
        assert fundamentals.cfo_to_net_profit(ctx).value == Decimal("-0.50")


class TestRelatedPartyExposure:
    def test_the_ratio_is_two_balance_sheet_lines_at_the_same_date(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.stock("related_party_receivables", 9 * BILLION)
        facts.stock("total_assets", 40 * BILLION)
        # 9 / 40 = 22,5% - above the 20% flag threshold
        result = fundamentals.related_party_receivables_to_assets(ctx)
        assert result.value == Decimal("0.2250")
        assert result.inputs["related_party_receivables_kind"] == "stock"
        assert result.inputs["total_assets_kind"] == "stock"


class TestGrowthNeedsAPriorYearComparable:
    @staticmethod
    def _eight_quarters(facts: Facts, code: str, prior: Decimal, current: Decimal) -> None:
        """Four quarters of the prior-year window, then four of the current one."""
        for fiscal_year, quarter in ((2024, 3), (2024, 4), (2025, 1), (2025, 2)):
            period_id = facts._quarter(fiscal_year, quarter)
            facts._fact(period_id, code, prior / 4)
        facts.flow(code, current)

    def test_revenue_growth(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        self._eight_quarters(facts, "revenue", 16 * BILLION, 20 * BILLION)
        # 20 / 16 - 1 = 25%
        result = fundamentals.revenue_growth_yoy(ctx)
        assert result.value == Decimal("0.2500")
        assert result.inputs["ttm_window"] == WINDOW_LABEL
        assert result.inputs["prior_year_ttm_window"] == "2024Q3-2025Q2"
        assert result.inputs["prior_year_revenue"] == "16000000000.0000"

    def test_net_profit_growth(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        self._eight_quarters(facts, "net_profit_after_tax", Decimal("1.6") * BILLION, 2 * BILLION)
        # 2 / 1,6 - 1 = 25%
        assert fundamentals.net_profit_growth_yoy(ctx).value == Decimal("0.2500")

    def test_a_fall_is_reported_as_a_negative_rate(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """What `net_profit_growth_negative_yoy` reads."""
        self._eight_quarters(facts, "net_profit_after_tax", 2 * BILLION, Decimal("1.5") * BILLION)
        # 1,5 / 2 - 1 = -25%
        assert fundamentals.net_profit_growth_yoy(ctx).value == Decimal("-0.2500")

    def test_a_missing_prior_year_names_the_quarter_it_needed(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        facts.flow("revenue", 20 * BILLION)

        with pytest.raises(
            MissingInput, match="no_prior_year_comparable:revenue:missing_quarter:2024Q3"
        ):
            fundamentals.revenue_growth_yoy(ctx)

    def test_growth_from_a_loss_is_refused(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """Going from -2 tỷ to +1 tỷ would read as -150%, and from -2 tỷ to -1 tỷ as +50%.
        Neither is a growth rate anyone can use."""
        self._eight_quarters(facts, "net_profit_after_tax", -2 * BILLION, BILLION)

        with pytest.raises(
            MissingInput, match="non_positive_denominator:net_profit_after_tax_prior_year"
        ):
            fundamentals.net_profit_growth_yoy(ctx)


class TestDilutionNeedsAnExactWindow:
    """shares_cagr_3y is annual rather than TTM: a share count is a stock, and its window
    is three years, so there is nothing for periods.py to sum."""

    def test_three_year_cagr(self, session: Session, ctx: MetricContext, facts: Facts) -> None:
        # 2025 is the newest annual report ending on or before 30/06/2026 - a 2026 annual
        # closes on 31/12/2026, which is after the valuation date and must not be read.
        facts.annual(2022, {"shares_outstanding": MILLION}, is_audited=True)
        facts.annual(2025, {"shares_outstanding": 2 * MILLION}, is_audited=True)
        result = fundamentals.shares_cagr_3y(ctx)
        # Doubling over three years is roughly 26% a year.
        assert Decimal("0.25") < result.value < Decimal("0.27")
        assert result.inputs["fiscal_year_then"] == 2022

    def test_a_missing_window_is_refused_rather_than_approximated(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """Comparing against whatever the oldest available year happens to be would change
        the metric's meaning per ticker without saying so."""
        facts.annual(2025, {"shares_outstanding": 2 * MILLION}, is_audited=True)
        facts.annual(2024, {"shares_outstanding": Decimal("1.8") * MILLION}, is_audited=True)

        with pytest.raises(MissingInput, match="no_shares_outstanding_for_fiscal_year:2022"):
            fundamentals.shares_cagr_3y(ctx)


class TestTheNewFlagsFireOnTheseMetrics:
    """End to end: statement facts in, `flag_instance` rows out.

    A flag is only worth adding if the metric it reads actually reaches it, and the chain
    has four links (fact → TTM window → metric_value → flag_instance). The flags are seeded
    from `rules/flags.yaml` rather than hand-written rows, because `calc/flags.py` refuses
    to evaluate when `flag_def` does not match the file on disk.
    """

    @staticmethod
    def _evaluate(session: Session, ctx: MetricContext) -> dict[str, dict[str, object]]:
        from quantumedge.calc.flags import evaluate_flags
        from quantumedge.config import load_settings
        from quantumedge.db.seed import seed_flags

        seed_flags(session, load_settings())
        run_id = start_run(session, PipelineStage.CALC, "t")
        run_metrics(
            session,
            run_id,
            fundamentals.CALC_VERSION,
            load_contexts(session, ctx.as_of, symbols=["TST"]),
            codes=list(fundamentals.FUNDAMENTAL_CODES),
        )
        evaluate_flags(session, run_id, ctx.as_of, symbols=["TST"])
        rows = session.execute(
            text(
                "SELECT flag_code, state, reason, evidence FROM flag_instance "
                "WHERE ticker_id = :t AND as_of_date = CAST(:d AS date)"
            ),
            {"t": ctx.ticker_id, "d": ctx.as_of},
        ).mappings()
        return {r["flag_code"]: dict(r) for r in rows}

    def test_all_four_fire_with_evidence(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        # Eight quarters so the growth comparison has a base: lãi giảm từ 2 tỷ xuống 1,5 tỷ.
        for fiscal_year, quarter in ((2024, 3), (2024, 4), (2025, 1), (2025, 2)):
            period_id = facts._quarter(fiscal_year, quarter)
            facts._fact(period_id, "net_profit_after_tax", Decimal("0.5") * BILLION)
        facts.flow("net_profit_after_tax", Decimal("1.5") * BILLION)
        facts.flow("cfo", -BILLION)
        facts.stock("total_equity", -3 * BILLION)
        facts.stock("shares_outstanding", MILLION)
        facts.stock("related_party_receivables", 9 * BILLION)
        facts.stock("total_assets", 40 * BILLION)

        flags = self._evaluate(session, ctx)
        for code in (
            "negative_equity",
            "cfo_negative_while_profit_positive",
            "related_party_receivables_over_20pct_assets",
            "net_profit_growth_negative_yoy",
        ):
            assert flags[code]["state"] == "active", f"{code}: {flags[code]}"
            # A negative statement about a named company needs something attached to it.
            assert flags[code]["evidence"], f"{code} fired without evidence"

    def test_with_no_statements_they_read_unknown_with_a_reason(
        self, session: Session, ctx: MetricContext, metric_defs: int
    ) -> None:
        """Invariant 4: absent inputs are `unknown`, never `inactive`. An `inactive`
        negative-equity flag on a company that has filed nothing would be a clean bill of
        health nobody earned."""
        flags = self._evaluate(session, ctx)
        for code in (
            "negative_equity",
            "cfo_negative_while_profit_positive",
            "related_party_receivables_over_20pct_assets",
            "net_profit_growth_negative_yoy",
        ):
            assert flags[code]["state"] == "unknown", f"{code}: {flags[code]}"
            assert flags[code]["reason"], f"{code} is unknown without saying why"

    def test_a_healthy_company_reads_inactive_not_unknown(
        self, session: Session, ctx: MetricContext, facts: Facts
    ) -> None:
        """The other half of three-valued state: a rule tested against real data that did
        not fire is a genuine negative, and must be distinguishable from missing data."""
        for fiscal_year, quarter in ((2024, 3), (2024, 4), (2025, 1), (2025, 2)):
            period_id = facts._quarter(fiscal_year, quarter)
            facts._fact(period_id, "net_profit_after_tax", Decimal("0.25") * BILLION)
        facts.flow("net_profit_after_tax", 2 * BILLION)
        facts.flow("cfo", Decimal("2.4") * BILLION)
        facts.stock("total_equity", 10 * BILLION)
        facts.stock("shares_outstanding", MILLION)
        facts.stock("related_party_receivables", BILLION)
        facts.stock("total_assets", 40 * BILLION)

        flags = self._evaluate(session, ctx)
        for code in (
            "negative_equity",
            "cfo_negative_while_profit_positive",
            "related_party_receivables_over_20pct_assets",
            "net_profit_growth_negative_yoy",
        ):
            assert flags[code]["state"] == "inactive", f"{code}: {flags[code]}"


class TestTheRulesFileAndTheCodeAgree:
    @staticmethod
    def _rules() -> list[dict[str, object]]:
        import yaml

        from quantumedge.config import load_settings

        data = yaml.safe_load(
            (load_settings().rules_dir / "metrics.yaml").read_text(encoding="utf-8")
        )
        return list(data["metrics"])

    def test_no_metric_in_the_rules_file_lacks_an_implementation(self) -> None:
        """A defined-but-unimplemented metric produces no row at all, so the fact sheet
        cannot report it as unavailable. This test is what keeps that from recurring."""
        from quantumedge.calc import liquidity  # noqa: F401  (registers its metrics)
        from quantumedge.calc.registry import REGISTRY

        defined = {str(m["code"]) for m in self._rules()}
        missing = sorted(defined - set(REGISTRY))
        assert not missing, f"defined in metrics.yaml but never implemented: {missing}"

    def test_every_fundamental_is_listed_in_fundamental_codes(self) -> None:
        """`cli.py` runs this list, so a metric left out of it is never attempted."""
        from quantumedge.calc.registry import REGISTRY

        for code in fundamentals.FUNDAMENTAL_CODES:
            assert code in REGISTRY, f"{code} is listed but not registered"
        assert len(set(fundamentals.FUNDAMENTAL_CODES)) == len(fundamentals.FUNDAMENTAL_CODES)

    def test_declared_required_facts_match_the_rules_file(self) -> None:
        """`requires_facts` is written twice - in the decorator, which gates the metric, and
        in metrics.yaml, which the page reads to explain what is missing. Drift between them
        means a page naming an input the metric does not actually use."""
        from quantumedge.calc.registry import REGISTRY

        for row in self._rules():
            code = str(row["code"])
            if code not in fundamentals.FUNDAMENTAL_CODES:
                continue
            declared = set(REGISTRY[code].requires_facts)
            in_rules = set(row.get("requires_facts") or [])  # type: ignore[arg-type]
            assert declared == in_rules, f"{code}: {declared} vs {in_rules}"

    def test_every_required_fact_exists_in_the_registry(self) -> None:
        """A metric requiring a fact code the extractor can never produce would be
        permanently unavailable, which is worse than not existing."""
        import yaml

        from quantumedge.calc.registry import REGISTRY
        from quantumedge.config import load_settings

        codes = {
            row["code"]
            for row in yaml.safe_load(
                (load_settings().rules_dir / "fact_codes.yaml").read_text(encoding="utf-8")
            )["fact_codes"]
        }
        for code in fundamentals.FUNDAMENTAL_CODES:
            unknown = set(REGISTRY[code].requires_facts) - codes
            assert not unknown, f"{code} requires undefined fact codes: {sorted(unknown)}"
