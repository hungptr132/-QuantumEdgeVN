"""Build a Circular 200 statement workbook from the extractor's own mapping spec.

Circular 200/2014/TT-BTC prescribes the form, and the form is the thing worth reproducing:

    Chỉ tiêu                              | Mã số | Thuyết minh | Số cuối năm
    TỔNG CỘNG TÀI SẢN                     |  270  |             | 1.234.567.890.000

* **Mã số** is the numeric line code fixed by the circular. It is the extractor's match
  key, so it comes from `rules/vas_lines.yaml` here, never from a copy.
* **Thuyết minh** is a note reference — a bare one- or two-digit number sitting *between*
  the code and the figure. Reading it as the figure is a real defect class, so this
  builder populates it by default rather than as a special case.
* Figures group thousands with dots and use a comma for the decimal (`1.234.567,89`),
  write negatives in parentheses, and print a dash for a line with no amount.
* A unit declaration (`Đơn vị: triệu đồng`) sits above the table. It is the one parse
  error the VAS identities cannot catch, because every identity is scale-invariant.

Each of those is a real document shape, so each is a flag on `workbook()`.

**Why the spec drives the layout.** The labels below are realistic Vietnamese prose, but
every one is pushed through `vas.match()` before it is written, and `_LAYOUT` is checked
for completeness against `spec.lines`. A mapping whose keywords or codes move no longer
matches this builder, and the builder raises `SpecDrift` instead of quietly emitting a
workbook that exercises nothing. A hardcoded copy of the labels would simply drift.

Figures are deliberately unmistakable: `1.234.567.890.000`, `987.654.321.000`. They
satisfy every VAS identity exactly and could not be read as any real company's accounts.
"""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from quantumedge.extract.mapping.vas import VasLine, VasSpec, fold, match

# A placeholder that is not, and cannot become, a listed Vietnamese ticker: HOSE/HNX
# symbols are three characters. Six characters starting ZZ keeps synthetic figures
# structurally incapable of being attached to a real company.
SYNTHETIC_SYMBOL = "ZZTEST"
SYNTHETIC_COMPANY_VI = "CÔNG TY CỔ PHẦN DỮ LIỆU KIỂM THỬ ZZTEST"

# Unit declarations, exactly as filings print them. `UNIT_VND` and `UNIT_DONG` both mean
# đồng, which is what the extractor assumes; the rest mean the document is denominated in
# something else and must be refused.
UNIT_VND = "Đơn vị: VND"
UNIT_DONG = "Đơn vị tính: đồng"
UNIT_THOUSAND = "Đơn vị: nghìn đồng"
UNIT_MILLION = "Đơn vị: triệu đồng"
UNIT_BILLION = "Đơn vị: tỷ đồng"

DASH = "-"

# ---------------------------------------------------------------------------------------
# The declared figures.
#
# Every VAS identity in rules/vas_lines.yaml holds exactly:
#   total_assets                 1.234.567.890.000
#     = total_liabilities          234.567.890.000
#     + total_equity             1.000.000.000.000
#     = total_liabilities_and_equity
#   revenue 987.654.321.000 − cost_of_goods_sold 654.321.000.000
#     = gross_profit             333.333.321.000
#
# Divisible by 10^6 and by 10^3 without remainder, so a "document printed in millions"
# and a "document printed in thousands" variant render as figures a filing would print
# rather than as recurring decimals.

BALANCED: dict[str, Decimal] = {
    # balance sheet (B01-DN)
    "cash_and_equivalents": Decimal(111_111_111_000),
    "total_assets": Decimal(1_234_567_890_000),
    "total_liabilities": Decimal(234_567_890_000),
    "total_equity": Decimal(1_000_000_000_000),
    "charter_capital": Decimal(900_000_000_000),
    "total_liabilities_and_equity": Decimal(1_234_567_890_000),
    # income statement (B02-DN)
    "revenue": Decimal(987_654_321_000),
    "cost_of_goods_sold": Decimal(654_321_000_000),
    "gross_profit": Decimal(333_333_321_000),
    "interest_expense": Decimal(12_345_678_000),
    "operating_profit": Decimal(222_222_222_000),
    "profit_before_tax": Decimal(200_000_000_000),
    "net_profit_after_tax": Decimal(160_000_000_000),
}


def unbalanced(by: Decimal = Decimal(1_000_000_000)) -> dict[str, Decimal]:
    """`BALANCED` with the balance sheet broken by more than the identity's tolerance.

    Only `total_liabilities_and_equity` moves, which is what a column misread looks like:
    one total disagrees and nothing else in the document does.
    """
    broken = dict(BALANCED)
    broken["total_liabilities_and_equity"] = BALANCED["total_liabilities_and_equity"] + by
    return broken


