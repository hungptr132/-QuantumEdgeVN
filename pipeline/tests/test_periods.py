"""The trailing-twelve-month view, and the two ways it could silently be wrong.

`calc/fundamentals.py` used to read `period_type = 'TTM'` rows that nothing in the pipeline
writes, so every TTM metric was permanently unavailable. The fix resolves TTM at read time
from quarterly facts — which introduces two failure modes worth a test each:

* summing three quarters and grossing them up to twelve months, producing a figure nobody
  filed; and
* summing a balance-sheet line over four quarters, producing one about four times too
  large that still looks entirely plausible.

The second is the dangerous one: no constraint and no reviewer would catch "total assets
2.000 tỷ" when the truth is 500 tỷ. So the flow/stock split is declared rather than
inferred, and `test_a_new_fact_code_must_be_classified` fails the suite if a line item is
added to the registry without a kind.
"""

from datetime import date
from decimal import Decimal

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc import periods
from quantumedge.calc.periods import (
    BASIS_AUDITED_ANNUAL,
    BASIS_FOUR_QUARTERS,
    BASIS_LATEST_QUARTER,
    QUARTERS_PER_YEAR,
    FactKind,
    TtmReader,
    UnclassifiedFactCode,
    classify,
    resolve_ttm_window,
    ttm_fact,
)
from quantumedge.calc.registry import MissingInput
from quantumedge.config import load_settings
from quantumedge.db.enums import PipelineStage
from quantumedge.runs import start_run

# A Q1 close, so the default window is 2025Q2-2026Q1 and spans two fiscal years. That is
# deliberate: a window inside a single year would silently exercise the audited-annual
# path in every test, and the plain four-quarter sum would never be checked.
AS_OF = date(2026, 3, 31)

BILLION = Decimal(1_000_000_000)

_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


class Statements:
    """Inserts statement periods and facts the way `extract/statements.py` does.

    Quarter ends match `extract.statements._period_end` exactly, because the audited-annual
    preference turns on an annual period covering the same twelve months — if the fixture
    invented its own period ends that comparison would be tested against fiction.
    """

    def __init__(self, session: Session, ticker_id: int) -> None:
        self.session = session
        self.ticker_id = ticker_id
        session.execute(
            text(
                "INSERT INTO source (code, kind, name) VALUES ('stmt', 'disclosure', 'stmt') "
                "ON CONFLICT DO NOTHING"
            )
        )
        self.run_id = start_run(session, PipelineStage.EXTRACT, "stmt")

    def _document(self, period_key: str) -> int:
        return int(
            self.session.execute(
                text(
                    "INSERT INTO raw_document (source_id, ingest_run_id, ticker_id, symbol, "
                    " doc_type, period_key, content_hash, payload, byte_size) "
                    "VALUES ((SELECT id FROM source WHERE code='stmt'), :r, :t, 'TST', "
                    " 'statement', :pk, :h, '{}'::jsonb, 2) RETURNING id"
                ),
                {
                    "r": self.run_id,
                    "t": self.ticker_id,
                    "pk": period_key,
                    "h": f"hash-{period_key}",
                },
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
        doc = self._document(key)
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
                    "src": doc,
                },
            ).scalar_one()
        )

    def _facts(self, period_id: int, facts: dict[str, Decimal]) -> None:
        for code, value in facts.items():
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

    def quarter(self, fiscal_year: int, quarter: int, facts: dict[str, Decimal]) -> int:
        month, day = _QUARTER_END[quarter]
        period_id = self._period(
            period_type="Q",
            fiscal_year=fiscal_year,
            fiscal_quarter=quarter,
            period_end=date(fiscal_year, month, day),
            is_audited=None,
        )
        self._facts(period_id, facts)
        return period_id

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
        self._facts(period_id, facts)
        return period_id

    def four_quarters(self, values: tuple[Decimal, ...], *, fact_code: str = "revenue") -> None:
        """2025Q2 … 2026Q1, one value each — the default window, oldest first."""
        for (fiscal_year, quarter), value in zip(
            ((2025, 2), (2025, 3), (2025, 4), (2026, 1)), values, strict=True
        ):
            self.quarter(fiscal_year, quarter, {fact_code: value})


@pytest.fixture
def statements(session: Session, seed_ticker: int, fact_defs: int) -> Statements:
    return Statements(session, seed_ticker)


