"""The fundamentals chain, end to end, on a synthetic Circular 200 filing set.

    filing → raw_document → financial_period + financial_fact → metric_value
           → metric_benchmark → flag_instance → fact_sheet → QA

Every link has unit tests. Nothing covered the joins, which is where a pipeline of this
shape actually breaks: a period type nobody writes, a fact code a metric reads but the
extractor never produces, a flag whose evidence reference points at a table the QA screen
does not resolve. Those are invisible to tests that stop at one stage boundary.

So this file runs the *real* modules in the real order — `ingest.filings`,
`extract.statements`, `calc.registry`, `calc.benchmarks`, `calc.flags`,
`factsheet.build`, `factsheet.qa`, `factsheet.store` — and asserts on what reached the
database, not on what each function returned.

**The data is unmistakably synthetic by construction.** The symbol is `ZZTEST`, which is
six characters and so cannot be a Vietnamese listing (HOSE and HNX symbols are three).
The figures are `1.234.567.890.000` and `987.654.321.000`. A negative statement about a
named company needs real evidence, and nothing here could be mistaken for one.

**What this harness provably cannot catch.** Every VAS identity is scale-invariant: a
statement printed in thousands balances exactly as well as one printed in đồng. A
document whose figures are uniformly 1000x too small passes all three identities, is
stored with full provenance, and is wrong. `TestScaleInvarianceIsNotCaught` asserts
exactly that, because the honest limit of a self-validating extractor is worth writing
down rather than discovering later. The only guard is the declared unit, which is read —
see `TestADeclaredNonDongUnitIsRefused` — and a document that simply omits the
declaration while printing in thousands is undefended.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from support import vas_workbook as vw

from quantumedge.calc import fundamentals, liquidity
from quantumedge.calc.benchmarks import (
    build_exchange_benchmarks,
    build_history_benchmarks,
    build_sector_benchmarks,
)
from quantumedge.calc.flags import FlagOutcome, evaluate_flags
from quantumedge.calc.registry import REGISTRY, CalcOutcome, load_contexts, run_metrics
from quantumedge.config import load_settings
from quantumedge.db.enums import PipelineStage
from quantumedge.db.seed import seed_fact_codes, seed_flags, seed_metrics, seed_sources
from quantumedge.extract.mapping.vas import VasSpec, spec_for
from quantumedge.extract.statements import StatementOutcome, extract_statements
from quantumedge.factsheet.build import build_fact_sheet
from quantumedge.factsheet.qa import QaReport, validate
from quantumedge.factsheet.schema import FactSheet
from quantumedge.factsheet.store import StoreOutcome, store_fact_sheet
from quantumedge.ingest.base import StoreResult, store_raw
from quantumedge.ingest.filings import archive, scan, to_payload
from quantumedge.runs import start_run

# Explicit, never today(). Far enough past the last filed period that the whole filing
# set is in scope, and inside the 18-month window the disclosure-absence rule uses.
AS_OF = date(2025, 6, 30)

# Two full years of quarters, so a twelve-month window and its prior-year comparable both
# resolve. `calc/periods.py` sums exactly four consecutive quarters and refuses to gross
# up three, so anything less leaves every TTM metric unavailable.
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
CURRENT_YEAR = 2024
PRIOR_YEAR = 2023

# Quarterly flows are a quarter of the annual figure. The prior year is twice as large,
# which makes year-on-year growth exactly −50%: a figure worth asserting, and one that
# drives a fundamental flag to `active` with a `metric_value` reference as its evidence.
CURRENT_QUARTER_DIVISOR = 4
PRIOR_QUARTER_DIVISOR = 2
EXPECTED_GROWTH = Decimal("-0.500000")

# Enough calendar to satisfy the 252-session windows, so the liquidity block the fact
# sheet requires is populated and the page is not stale. Price data is scaffolding here,
# not the subject: it is inserted directly rather than ingested.
SESSIONS = 260
PRICE_SOURCE = "vndirect_dchart"


# ---------------------------------------------------------------------------------------
# fixtures


@pytest.fixture(scope="module")
def spec() -> VasSpec:
    return spec_for(load_settings().rules_dir)


@pytest.fixture
def universe(session: Session) -> int:
    """A covered ticker plus every reference table the chain reads. Returns ticker id.

    Reference rows come from the real seeders, not stubs. `metric_value.metric_code`,
    `financial_fact.fact_code` and `flag_instance.flag_code` are all foreign keys, so a
    metric implemented without a `rules/metrics.yaml` entry, or a fact code the registry
    never declared, fails here rather than in production.
    """
    settings = load_settings()
    session.execute(
        text(
            "INSERT INTO exchange (code, name) VALUES ('HOSE', 'Ho Chi Minh Stock Exchange') "
            "ON CONFLICT DO NOTHING"
        )
    )
    sector_id = session.execute(
        text(
            "INSERT INTO sector (code, name_vi) VALUES ('zz_synthetic', 'Ngành dữ liệu kiểm thử') "
            "RETURNING id"
        )
    ).scalar_one()
    company_id = session.execute(
        text(
            "INSERT INTO company (legal_name_vi, short_name, sector_id) "
            "VALUES (:name, 'ZZTEST', :s) RETURNING id"
        ),
        {"name": vw.SYNTHETIC_COMPANY_VI, "s": sector_id},
    ).scalar_one()
    ticker_id = session.execute(
        text(
            "INSERT INTO ticker (symbol, company_id, exchange_code, is_covered) "
            "VALUES (:sym, :c, 'HOSE', true) RETURNING id"
        ),
        {"sym": vw.SYNTHETIC_SYMBOL, "c": company_id},
    ).scalar_one()

    seed_sources(session, settings)
    seed_fact_codes(session, settings)
    seed_metrics(session, settings)
    seed_flags(session, settings)
    return int(ticker_id)


@pytest.fixture
def filings_root(tmp_path: Path) -> Path:
    """The folder a person drops filings into: data/filings/<SYMBOL>/."""
    root = tmp_path / "filings"
    (root / vw.SYNTHETIC_SYMBOL).mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------------------
# the chain, one real stage at a time


def write_filing_set(root: Path, spec: VasSpec) -> list[Path]:
    """Eight quarterly filings and the annual that covers the latest four.

    `<year>-<period>-<description>.xlsx`: the filename is what carries the fiscal period.
    `ingest.filings` refuses a name it cannot read a period from rather than guessing from
    the file's contents or its modification date.
    """
    folder = root / vw.SYNTHETIC_SYMBOL
    written: list[Path] = []
    for year, divisor in (
        (PRIOR_YEAR, PRIOR_QUARTER_DIVISOR),
        (CURRENT_YEAR, CURRENT_QUARTER_DIVISOR),
    ):
        for quarter in QUARTERS:
            written.append(
                vw.workbook(
                    folder / f"{year}-{quarter}-bao-cao-tai-chinh.xlsx",
                    spec,
                    vw.quarterly(spec, divisor),
                )
            )
    written.append(
        vw.workbook(
            folder / f"{CURRENT_YEAR}-Y-bao-cao-tai-chinh-kiem-toan.xlsx",
            spec,
            vw.BALANCED,
        )
    )
    return written


def ingest(session: Session, root: Path, store: Path) -> StoreResult:
    """The real filing ingest: scan, hash, archive under the hash, record the bytes."""
    run_id = start_run(session, PipelineStage.INGEST, "roundtrip", source_code="company_filing")
    outcome = scan(root)
    assert not outcome.rejected, outcome.rejected
    payloads = [to_payload(found, archive(found, store)) for found in outcome.found]
    return store_raw(session, run_id, payloads)


def extract(session: Session, spec: VasSpec) -> StatementOutcome:
    run_id = start_run(session, PipelineStage.EXTRACT, "roundtrip")
    return extract_statements(session, run_id, spec)


def defined_metric_codes(session: Session) -> set[str]:
    return {str(c) for c in session.execute(text("SELECT code FROM metric_def")).scalars()}


def metric_code_groups(session: Session) -> tuple[list[str], list[str]]:
    """(liquidity codes, fundamental codes), both restricted to what `metric_def` declares.

    `cli.py` runs two explicit lists rather than the whole registry, and so does this. The
    intersection with `metric_def` is deliberate: a metric registered in code before its
    `rules/metrics.yaml` entry lands violates `metric_value.metric_code`'s foreign key, and
    that is a reference-data gap to report, not a reason for a chain test to raise an
    IntegrityError with nothing useful in the message.
    """
    defined = defined_metric_codes(session)
    fundamental = [c for c in fundamentals.FUNDAMENTAL_CODES if c in defined]
    rest = [c for c in REGISTRY if c not in fundamentals.FUNDAMENTAL_CODES and c in defined]
    return sorted(rest), fundamental


def calc_metrics(session: Session) -> tuple[CalcOutcome, CalcOutcome]:
    """Both metric groups, each stamped with its own calc_version, as the CLI does."""
    market_codes, fundamental_codes = metric_code_groups(session)
    contexts = load_contexts(session, AS_OF, symbols=[vw.SYNTHETIC_SYMBOL])
    assert contexts, "the covered ticker produced no metric context"

    market_run = start_run(session, PipelineStage.CALC, "liquidity", as_of=AS_OF)
    market = run_metrics(session, market_run, liquidity.CALC_VERSION, contexts, codes=market_codes)

    fundamental_run = start_run(session, PipelineStage.CALC, "fundamentals", as_of=AS_OF)
    fundamental = run_metrics(
        session, fundamental_run, fundamentals.CALC_VERSION, contexts, codes=fundamental_codes
    )
    return market, fundamental


def calc_benchmarks(session: Session) -> None:
    run_id = start_run(session, PipelineStage.CALC, "benchmarks", as_of=AS_OF)
    build_sector_benchmarks(session, run_id, AS_OF)
    build_exchange_benchmarks(session, run_id, AS_OF)
    build_history_benchmarks(session, run_id, AS_OF)


def calc_flags(session: Session) -> tuple[int, FlagOutcome]:
    run_id = start_run(session, PipelineStage.CALC, "flags", as_of=AS_OF)
    return run_id, evaluate_flags(session, run_id, AS_OF, symbols=[vw.SYNTHETIC_SYMBOL])


def seed_price_scaffolding(session: Session, ticker_id: int) -> None:
    """Calendar and bars, inserted directly. Not part of the chain under test.

    The fact sheet requires a liquidity block and a price no more than five sessions
    stale, so without this the QA report would fail for reasons that have nothing to do
    with fundamentals. Weekdays only, which is close enough to a trading calendar for a
    window count.
    """
    run_id = start_run(session, PipelineStage.EXTRACT, "prices")
    doc_id = session.execute(
        text(
            "INSERT INTO raw_document (source_id, ingest_run_id, ticker_id, symbol, doc_type, "
            " period_key, content_hash, payload, byte_size) "
            "VALUES ((SELECT id FROM source WHERE code = :src), :run, :t, :sym, "
            " 'price_history', 'zz-synthetic', 'zz-synthetic-prices', '{}'::jsonb, 2) "
            "RETURNING id"
        ),
        {"src": PRICE_SOURCE, "run": run_id, "t": ticker_id, "sym": vw.SYNTHETIC_SYMBOL},
    ).scalar_one()

    sessions: list[date] = []
    day = AS_OF
    while len(sessions) < SESSIONS:
        if day.weekday() < 5:
            sessions.append(day)
        day -= timedelta(days=1)

    calendar_rows = [{"d": d} for d in sessions]
    session.execute(
        text(
            "INSERT INTO market_calendar (exchange_code, trade_date, is_trading_day, source) "
            "VALUES ('HOSE', :d, true, 'synthetic') ON CONFLICT DO NOTHING"
        ),
        calendar_rows,
    )

    bars = []
    for index, trade_date in enumerate(sessions):
        close = Decimal(12_000 + (index % 7) * 100)
        volume = 100_000 + (index % 11) * 10_000
        bars.append(
            {
                "t": ticker_id,
                "d": trade_date,
                "c": close,
                "v": volume,
                "turnover": close * volume,
                "doc": doc_id,
                "run": run_id,
            }
        )
    session.execute(
        text(
            "INSERT INTO price_bar (ticker_id, trade_date, close, volume, turnover_vnd, "
            " source_raw_document_id, extract_run_id) "
            "VALUES (:t, :d, :c, :v, :turnover, :doc, :run)"
        ),
        bars,
    )


@dataclass
class ChainResult:
    """What the data stages produced, so each link can be asserted on separately."""

    ticker_id: int
    stored: StoreResult
    extracted: StatementOutcome
    market_metrics: CalcOutcome
    fundamental_metrics: CalcOutcome
    flag_run_id: int
    flags: FlagOutcome


@dataclass
class SheetResult:
    sheet: FactSheet
    qa: QaReport
    store_outcome: StoreOutcome


def run_chain(
    session: Session, ticker_id: int, root: Path, store: Path, spec: VasSpec
) -> ChainResult:
    """Ingest through flags. Running it twice must change nothing (invariant 7).

    The fact sheet is a separate call rather than the tail of this one, because it is the
    link that is currently broken: see
    `TestTheFactSheetCannotBeBuiltWhileADataAbsenceFlagIsActive`. Folding it in here would
    make every assertion about the earlier stages fail for a reason that has nothing to do
    with them.
    """
    stored = ingest(session, root, store)
    extracted = extract(session, spec)
    market, fundamental = calc_metrics(session)
    calc_benchmarks(session)
    flag_run, flags = calc_flags(session)
    return ChainResult(
        ticker_id=ticker_id,
        stored=stored,
        extracted=extracted,
        market_metrics=market,
        fundamental_metrics=fundamental,
        flag_run_id=flag_run,
        flags=flags,
    )


def build_sheet(session: Session, chain: ChainResult) -> SheetResult:
    """The real fact sheet stage: assemble, validate, store as a draft."""
    sheet = build_fact_sheet(session, vw.SYNTHETIC_SYMBOL, AS_OF, chain.flag_run_id)
    report = validate(session, sheet, vw.SYNTHETIC_SYMBOL)
    outcome = store_fact_sheet(session, sheet, chain.flag_run_id, as_of=AS_OF)
    return SheetResult(sheet=sheet, qa=report, store_outcome=outcome)


@pytest.fixture
def chain(
    session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
) -> ChainResult:
    write_filing_set(filings_root, spec)
    seed_price_scaffolding(session, universe)
    return run_chain(session, universe, filings_root, tmp_path / "store", spec)


# ---------------------------------------------------------------------------------------
# reading what reached the database


def facts_of(
    session: Session, ticker_id: int, *, period_key: str | None = None
) -> dict[str, Decimal]:
    """`{fact_code: value}` for one period, or across all of them if none is named."""
    clause = ""
    params: dict[str, Any] = {"t": ticker_id}
    if period_key is not None:
        year, _, label = period_key.partition("-")
        params["fy"] = int(year)
        if label == "Y":
            clause = "AND fp.period_type = CAST('Y' AS period_type) AND fp.fiscal_year = :fy"
        else:
            clause = (
                "AND fp.period_type = CAST('Q' AS period_type) AND fp.fiscal_year = :fy "
                "AND fp.fiscal_quarter = :fq"
            )
            params["fq"] = int(label[1])
    rows = session.execute(
        text(
            "SELECT ff.fact_code, ff.value FROM financial_fact ff "
            "JOIN financial_period fp ON fp.id = ff.financial_period_id "
            f"WHERE fp.ticker_id = :t {clause}"
        ),
        params,
    ).all()
    return {str(code): Decimal(str(value)) for code, value in rows}


def counts(session: Session, ticker_id: int) -> dict[str, int]:
    """Row counts for every table the chain writes, so a second pass can be compared."""
    queries = {
        "raw_document": "SELECT count(*) FROM raw_document WHERE ticker_id = :t",
        "financial_period": "SELECT count(*) FROM financial_period WHERE ticker_id = :t",
        "financial_fact": (
            "SELECT count(*) FROM financial_fact ff JOIN financial_period fp "
            "ON fp.id = ff.financial_period_id WHERE fp.ticker_id = :t"
        ),
        "metric_value": "SELECT count(*) FROM metric_value WHERE ticker_id = :t",
        "flag_instance": "SELECT count(*) FROM flag_instance WHERE ticker_id = :t",
        "fact_sheet": "SELECT count(*) FROM fact_sheet WHERE ticker_id = :t",
    }
    return {
        name: int(session.execute(text(sql), {"t": ticker_id}).scalar_one())
        for name, sql in queries.items()
    }


def metric_row(session: Session, ticker_id: int, code: str) -> dict[str, Any]:
    row = (
        session.execute(
            text(
                "SELECT value, is_available, unavailable_reason, inputs, calc_version "
                "FROM metric_value WHERE ticker_id = :t AND metric_code = :c "
                "AND as_of_date = CAST(:d AS date)"
            ),
            {"t": ticker_id, "c": code, "d": AS_OF},
        )
        .mappings()
        .first()
    )
    assert row is not None, f"{code} produced no metric_value row at all"
    return dict(row)


def flag_row(session: Session, ticker_id: int, code: str) -> dict[str, Any]:
    row = (
        session.execute(
            text(
                "SELECT state::text AS state, reason, evidence, triggered_values "
                "FROM flag_instance WHERE ticker_id = :t AND flag_code = :c "
                "AND as_of_date = CAST(:d AS date)"
            ),
            {"t": ticker_id, "c": code, "d": AS_OF},
        )
        .mappings()
        .first()
    )
    assert row is not None, f"{code} was never evaluated"
    return dict(row)


def check(report: QaReport, code: str) -> dict[str, Any]:
    found = [c for c in report.checks if c["check"] == code]
    assert found, f"QA ran no check called {code!r}; it has {[c['check'] for c in report.checks]}"
    return found[0]


# ---------------------------------------------------------------------------------------
# the roundtrip


class TestExtractedFacts:
    def test_every_filing_produced_a_period_and_its_facts(
        self, chain: ChainResult, spec: VasSpec
    ) -> None:
        """Nine documents in, nine periods out: eight quarters and one annual."""
        documents = 2 * len(QUARTERS) + 1
        assert chain.stored.inserted == documents
        assert chain.extracted.documents_read == documents
        assert chain.extracted.skipped == 0, chain.extracted.notes
        assert chain.extracted.identity_failures == []
        assert chain.extracted.periods_written == documents
        # One fact per mapped line per document, counted from the mapping rather than
        # written down: adding a line to rules/vas_lines.yaml needs no change here, only a
        # row in the builder's layout - which the builder itself enforces.
        assert chain.extracted.facts_written == documents * len(spec.lines)

    def test_no_stored_fact_lacks_the_field_it_came_from(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Invariant 2, checked against the database rather than the parser's intent.

        `source_field_path` is NOT NULL, so zero is the only acceptable answer; the query
        exists because a future writer could satisfy the constraint with an empty string,
        which traces to nothing.
        """
        unsourced = session.execute(
            text(
                "SELECT count(*) FROM financial_fact ff "
                "JOIN financial_period fp ON fp.id = ff.financial_period_id "
                "WHERE fp.ticker_id = :t "
                "  AND (ff.source_field_path IS NULL OR btrim(ff.source_field_path) = '')"
            ),
            {"t": chain.ticker_id},
        ).scalar_one()
        assert unsourced == 0

    def test_each_field_path_names_a_sheet_a_row_and_the_figure_column(
        self, session: Session, chain: ChainResult
    ) -> None:
        """`CDKT!r14c4` — a reader can open the filing and land on the cell.

        Column 4 is `Số cuối năm`. Column 3 is `Thuyết minh`, and a path ending `c3` would
        mean a note reference had been read as the figure.
        """
        paths = [
            str(p)
            for p in session.execute(
                text(
                    "SELECT ff.source_field_path FROM financial_fact ff "
                    "JOIN financial_period fp ON fp.id = ff.financial_period_id "
                    "WHERE fp.ticker_id = :t"
                ),
                {"t": chain.ticker_id},
            ).scalars()
        ]
        assert paths
        for path in paths:
            sheet, _, cell = path.partition("!")
            assert sheet in {"CDKT", "KQHDKD"}, path
            assert cell.endswith("c4"), f"{path} does not point at the Số cuối năm column"

    def test_the_mapping_is_still_entirely_unverified_and_says_so(self, chain: ChainResult) -> None:
        """Every entry in rules/vas_lines.yaml is `assumed`: read off the circular, never
        confirmed against a real filing. The run has to report how much rests on that, or
        an unverified mapping quietly becomes a trusted one."""
        assert chain.extracted.assumed_lines_used == chain.extracted.facts_written

    def test_an_unmapped_line_is_reported_rather_than_guessed(self, chain: ChainResult) -> None:
        """Mã số 320 is in the workbook and deliberately absent from the mapping: the
        circular numbers short-term borrowings differently across its revisions, and
        getting it the wrong way round would invert net_debt_ebitda."""
        assert any(u.startswith("320 ") for u in chain.extracted.unmapped), chain.extracted.unmapped


