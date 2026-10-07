"""Filing ingest and statement extraction, end to end against a generated workbook.

The fixture is built in the shape of a Circular 200 statement — a Mã số column, Vietnamese
labels, dot-grouped figures, parenthesised negatives, and the irrelevant rows a real filing
carries — rather than a tidy table. A parser that only works on tidy input is not a parser
for filings.

The behaviour that matters most here is the refusal: a statement whose own arithmetic does
not hold writes nothing at all. That is the rule "an absent figure is cheaper than a wrong
one" as executable code.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.config import load_settings
from quantumedge.db.enums import PipelineStage
from quantumedge.extract.mapping.vas import spec_for
from quantumedge.extract.statements import (
    EXTRACTOR_VERSION,
    _period_end,
    extract_statements,
    interpret,
    read_document,
)
from quantumedge.ingest.base import store_raw
from quantumedge.ingest.filings import (
    FilingNameError,
    archive,
    parse_filename,
    scan,
    to_payload,
)
from quantumedge.runs import start_run


@pytest.fixture(scope="module")
def spec():
    return spec_for(load_settings().rules_dir)


# Figures that satisfy every identity in the spec:
#   assets 1,000,000,000 == liabilities 400,000,000 + equity 600,000,000
#   revenue 800,000,000 - cogs 560,000,000 == gross 240,000,000
BALANCED = {
    "total_assets": Decimal(1_000_000_000),
    "total_liabilities": Decimal(400_000_000),
    "total_equity": Decimal(600_000_000),
    "total_liabilities_and_equity": Decimal(1_000_000_000),
    "revenue": Decimal(800_000_000),
    "cost_of_goods_sold": Decimal(560_000_000),
    "gross_profit": Decimal(240_000_000),
}


def _workbook(path: Path, *, unbalance: bool = False, as_text: bool = False) -> Path:
    """A workbook shaped like a filed VAS statement.

    `as_text` writes the figures as Vietnamese-formatted strings instead of numbers, which
    is what a PDF-to-XLSX conversion produces and what the dot-grouping parser exists for.
    """
    total_nguon_von = 1_000_000_000 if not unbalance else 900_000_000

    def amount(value: int) -> object:
        if not as_text:
            return value
        return (
            f"({abs(value):,})".replace(",", ".") if value < 0 else f"{value:,}".replace(",", ".")
        )

    wb = Workbook()
    bs = wb.active
    bs.title = "CDKT"
    rows: list[list[object]] = [
        ["CÔNG TY CỔ PHẦN KIỂM THỬ", None, None],
        ["BẢNG CÂN ĐỐI KẾ TOÁN", None, None],
        ["Tại ngày 31 tháng 12 năm 2024", None, None],
        ["TÀI SẢN", "Mã số", "Số cuối năm"],
        ["A - TÀI SẢN NGẮN HẠN", "100", amount(450_000_000)],
        ["I. Tiền và các khoản tương đương tiền", "110", amount(120_000_000)],
        ["   1. Tiền", "111", amount(20_000_000)],
        ["B - TÀI SẢN DÀI HẠN", "200", amount(550_000_000)],
        ["TỔNG CỘNG TÀI SẢN", "270", amount(1_000_000_000)],
        ["NGUỒN VỐN", "Mã số", "Số cuối năm"],
        ["C - NỢ PHẢI TRẢ", "300", amount(400_000_000)],
        ["D - VỐN CHỦ SỞ HỮU", "400", amount(600_000_000)],
        ["   1. Vốn góp của chủ sở hữu", "411", amount(500_000_000)],
        ["TỔNG CỘNG NGUỒN VỐN", "440", amount(total_nguon_von)],
    ]
    for row in rows:
        bs.append(row)

    income = wb.create_sheet("KQHDKD")
    for row in [
        ["BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH", None, None],
        ["CHỈ TIÊU", "Mã số", "Năm nay"],
        ["1. Doanh thu bán hàng và cung cấp dịch vụ", "01", amount(820_000_000)],
        ["2. Các khoản giảm trừ doanh thu", "02", amount(20_000_000)],
        ["3. Doanh thu thuần về bán hàng và cung cấp dịch vụ", "10", amount(800_000_000)],
        ["4. Giá vốn hàng bán", "11", amount(560_000_000)],
        ["5. Lợi nhuận gộp về bán hàng và cung cấp dịch vụ", "20", amount(240_000_000)],
        ["7. Chi phí tài chính", "22", amount(60_000_000)],
        ["   - trong đó: Chi phí lãi vay", "23", amount(55_000_000)],
        ["10. Lợi nhuận thuần từ hoạt động kinh doanh", "30", amount(90_000_000)],
        ["15. Tổng lợi nhuận kế toán trước thuế", "50", amount(85_000_000)],
        ["18. Lợi nhuận sau thuế thu nhập doanh nghiệp", "60", amount(68_000_000)],
    ]:
        income.append(row)

    wb.save(path)
    return path


@pytest.fixture
def filings_dir(tmp_path: Path) -> Path:
    d = tmp_path / "filings" / "TST"
    d.mkdir(parents=True)
    return d


def _ingest(session: Session, seed_ticker: int, filings_dir: Path, store: Path) -> int:
    """Scan, archive and store every filing. Returns the ingest run id."""
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) "
            "VALUES ('company_filing', 'manual', 'Filings') ON CONFLICT DO NOTHING"
        )
    )
    run_id = start_run(session, PipelineStage.INGEST, "t", source_code="company_filing")
    outcome = scan(filings_dir.parent)
    for found in outcome.found:
        stored = archive(found, store)
        store_raw(session, run_id, [to_payload(found, stored)])
    return run_id


def _by_code(spec, path: Path) -> dict:
    """Interpret every row of a document, keyed by the Mã số it resolved to.

    Interpretation now needs the mapping spec - it prefers a code the registry knows over
    one that merely looks like a code - so reading and interpreting are two steps.
    """
    out = {}
    for row in read_document(path).rows:
        candidate = interpret(spec, row)
        if candidate is not None:
            out.setdefault(candidate.vas_code, candidate)
    return out


class TestFilenameCarriesThePeriod:
    """Refusing to guess. Inferring the period from the file's contents would mean parsing
    before storing, and from the modification date would attach real figures to the wrong
    year — a wrong number with clean provenance."""

    def test_an_annual_filing(self) -> None:
        parsed = parse_filename("2024-Y-audited-financial-statements")
        assert parsed.fiscal_year == 2024
        assert parsed.period_type == "Y"
        assert parsed.fiscal_quarter is None
        assert parsed.period_key == "2024-Y"
        assert parsed.title == "audited financial statements"

    def test_a_quarterly_filing(self) -> None:
        parsed = parse_filename("2024-Q3-bao-cao-tai-chinh")
        assert parsed.period_type == "Q"
        assert parsed.fiscal_quarter == 3
        assert parsed.period_key == "2024-Q3"

    def test_case_and_underscores_are_accepted(self) -> None:
        assert parse_filename("2024_y_statements").period_type == "Y"
        assert parse_filename("2024-q1-x").fiscal_quarter == 1

    def test_an_unreadable_name_is_refused_with_guidance(self) -> None:
        with pytest.raises(FilingNameError, match="cannot read a fiscal period"):
            parse_filename("balance-sheet-final-v2")
        # The message has to tell a person what to rename the file to.
        try:
            parse_filename("nope")
        except FilingNameError as exc:
            assert "2024-Y" in str(exc)

    def test_period_ends_are_the_last_day(self) -> None:
        from datetime import date

        assert _period_end(2024, "Y") == date(2024, 12, 31)
        assert _period_end(2024, "Q1") == date(2024, 3, 31)
        assert _period_end(2024, "H1") == date(2024, 6, 30)


class TestScanAndArchive:
    def test_supported_files_are_found_and_hashed(self, filings_dir: Path) -> None:
        _workbook(filings_dir / "2024-Y-statements.xlsx")
        outcome = scan(filings_dir.parent)

        assert len(outcome.found) == 1
        found = outcome.found[0]
        assert found.symbol == "TST"
        assert len(found.content_hash) == 64
        assert found.byte_size > 0

    def test_unsupported_and_misnamed_files_are_reported_not_ignored(
        self, filings_dir: Path
    ) -> None:
        (filings_dir / "notes.txt").write_text("x", encoding="utf-8")
        _workbook(filings_dir / "no-period-here.xlsx")

        outcome = scan(filings_dir.parent)

        assert outcome.found == []
        assert len(outcome.rejected) == 2
        assert any("unsupported type" in r for r in outcome.rejected)
        assert any("cannot read a fiscal period" in r for r in outcome.rejected)

    def test_the_archive_is_content_addressed(self, filings_dir: Path, tmp_path: Path) -> None:
        """The same document filed twice under different names lands in one place, and
        stored bytes can never be silently replaced — a changed document is a new hash."""
        _workbook(filings_dir / "2024-Y-statements.xlsx")
        _workbook(filings_dir / "2024-Q1-statements.xlsx")
        store = tmp_path / "store"

        paths = {archive(f, store) for f in scan(filings_dir.parent).found}

        # Identical content, so one file in the store despite two filings.
        assert len(paths) == 1
        assert len(list(store.iterdir())) == 1


class TestReadingAWorkbook:
    def test_mapped_lines_are_located_with_their_cell(self, filings_dir: Path, spec) -> None:
        path = _workbook(filings_dir / "2024-Y-statements.xlsx")
        candidates = _by_code(spec, path)

        assert candidates["270"].amount == Decimal(1_000_000_000)
        assert "TỔNG CỘNG TÀI SẢN" in candidates["270"].label
        # The field path has to point at a cell a person can open the file and find.
        assert candidates["270"].field_path.startswith("CDKT!r")

        assert candidates["60"].amount == Decimal(68_000_000)
        assert candidates["60"].field_path.startswith("KQHDKD!r")

    def test_dot_grouped_text_figures_are_read_correctly(self, filings_dir: Path, spec) -> None:
        """What a PDF-to-XLSX conversion produces. "1.000.000.000" must not become 1.0."""
        path = _workbook(filings_dir / "2024-Y-statements.xlsx", as_text=True)
        candidates = _by_code(spec, path)

        assert candidates["270"].amount == Decimal(1_000_000_000)
        assert candidates["11"].amount == Decimal(560_000_000)

    def test_the_code_column_is_not_mistaken_for_the_amount(self, filings_dir: Path, spec) -> None:
        """The Mã số is itself a small number. Taking the first numeric cell in the row
        would report total assets as 270."""
        path = _workbook(filings_dir / "2024-Y-statements.xlsx")
        candidates = _by_code(spec, path)
        assert candidates["270"].amount != Decimal(270)


class TestExtractionWritesFactsWithProvenance:
    def test_a_balanced_filing_produces_facts(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        _workbook(filings_dir / "2024-Y-statements.xlsx")
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")

        outcome = extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert outcome.periods_written == 1
        assert outcome.facts_written >= 7
        assert outcome.identity_failures == []

        rows = dict(
            session.execute(
                text(
                    "SELECT ff.fact_code, ff.value FROM financial_fact ff "
                    "JOIN financial_period fp ON fp.id = ff.financial_period_id "
                    "WHERE fp.ticker_id = :t"
                ),
                {"t": seed_ticker},
            ).all()
        )
        assert rows["total_assets"] == Decimal("1000000000.0000")
        assert rows["net_profit_after_tax"] == Decimal("68000000.0000")
        assert rows["interest_expense"] == Decimal("55000000.0000")

    def test_every_fact_names_the_cell_it_came_from(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        """Invariant 2. A figure a reader cannot trace back is worse than no figure."""
        _workbook(filings_dir / "2024-Y-statements.xlsx")
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")
        extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        rows = session.execute(
            text(
                "SELECT ff.fact_code, ff.source_field_path, ff.extractor_version "
                "FROM financial_fact ff JOIN financial_period fp "
                "  ON fp.id = ff.financial_period_id WHERE fp.ticker_id = :t"
            ),
            {"t": seed_ticker},
        ).all()

        assert rows
        for row in rows:
            assert row.source_field_path, row.fact_code
            assert "!r" in row.source_field_path
            assert row.extractor_version == EXTRACTOR_VERSION

    def test_the_period_is_recorded_with_audited_unknown(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        """Whether a filing is audited is not knowable from a filename, and `false` would
        assert it is unaudited. NULL is the honest value."""
        _workbook(filings_dir / "2024-Y-statements.xlsx")
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")
        extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        row = session.execute(
            text(
                "SELECT period_type::text, fiscal_year, period_end, is_audited "
                "FROM financial_period WHERE ticker_id = :t"
            ),
            {"t": seed_ticker},
        ).one()
        assert row.period_type == "Y"
        assert row.fiscal_year == 2024
        assert str(row.period_end) == "2024-12-31"
        assert row.is_audited is None

    def test_re_extracting_converges(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        _workbook(filings_dir / "2024-Y-statements.xlsx")
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")
        for _ in range(2):
            extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        count = session.execute(
            text(
                "SELECT count(*) FROM financial_fact ff JOIN financial_period fp "
                "ON fp.id = ff.financial_period_id WHERE fp.ticker_id = :t"
            ),
            {"t": seed_ticker},
        ).scalar_one()
        periods = session.execute(
            text("SELECT count(*) FROM financial_period WHERE ticker_id = :t"),
            {"t": seed_ticker},
        ).scalar_one()
        assert periods == 1
        assert count >= 7


class TestAStatementThatFailsItsOwnArithmeticIsRefused:
    """The behaviour this module exists for. VAS prescribes that total assets equal total
    liabilities plus equity, so a parse that takes the wrong column breaks the identity —
    and the whole period is refused rather than written."""

    def test_nothing_is_written_when_an_identity_fails(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        _workbook(filings_dir / "2024-Y-statements.xlsx", unbalance=True)
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")

        outcome = extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert outcome.facts_written == 0
        assert outcome.periods_written == 0
        assert outcome.identity_failures
        assert "balance_sheet_balances" in outcome.identity_failures[0]

        written = session.execute(
            text(
                "SELECT count(*) FROM financial_fact ff JOIN financial_period fp "
                "ON fp.id = ff.financial_period_id WHERE fp.ticker_id = :t"
            ),
            {"t": seed_ticker},
        ).scalar_one()
        assert written == 0, "a failing statement must write no facts at all"

    def test_the_failure_says_by_how_much_it_missed(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        """So the gap can be traced to a column rather than guessed at."""
        _workbook(filings_dir / "2024-Y-statements.xlsx", unbalance=True)
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")

        outcome = extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert "gap=100000000" in outcome.identity_failures[0]


class TestAssumedMappingsAreCounted:
    def test_the_run_reports_how_many_unverified_lines_it_used(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        """Every mapping is currently `assumed` — derived from the circular's structure,
        not yet confirmed against a real filing. The count has to be visible, or an
        unverified mapping silently becomes a trusted one."""
        _workbook(filings_dir / "2024-Y-statements.xlsx")
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")

        outcome = extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert outcome.assumed_lines_used == outcome.facts_written
        assert outcome.as_stats()["assumed_lines_used"] > 0


def _transcription(path: Path, *, unbalance: bool = False, omit_cite: bool = False) -> Path:
    """A statement transcribed by hand, as a person would fill the template in."""
    import yaml

    total = 900_000_000 if unbalance else 1_000_000_000

    def entry(value: int) -> dict[str, object]:
        row: dict[str, object] = {"value": value}
        if not omit_cite:
            row["cite"] = "Bang CDKT, trang 5"
        return row

    yaml.safe_dump(
        {
            "source": {
                "document": "BCTC hop nhat da kiem toan 2024",
                "url": "https://example.invalid/bctc.pdf",
                "transcribed_by": "richard",
            },
            "facts": {
                "total_assets": entry(1_000_000_000),
                "total_liabilities": entry(400_000_000),
                "total_equity": entry(600_000_000),
                "total_liabilities_and_equity": entry(total),
                "revenue": entry(800_000_000),
                "cost_of_goods_sold": entry(560_000_000),
                "gross_profit": entry(240_000_000),
                "net_profit_after_tax": entry(68_000_000),
                # A line the transcriber could not find: left blank, not zeroed.
                "interest_expense": None,
            },
        },
        path.open("w", encoding="utf-8"),
        allow_unicode=True,
    )
    return path


class TestTranscribedStatements:
    """The route for scanned filings. A person typing figures from their own source
    document is the recorded fallback — the no-retyping rule binds models, not people —
    but the transcription is held to exactly the same standard as a parse."""

    def test_a_transcription_produces_facts_citing_their_page(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        _transcription(filings_dir / "2024-Y-transcribed.yaml")
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")

        outcome = extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert outcome.periods_written == 1
        assert outcome.facts_written == 8  # nine keys, one deliberately blank
        assert outcome.identity_failures == []

        rows = dict(
            session.execute(
                text(
                    "SELECT ff.fact_code, ff.source_field_path FROM financial_fact ff "
                    "JOIN financial_period fp ON fp.id = ff.financial_period_id "
                    "WHERE fp.ticker_id = :t"
                ),
                {"t": seed_ticker},
            ).all()
        )
        # The citation is the provenance, and it names the document and the page.
        assert "BCTC hop nhat da kiem toan 2024" in rows["total_assets"]
        assert "trang 5" in rows["total_assets"]
        # A blank line yields no row rather than a zero.
        assert "interest_expense" not in rows

    def test_a_transcription_must_still_balance(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        """The point of routing transcriptions through the same identities: a typo in one
        figure breaks the balance sheet and the whole period is refused. Hand entry gets no
        more trust than a parser does."""
        _transcription(filings_dir / "2024-Y-transcribed.yaml", unbalance=True)
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")

        outcome = extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert outcome.facts_written == 0
        assert outcome.identity_failures
        written = session.execute(
            text(
                "SELECT count(*) FROM financial_fact ff JOIN financial_period fp "
                "ON fp.id = ff.financial_period_id WHERE fp.ticker_id = :t"
            ),
            {"t": seed_ticker},
        ).scalar_one()
        assert written == 0

    def test_a_figure_without_a_citation_is_refused(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        """source_field_path is NOT NULL for a reason. An untraceable figure is worse than
        no figure, so the reader refuses the file rather than inventing a path."""
        _transcription(filings_dir / "2024-Y-transcribed.yaml", omit_cite=True)
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")

        outcome = extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert outcome.facts_written == 0
        assert outcome.skipped == 1
        assert any("cite" in note for note in outcome.notes)

    def test_transcribed_facts_are_not_counted_as_assumed_mappings(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        """`assumed` tracks unverified VAS line codes. A transcription bypasses the code
        mapping entirely — the person named the line — so counting it there would overstate
        how much rests on an unconfirmed circular reading."""
        _transcription(filings_dir / "2024-Y-transcribed.yaml")
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")

        outcome = extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert outcome.facts_written > 0
        assert outcome.assumed_lines_used == 0


class TestAnUnusedDocumentIsNotClaimedAsASource:
    """A stored document that produced nothing must not appear in the sheet's lineage.

    Found in practice: a blank transcription template was ingested, and the fact sheet
    then listed `company_filing` among its data sources despite having no figure from it.
    raw_document is append-only, so the bad row cannot be deleted - the fix has to be that
    lineage reflects contribution, not mere presence.
    """

    def test_a_filing_that_yielded_no_facts_is_absent_from_lineage(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        metric_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        import yaml

        from quantumedge.factsheet.build import _sources

        # A form saved before anything was typed into it.
        empty = filings_dir / "2024-Y-transcribed.yaml"
        yaml.safe_dump(
            {"source": {"document": ""}, "facts": {"total_assets": None}},
            empty.open("w", encoding="utf-8"),
        )
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")
        extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert "company_filing" not in _sources(session, seed_ticker)

    def test_a_filing_that_yielded_facts_does_appear(
        self,
        session: Session,
        seed_ticker: int,
        fact_defs: int,
        filings_dir: Path,
        tmp_path: Path,
        spec,
    ) -> None:
        from quantumedge.factsheet.build import _sources

        _transcription(filings_dir / "2024-Y-transcribed.yaml")
        _ingest(session, seed_ticker, filings_dir, tmp_path / "store")
        extract_statements(session, start_run(session, PipelineStage.EXTRACT, "t"), spec)

        assert "company_filing" in _sources(session, seed_ticker)