class TestAFlowIsSummedOverExactlyFourQuarters:
    def test_four_quarters_sum_to_the_filed_total(
        self, session: Session, statements: Statements
    ) -> None:
        # 100 + 200 + 300 + 400 = 1.000 tỷ, computed by hand.
        statements.four_quarters((100 * BILLION, 200 * BILLION, 300 * BILLION, 400 * BILLION))

        window = resolve_ttm_window(session, statements.ticker_id, AS_OF)
        fact = ttm_fact(session, window, "revenue")

        assert fact.value == 1000 * BILLION
        assert fact.basis == BASIS_FOUR_QUARTERS
        assert fact.kind is FactKind.FLOW
        assert window.label == "2025Q2-2026Q1"
        assert window.window_start == date(2025, 4, 1)
        assert window.window_end == date(2026, 3, 31)

    def test_the_result_names_every_row_it_used(
        self, session: Session, statements: Statements
    ) -> None:
        """Invariant 2: a figure that cannot name its sources cannot be published."""
        statements.four_quarters((BILLION, BILLION, BILLION, BILLION))

        window = resolve_ttm_window(session, statements.ticker_id, AS_OF)
        fact = ttm_fact(session, window, "revenue")

        assert len(fact.fact_ids) == QUARTERS_PER_YEAR
        assert fact.period_ids == window.period_ids
        inputs = fact.as_inputs()
        assert inputs["revenue"] == "4000000000.0000"
        assert sorted(inputs["revenue_quarters"]) == ["2025Q2", "2025Q3", "2025Q4", "2026Q1"]

    def test_a_missing_quarter_is_unavailable_and_names_the_quarter(
        self, session: Session, statements: Statements
    ) -> None:
        """Three quarters grossed up by 4/3 would be a number nobody filed, and nothing
        downstream could distinguish it from a real one."""
        statements.quarter(2025, 2, {"revenue": 100 * BILLION})
        statements.quarter(2025, 4, {"revenue": 300 * BILLION})
        statements.quarter(2026, 1, {"revenue": 400 * BILLION})

        with pytest.raises(MissingInput, match="missing_quarter:2025Q3"):
            resolve_ttm_window(session, statements.ticker_id, AS_OF)

    def test_a_quarter_that_omits_the_line_item_names_both(
        self, session: Session, statements: Statements
    ) -> None:
        """The quarter was filed but this line was not in it — a different failure from a
        missing filing, and the page should be able to say which."""
        statements.quarter(2025, 2, {"revenue": 100 * BILLION})
        statements.quarter(2025, 3, {"total_assets": 500 * BILLION})
        statements.quarter(2025, 4, {"revenue": 300 * BILLION})
        statements.quarter(2026, 1, {"revenue": 400 * BILLION})

        window = resolve_ttm_window(session, statements.ticker_id, AS_OF)
        with pytest.raises(MissingInput, match="missing_fact_in_quarter:revenue:2025Q3"):
            ttm_fact(session, window, "revenue")

    def test_no_quarterly_statements_at_all_says_so(
        self, session: Session, statements: Statements
    ) -> None:
        statements.annual(2025, {"revenue": 900 * BILLION})

        with pytest.raises(MissingInput, match="no_quarterly_period_on_or_before:2026-03-31"):
            resolve_ttm_window(session, statements.ticker_id, AS_OF)


class TestAStockIsNeverSummed:
    """The 4x bug, asserted directly."""

    def test_a_balance_sheet_line_takes_the_latest_quarter_not_the_sum(
        self, session: Session, statements: Statements
    ) -> None:
        for fiscal_year, quarter in ((2025, 2), (2025, 3), (2025, 4), (2026, 1)):
            statements.quarter(fiscal_year, quarter, {"total_assets": 500 * BILLION})

        window = resolve_ttm_window(session, statements.ticker_id, AS_OF)
        fact = ttm_fact(session, window, "total_assets")

        assert fact.value == 500 * BILLION
        # Spelled out, because this is the specific wrong answer the split exists to stop:
        # it is plausible at a glance and no constraint downstream would reject it.
        assert fact.value != 2000 * BILLION
        assert fact.kind is FactKind.STOCK
        assert fact.basis == BASIS_LATEST_QUARTER
        assert fact.period_ids == (window.latest.period_id,)
        assert len(fact.fact_ids) == 1

    def test_the_balance_taken_is_the_one_at_the_window_end(
        self, session: Session, statements: Statements
    ) -> None:
        """A rising balance makes "latest" distinguishable from "first" or "average"."""
        statements.quarter(2025, 2, {"total_equity": 100 * BILLION})
        statements.quarter(2025, 3, {"total_equity": 110 * BILLION})
        statements.quarter(2025, 4, {"total_equity": 120 * BILLION})
        statements.quarter(2026, 1, {"total_equity": 130 * BILLION})

        window = resolve_ttm_window(session, statements.ticker_id, AS_OF)
        assert ttm_fact(session, window, "total_equity").value == 130 * BILLION

    def test_a_stock_missing_at_the_window_end_names_that_quarter(
        self, session: Session, statements: Statements
    ) -> None:
        statements.quarter(2025, 2, {"total_assets": 500 * BILLION})
        statements.quarter(2025, 3, {"total_assets": 500 * BILLION})
        statements.quarter(2025, 4, {"total_assets": 500 * BILLION})
        statements.quarter(2026, 1, {"revenue": 400 * BILLION})

        window = resolve_ttm_window(session, statements.ticker_id, AS_OF)
        with pytest.raises(MissingInput, match="missing_fact_at_period_end:total_assets:2026Q1"):
            ttm_fact(session, window, "total_assets")