def scaled(by: int, figures: dict[str, Decimal] | None = None) -> dict[str, Decimal]:
    """`figures` divided through — a statement printed in a larger unit.

    Every identity survives this untouched. That is the point of the helper and the whole
    reason the extractor has to read the unit declaration.
    """
    source = BALANCED if figures is None else figures
    return {code: value / Decimal(by) for code, value in source.items()}


def quarterly(
    spec: VasSpec, by: int, figures: dict[str, Decimal] | None = None
) -> dict[str, Decimal]:
    """One period's figures: income-statement flows divided, balance-sheet lines left alone.

    A balance sheet is point-in-time. Dividing total assets by four describes a different,
    smaller company — and the balance identity still holds, because both sides were divided
    together, so nothing in the document would say so. Which half of the statement a line
    belongs to is read from the mapping's own `statement` field rather than decided here.
    """
    source = BALANCED if figures is None else figures
    statements = {line.fact_code: line.statement for line in spec.lines.values()}
    out: dict[str, Decimal] = {}
    for code, value in source.items():
        statement = statements.get(code)
        if statement is None:
            raise SpecDrift(
                f"{code!r} has a declared figure and the mapping does not say which "
                "statement it belongs to, so it cannot be scaled to one period."
            )
        out[code] = value / Decimal(by) if statement == "income" else value
    return out


# ---------------------------------------------------------------------------------------
# Layout
#
# Labels are realistic prose, not the mapping's folded keywords, because a folded label is
# not what a filing prints. They are validated against the mapping's keywords at build
# time, so realism cannot drift into not-testing-anything.

_LABELS: dict[str, str] = {
    "cash_and_equivalents": "I. Tiền và các khoản tương đương tiền",
    "total_assets": "TỔNG CỘNG TÀI SẢN",
    "total_liabilities": "C - NỢ PHẢI TRẢ",
    "total_equity": "D - VỐN CHỦ SỞ HỮU",
    "charter_capital": "1. Vốn góp của chủ sở hữu",
    "total_liabilities_and_equity": "TỔNG CỘNG NGUỒN VỐN",
    "revenue": "3. Doanh thu thuần về bán hàng và cung cấp dịch vụ",
    "cost_of_goods_sold": "4. Giá vốn hàng bán",
    "gross_profit": "5. Lợi nhuận gộp về bán hàng và cung cấp dịch vụ",
    "interest_expense": "    - Trong đó: Chi phí lãi vay",
    "operating_profit": "10. Lợi nhuận thuần từ hoạt động kinh doanh",
    "profit_before_tax": "15. Tổng lợi nhuận kế toán trước thuế",
    "net_profit_after_tax": "18. Lợi nhuận sau thuế thu nhập doanh nghiệp",
}

# Thuyết minh references. Bare one- and two-digit numbers in the column between the Mã số
# and the figure. `20` on the revenue line is deliberate: it is itself a mapped Mã số, so
# the row carries two cells the registry recognises and the extractor must still prefer
# the one in the Mã số column.
_NOTES: dict[str, str] = {
    "cash_and_equivalents": "5",
    "total_liabilities": "14",
    "total_equity": "19",
    "charter_capital": "19",
    "revenue": "20",
    "cost_of_goods_sold": "24",
    "net_profit_after_tax": "12",
}


@dataclass(frozen=True)
class _Mapped:
    """A row the mapping knows. Its Mã số and label come from the spec."""

    fact_code: str


@dataclass(frozen=True)
class _Filler:
    """A row a real filing carries and the mapping deliberately does not map.

    `320` is here on purpose: `rules/vas_lines.yaml` records short_term_debt as unmapped
    pending confirmation against a real filing, so a workbook without the line would hide
    that the extractor correctly declines to guess at it.
    """

    label: str
    vas_code: str | None
    amount: Decimal | None


@dataclass(frozen=True)
class _Sheet:
    title: str
    statement: str
    heading_lines: tuple[str, ...]
    form_code: str
    column_header: tuple[str, ...]
    rows: tuple[_Mapped | _Filler, ...]


_COLUMNS = ("Chỉ tiêu", "Mã số", "Thuyết minh", "Số cuối năm")

