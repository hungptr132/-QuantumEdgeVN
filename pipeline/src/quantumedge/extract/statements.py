"""Stored filings → `financial_period` + `financial_fact`.

Reads bytes already captured by `ingest/filings.py` and never fetches, so improving this
parser is a re-run rather than a re-download, and the document cannot shift underneath a
fix.

Three things make it safe enough to ship before it is perfect:

**Every figure names the cell it came from.** `source_field_path` is `CDKT!r14c3` for a
spreadsheet or `p3.t1.r7.c2` for a PDF table. A reader can open the filing and land on the
number. That is why extraction is allowed to be imperfect: a wrong figure is traceable and
correctable, where an unattributed one is neither.

**A statement that fails its own arithmetic is refused in full.** VAS prescribes identities
— total assets equals total liabilities plus equity — so the document validates itself.

**A statement that cannot be checked at all is also refused.** This is the part that was
wrong at first. `check_identities` skips an identity whose inputs are absent, so a filing
missing its totals produced an empty check list, no failures, and every figure written
with nothing verifying it. Zero checks is not a pass.

Two failure classes the identities provably cannot catch, each with its own guard:

* **Denomination.** Every identity is scale-invariant, so a statement printed in
  `triệu đồng` balances perfectly and is stored a million times too small. The declared
  unit is read, and anything other than đồng is refused rather than rescaled.
* **Column confusion.** Circular 200's balance sheet runs
  `Chỉ tiêu | Mã số | Thuyết minh | Số cuối năm`, and many filings number the Thuyết minh
  column with bare integers. Taking the first number after the code read a note reference
  as the figure — and for the facts no identity covers, it balanced fine.
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
    detect_unit,
    match,
    parse_amount,
)
from quantumedge.ingest.filings import DOC_TYPE

log = structlog.get_logger(__name__)

EXTRACTOR_VERSION = "statements/vas-v2"

MAX_SCAN_COLUMNS = 12

# A Mã số is one to three digits. So is a Thuyết minh (note) reference, which is the whole
# problem — see the module docstring.
MAX_CODE_DIGITS = 3

# The smallest figure that can plausibly be a statement line in đồng.
#
# Every mapped fact is a VND amount, and VND is not a currency with small numbers: the
# least significant line on a real balance sheet still runs to thousands. A note reference
# is one or two digits. So a floor cleanly separates them where position alone does not.
#
# Zero is allowed through explicitly: a debt-free company really does file 0, and 0 is
# never a note reference, so it carries no ambiguity.
MIN_PLAUSIBLE_AMOUNT = Decimal(1000)

# How much of a document's text to read when looking for its unit declaration. The line
# sits in the header; scanning the whole filing would find "triệu đồng" in note prose and
# refuse almost everything.
UNIT_SAMPLE_CHARS = 2000


@dataclass
class RawRow:
    """One row of a document, uninterpreted.

    Which cell is the code and which the amount is decided later, once the mapping spec is
    available to say which codes are real.
    """

    cells: list[Any]
    field_path_prefix: str
    row_index: int


@dataclass
class ReadResult:
    rows: list[RawRow]
    # Text from the top of the document, for the unit declaration.
    header_text: str


@dataclass
class Candidate:
    """A row interpreted into something that might be a mapped line item."""

    vas_code: str
    label: str
    amount: Decimal | None
    field_path: str
    # Set only by the transcription path, where a person has already named the line and
    # there is no Mã số to match on. Identity checks still apply.
    fact_code: str | None = None


@dataclass
class StatementOutcome(ExtractResult):
    facts_written: int = 0
    facts_unchanged: int = 0
    periods_written: int = 0
    assumed_lines_used: int = 0
    identity_failures: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        stats = super().as_stats()
        stats.update(
            {
                "facts_written": self.facts_written,
                "facts_unchanged": self.facts_unchanged,
                "periods_written": self.periods_written,
                "assumed_lines_used": self.assumed_lines_used,
            }
        )
        if self.identity_failures:
            stats["identity_failures"] = self.identity_failures
        if self.unmapped:
            stats["unmapped_sample"] = self.unmapped[:15]
        return stats


# --- readers: bytes to rows, no interpretation ------------------------------------------


def _read_xlsx(path: Path) -> ReadResult:
    """Every row of every sheet, plus header text.

    All sheets rather than guessing which holds the balance sheet: filing workbooks name
    their sheets inconsistently, and the Mã số plus label guard identifies a line far more
    reliably than a sheet name does.
    """
    from openpyxl import load_workbook  # noqa: PLC0415

    # data_only reads the cached value rather than the formula, because a filing's totals
    # are usually formulas and the published number is what matters. Note the limitation:
    # if the producing application wrote no cache the cell reads as None, which is why a
    # mapped line with no amount is reported rather than skipped.
    workbook = load_workbook(path, read_only=True, data_only=True)
    rows: list[RawRow] = []
    header: list[str] = []

    try:
        for sheet in workbook.worksheets:
            for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if not row:
                    continue
                cells = list(row[:MAX_SCAN_COLUMNS])
                if len("".join(header)) < UNIT_SAMPLE_CHARS:
                    header.extend(str(c) for c in cells if isinstance(c, str))
                rows.append(
                    RawRow(cells=cells, field_path_prefix=f"{sheet.title}!", row_index=row_index)
                )
    finally:
        workbook.close()

    return ReadResult(rows=rows, header_text=" ".join(header)[:UNIT_SAMPLE_CHARS])


def _read_pdf(path: Path) -> ReadResult:
    """Table rows from a PDF, with the page and table in the field path.

    A scanned filing has no text layer and yields nothing. Reported as unreadable rather
    than worked around: OCR on a financial statement would be guessing at digits, which is
    the one thing this pipeline may not do.
    """
    import pdfplumber  # noqa: PLC0415

    rows: list[RawRow] = []
    header = ""
    with pdfplumber.open(path) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            if len(header) < UNIT_SAMPLE_CHARS:
                header += " " + (page.extract_text() or "")
            for table_index, table in enumerate(page.extract_tables() or [], start=1):
                for row_index, row in enumerate(table or [], start=1):
                    rows.append(
                        RawRow(
                            cells=[(c or "").strip() for c in row[:MAX_SCAN_COLUMNS]],
                            field_path_prefix=f"p{page_index}.t{table_index}.",
                            row_index=row_index,
                        )
                    )
    return ReadResult(rows=rows, header_text=header[:UNIT_SAMPLE_CHARS])


def _read_yaml(path: Path) -> ReadResult:
    """A hand transcription needs no column interpretation, so it takes a separate path.

    Present in the reader table only so the dispatch stays uniform.
    """
    return ReadResult(rows=[], header_text="")


READERS = {
    ".xlsx": _read_xlsx,
    ".xls": _read_xlsx,
    ".pdf": _read_pdf,
    ".yaml": _read_yaml,
    ".yml": _read_yaml,
}


def read_document(path: Path) -> ReadResult:
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        raise ValueError(f"no reader for {path.suffix}")
    return reader(path)


# --- interpretation: rows to candidates, with the spec available ------------------------


def _is_code_shaped(cell: Any) -> bool:
    as_text = str(cell).strip() if cell is not None else ""
    return as_text.isdigit() and 1 <= len(as_text) <= MAX_CODE_DIGITS


def _plausible_amount(cell: Any) -> Decimal | None:
    """Parse a cell as a statement figure, rejecting note-reference-sized integers."""
    amount = parse_amount(cell)
    if amount is None:
        return None
    if amount == 0:
        return amount
    if abs(amount) < MIN_PLAUSIBLE_AMOUNT:
        return None
    return amount


def interpret(spec: VasSpec, row: RawRow) -> Candidate | None:
    """Decide which cell is the Mã số and which is the amount, or give up on the row.

    Both choices used to be positional — the first code-shaped cell anywhere, then the
    first number after it — and both were wrong in ways real filings produce:

    * a workbook numbering its own items (`1 | 3. Doanh thu thuần | 10 | 800.000.000`)
      bound the code to `1` and never saw the real code `10`;
    * the Thuyết minh column sits between the code and the figure, so a bare note number
      was read as the amount.

    Preferring a code the mapping actually knows fixes the first; the magnitude floor
    fixes the second.
    """
    cells = row.cells
    text_cells = [str(c).strip() for c in cells if isinstance(c, str) and str(c).strip()]
    if not text_cells:
        return None
    label = " ".join(c for c in text_cells if _plausible_amount(c) is None)
    if not label:
        return None

    code_positions = [i for i, c in enumerate(cells) if _is_code_shaped(c)]
    if not code_positions:
        return None

    # A code the registry knows, in preference to one that merely looks like a code.
    known = [i for i in code_positions if str(cells[i]).strip() in spec.lines]
    code_index = known[0] if known else code_positions[0]
    code = str(cells[code_index]).strip()

    amount: Decimal | None = None
    amount_index: int | None = None
    for i in range(code_index + 1, len(cells)):
        parsed = _plausible_amount(cells[i])
        if parsed is not None:
            amount, amount_index = parsed, i
            break

    column = (amount_index if amount_index is not None else code_index) + 1
    return Candidate(
        vas_code=code,
        label=label,
        amount=amount,
        field_path=f"{row.field_path_prefix}r{row.row_index}c{column}",
    )


def transcribed_candidates(path: Path, allowed_codes: set[str]) -> list[Candidate]:
    """A hand-transcribed statement, validated against the fact registry.

    `fact_code` comes straight from a hand-edited file, so it is checked here. Left
    unchecked, a typo did two things: it removed the line from the identity inputs, quietly
    disabling the check that would have caught the gap, and then violated the
    `financial_fact.fact_code` foreign key at insert time — outside the per-document error
    handling, so one typo in one ticker's file aborted the run for every other symbol.
    """
    import yaml  # noqa: PLC0415

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    source = data.get("source") or {}
    document = str(source.get("document") or path.stem)
    facts = data.get("facts") or {}

    declared_unit = str(source.get("unit") or "").strip()
    if declared_unit and detect_unit(f"don vi {declared_unit}") is not None:
        raise ValueError(
            f"source.unit is {declared_unit!r}. Figures must be transcribed in đồng - "
            "rescaling them here would be guesswork. Multiply them out, or state đồng."
        )

    candidates: list[Candidate] = []
    for fact_code, entry in facts.items():
        code = str(fact_code)
        if isinstance(entry, dict):
            raw_value = entry.get("value")
            cite = str(entry.get("cite") or "").strip()
            has_value_key = "value" in entry
        else:
            raw_value, cite, has_value_key = entry, "", entry is not None

        blank = (
            entry is None
            or raw_value is None
            or (isinstance(raw_value, str) and not raw_value.strip())
        )
        if blank:
            # Deliberately empty: a line the transcriber could not find. Not zero.
            continue

        if code not in allowed_codes:
            raise ValueError(
                f"{code!r} is not a known fact code. Check it against rules/fact_codes.yaml "
                "- an unknown code would otherwise be written as a figure nothing can "
                "interpret."
            )

        amount = parse_amount(raw_value)
        if amount is None and has_value_key:
            # Reported, not dropped. A figure typed with comma grouping ("1,234,567,890")
            # or a trailing word ("1.000 tỷ") used to vanish with no note at all - and
            # losing a total silently disabled the identity that would have caught it.
            raise ValueError(
                f"could not read {raw_value!r} as a figure for {code}. Use dots for "
                "thousands and a comma for any decimal, with no units: 1.234.567.890"
            )
        if amount is None:
            continue

        if not cite:
            raise ValueError(
                f"{code!r} has a value but no `cite`. Every transcribed figure must say "
                'where it came from, e.g. cite: "Bảng CĐKT, mã số 270, trang 5".'
            )

        candidates.append(
            Candidate(
                vas_code="",
                label=code,
                amount=amount,
                field_path=f"{document} :: {cite}",
                fact_code=code,
            )
        )
    return candidates


def _period_end(fiscal_year: int, period_label: str) -> date:
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


def _allowed_fact_codes(session: Session) -> set[str]:
    return {str(c) for c in session.execute(text("SELECT code FROM financial_fact_def")).scalars()}


def extract_statements(
    session: Session,
    run_id: int,
    spec: VasSpec,
    *,
    symbols: list[str] | None = None,
) -> StatementOutcome:
    """Parse stored filings into facts, refusing any period that cannot be verified."""
    outcome = StatementOutcome()
    allowed_codes = _allowed_fact_codes(session)

    clause = "AND rd.symbol = ANY(:symbols)" if symbols else ""
    params: dict[str, Any] = {"doc_type": DOC_TYPE}
    if symbols:
        params["symbols"] = symbols

    # One document per (symbol, period): the NEWEST. Without DISTINCT ON every stored
    # version was processed and each upserted over the last, so the final writer was the
    # OLDEST - an audited filing ingested after an unaudited one was silently replaced by
    # the superseded figures.
    documents = (
        session.execute(
            text(
                f"""
                SELECT DISTINCT ON (rd.symbol, rd.period_key)
                       rd.id, rd.symbol, rd.ticker_id, rd.payload, rd.period_key
                FROM raw_document rd
                WHERE rd.doc_type = :doc_type {clause}
                ORDER BY rd.symbol, rd.period_key, rd.fetched_at DESC, rd.id DESC
                """
            ),
            params,
        )
        .mappings()
        .all()
    )

    for doc in documents:
        outcome.documents_read += 1
        label = f"{doc['symbol']} {doc['period_key']}"
        payload = doc["payload"] or {}
        stored = payload.get("stored_path")

        if doc["ticker_id"] is None:
            outcome.skipped += 1
            outcome.notes.append(f"{doc['symbol']}: not a covered ticker")
            continue
        if not stored or not Path(stored).exists():
            outcome.skipped += 1
            outcome.notes.append(f"{label}: stored file missing")
            continue

        try:
            matched = _match_document(Path(stored), spec, allowed_codes, outcome, label)
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop a run
            outcome.skipped += 1
            outcome.notes.append(f"{label}: {exc}")
            continue

        if matched is None:
            outcome.skipped += 1
            continue

        values = {code: amount for code, (amount, _, _) in matched.items()}
        checks = check_identities(spec, values)

        # Zero checks is not a pass. An identity whose inputs are absent is skipped, so a
        # filing missing its totals used to sail through with nothing verifying a single
        # figure - while this module's own docstring claimed otherwise.
        if not checks:
            outcome.skipped += 1
            detail = (
                f"{label}: refused - none of the VAS identities could be tested. "
                f"Found {sorted(values)}; at least one identity needs all of its inputs "
                "(typically total_assets and total_liabilities_and_equity)."
            )
            outcome.identity_failures.append(detail)
            log.warning("statements.unverifiable", detail=detail)
            continue

        failed = [c for c in checks if not c.passed]
        if failed:
            outcome.skipped += 1
            for check in failed:
                detail = f"{label}: {check.name} failed - {check.detail}"
                outcome.identity_failures.append(detail)
                log.warning("statements.identity_failed", detail=detail)
            continue

        period_id, period_is_new = _upsert_period(session, doc, payload)
        if period_id is None:
            outcome.skipped += 1
            outcome.notes.append(f"{label}: could not record period")
            continue
        if period_is_new:
            outcome.periods_written += 1

        for fact_code, (amount, field_path, assumed) in sorted(matched.items()):
            wrote = _upsert_fact(
                session,
                period_id=period_id,
                fact_code=fact_code,
                amount=amount,
                doc_id=doc["id"],
                field_path=field_path,
                run_id=run_id,
            )
            if wrote:
                outcome.facts_written += 1
                outcome.rows_written += 1
                if assumed:
                    outcome.assumed_lines_used += 1
            else:
                outcome.facts_unchanged += 1

        log.info(
            "statements.extracted",
            symbol=doc["symbol"],
            period=doc["period_key"],
            facts=len(matched),
            identities_checked=len(checks),
        )

    return outcome


def _match_document(
    path: Path,
    spec: VasSpec,
    allowed_codes: set[str],
    outcome: StatementOutcome,
    label: str,
) -> dict[str, tuple[Decimal, str, bool]] | None:
    """Every mapped figure in one document, or None if there is nothing usable.

    Raises for a document that must be refused outright: unreadable, denominated in
    something other than đồng, or a transcription that cannot be trusted.
    """
    matched: dict[str, tuple[Decimal, str, bool]] = {}

    if path.suffix.lower() in {".yaml", ".yml"}:
        candidates = transcribed_candidates(path, allowed_codes)
        if not candidates:
            outcome.notes.append(f"{label}: no figures filled in")
            return None
        for transcribed in candidates:
            assert transcribed.fact_code is not None
            assert transcribed.amount is not None
            matched.setdefault(
                transcribed.fact_code, (transcribed.amount, transcribed.field_path, False)
            )
        return matched

    result = read_document(path)
    if not result.rows:
        outcome.notes.append(
            f"{label}: no table rows found (a scanned PDF has no text layer; OCR is not used)"
        )
        return None

    # The one error the identities cannot catch, because they are all scale-invariant.
    unit = detect_unit(result.header_text)
    if unit is not None:
        raise ValueError(
            f"declared in {unit}, not đồng. Every identity balances just as well in "
            "millions, so this cannot be caught downstream and is refused here rather than "
            "rescaled on a guess."
        )

    missing_amounts: list[str] = []
    for row in result.rows:
        candidate = interpret(spec, row)
        if candidate is None:
            continue

        result_match = match(spec, candidate.vas_code, candidate.label)
        if not result_match.ok:
            reason = result_match.reason or ""
            if reason.startswith("label_code_mismatch"):
                # A mapping entry is wrong, not an uninteresting row. Worth surfacing.
                outcome.notes.append(f"{label}: {reason}")
            elif reason.startswith("unmapped"):
                outcome.unmapped.append(f"{candidate.vas_code} {candidate.label[:40]}")
            continue

        line = result_match.line
        assert line is not None

        if candidate.amount is None:
            # A mapped line with no readable figure. Usually a formula cell whose producing
            # application wrote no cached value. Reported, because losing a total silently
            # disables the identity that would have caught the rest.
            missing_amounts.append(f"{line.fact_code}@{candidate.field_path}")
            continue

        amount = abs(candidate.amount) if line.is_deduction else candidate.amount
        # First occurrence wins: a filing repeats the balance sheet in its notes, and the
        # primary statement comes first.
        matched.setdefault(line.fact_code, (amount, candidate.field_path, line.is_assumed))

    if missing_amounts:
        outcome.notes.append(
            f"{label}: mapped lines with no readable figure: {', '.join(missing_amounts[:6])}"
        )
    if not matched:
        outcome.notes.append(f"{label}: no mapped lines found")
        return None
    return matched


def _upsert_fact(
    session: Session,
    *,
    period_id: int,
    fact_code: str,
    amount: Decimal,
    doc_id: int,
    field_path: str,
    run_id: int,
) -> bool:
    """Write one fact. Returns True only if something actually changed.

    The `WHERE` on the conflict clause is what makes invariant 7 *reportable* rather than
    merely true: an unchanged row is not rewritten, so `RETURNING` is empty and the caller
    counts it unchanged. Before this, a second run reported the same `facts_written` as the
    first and looked as though it had done the work twice.
    """
    written = session.execute(
        text(
            "INSERT INTO financial_fact (financial_period_id, fact_code, value, unit, "
            " source_raw_document_id, source_field_path, extractor_version, extract_run_id) "
            "VALUES (:p, :c, :v, 'VND', :doc, :path, :ver, :run) "
            "ON CONFLICT (financial_period_id, fact_code) DO UPDATE SET "
            " value = EXCLUDED.value, "
            " source_raw_document_id = EXCLUDED.source_raw_document_id, "
            " source_field_path = EXCLUDED.source_field_path, "
            " extractor_version = EXCLUDED.extractor_version, "
            " extract_run_id = EXCLUDED.extract_run_id "
            "WHERE financial_fact.value IS DISTINCT FROM EXCLUDED.value "
            "   OR financial_fact.source_field_path IS DISTINCT FROM EXCLUDED.source_field_path "
            "   OR financial_fact.source_raw_document_id "
            "        IS DISTINCT FROM EXCLUDED.source_raw_document_id "
            "RETURNING id"
        ),
        {
            "p": period_id,
            "c": fact_code,
            "v": amount,
            "doc": doc_id,
            "path": field_path,
            "ver": EXTRACTOR_VERSION,
            "run": run_id,
        },
    ).scalar_one_or_none()
    return written is not None


def _upsert_period(session: Session, doc: Any, payload: dict[str, Any]) -> tuple[int | None, bool]:
    """Find or create the period a document covers. Returns (id, created)."""
    year = payload.get("fiscal_year")
    label = payload.get("period_label")
    period_type = payload.get("period_type")
    if not (year and label and period_type):
        return None, False

    params = {
        "t": doc["ticker_id"],
        "pt": period_type,
        "fy": int(year),
        # For a half-year this is the half (1 or 2), not a quarter: see
        # ingest/filings.HALVES. Without it, H1 and H2 shared one period row and the
        # second filing's figures silently replaced the first's.
        "fq": payload.get("fiscal_quarter"),
        "pe": _period_end(int(year), str(label)),
        "src": doc["id"],
        # NULL, not false: whether a filing is audited is not knowable from a filename,
        # and false would assert it is unaudited.
        "audited": None,
    }
    created = session.execute(
        text(
            "INSERT INTO financial_period (ticker_id, period_type, fiscal_year, "
            " fiscal_quarter, period_end, is_audited, source_raw_document_id) "
            "VALUES (:t, CAST(:pt AS period_type), :fy, :fq, :pe, :audited, :src) "
            "ON CONFLICT DO NOTHING RETURNING id"
        ),
        params,
    ).scalar_one_or_none()
    if created is not None:
        return int(created), True

    existing = session.execute(
        text(
            "SELECT id FROM financial_period WHERE ticker_id = :t "
            "AND period_type = CAST(:pt AS period_type) AND fiscal_year = :fy "
            "AND coalesce(fiscal_quarter, 0) = coalesce(:fq, 0)"
        ),
        params,
    ).scalar_one_or_none()
    return (int(existing), False) if existing is not None else (None, False)


def identity_report(spec: VasSpec, facts: dict[str, Decimal]) -> list[IdentityCheck]:
    """Exposed so the CLI can show why a filing was refused."""
    return check_identities(spec, facts)