class TestAnAuditedAnnualIsBetterEvidence:
    """When an audited annual covers the same twelve months, it wins — and says so."""

    AUDITED_AS_OF = date(2025, 12, 31)

    def _full_year_of_quarters(self, statements: Statements) -> None:
        for quarter, value in ((1, 100), (2, 200), (3, 300), (4, 400)):
            statements.quarter(2025, quarter, {"revenue": Decimal(value) * BILLION})

    def test_the_audited_figure_is_used_and_recorded_as_preferred(
        self, session: Session, statements: Statements
    ) -> None:
        """The audited total differs from the sum of the unaudited quarters on purpose:
        that is the normal Vietnamese case (audit adjustments land in Q4 or a restatement),
        and it is the only way to prove which figure was actually used."""
        self._full_year_of_quarters(statements)
        annual_id = statements.annual(2025, {"revenue": 950 * BILLION}, is_audited=True)

        window = resolve_ttm_window(session, statements.ticker_id, self.AUDITED_AS_OF)
        assert window.audited_annual is not None
        assert window.audited_annual.period_id == annual_id

        fact = ttm_fact(session, window, "revenue")
        assert fact.value == 950 * BILLION
        assert fact.value != 1000 * BILLION  # not the sum of the four quarters
        assert fact.basis == BASIS_AUDITED_ANNUAL
        assert fact.audited_annual_preferred is True
        assert fact.period_ids == (annual_id,)
        assert fact.as_inputs()["revenue_audited_annual_preferred"] is True

    def test_an_unaudited_annual_is_not_preferred(
        self, session: Session, statements: Statements
    ) -> None:
        """`is_audited` is three-valued: NULL means *not known to be audited*, which is no
        licence to call the figure audited."""
        self._full_year_of_quarters(statements)
        statements.annual(2025, {"revenue": 950 * BILLION}, is_audited=None)

        window = resolve_ttm_window(session, statements.ticker_id, self.AUDITED_AS_OF)
        assert window.audited_annual is None

        fact = ttm_fact(session, window, "revenue")
        assert fact.value == 1000 * BILLION
        assert fact.basis == BASIS_FOUR_QUARTERS

    def test_an_annual_covering_a_different_twelve_months_is_not_preferred(
        self, session: Session, statements: Statements
    ) -> None:
        """The default window straddles two fiscal years, so no annual report covers it."""
        statements.four_quarters((100 * BILLION, 200 * BILLION, 300 * BILLION, 400 * BILLION))
        statements.annual(2025, {"revenue": 950 * BILLION}, is_audited=True)

        window = resolve_ttm_window(session, statements.ticker_id, AS_OF)
        assert window.audited_annual is None
        assert ttm_fact(session, window, "revenue").value == 1000 * BILLION

    def test_a_line_item_absent_from_the_audited_annual_falls_back_to_the_quarters(
        self, session: Session, statements: Statements
    ) -> None:
        """Preferring the audited report must not mean losing a figure it omitted."""
        self._full_year_of_quarters(statements)
        statements.annual(2025, {"total_assets": 500 * BILLION}, is_audited=True)

        window = resolve_ttm_window(session, statements.ticker_id, self.AUDITED_AS_OF)
        fact = ttm_fact(session, window, "revenue")
        assert fact.value == 1000 * BILLION
        assert fact.basis == BASIS_FOUR_QUARTERS