_LAYOUT: tuple[_Sheet, ...] = (
    _Sheet(
        title="CDKT",
        statement="balance",
        heading_lines=(SYNTHETIC_COMPANY_VI, "BẢNG CÂN ĐỐI KẾ TOÁN", "Tại ngày 31/12/2024"),
        form_code="Mẫu số B 01 - DN",
        column_header=("TÀI SẢN", *_COLUMNS[1:]),
        rows=(
            _Filler("A - TÀI SẢN NGẮN HẠN", "100", Decimal(400_000_000_000)),
            _Mapped("cash_and_equivalents"),
            _Filler("1. Tiền", "111", Decimal(55_555_555_000)),
            _Filler("B - TÀI SẢN DÀI HẠN", "200", Decimal(834_567_890_000)),
            _Mapped("total_assets"),
            _Filler("NGUỒN VỐN", None, None),
            _Mapped("total_liabilities"),
            _Filler("1. Vay và nợ thuê tài chính ngắn hạn", "320", Decimal(123_456_789_000)),
            _Mapped("total_equity"),
            _Mapped("charter_capital"),
            _Mapped("total_liabilities_and_equity"),
        ),
    ),
    _Sheet(
        title="KQHDKD",
        statement="income",
        heading_lines=(
            SYNTHETIC_COMPANY_VI,
            "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
            "Năm 2024",
        ),
        form_code="Mẫu số B 02 - DN",
        column_header=_COLUMNS,
        rows=(
            _Filler("1. Doanh thu bán hàng và cung cấp dịch vụ", "01", Decimal(10**12)),
            _Filler("2. Các khoản giảm trừ doanh thu", "02", Decimal(12_345_679_000)),
            _Mapped("revenue"),
            _Mapped("cost_of_goods_sold"),
            _Mapped("gross_profit"),
            _Filler("6. Doanh thu hoạt động tài chính", "21", Decimal(5_000_000_000)),
            _Filler("7. Chi phí tài chính", "22", Decimal(20_000_000_000)),
            _Mapped("interest_expense"),
            _Mapped("operating_profit"),
            _Mapped("profit_before_tax"),
            _Mapped("net_profit_after_tax"),
        ),
    ),
)


class SpecDrift(AssertionError):
    """The mapping spec and this builder no longer describe the same document.

    Raised rather than worked around. A harness that silently skips a line the mapping has
    started claiming, or writes a label the mapping no longer accepts, reports success
    while testing less than it did yesterday — which is worse than a red test.
    """


# ---------------------------------------------------------------------------------------
# Figure rendering


def _group(digits: str) -> str:
    return f"{int(digits):,}".replace(",", ".")


def vn_figure(value: Decimal, *, parenthesise: bool = False) -> str:
    """A figure as a Vietnamese statement prints it: `1.234.567,89`, `(654.321.000)`.

    Dots group thousands and a comma marks the decimal — the opposite of the English
    convention, which is why reading one as the other silently shrinks a figure by three
    orders of magnitude per group.
    """
    negative = value < 0 or parenthesise
    whole, _, fraction = format(abs(value), "f").partition(".")
    rendered = _group(whole)
    if fraction and fraction.strip("0"):
        rendered = f"{rendered},{fraction.rstrip('0')}"
    return f"({rendered})" if negative else rendered


def _cell(value: Decimal | None, *, deduction: bool, numeric: bool, parenthesise: bool) -> Any:
    """One figure cell, in whichever of the real forms was asked for."""
    if value is None:
        # VAS forms print a dash for a line with no amount. Not a zero: zero is a claim.
        return DASH
    if numeric:
        # openpyxl hands these back as int/float, which is what a natively produced
        # workbook looks like. A deduction printed in parentheses is stored negative.
        signed = -value if (deduction and parenthesise) else value
        return int(signed) if signed == signed.to_integral_value() else float(signed)
    return vn_figure(value, parenthesise=deduction and parenthesise)


# ---------------------------------------------------------------------------------------
# Build


def _lines_by_fact(spec: VasSpec) -> dict[str, VasLine]:
    by_fact: dict[str, VasLine] = {}
    for line in spec.lines.values():
        if line.fact_code in by_fact:
            raise SpecDrift(
                f"{line.fact_code!r} is mapped from two Mã số "
                f"({by_fact[line.fact_code].vas_code} and {line.vas_code}); this builder "
                "cannot know which one a filing prints."
            )
        by_fact[line.fact_code] = line
    return by_fact


def _label_for(spec: VasSpec, line: VasLine) -> str:
    """A prose label the mapping accepts for this line, or raise.

    Fallback for a line `_LABELS` has not been given prose for: the mapping's own declared
    keywords, joined. Diacritic-free and clumsy, but it is a shape real PDF text layers
    produce, and it keeps a newly mapped line exercised rather than skipped.
    """
    label = _LABELS.get(line.fact_code) or " ".join(line.label_keywords)
    if not label:
        raise SpecDrift(
            f"{line.fact_code!r} (Mã số {line.vas_code}) declares no label_keywords and has "
            "no prose label here, so no row can be written that the mapping would accept."
        )
    result = match(spec, line.vas_code, label)
    if not result.ok:
        raise SpecDrift(
            f"the mapping no longer accepts this builder's label for {line.fact_code!r}: "
            f"{result.reason}. Update _LABELS in tests/support/vas_workbook.py to prose "
            "that satisfies the mapping's keywords, or the workbook tests nothing."
        )
    return label


