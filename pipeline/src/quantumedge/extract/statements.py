"""Stored filings → `financial_period` + `financial_fact`.

Reads bytes already captured by `ingest/filings.py` and never fetches, so improving this
parser is a re-run rather than a re-download, and the document cannot shift underneath a
fix.

Two things make this safe enough to ship before it is perfect:

**Every figure names the cell it came from.** `source_field_path` is `Sheet1!C42` for a
spreadsheet or `p3.t1.r7.c2` for a PDF table. A reader can open the filing and land on
the number. That is the invariant the whole product rests on, and it is why extraction is
allowed to be imperfect: a wrong figure is traceable and correctable, where an
unattributed one is neither.

**A statement that fails its own arithmetic is refused, not published.** VAS prescribes
identities — total assets equals total liabilities plus equity — so the document validates
itself. A parse that grabs the wrong column or loses a sign breaks them, and the whole
period is recorded as failed rather than writing figures that balance to nothing. An
absent figure is cheaper than a wrong one, and this is where that rule earns its keep.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.extract.base import ExtractResult
from quantumedge.extract.mapping.vas import (
    IdentityCheck,
    VasSpec,
    check_identities,
    match,
    parse_amount,
)
from quantumedge.ingest.filings import DOC_TYPE

log = structlog.get_logger(__name__)

EXTRACTOR_VERSION = "statements/vas-v1"

# A statement row is (Mã số, label, amount). Finding which columns hold which is the only
# heuristic in this module, and it is bounded: a row must yield a code that the mapping
# recognises, or it is skipped. Nothing is inferred from position alone.
MAX_SCAN_COLUMNS = 12


@dataclass
class Candidate:
    """One row of a statement that might be a mapped line item."""

    vas_code: str
    label: str
    amount: Decimal | None
    field_path: str
    # Set only by the transcription reader, where a person has already named the line and
    # there is no Ma so to match on. When present, VAS matching is skipped - but the
    # identity checks are NOT, so a mistyped figure still breaks the balance sheet and is
    # refused like any other bad parse.
    fact_code: str | None = None


@dataclass
class StatementOutcome(ExtractResult):
    facts_written: int = 0
    periods_written: int = 0
    assumed_lines_used: int = 0
    identity_failures: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        stats = super().as_stats()
        stats.update(
            {
                "facts_written": self.facts_written,
                "periods_written": self.periods_written,
                "assumed_lines_used": self.assumed_lines_used,
            }
        )
        if self.identity_failures:
            stats["identity_failures"] = self.identity_failures
        if self.unmapped:
            # Capped: a statement has hundreds of unmapped rows and they are normal.
            stats["unmapped_sample"] = self.unmapped[:15]
        return stats


def _rows_from_xlsx(path: Path) -> list[Candidate]:
    """Every row of every sheet, as (code, label, first numeric value after the label).

    Scanning all sheets rather than guessing which one holds the balance sheet: a filing
    workbook names its sheets inconsistently, and the Mã số plus label guard identifies a
    line far more reliably than a sheet name does.
    """
    from openpyxl import load_workbook  # noqa: PLC0415

    # data_only: read the cached values, not the formulas. A filing's totals are often
    # formulas, and we want the number the company published.
    workbook = load_workbook(path, read_only=True, data_only=True)
    candidates: list[Candidate] = []

    try:
        for sheet in workbook.worksheets:
            for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if not row:
                    continue
                cells = list(row[:MAX_SCAN_COLUMNS])
                label_parts = [str(c).strip() for c in cells if isinstance(c, str) and c.strip()]
                if not label_parts:
                    continue
                label = " ".join(label_parts)

                # The Mã số is a short integer-like cell. Labels are long strings and
                # amounts are large, so a 1-3 digit cell is the code.
                code = ""
                code_column = None
                for col_index, cell in enumerate(cells):
                    as_text = str(cell).strip() if cell is not None else ""
                    if as_text.isdigit() and 1 <= len(as_text) <= 3:
                        code, code_column = as_text, col_index
                        break
                if not code or code_column is None:
                    continue

                # The amount is the first parseable number AFTER the code column. Taking
                # the first number anywhere would pick up the code itself.
                amount = None
                amount_column = None
                for col_index in range(code_column + 1, len(cells)):
                    parsed = parse_amount(cells[col_index])
                    if parsed is not None:
                        amount, amount_column = parsed, col_index
                        break

                candidates.append(
                    Candidate(
                        vas_code=code,
                        label=label,
                        amount=amount,
                        field_path=(
                            f"{sheet.title}!r{row_index}"
                            f"c{(amount_column if amount_column is not None else code_column) + 1}"
                        ),
                    )
                )
    finally:
        workbook.close()

    return candidates


def _rows_from_pdf(path: Path) -> list[Candidate]:
    """Table rows from a PDF, with the page and table in the field path.

    A scanned filing has no text layer and yields nothing here. That is reported as a
    failure rather than worked around: OCR on a financial statement would be guessing at
    digits, which is the one thing this pipeline may not do.
    """
    import pdfplumber  # noqa: PLC0415

    candidates: list[Candidate] = []
    with pdfplumber.open(path) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            for table_index, table in enumerate(page.extract_tables() or [], start=1):
                for row_index, row in enumerate(table or [], start=1):
                    cells = [(c or "").strip() for c in row[:MAX_SCAN_COLUMNS]]
                    label_parts = [c for c in cells if c and not _looks_numeric(c)]
                    if not label_parts:
                        continue
                    label = " ".join(label_parts)

                    code = ""
                    code_column = None
                    for col_index, cell in enumerate(cells):
                        if cell.isdigit() and 1 <= len(cell) <= 3:
                            code, code_column = cell, col_index
                            break
                    if not code or code_column is None:
                        continue

                    amount = None
                    amount_column = None
                    for col_index in range(code_column + 1, len(cells)):
                        parsed = parse_amount(cells[col_index])
                        if parsed is not None:
                            amount, amount_column = parsed, col_index
                            break

                    column = (amount_column if amount_column is not None else code_column) + 1
                    candidates.append(
                        Candidate(
                            vas_code=code,
                            label=label,
                            amount=amount,
                            field_path=f"p{page_index}.t{table_index}.r{row_index}.c{column}",
                        )
                    )
    return candidates


def _looks_numeric(cell: str) -> bool:
    return parse_amount(cell) is not None


def _rows_from_yaml(path: Path) -> list[Candidate]:
    """A statement transcribed by hand, as YAML.

    Exists because the automated route needs a spreadsheet, and a great many Vietnamese
    filings are scanned PDFs with no text layer. Rather than OCR them - guessing at digits
    is the one thing this pipeline may not do - a person reads the filing and types the
    dozen figures that matter, citing where each came from.

    That is not a loosening of the no-retyping rule. The rule is that no *model* may
    retype a figure; a person transcribing their own source document, recording the page
    it came from, is the recorded fallback. And the transcription is held to the same
    standard as a parse: every figure needs a citation, and the whole statement must
    satisfy the VAS identities or it is refused. A typo in total_equity breaks the balance
    sheet and nothing is written.
    """
    import yaml  # noqa: PLC0415

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    source = data.get("source") or {}
    document = str(source.get("document") or path.stem)
    facts = data.get("facts") or {}

    candidates: list[Candidate] = []
    for fact_code, entry in facts.items():
        if entry is None:
            # A key left blank is a line the transcriber could not find. Skipped, not zero.
            continue
        if isinstance(entry, dict):
            raw_value = entry.get("value")
            cite = str(entry.get("cite") or "").strip()
        else:
            raw_value, cite = entry, ""

        amount = parse_amount(raw_value)
        if amount is None:
            continue
        if not cite:
            # source_field_path is NOT NULL for a reason: a figure nobody can trace is
            # worse than no figure. A transcription without a citation is refused.
            raise ValueError(
                f"{path.name}: '{fact_code}' has a value but no `cite`. "
                "Every transcribed figure must say where it came from, e.g. "
                'cite: "Bang CDKT, ma so 270, trang 5".'
            )

        candidates.append(
            Candidate(
                vas_code="",
                label=str(fact_code),
                amount=amount,
                field_path=f"{document} :: {cite}",
                fact_code=str(fact_code),
            )
        )
    return candidates


READERS = {
    ".xlsx": _rows_from_xlsx,
    ".xls": _rows_from_xlsx,
    ".pdf": _rows_from_pdf,
    ".yaml": _rows_from_yaml,
    ".yml": _rows_from_yaml,
}


def read_candidates(path: Path) -> list[Candidate]:
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        raise ValueError(f"no reader for {path.suffix}")
    return reader(path)


def _period_end(fiscal_year: int, period_label: str) -> date:
    """The last day of the period, which `financial_period.period_end` requires."""
    ends = {
        "Y": (12, 31),
        "H1": (6, 30),
        "H2": (12, 31),
        "Q1": (3, 31),
        "Q2": (6, 30),
        "Q3": (9, 30),
        "Q4": (12, 31),
    }
    month, day = ends[period_label]
    return date(fiscal_year, month, day)


def extract_statements(
    session: Session,
    run_id: int,
    spec: VasSpec,
    *,
    symbols: list[str] | None = None,
) -> StatementOutcome:
    """Parse every stored filing into facts, refusing any period that fails its identities."""
    outcome = StatementOutcome()

    clause = "AND rd.symbol = ANY(:symbols)" if symbols else ""
    params: dict[str, Any] = {"doc_type": DOC_TYPE}
    if symbols:
        params["symbols"] = symbols

    documents = (
        session.execute(
            text(
                f"""
                SELECT rd.id, rd.symbol, rd.ticker_id, rd.payload, rd.period_key
                FROM raw_document rd
                WHERE rd.doc_type = :doc_type {clause}
                ORDER BY rd.symbol, rd.period_key, rd.fetched_at DESC
                """
            ),
            params,
        )
        .mappings()
        .all()
    )

    for doc in documents:
        outcome.documents_read += 1
        payload = doc["payload"] or {}
        stored = payload.get("stored_path")

        if doc["ticker_id"] is None:
            outcome.skipped += 1
            outcome.notes.append(f"{doc['symbol']}: not a covered ticker")
            continue
        if not stored or not Path(stored).exists():
            outcome.skipped += 1
            outcome.notes.append(f"{doc['symbol']} {doc['period_key']}: stored file missing")
            continue

        try:
            candidates = read_candidates(Path(stored))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop a run
            outcome.skipped += 1
            outcome.notes.append(
                f"{doc['symbol']} {doc['period_key']}: unreadable ({type(exc).__name__}: {exc})"
            )
            continue

        if not candidates:
            # Most likely a scanned PDF with no text layer. Reported, never OCR'd.
            outcome.skipped += 1
            outcome.notes.append(
                f"{doc['symbol']} {doc['period_key']}: no table rows found "
                "(a scanned PDF has no text layer; OCR is not used)"
            )
            continue

        matched: dict[str, tuple[Decimal, str, bool]] = {}
        for candidate in candidates:
            if candidate.amount is None:
                continue
            if candidate.fact_code is not None:
                # Transcribed: the person named the line, so there is nothing to match.
                # Still subject to the identity checks below.
                matched.setdefault(
                    candidate.fact_code, (candidate.amount, candidate.field_path, False)
                )
                continue
            result = match(spec, candidate.vas_code, candidate.label)
            if not result.ok:
                if result.reason and result.reason.startswith("label_code_mismatch"):
                    # Worth surfacing: it means a mapping entry is wrong, not that the row
                    # is uninteresting.
                    outcome.notes.append(f"{doc['symbol']} {doc['period_key']}: {result.reason}")
                elif result.reason and result.reason.startswith("unmapped"):
                    outcome.unmapped.append(f"{candidate.vas_code} {candidate.label[:40]}")
                continue

            line = result.line
            assert line is not None
            # First occurrence wins: a filing repeats the balance sheet in the notes, and
            # the primary statement comes first.
            matched.setdefault(
                line.fact_code, (candidate.amount, candidate.field_path, line.is_assumed)
            )

        if not matched:
            outcome.skipped += 1
            outcome.notes.append(f"{doc['symbol']} {doc['period_key']}: no mapped lines found")
            continue

        values = {code: amount for code, (amount, _, _) in matched.items()}
        checks = check_identities(spec, values)
        failed = [c for c in checks if not c.passed]
        if failed:
            # The document disagrees with itself, so the parse is wrong. Write nothing for
            # this period: figures that do not balance are worse than no figures.
            outcome.skipped += 1
            for check in failed:
                detail = (
                    f"{doc['symbol']} {doc['period_key']}: {check.name} failed - {check.detail}"
                )
                outcome.identity_failures.append(detail)
                log.warning("statements.identity_failed", detail=detail)
            continue

        period_id = _upsert_period(session, doc, payload)
        if period_id is None:
            outcome.skipped += 1
            outcome.notes.append(f"{doc['symbol']} {doc['period_key']}: could not record period")
            continue
        outcome.periods_written += 1

        for fact_code, (amount, field_path, assumed) in sorted(matched.items()):
            session.execute(
                text(
                    "INSERT INTO financial_fact (financial_period_id, fact_code, value, unit, "
                    " source_raw_document_id, source_field_path, extractor_version, "
                    " extract_run_id) "
                    "VALUES (:p, :c, :v, 'VND', :doc, :path, :ver, :run) "
                    "ON CONFLICT (financial_period_id, fact_code) DO UPDATE SET "
                    " value = EXCLUDED.value, "
                    " source_raw_document_id = EXCLUDED.source_raw_document_id, "
                    " source_field_path = EXCLUDED.source_field_path, "
                    " extractor_version = EXCLUDED.extractor_version, "
                    " extract_run_id = EXCLUDED.extract_run_id"
                ),
                {
                    "p": period_id,
                    "c": fact_code,
                    "v": amount,
                    "doc": doc["id"],
                    "path": field_path,
                    "ver": EXTRACTOR_VERSION,
                    "run": run_id,
                },
            )
            outcome.facts_written += 1
            outcome.rows_written += 1
            if assumed:
                outcome.assumed_lines_used += 1

        log.info(
            "statements.extracted",
            symbol=doc["symbol"],
            period=doc["period_key"],
            facts=len(matched),
            identities_checked=len(checks),
        )

    return outcome


def _upsert_period(session: Session, doc: Any, payload: dict[str, Any]) -> int | None:
    """Create or find the financial_period this document covers."""
    year = payload.get("fiscal_year")
    label = payload.get("period_label")
    period_type = payload.get("period_type")
    if not (year and label and period_type):
        return None

    params = {
        "t": doc["ticker_id"],
        "pt": period_type,
        "fy": int(year),
        "fq": payload.get("fiscal_quarter"),
        "pe": _period_end(int(year), str(label)),
        "src": doc["id"],
        # NULL, not false: whether a filing is audited is not knowable from a filename,
        # and `false` would assert it is unaudited.
        "audited": None,
    }
    found = session.execute(
        text(
            "INSERT INTO financial_period (ticker_id, period_type, fiscal_year, "
            " fiscal_quarter, period_end, is_audited, source_raw_document_id) "
            "VALUES (:t, CAST(:pt AS period_type), :fy, :fq, :pe, :audited, :src) "
            "ON CONFLICT DO NOTHING RETURNING id"
        ),
        params,
    ).scalar_one_or_none()
    if found is not None:
        return int(found)

    existing = session.execute(
        text(
            "SELECT id FROM financial_period WHERE ticker_id = :t "
            "AND period_type = CAST(:pt AS period_type) AND fiscal_year = :fy "
            "AND coalesce(fiscal_quarter, 0) = coalesce(:fq, 0)"
        ),
        params,
    ).scalar_one_or_none()
    return int(existing) if existing is not None else None


def identity_report(spec: VasSpec, facts: dict[str, Decimal]) -> list[IdentityCheck]:
    """Exposed so the CLI can show why a filing was refused."""
    return check_identities(spec, facts)