class TestThePriorYearWindow:
    """What a year-on-year growth metric compares against."""

    def test_shifting_four_quarters_back_gives_the_prior_year(
        self, session: Session, statements: Statements
    ) -> None:
        for fiscal_year, quarter, value in (
            (2024, 2, 50),
            (2024, 3, 60),
            (2024, 4, 70),
            (2025, 1, 80),
            (2025, 2, 100),
            (2025, 3, 200),
            (2025, 4, 300),
            (2026, 1, 400),
        ):
            statements.quarter(fiscal_year, quarter, {"revenue": Decimal(value) * BILLION})

        reader = TtmReader(session, statements.ticker_id, AS_OF)
        assert reader.fact("revenue").value == 1000 * BILLION
        prior = reader.fact("revenue", shift_quarters=QUARTERS_PER_YEAR)
        # 50 + 60 + 70 + 80 = 260 tỷ
        assert prior.value == 260 * BILLION
        assert reader.window(shift_quarters=QUARTERS_PER_YEAR).label == "2024Q2-2025Q1"

    def test_a_short_history_names_the_quarter_the_comparison_needs(
        self, session: Session, statements: Statements
    ) -> None:
        statements.four_quarters((100 * BILLION, 200 * BILLION, 300 * BILLION, 400 * BILLION))
        reader = TtmReader(session, statements.ticker_id, AS_OF)

        with pytest.raises(MissingInput, match="missing_quarter:2024Q2"):
            reader.window(shift_quarters=QUARTERS_PER_YEAR)


class TestTheFlowStockSplitIsDeclared:
    """A new line item must be classified, or this suite fails."""

    @staticmethod
    def _registry() -> list[dict[str, str]]:
        path = load_settings().rules_dir / "fact_codes.yaml"
        return list(yaml.safe_load(path.read_text(encoding="utf-8"))["fact_codes"])

    def test_a_new_fact_code_must_be_classified(self) -> None:
        """The test the task of adding a line item has to pass.

        Defaulting an unclassified code to either kind produces a wrong number rather than
        an absent one: flow would quadruple a balance, stock would report three months of
        an income line as a year.
        """
        codes = [row["code"] for row in self._registry()]
        missing = periods.unclassified(codes)
        assert not missing, (
            "these fact codes are in rules/fact_codes.yaml but classified in neither "
            "FLOW_FACT_CODES nor STOCK_FACT_CODES in calc/periods.py - a TTM figure "
            f"cannot be computed for them: {missing}"
        )

    def test_the_declared_sets_agree_with_the_statement_each_line_came_from(self) -> None:
        """The second, independent authority: `financial_fact_def.statement` already says
        whether a line is an income/cash-flow item or a balance-sheet one. A declared kind
        that contradicts it is a typo, and this is what catches it. `derived` implies
        nothing (EBITDA is a flow; a derived net-debt line would be a stock), so those
        rely on the declared set alone."""
        for row in self._registry():
            expected = periods.KIND_BY_STATEMENT.get(row["statement"])
            if expected is None:
                continue
            assert classify(row["code"]) is expected, (
                f"{row['code']} is declared {classify(row['code'])} but its statement "
                f"'{row['statement']}' implies {expected}"
            )

    def test_nothing_is_classified_that_the_registry_does_not_define(self) -> None:
        """The reverse direction: a code removed from the registry leaves a dead entry
        here, and a metric reading it would fail for a reason nobody could explain."""
        defined = {row["code"] for row in self._registry()}
        declared = periods.FLOW_FACT_CODES | periods.STOCK_FACT_CODES
        assert not declared - defined

    def test_the_two_sets_do_not_overlap(self) -> None:
        assert not periods.FLOW_FACT_CODES & periods.STOCK_FACT_CODES

    def test_an_unknown_code_raises_rather_than_guessing(self) -> None:
        with pytest.raises(UnclassifiedFactCode, match="inventories"):
            classify("inventories")


class TestDescribeTtm:
    def test_it_reports_the_window_and_a_reason_per_line_item(
        self, session: Session, statements: Statements
    ) -> None:
        """What `qe calc ttm` prints: enough to tell "the window is short a quarter" from
        "only this one line item is absent"."""
        statements.four_quarters((100 * BILLION, 200 * BILLION, 300 * BILLION, 400 * BILLION))

        report = periods.describe_ttm(session, statements.ticker_id, AS_OF)
        assert report["window"]["ttm_window"] == "2025Q2-2026Q1"

        items = {row["fact_code"]: row for row in report["items"]}
        assert items["revenue"]["value"] == "1000000000000.0000"
        assert items["revenue"]["basis"] == BASIS_FOUR_QUARTERS
        assert items["total_assets"]["value"] is None
        assert "total_assets" in items["total_assets"]["unavailable_reason"]

    def test_with_no_statements_it_reports_the_window_itself_as_the_problem(
        self, session: Session, statements: Statements
    ) -> None:
        report = periods.describe_ttm(session, statements.ticker_id, AS_OF)
        assert report["window"] is None
        assert report["unavailable_reason"].startswith("no_quarterly_period_on_or_before")
        assert report["quarters_available"] == []