class TestTheStoredFiguresSatisfyTheCircularsArithmetic:
    """Re-checking the identities against what was *stored*, not what was parsed.

    `extract_statements` checks them before writing, so this looks redundant. It is not:
    the check is over an in-memory dict keyed by fact code, and everything between it and
    the row — the upsert's conflict clause, the period resolution, `numeric(24,4)` — could
    attach a figure to the wrong period or the wrong code without breaking that dict.
    """

    def test_the_annual_balance_sheet_balances(self, session: Session, chain: ChainResult) -> None:
        facts = facts_of(session, chain.ticker_id, period_key=f"{CURRENT_YEAR}-Y")
        assert facts["total_assets"] == facts["total_liabilities_and_equity"]
        assert (
            facts["total_liabilities"] + facts["total_equity"]
            == facts["total_liabilities_and_equity"]
        )
        assert facts["total_assets"] == Decimal("1234567890000.0000")

    def test_revenue_less_cost_of_goods_sold_is_gross_profit(
        self, session: Session, chain: ChainResult
    ) -> None:
        facts = facts_of(session, chain.ticker_id, period_key=f"{CURRENT_YEAR}-Y")
        assert facts["revenue"] - facts["cost_of_goods_sold"] == facts["gross_profit"]

    def test_a_parenthesised_deduction_is_stored_as_a_magnitude(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Giá vốn hàng bán is printed `(654.321.000.000)` by the builder, as many filings
        do. Stored signed, the gross-profit identity would come out at revenue PLUS cost
        and a perfectly good balance sheet in the same workbook would be thrown away."""
        facts = facts_of(session, chain.ticker_id, period_key=f"{CURRENT_YEAR}-Y")
        assert facts["cost_of_goods_sold"] > 0

    def test_every_quarter_holds_a_quarter_of_the_annual_flow(
        self, session: Session, chain: ChainResult
    ) -> None:
        """The figures are per-period, not cumulative: a quarterly row carrying the
        year-to-date figure would make a four-quarter sum roughly 2.5x too large."""
        annual = facts_of(session, chain.ticker_id, period_key=f"{CURRENT_YEAR}-Y")
        summed = Decimal(0)
        for quarter in QUARTERS:
            facts = facts_of(session, chain.ticker_id, period_key=f"{CURRENT_YEAR}-{quarter}")
            assert facts["revenue"] == annual["revenue"] / CURRENT_QUARTER_DIVISOR
            summed += facts["revenue"]
        assert summed == annual["revenue"]


class TestMetricsFlipFromUnavailableToAvailable:
    def test_the_metrics_whose_facts_are_now_present_are_available(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Four profitability ratios, two growth rates, and interest coverage.

        Everything else stays unavailable, and the reason names the exact line item the
        mapping does not yet produce. That is the point of running a data-gated metric
        rather than skipping it: a metric never attempted cannot say *không công bố*.

        `interest_coverage` is here because EBIT is derived in `calc/periods.py` from
        `profit_before_tax + interest_expense`, both of which `vas_lines.yaml` maps. It
        used to require the registry code `ebit`, which nothing writes and nothing could
        write — so the product's flagship leverage rule was unreachable on any data.
        """
        available = {
            code
            for code in fundamentals.FUNDAMENTAL_CODES
            if code in defined_metric_codes(session)
            and metric_row(session, chain.ticker_id, code)["is_available"]
        }
        assert available == {
            "roe",
            "roa",
            "net_margin",
            "gross_margin",
            "revenue_growth_yoy",
            "net_profit_growth_yoy",
            "interest_coverage",
        }

    def test_an_available_metric_is_the_figure_the_filings_support(
        self, session: Session, chain: ChainResult
    ) -> None:
        """160.000.000.000 of twelve-month profit on 1.000.000.000.000 of equity."""
        assert metric_row(session, chain.ticker_id, "roe")["value"] == Decimal("0.160000")
        # The prior year is exactly twice the current one, by construction.
        assert (
            metric_row(session, chain.ticker_id, "revenue_growth_yoy")["value"] == EXPECTED_GROWTH
        )
        assert (
            metric_row(session, chain.ticker_id, "net_profit_growth_yoy")["value"]
            == EXPECTED_GROWTH
        )

    def test_an_available_metric_names_the_fact_rows_it_consumed(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Invariant 2 at the calc layer: `inputs` has to let a reader recompute the figure
        from rows that exist, not merely restate the answer."""
        inputs = metric_row(session, chain.ticker_id, "roe")["inputs"]
        assert inputs["net_profit_after_tax"] == "160000000000.0000"
        assert inputs["total_equity"] == "1000000000000.0000"

        fact_ids = [*inputs["net_profit_after_tax_fact_ids"], *inputs["total_equity_fact_ids"]]
        assert len(fact_ids) == 5, "four quarters of profit plus one equity balance"
        resolved = session.execute(
            text("SELECT count(*) FROM financial_fact WHERE id = ANY(:ids)"),
            {"ids": fact_ids},
        ).scalar_one()
        assert resolved == len(fact_ids), "inputs names a financial_fact row that does not exist"

    def test_a_flow_is_summed_and_a_balance_is_point_in_time(
        self, session: Session, chain: ChainResult
    ) -> None:
        """The split that cannot be got wrong quietly: summing four balance-sheet dates
        gives a figure about four times too large which still looks plausible."""
        inputs = metric_row(session, chain.ticker_id, "roe")["inputs"]
        assert inputs["net_profit_after_tax_kind"] == "flow"
        assert len(inputs["net_profit_after_tax_fact_ids"]) == 4
        assert inputs["total_equity_kind"] == "stock"
        assert len(inputs["total_equity_fact_ids"]) == 1

    def test_an_unavailable_metric_names_the_line_item_it_is_waiting_for(
        self, session: Session, chain: ChainResult
    ) -> None:
        """`rules/vas_lines.yaml` records short_term_debt, long_term_debt, cfo,
        depreciation_amortisation and shares_outstanding as deliberately unmapped. Each
        dependent metric has to say which one it wants.

        The line named is always one a filing actually prints. `ebit` and `ebitda` are not
        in this list because they are not transcribed at all — they are derived in
        `calc/periods.py` — so a reason naming them would send the reader looking for a
        line no statement contains.
        """
        for code, wanted in (
            ("pe", "shares_outstanding"),
            ("net_debt_ebitda", "short_term_debt"),
            ("ev_ebitda", "depreciation_amortisation"),
            ("cfo_to_net_profit", "cfo"),
        ):
            if code not in defined_metric_codes(session):  # pragma: no cover - defensive
                continue
            row = metric_row(session, chain.ticker_id, code)
            assert not row["is_available"]
            assert wanted in str(row["unavailable_reason"]), (code, row["unavailable_reason"])

    def test_each_group_is_stamped_with_its_own_calc_version(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Invariant 3: a derived row names its producer, and the two metric families are
        produced by different code at different versions."""
        assert metric_row(session, chain.ticker_id, "roe")["calc_version"] == (
            fundamentals.CALC_VERSION
        )
        assert metric_row(session, chain.ticker_id, "close_price")["calc_version"] == (
            liquidity.CALC_VERSION
        )


class TestTheAuditedAnnualIsNeverPreferred:
    """A link that is broken, asserted as broken rather than skipped.

    `calc/periods.py` prefers an audited annual figure over four unaudited quarters, and
    says so in `metric_value.inputs`. It can never happen through this chain:
    `_audited_annual` requires `is_audited IS TRUE`, and `extract/statements.py` writes
    NULL because auditedness is not knowable from a filename. So the preference is
    unreachable from the only path that creates a `financial_period`.
    """

    def test_the_annual_filing_is_recorded_with_audited_unknown(
        self, session: Session, chain: ChainResult
    ) -> None:
        audited = session.execute(
            text(
                "SELECT is_audited FROM financial_period WHERE ticker_id = :t "
                "AND period_type = CAST('Y' AS period_type) AND fiscal_year = :fy"
            ),
            {"t": chain.ticker_id, "fy": CURRENT_YEAR},
        ).scalar_one()
        assert audited is None

    def test_so_the_twelve_month_figure_comes_from_four_quarters(
        self, session: Session, chain: ChainResult
    ) -> None:
        inputs = metric_row(session, chain.ticker_id, "roe")["inputs"]
        assert inputs["net_profit_after_tax_basis"] == "four_quarters"
        assert "net_profit_after_tax_audited_annual_preferred" not in inputs


class TestFlags:
    def test_a_fundamental_flag_fires_on_an_extracted_figure_and_cites_it(
        self, session: Session, chain: ChainResult
    ) -> None:
        """The link the chain exists to prove: a flag that read `unknown` for want of a
        statement now reads `active`, and its evidence points at the `metric_value` row,
        which points at the `financial_fact` rows, which point at cells in the filing.

        `flag_active_needs_evidence` means the row could not have been stored without it.
        """
        row = flag_row(session, chain.ticker_id, "net_profit_growth_negative_yoy")
        assert row["state"] == "active"
        assert row["triggered_values"]["value"] == str(EXPECTED_GROWTH)

        assert len(row["evidence"]) == 1
        ref = row["evidence"][0]["ref"]
        table, _, metric_value_id = ref.partition(":")
        assert table == "metric_value"
        resolved = (
            session.execute(
                text("SELECT metric_code, inputs FROM metric_value WHERE id = :i"),
                {"i": int(metric_value_id)},
            )
            .mappings()
            .one()
        )
        assert resolved["metric_code"] == "net_profit_growth_yoy"
        fact_ids = resolved["inputs"]["net_profit_after_tax_fact_ids"]
        traceable = session.execute(
            text(
                "SELECT count(*) FROM financial_fact "
                "WHERE id = ANY(:ids) AND btrim(source_field_path) <> ''"
            ),
            {"ids": fact_ids},
        ).scalar_one()
        assert traceable == len(fact_ids), "the evidence chain stops before the filing"

    def test_the_disclosure_absence_rule_leaves_unknown_and_carries_evidence(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Before any statement existed every fundamental flag read `unknown` with
        `no_statement_source_configured`. With periods in the database the absence rule can
        be tested, and it reports `active` — with evidence, which the
        `flag_active_needs_evidence` CHECK means the row could not exist without.
        """
        row = flag_row(session, chain.ticker_id, "no_audited_annual_statement_18m")
        assert row["state"] == "active"
        assert row["evidence"], "an active flag with no evidence cannot be stored at all"
        assert row["evidence"][0]["kind"] == "data_absence"
        assert row["evidence"][0]["ref"] == f"ticker:{chain.ticker_id}"
        assert chain.flags.active >= 1

    def test_that_flag_fires_because_audited_is_unknown_not_because_nothing_was_filed(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Recorded because it is uncomfortable, not because it is right.

        An audited annual report *was* ingested for the fiscal year in scope. The rule
        asks for `is_audited = true`, the extractor can only write NULL, and the result is
        a high-severity disclosure flag asserting the opposite of what happened. The flag
        is honest about the database and wrong about the company.
        """
        row = flag_row(session, chain.ticker_id, "no_audited_annual_statement_18m")
        assert "audited=True" in row["evidence"][0]["label"]
        filed = session.execute(
            text(
                "SELECT count(*) FROM financial_period WHERE ticker_id = :t "
                "AND period_type = CAST('Y' AS period_type) "
                "AND period_end > CAST(:d AS date) - make_interval(months => 18)"
            ),
            {"t": chain.ticker_id, "d": AS_OF},
        ).scalar_one()
        assert filed == 1, "an annual period inside the window exists and the flag still fires"

    def test_a_flag_whose_metric_is_unavailable_is_unknown_with_the_reason(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Never `inactive`. `inactive` claims the rule was tested and did not fire.

        `negative_equity` is the one worth naming: equity is extracted, but the rule reads
        it through `book_value_per_share`, which needs a share count the mapping does not
        produce. So a leverage rule stays unassessable on a company whose equity is in the
        database — correct, and visible.

        `interest_coverage_below_2x` is deliberately *not* in this list any more. It was,
        and it should not have been: EBIT is derived from two lines the mapping does
        produce, so the rule is now genuinely assessable. See the test below.
        """
        for code in ("negative_equity", "related_party_receivables_over_20pct_assets"):
            row = flag_row(session, chain.ticker_id, code)
            assert row["state"] == "unknown", code
            assert row["reason"], code

    def test_the_leverage_rule_is_assessed_rather_than_unassessable(
        self, session: Session, chain: ChainResult
    ) -> None:
        """`interest_coverage_below_2x` is the rule the product's pitch rests on, and it
        could not fire on any data: it required the registry code `ebit`, which nothing
        wrote and — given that extract may only transcribe — nothing could write.

        Derived in calc, it is now tested against the filings. The synthetic company
        covers its interest comfortably, so the honest answer is `inactive`: tested, did
        not fire. That is a different claim from `unknown`, and the difference is the one
        this whole harness exists to keep straight.
        """
        row = flag_row(session, chain.ticker_id, "interest_coverage_below_2x")
        assert row["state"] == "inactive"
        assert row["triggered_values"]["metric"] == "interest_coverage"

    def test_a_flag_tested_against_real_data_is_inactive_not_unknown(
        self, session: Session, chain: ChainResult
    ) -> None:
        row = flag_row(session, chain.ticker_id, "price_below_par_value")
        assert row["state"] == "inactive"
        assert row["triggered_values"]["metric"] == "price_to_par"

    def test_a_benchmark_flag_is_unknown_because_one_ticker_is_not_a_peer_group(
        self, session: Session, chain: ChainResult
    ) -> None:
        """`min_benchmark_sample` is 3 and this universe has one name. The benchmark row is
        still written, unavailable, so the rule can say `benchmark_unavailable` rather than
        `benchmark_not_computed` - two different states a reader is entitled to tell
        apart."""
        row = flag_row(session, chain.ticker_id, "turnover_below_quarter_of_sector_median")
        assert row["state"] == "unknown"
        assert "benchmark" in str(row["reason"])


class TestADataAbsenceFlagReachesThePage:
    """The break this harness was built to find, now asserted as the fix.

    `calc/rule_engine._data_absence` attaches evidence with `kind="data_absence"`, and
    `factsheet/schema.Provenance.kind` is a closed `Literal` that did not list it — so
    `build_fact_sheet` raised `ValidationError` the moment the flag went `active`. It goes
    active for every ticker that has a `financial_period` and no `Y` period marked
    `is_audited IS TRUE`, so the first ticker to gain statements *and* a disclosure gap
    would have had its sheet refuse to build: the sheet for the company the gap is about.

    Nothing caught it because the two halves were unit-tested apart. `test_flags.py`
    proves the evaluator produces the evidence; `test_factsheet_review.py` builds sheets
    whose flags are hand-seeded with `kind="metric"`. The join is the only place it lived,
    which is the argument for this whole file.
    """

    def test_the_absence_itself_is_cited_as_the_evidence(
        self, session: Session, chain: ChainResult
    ) -> None:
        """A `data_absence` provenance has a ref and a label but no document and no URL,
        because what it points at is the thing that is not there."""
        active = flag_row(session, chain.ticker_id, "no_audited_annual_statement_18m")
        assert active["state"] == "active"
        assert active["evidence"][0]["kind"] == "data_absence"
        assert active["evidence"][0]["source_url"] is None

    def test_the_sheet_builds_and_carries_that_evidence_through(
        self, session: Session, chain: ChainResult
    ) -> None:
        sheet = build_fact_sheet(session, vw.SYNTHETIC_SYMBOL, AS_OF, chain.flag_run_id)

        gap_flag = next(f for f in sheet.flags if f.code == "no_audited_annual_statement_18m")
        assert gap_flag.state == "active"
        assert [e.kind for e in gap_flag.evidence] == ["data_absence"]


class TestTheFactSheetAndItsQa:
    def test_the_sheet_is_built_for_the_right_company(
        self, session: Session, chain: ChainResult
    ) -> None:
        sheet = build_sheet(session, chain).sheet
        assert sheet.ticker.symbol == vw.SYNTHETIC_SYMBOL
        assert sheet.as_of_date == AS_OF
        assert sheet.lineage.calc_run_id == chain.flag_run_id
        assert sheet.lineage.rule_spec_version != "none"

    def test_the_filing_is_claimed_as_a_source_because_it_contributed_figures(
        self, session: Session, chain: ChainResult
    ) -> None:
        assert "company_filing" in build_sheet(session, chain).sheet.lineage.data_sources

    def test_an_available_fundamental_reaches_the_page_preformatted(
        self, session: Session, chain: ChainResult
    ) -> None:
        """The web layer contains no formatting logic, so the sheet carries the string."""
        sheet = build_sheet(session, chain).sheet
        figures = {
            f.code: f
            for f in [*sheet.headline_metrics, *sheet.other_metrics, *sheet.liquidity.figures]
        }
        assert figures["roe"].available
        assert figures["roe"].display == "16,00%"
        assert figures["roe"].provenance

    def test_an_unavailable_fundamental_becomes_a_stated_disclosure_gap(
        self, session: Session, chain: ChainResult
    ) -> None:
        gaps = {g.field: g for g in build_sheet(session, chain).sheet.disclosure_gaps}
        assert "metric.pe" in gaps
        assert "shares_outstanding" in gaps["metric.pe"].reason

    def test_the_qa_report_passes_check_by_check(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Named individually rather than `assert report.passed`: a check that stopped
        running would make a blanket assertion pass with less coverage than before."""
        report = build_sheet(session, chain).qa
        expected = (
            "symbol_matches",
            "every_figure_has_provenance",
            "provenance_refs_resolve",
            "has_liquidity_block",
            "headline_has_at_least_five_metrics",
            "has_flags_section",
            "has_disclosure_gaps_section",
            "has_lineage",
            "unavailable_figures_display_not_disclosed",
            "unavailable_figures_state_a_reason",
            "active_flags_carry_evidence",
            "unknown_flags_state_a_reason",
            "single_rule_spec_version",
            "price_is_fresh",
            "no_prohibited_language",
            "prose_numbers_are_all_allowed",
            "allowed_numbers_populated",
        )
        for code in expected:
            assert check(report, code)["passed"], check(report, code)
        assert report.passed, report.as_dict()["failed"]

    def test_the_sheet_is_stored_as_a_draft_awaiting_a_human(
        self, session: Session, chain: ChainResult
    ) -> None:
        """Invariant 5. Nothing publishes without a stamp, and the chain stops here."""
        outcome = build_sheet(session, chain).store_outcome
        assert outcome.created
        assert outcome.version == 1
        status = session.execute(
            text("SELECT status::text FROM fact_sheet WHERE id = :i"),
            {"i": outcome.fact_sheet_id},
        ).scalar_one()
        assert status == "draft"


class TestASecondRunChangesNothing:
    """Invariant 7, across the whole chain rather than one stage at a time.

    Each stage is individually idempotent and tested as such. What is not otherwise
    covered is that they are idempotent *together*: an extract that rewrote an unchanged
    fact would move `extract_run_id`, which no metric reads, so nothing downstream would
    notice — until a reviewer asked why a figure changed on a day nothing was filed.
    """

    def test_nothing_is_reinserted_and_no_row_count_moves(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        write_filing_set(filings_root, spec)
        seed_price_scaffolding(session, universe)
        store = tmp_path / "store"

        first = run_chain(session, universe, filings_root, store, spec)
        before = counts(session, universe)

        second = run_chain(session, universe, filings_root, store, spec)
        after = counts(session, universe)

        assert second.stored.inserted == 0
        assert second.stored.unchanged == first.stored.inserted
        assert second.extracted.facts_written == 0
        assert second.extracted.facts_unchanged == first.extracted.facts_written
        assert second.extracted.periods_written == 0
        assert after == before

    def test_the_fact_sheet_reports_unchanged_and_keeps_its_version(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        """A rebuild that changes nothing must not create a new draft, because a new draft
        discards the review stamp the product sells."""
        write_filing_set(filings_root, spec)
        seed_price_scaffolding(session, universe)
        store = tmp_path / "store"

        first = build_sheet(session, run_chain(session, universe, filings_root, store, spec))
        second = build_sheet(session, run_chain(session, universe, filings_root, store, spec))

        assert not second.store_outcome.created
        assert "unchanged" in second.store_outcome.note
        assert second.store_outcome.version == first.store_outcome.version
        assert second.store_outcome.payload_hash == first.store_outcome.payload_hash


# ---------------------------------------------------------------------------------------
# the negative cases, which are the point


class TestAStatementThatFailsItsOwnArithmeticWritesNothing:
    def test_an_unbalanced_balance_sheet_is_refused_in_full(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        """One total disagrees by 1.000.000.000 — a column misread, not rounding. The
        whole period is refused: an absent figure is cheaper than a wrong one, and the
        income statement in the same workbook goes with it rather than being half-trusted.
        """
        vw.workbook(
            filings_root / vw.SYNTHETIC_SYMBOL / f"{CURRENT_YEAR}-Y-bctc.xlsx",
            spec,
            vw.unbalanced(),
        )
        ingest(session, filings_root, tmp_path / "store")

        outcome = extract(session, spec)

        assert outcome.facts_written == 0
        assert outcome.periods_written == 0
        assert outcome.skipped == 1
        assert any("balance_sheet_balances" in f for f in outcome.identity_failures)
        # The failure says by how much, so the gap can be traced to a column.
        assert any("gap=1000000000" in f for f in outcome.identity_failures)

    def test_and_leaves_no_facts_or_periods_behind(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        vw.workbook(
            filings_root / vw.SYNTHETIC_SYMBOL / f"{CURRENT_YEAR}-Y-bctc.xlsx",
            spec,
            vw.unbalanced(),
        )
        ingest(session, filings_root, tmp_path / "store")
        extract(session, spec)

        rows = counts(session, universe)
        assert rows["financial_fact"] == 0
        assert rows["financial_period"] == 0
        # The bytes are kept. raw_document is append-only, so a better parser can be run
        # against the same document without re-fetching it.
        assert rows["raw_document"] == 1


class TestADeclaredNonDongUnitIsRefused:
    """The defect that was found and fixed once, kept fixed.

    A statement printed in `triệu đồng` and read at face value stores every figure a
    million times too small — with clean provenance, and balancing perfectly, because
    every VAS identity is scale-invariant. Nothing downstream can catch it. So the
    declared unit is read, and anything other than đồng is refused rather than rescaled:
    applying a multiplier would mean trusting the detection, and being wrong would produce
    a figure a million out that looks verified.
    """

    @pytest.mark.parametrize(
        ("header", "named"),
        [
            (vw.UNIT_MILLION, "triệu đồng"),
            (vw.UNIT_THOUSAND, "nghìn đồng"),
            (vw.UNIT_BILLION, "tỷ đồng"),
        ],
    )
    def test_a_document_denominated_in_anything_else_writes_no_facts(
        self,
        session: Session,
        universe: int,
        filings_root: Path,
        tmp_path: Path,
        spec: VasSpec,
        header: str,
        named: str,
    ) -> None:
        divisor = {vw.UNIT_MILLION: 10**6, vw.UNIT_THOUSAND: 10**3, vw.UNIT_BILLION: 10**9}[header]
        vw.workbook(
            filings_root / vw.SYNTHETIC_SYMBOL / f"{CURRENT_YEAR}-Y-bctc.xlsx",
            spec,
            vw.scaled(divisor),
            unit_header=header,
        )
        ingest(session, filings_root, tmp_path / "store")

        outcome = extract(session, spec)

        assert outcome.facts_written == 0
        assert outcome.skipped == 1
        assert any(named in note for note in outcome.notes), outcome.notes
        assert counts(session, universe)["financial_fact"] == 0

    def test_a_document_that_declares_dong_is_accepted(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        """The refusal has to be about the unit, not about there being a header at all."""
        vw.workbook(
            filings_root / vw.SYNTHETIC_SYMBOL / f"{CURRENT_YEAR}-Y-bctc.xlsx",
            spec,
            unit_header=vw.UNIT_DONG,
        )
        ingest(session, filings_root, tmp_path / "store")

        outcome = extract(session, spec)

        assert outcome.facts_written == len(spec.lines)
        assert outcome.identity_failures == []


class TestANoteReferenceIsNotAFigure:
    """Circular 200's balance sheet runs `Chỉ tiêu | Mã số | Thuyết minh | Số cuối năm`,
    and many filings number the Thuyết minh column with bare integers. Taking the first
    number after the Mã số read a note reference as the figure — and for the facts no
    identity covers, the document still balanced.
    """

    def test_the_figure_is_read_from_the_column_after_the_note(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        vw.workbook(
            filings_root / vw.SYNTHETIC_SYMBOL / f"{CURRENT_YEAR}-Y-bctc.xlsx",
            spec,
            notes=True,
        )
        ingest(session, filings_root, tmp_path / "store")
        extract(session, spec)

        facts = facts_of(session, universe)
        # 12 is the note reference the builder prints beside Lợi nhuận sau thuế.
        assert vw.note_reference_of("net_profit_after_tax") == "12"
        assert facts["net_profit_after_tax"] == Decimal("160000000000.0000")
        assert facts["cash_and_equivalents"] == Decimal("111111111000.0000")

    def test_a_note_reference_that_is_itself_a_ma_so_does_not_win(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        """The revenue row carries note `20`, which is also the Mã số of Lợi nhuận gộp. Two
        cells the registry recognises, and the one in the Mã số column has to be chosen —
        otherwise the figure would be filed under gross profit."""
        assert vw.note_reference_of("revenue") == "20"
        vw.workbook(
            filings_root / vw.SYNTHETIC_SYMBOL / f"{CURRENT_YEAR}-Y-bctc.xlsx",
            spec,
            notes=True,
        )
        ingest(session, filings_root, tmp_path / "store")
        extract(session, spec)

        facts = facts_of(session, universe)
        assert facts["revenue"] == Decimal("987654321000.0000")
        assert facts["gross_profit"] == Decimal("333333321000.0000")


class TestAMappedLineWithNoFigureIsAbsentNotZero:
    def test_a_dashed_line_produces_no_fact_and_is_reported(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        """VAS forms print a dash for a line with no amount. Zero is a different claim: a
        debt-free company filing 0 and a company that filed nothing are not the same, and
        defaulting to zero would make `charter_capital` read as if none was subscribed."""
        vw.workbook(
            filings_root / vw.SYNTHETIC_SYMBOL / f"{CURRENT_YEAR}-Y-bctc.xlsx",
            spec,
            dashed=("charter_capital",),
        )
        ingest(session, filings_root, tmp_path / "store")

        outcome = extract(session, spec)

        facts = facts_of(session, universe)
        assert "charter_capital" not in facts
        assert facts["total_assets"] == Decimal("1234567890000.0000")
        assert any("charter_capital@" in note for note in outcome.notes), outcome.notes


class TestScaleInvarianceIsNotCaught:
    """The honest limit of a self-validating extractor, asserted as a limit.

    Every identity in `rules/vas_lines.yaml` is a sum of line items compared to another
    sum of line items. Multiply every figure in a document by the same constant and both
    sides scale together, so the identity still holds exactly. A filing printed in
    thousands with no unit declaration — or with a declaration the reader does not
    recognise — is therefore accepted, stored with full provenance, and understated by a
    factor of 1000.

    This is not a defect in the identity checks; it is what scale invariance means. The
    only possible guard is the declared unit, which `detect_unit` reads and which this
    document deliberately omits. Nothing further downstream can help: a metric is a ratio
    of two facts from the same document, so `roe` comes out identical either way.

    Writing it down is the mitigation. The alternatives are a magnitude plausibility
    check against the company's own history (impossible on a first filing) or against
    market capitalisation (which needs a share count the mapping does not yet produce).
    """

    def test_a_document_scaled_by_1000_passes_every_identity(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        vw.workbook(
            filings_root / vw.SYNTHETIC_SYMBOL / f"{CURRENT_YEAR}-Y-bctc.xlsx",
            spec,
            vw.scaled(1000),
            unit_header=None,
        )
        ingest(session, filings_root, tmp_path / "store")

        outcome = extract(session, spec)

        assert outcome.identity_failures == []
        assert outcome.facts_written == len(spec.lines)

    def test_and_the_figures_are_stored_a_thousand_fold_too_small(
        self, session: Session, universe: int, filings_root: Path, tmp_path: Path, spec: VasSpec
    ) -> None:
        vw.workbook(
            filings_root / vw.SYNTHETIC_SYMBOL / f"{CURRENT_YEAR}-Y-bctc.xlsx",
            spec,
            vw.scaled(1000),
            unit_header=None,
        )
        ingest(session, filings_root, tmp_path / "store")
        extract(session, spec)

        facts = facts_of(session, universe)
        assert facts["total_assets"] == Decimal("1234567890.0000")
        assert facts["total_assets"] * 1000 == vw.BALANCED["total_assets"]
        # And it has clean provenance, which is what makes it dangerous rather than merely
        # wrong: every figure names the cell it came from.
        unsourced = session.execute(
            text(
                "SELECT count(*) FROM financial_fact ff "
                "JOIN financial_period fp ON fp.id = ff.financial_period_id "
                "WHERE fp.ticker_id = :t AND btrim(ff.source_field_path) = ''"
            ),
            {"t": universe},
        ).scalar_one()
        assert unsourced == 0


class TestTheBuilderCannotDriftFromTheMapping:
    """The harness's own guard. A workbook built from a stale copy of the mapping's labels
    would pass every test above while exercising nothing, so drift is a hard failure."""

    def test_every_mapped_line_is_placed_on_a_sheet(self, spec: VasSpec) -> None:
        placed = {
            row.fact_code
            for sheet in vw._LAYOUT
            for row in sheet.rows
            if isinstance(row, vw._Mapped)
        }
        assert placed == {line.fact_code for line in spec.lines.values()}

    def test_every_label_satisfies_the_mappings_own_keywords(self, spec: VasSpec) -> None:
        for line in spec.lines.values():
            label = vw.label_of(spec, line.fact_code)
            for keyword in line.label_keywords:
                assert keyword in vw.folded(label), (line.fact_code, keyword)

    def test_figures_are_rendered_the_way_a_filing_prints_them(self) -> None:
        assert vw.vn_figure(Decimal("1234567.89")) == "1.234.567,89"
        assert vw.vn_figure(Decimal(654_321_000_000), parenthesise=True) == "(654.321.000.000)"
        assert vw.vn_figure(Decimal(-1000)) == "(1.000)"

    def test_the_declared_figures_satisfy_every_identity_at_every_scale(self) -> None:
        for divisor in (1, 4, 8, 1000, 10**6):
            figures = vw.scaled(divisor)
            assert (
                figures["total_liabilities"] + figures["total_equity"]
                == figures["total_liabilities_and_equity"]
            )
            assert figures["total_assets"] == figures["total_liabilities_and_equity"]
            assert figures["revenue"] - figures["cost_of_goods_sold"] == figures["gross_profit"]