def _check_layout_covers_spec(spec: VasSpec) -> None:
    placed = {row.fact_code for sheet in _LAYOUT for row in sheet.rows if isinstance(row, _Mapped)}
    declared = {line.fact_code for line in spec.lines.values()}
    missing = sorted(declared - placed)
    if missing:
        raise SpecDrift(
            f"rules/vas_lines.yaml maps {', '.join(missing)} and _LAYOUT places no row for "
            "them, so the roundtrip would silently stop covering those lines. Add them to "
            "the sheet for their statement."
        )
    unknown = sorted(placed - declared)
    if unknown:
        raise SpecDrift(
            f"_LAYOUT places {', '.join(unknown)}, which the mapping no longer declares."
        )
    statements = {sheet.statement for sheet in _LAYOUT}
    orphans = sorted({line.statement for line in spec.lines.values()} - statements)
    if orphans:
        raise SpecDrift(
            f"the mapping declares statement(s) {', '.join(orphans)} and this builder emits "
            f"only {sorted(statements)}. Add a sheet, or those lines go untested."
        )


def workbook(
    path: Path,
    spec: VasSpec,
    figures: dict[str, Decimal] | None = None,
    *,
    unit_header: str | None = UNIT_VND,
    notes: bool = True,
    parenthesise_deductions: bool = True,
    numeric_cells: bool = False,
    dashed: tuple[str, ...] = (),
) -> Path:
    """Write a Circular 200 workbook and return its path.

    * `figures` — fact_code → amount in whatever unit the document declares. Defaults to
      `BALANCED`. A fact code absent from the dict is printed as a dash.
    * `unit_header` — the declaration above the table, or None for a filing that omits it.
      It is independent of `figures` on purpose: `unit_header=UNIT_MILLION` with
      unscaled figures is a *mislabelled* document, and `scaled(1000)` with
      `unit_header=None` is an undeclared one. Both happen.
    * `notes` — populate the Thuyết minh column with note references.
    * `parenthesise_deductions` — print `is_deduction` lines in parentheses, as roughly
      half of real filings do.
    * `numeric_cells` — write numbers instead of Vietnamese text, as a natively produced
      workbook does, rather than the strings a PDF-to-XLSX conversion leaves.
    * `dashed` — fact codes to print as a dash even though a figure was supplied.
    """
    _check_layout_covers_spec(spec)
    values = BALANCED if figures is None else figures
    by_fact = _lines_by_fact(spec)

    book = Workbook()
    # openpyxl seeds a default sheet; the first layout sheet takes it over so the workbook
    # has no empty stray.
    first = book.active
    if first is None:  # pragma: no cover - openpyxl always provides one
        raise RuntimeError("openpyxl produced a workbook with no active sheet")

    for index, sheet in enumerate(_LAYOUT):
        target = first if index == 0 else book.create_sheet()
        target.title = sheet.title

        for heading in sheet.heading_lines:
            target.append([heading])
        target.append([None, None, None, sheet.form_code])
        if unit_header is not None:
            target.append([None, None, None, unit_header])
        target.append(list(sheet.column_header))

        for row in sheet.rows:
            if isinstance(row, _Filler):
                target.append(
                    [
                        row.label,
                        row.vas_code,
                        None,
                        _cell(
                            row.amount,
                            deduction=False,
                            numeric=numeric_cells,
                            parenthesise=False,
                        ),
                    ]
                )
                continue

            line = by_fact.get(row.fact_code)
            if line is None:
                raise SpecDrift(f"the mapping no longer declares {row.fact_code!r}")
            if line.statement != sheet.statement:
                raise SpecDrift(
                    f"the mapping moved {row.fact_code!r} to statement {line.statement!r}; "
                    f"_LAYOUT still places it on the {sheet.statement!r} sheet."
                )
            amount = None if row.fact_code in dashed else values.get(row.fact_code)
            target.append(
                [
                    _label_for(spec, line),
                    line.vas_code,
                    _NOTES.get(row.fact_code) if notes else None,
                    _cell(
                        amount,
                        deduction=line.is_deduction,
                        numeric=numeric_cells,
                        parenthesise=parenthesise_deductions,
                    ),
                ]
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


def declared_unit_is_detected(spec: VasSpec) -> None:
    """No-op kept out of the public surface; see tests for the unit assertions."""
    raise NotImplementedError


def note_reference_of(fact_code: str) -> str | None:
    """The Thuyết minh value this builder prints for a line, so a test can name it."""
    return _NOTES.get(fact_code)


def label_of(spec: VasSpec, fact_code: str) -> str:
    """The prose label this builder prints for a mapped line."""
    line = _lines_by_fact(spec).get(fact_code)
    if line is None:
        raise SpecDrift(f"{fact_code!r} is not mapped by rules/vas_lines.yaml")
    return _label_for(spec, line)


def folded(text: str) -> str:
    """Re-exported so a test can assert on labels the way the mapping compares them."""
    return fold(text)
