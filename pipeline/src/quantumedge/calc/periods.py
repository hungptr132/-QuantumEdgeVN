"""Trailing twelve months, resolved at read time from stored quarterly facts.

`fundamentals.py` used to read its inputs from a period of `period_type = 'TTM'`. Nothing
in this pipeline ever creates one: `extract/statements.py` writes `Q`, `H` and `Y` periods
only, so every TTM-based metric was permanently unavailable — and would have stayed
unavailable after quarterly statements landed, which is the worst kind of bug because the
page would keep saying *không công bố* while the data sat in the database.

The fix is not to materialise a TTM period. `financial_period` and `financial_fact` belong
to the extract stage and calc may not write them (CLAUDE.md, stage table), and a stored
TTM row would also be a derived figure with no source document of its own — it could not
satisfy invariant 2. So TTM is a *view*, computed here on every read, carrying the period
ids and fact ids it summed so `metric_value.inputs` can name them.

Three rules this module exists to enforce:

* **A flow is the sum of exactly four consecutive quarters.** Three quarters scaled up by
  4/3 is a fabricated number, and nothing downstream could tell it from a real one. A gap
  is reported as unavailable, naming the quarter that is missing.
* **A stock is point-in-time: the latest quarter's balance, never a sum.** Adding four
  balance-sheet dates together yields a figure roughly four times too large which still
  looks plausible — total assets of a mid-cap are "big" either way — so no reviewer and no
  constraint would catch it. The flow/stock split is therefore declared, not inferred per
  call site, and `test_periods.py` fails if a new fact code arrives unclassified.
* **An audited annual beats four unaudited quarters.** When the window is exactly Q1–Q4 of
  one fiscal year and an audited `Y` period covers it, that figure is used instead and the
  output says so. The product sells evidence, and audited is better evidence.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc.registry import MissingInput

QUARTERS_PER_YEAR = 4

# How a figure was arrived at. Recorded on every result, because "summed from four
# quarters" and "taken from the audited annual report" are different claims about the
# same number and a reader checking the page is entitled to know which one applies.
BASIS_FOUR_QUARTERS = "four_quarters"
BASIS_LATEST_QUARTER = "latest_quarter"
BASIS_AUDITED_ANNUAL = "audited_annual"
BASIS_DERIVED = "derived_in_calc"

# Line items computed from transcribed ones. `financial_fact_def` marks both `is_derived`
# and nothing produced them, so `interest_coverage`, `ev_ebitda` and `net_debt_ebitda`
# could never become available no matter what was extracted — the flagship leverage rule
# among them.
#
# They are derived here rather than stored by extract, and the invariants decide which:
# extract may read `raw_document` only, and `financial_fact.source_field_path` is NOT
# NULL. A derived figure has no field in any document to point at, so storing one would
# mean writing a provenance that is not true — a worse outcome than not having the number.
# Calc is where a figure computed from other figures belongs, and the components it used
# are recorded in `metric_value.inputs` exactly as a transcribed fact's ids are.
#
# Each entry is (component fact code, sign). Every component is itself resolved over the
# same window, so an audited annual still wins where one exists.
DERIVED_FACT_CODES: dict[str, tuple[tuple[str, int], ...]] = {
    # VAS Mã số 50 (`Tổng lợi nhuận kế toán trước thuế`) is before tax but *after*
    # interest, so interest expense goes back on to reach EBIT. Getting the sign backwards
    # would roughly double interest coverage for a leveraged company, which is the
    # direction that flatters.
    "ebit": (("profit_before_tax", 1), ("interest_expense", 1)),
    # Depreciation sits in the cash-flow statement and is not mapped in vas_lines.yaml
    # yet, so this stays unavailable — naming the line it waits for, which is the point.
    "ebitda": (("ebit", 1), ("depreciation_amortisation", 1)),
}


class FactKind(StrEnum):
    """Whether a line item accumulates over a period or is measured at an instant."""

    FLOW = "flow"
    STOCK = "stock"


# The declared split. Membership here is the authority: a line item is summed over the
# window if and only if it is listed as a flow. Nothing guesses from the code's name.
FLOW_FACT_CODES: frozenset[str] = frozenset(
    {
        "revenue",
        "cost_of_goods_sold",
        "gross_profit",
        "operating_profit",
        "ebit",
        "ebitda",
        "interest_expense",
        "profit_before_tax",
        "net_profit_after_tax",
        "depreciation_amortisation",
        "cfo",
        "capex",
    }
)

STOCK_FACT_CODES: frozenset[str] = frozenset(
    {
        "total_assets",
        "total_liabilities",
        "total_equity",
        "total_liabilities_and_equity",
        "charter_capital",
        "short_term_debt",
        "long_term_debt",
        "cash_and_equivalents",
        "related_party_receivables",
        "shares_outstanding",
    }
)

# `financial_fact_def.statement` already decides the kind for most codes, and the test
# cross-checks the declared sets against it. `derived` is deliberately absent: a derived
# figure can be either kind (ebitda is a flow, a derived net-debt line would be a stock),
# so for those the declared set is the only authority.
KIND_BY_STATEMENT: dict[str, FactKind] = {
    "income": FactKind.FLOW,
    "cashflow": FactKind.FLOW,
    "balance": FactKind.STOCK,
    "meta": FactKind.STOCK,
}

# The calendar month and day each fiscal quarter opens on, for the window's start date.
_QUARTER_START: dict[int, tuple[int, int]] = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}


class UnclassifiedFactCode(LookupError):
    """A fact code that is neither a declared flow nor a declared stock.

    Raised rather than defaulted. Defaulting to flow would silently quadruple a new
    balance-sheet line; defaulting to stock would silently report one quarter of a new
    income line as a year. Both are wrong numbers, and a wrong number is more expensive
    than an absent one.
    """


def classify(fact_code: str) -> FactKind:
    """Flow or stock, from the declared sets only."""
    if fact_code in FLOW_FACT_CODES:
        return FactKind.FLOW
    if fact_code in STOCK_FACT_CODES:
        return FactKind.STOCK
    raise UnclassifiedFactCode(
        f"fact_code '{fact_code}' is in neither FLOW_FACT_CODES nor STOCK_FACT_CODES; "
        "add it to one in calc/periods.py before a metric reads it"
    )


def unclassified(fact_codes: Iterable[str]) -> list[str]:
    """Which of these codes have no declared kind. Used by the test over the registry."""
    return sorted(c for c in fact_codes if c not in FLOW_FACT_CODES and c not in STOCK_FACT_CODES)


def quarter_label(index: int) -> str:
    """Human label for a quarter index, so a gap can be named in a reason string."""
    return f"{index // QUARTERS_PER_YEAR}Q{index % QUARTERS_PER_YEAR + 1}"


@dataclass(frozen=True)
class Quarter:
    period_id: int
    fiscal_year: int
    fiscal_quarter: int
    period_end: date

    @property
    def index(self) -> int:
        """A single ordinal, so "the previous quarter" is arithmetic rather than a case."""
        return self.fiscal_year * QUARTERS_PER_YEAR + self.fiscal_quarter - 1

    @property
    def label(self) -> str:
        return f"{self.fiscal_year}Q{self.fiscal_quarter}"


@dataclass(frozen=True)
class AuditedAnnual:
    period_id: int
    fiscal_year: int
    period_end: date


@dataclass(frozen=True)
class TtmWindow:
    """Four consecutive quarters, and the audited annual covering them if there is one."""

    quarters: tuple[Quarter, ...]  # exactly four, oldest first
    audited_annual: AuditedAnnual | None

    @property
    def latest(self) -> Quarter:
        return self.quarters[-1]

    @property
    def window_start(self) -> date:
        month, day = _QUARTER_START[self.quarters[0].fiscal_quarter]
        return date(self.quarters[0].fiscal_year, month, day)

    @property
    def window_end(self) -> date:
        return self.latest.period_end

    @property
    def label(self) -> str:
        return f"{self.quarters[0].label}-{self.latest.label}"

    @property
    def period_ids(self) -> tuple[int, ...]:
        return tuple(q.period_id for q in self.quarters)

    def as_inputs(self, *, prefix: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {
            f"{prefix}ttm_window": self.label,
            f"{prefix}ttm_window_start": str(self.window_start),
            f"{prefix}ttm_window_end": str(self.window_end),
            f"{prefix}ttm_quarter_period_ids": list(self.period_ids),
        }
        if self.audited_annual is not None:
            out[f"{prefix}audited_annual_period_id"] = self.audited_annual.period_id
        return out


@dataclass(frozen=True)
class TtmFact:
    """One line item resolved over a TTM window, with everything needed to re-derive it."""

    fact_code: str
    kind: FactKind
    value: Decimal
    basis: str
    period_ids: tuple[int, ...]
    fact_ids: tuple[int, ...]
    window_label: str
    # (quarter label, value) per summed quarter. Empty for a stock or an annual figure,
    # which have exactly one component already named by period_ids.
    components: tuple[tuple[str, str], ...] = ()

    @property
    def audited_annual_preferred(self) -> bool:
        return self.basis == BASIS_AUDITED_ANNUAL

    def as_inputs(self, *, prefix: str = "") -> dict[str, Any]:
        """The fragment a metric merges into `metric_value.inputs` (invariant 2).

        `prefix` exists for the growth metrics, which read the same line item over two
        windows: without it the prior year's ids would overwrite the current year's and
        the row would no longer show what was actually compared.
        """
        code = f"{prefix}{self.fact_code}"
        out: dict[str, Any] = {
            code: str(self.value),
            f"{code}_basis": self.basis,
            f"{code}_kind": str(self.kind),
            f"{code}_fact_ids": list(self.fact_ids),
            f"{code}_period_ids": list(self.period_ids),
            f"{code}_ttm_window": self.window_label,
        }
        if self.components:
            out[f"{code}_quarters"] = dict(self.components)
        if self.audited_annual_preferred:
            # Said explicitly rather than inferred from `basis` by whoever reads the row:
            # preferring the audited figure is a decision, and decisions get recorded.
            out[f"{code}_audited_annual_preferred"] = True
        return out


def load_quarters(session: Session, ticker_id: int, as_of: date) -> tuple[Quarter, ...]:
    """Every quarterly period ending on or before `as_of`, newest first.

    `fiscal_quarter IS NOT NULL` is not belt-and-braces: `financial_period` also stores
    halves with `period_type = 'H'`, and a row without a quarter number cannot be placed
    on the quarter ordinal at all.
    """
    rows = (
        session.execute(
            text(
                """
                SELECT id, fiscal_year, fiscal_quarter, period_end
                FROM financial_period
                WHERE ticker_id = :t
                  AND period_type = CAST('Q' AS period_type)
                  AND fiscal_quarter IS NOT NULL
                  AND period_end <= CAST(:as_of AS date)
                ORDER BY fiscal_year DESC, fiscal_quarter DESC
                """
            ),
            {"t": ticker_id, "as_of": as_of},
        )
        .mappings()
        .all()
    )
    return tuple(
        Quarter(
            period_id=int(r["id"]),
            fiscal_year=int(r["fiscal_year"]),
            fiscal_quarter=int(r["fiscal_quarter"]),
            period_end=r["period_end"],
        )
        for r in rows
    )


def _audited_annual(session: Session, ticker_id: int, fiscal_year: int) -> AuditedAnnual | None:
    """The audited annual period for one fiscal year, or None.

    `is_audited IS TRUE` on purpose: the column is three-valued and NULL means *not known
    to be audited*, which is not a licence to call the figure audited.
    """
    row = (
        session.execute(
            text(
                """
                SELECT id, fiscal_year, period_end
                FROM financial_period
                WHERE ticker_id = :t
                  AND period_type = CAST('Y' AS period_type)
                  AND fiscal_year = :fy
                  AND is_audited IS TRUE
                LIMIT 1
                """
            ),
            {"t": ticker_id, "fy": fiscal_year},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return AuditedAnnual(
        period_id=int(row["id"]), fiscal_year=int(row["fiscal_year"]), period_end=row["period_end"]
    )


def build_window(
    session: Session,
    ticker_id: int,
    quarters: Sequence[Quarter],
    *,
    shift_quarters: int = 0,
    as_of: date | None = None,
) -> TtmWindow:
    """Four consecutive quarters ending `shift_quarters` back from the newest one.

    `shift_quarters=4` gives the prior-year window, which is what a year-on-year growth
    metric compares against. Raises MissingInput naming the first absent quarter, so the
    page can say which filing it is waiting for instead of showing an unexplained blank.
    """
    if not quarters:
        raise MissingInput(f"no_quarterly_period_on_or_before:{as_of}")

    by_index = {q.index: q for q in quarters}
    anchor = max(by_index) - shift_quarters
    window: list[Quarter] = []
    for index in range(anchor - QUARTERS_PER_YEAR + 1, anchor + 1):
        found = by_index.get(index)
        if found is None:
            # Never sum what is present and scale it up: three quarters grossed to twelve
            # months is a number nobody filed.
            raise MissingInput(f"missing_quarter:{quarter_label(index)}")
        window.append(found)

    annual = None
    first, last = window[0], window[-1]
    covers_one_fiscal_year = (
        first.fiscal_year == last.fiscal_year
        and first.fiscal_quarter == 1
        and last.fiscal_quarter == 4
    )
    if covers_one_fiscal_year:
        annual = _audited_annual(session, ticker_id, last.fiscal_year)

    return TtmWindow(quarters=tuple(window), audited_annual=annual)


def resolve_ttm_window(
    session: Session, ticker_id: int, as_of: date, *, shift_quarters: int = 0
) -> TtmWindow:
    """Load the quarters and build one window. Convenience over the two primitives."""
    quarters = load_quarters(session, ticker_id, as_of)
    return build_window(session, ticker_id, quarters, shift_quarters=shift_quarters, as_of=as_of)


def _fact_rows(
    session: Session, period_ids: Sequence[int], fact_code: str
) -> dict[int, tuple[int, Decimal]]:
    """{period_id: (fact_id, value)} for one line item across several periods."""
    rows = (
        session.execute(
            text(
                """
                SELECT id, financial_period_id, value
                FROM financial_fact
                WHERE financial_period_id = ANY(:pids)
                  AND fact_code = :code
                  AND value IS NOT NULL
                """
            ),
            {"pids": list(period_ids), "code": fact_code},
        )
        .mappings()
        .all()
    )
    return {int(r["financial_period_id"]): (int(r["id"]), Decimal(str(r["value"]))) for r in rows}


def _annual_ids(window: TtmWindow) -> list[int]:
    return [] if window.audited_annual is None else [window.audited_annual.period_id]


def _derive(session: Session, window: TtmWindow, fact_code: str, kind: FactKind) -> TtmFact:
    """Compute a derived line item from its components over the same window.

    A missing component propagates its own reason outward unchanged, so the caller is told
    which transcribed line is absent — `missing_fact_in_quarter:interest_expense:2025Q3`
    rather than a useless `missing_fact:ebit`.
    """
    total = Decimal(0)
    period_ids: list[int] = []
    fact_ids: list[int] = []
    components: list[tuple[str, str]] = []

    for component, sign in DERIVED_FACT_CODES[fact_code]:
        resolved = ttm_fact(session, window, component)
        total += resolved.value * sign
        period_ids.extend(resolved.period_ids)
        fact_ids.extend(resolved.fact_ids)
        components.append((f"{'+' if sign > 0 else '-'}{component}", str(resolved.value)))

    return TtmFact(
        fact_code=fact_code,
        kind=kind,
        value=total,
        basis=BASIS_DERIVED,
        period_ids=tuple(dict.fromkeys(period_ids)),
        fact_ids=tuple(dict.fromkeys(fact_ids)),
        window_label=window.label,
        components=tuple(components),
    )


def ttm_fact(session: Session, window: TtmWindow, fact_code: str) -> TtmFact:
    """Resolve one line item over `window`.

    A flow is summed across all four quarters; a stock is read at the window's end. If an
    audited annual covers the window and reports the item, that figure wins for either
    kind — for a stock the annual balance-sheet date is the same instant as Q4's, so the
    only difference is that one of the two has been audited.
    """
    kind = classify(fact_code)

    if fact_code in DERIVED_FACT_CODES:
        # Only when nothing transcribed it. A filing that prints EBIT outright is better
        # evidence than our arithmetic, and it carries a real source_field_path.
        transcribed = _fact_rows(session, [*window.period_ids, *_annual_ids(window)], fact_code)
        if not transcribed:
            return _derive(session, window, fact_code, kind)

    if window.audited_annual is not None:
        annual_id = window.audited_annual.period_id
        found = _fact_rows(session, [annual_id], fact_code).get(annual_id)
        if found is not None:
            fact_id, value = found
            return TtmFact(
                fact_code=fact_code,
                kind=kind,
                value=value,
                basis=BASIS_AUDITED_ANNUAL,
                period_ids=(annual_id,),
                fact_ids=(fact_id,),
                window_label=window.label,
            )

    rows = _fact_rows(session, window.period_ids, fact_code)

    if kind is FactKind.STOCK:
        # Point in time. Summing four balance sheets would be about four times too large
        # and would still look like a plausible figure, which is why this branch exists
        # at all rather than one generic "add up the window" path.
        latest = window.latest
        found = rows.get(latest.period_id)
        if found is None:
            raise MissingInput(f"missing_fact_at_period_end:{fact_code}:{latest.label}")
        fact_id, value = found
        return TtmFact(
            fact_code=fact_code,
            kind=kind,
            value=value,
            basis=BASIS_LATEST_QUARTER,
            period_ids=(latest.period_id,),
            fact_ids=(fact_id,),
            window_label=window.label,
        )

    total = Decimal(0)
    fact_ids: list[int] = []
    components: list[tuple[str, str]] = []
    for quarter in window.quarters:
        found = rows.get(quarter.period_id)
        if found is None:
            raise MissingInput(f"missing_fact_in_quarter:{fact_code}:{quarter.label}")
        fact_id, value = found
        total += value
        fact_ids.append(fact_id)
        components.append((quarter.label, str(value)))

    return TtmFact(
        fact_code=fact_code,
        kind=kind,
        value=total,
        basis=BASIS_FOUR_QUARTERS,
        period_ids=window.period_ids,
        fact_ids=tuple(fact_ids),
        window_label=window.label,
        components=tuple(components),
    )


class TtmReader:
    """TTM figures for one ticker at one date, caching within its own lifetime.

    Deliberately not a module-level cache: a stale TTM figure surviving between calc runs
    would be invisible, and the whole point of the stage is that re-running reproduces the
    same answer from the database rather than from memory.
    """

    def __init__(self, session: Session, ticker_id: int, as_of: date) -> None:
        self.session = session
        self.ticker_id = ticker_id
        self.as_of = as_of
        self._quarters: tuple[Quarter, ...] | None = None
        self._windows: dict[int, TtmWindow] = {}
        self._facts: dict[tuple[str, int], TtmFact] = {}

    def quarters(self) -> tuple[Quarter, ...]:
        if self._quarters is None:
            self._quarters = load_quarters(self.session, self.ticker_id, self.as_of)
        return self._quarters

    def window(self, *, shift_quarters: int = 0) -> TtmWindow:
        cached = self._windows.get(shift_quarters)
        if cached is None:
            cached = build_window(
                self.session,
                self.ticker_id,
                self.quarters(),
                shift_quarters=shift_quarters,
                as_of=self.as_of,
            )
            self._windows[shift_quarters] = cached
        return cached

    def fact(self, fact_code: str, *, shift_quarters: int = 0) -> TtmFact:
        key = (fact_code, shift_quarters)
        cached = self._facts.get(key)
        if cached is None:
            cached = ttm_fact(self.session, self.window(shift_quarters=shift_quarters), fact_code)
            self._facts[key] = cached
        return cached


def describe_ttm(session: Session, ticker_id: int, as_of: date) -> dict[str, Any]:
    """Every declared line item resolved over the current TTM window, or its reason.

    Exists for the CLI: when a fundamental reads *không công bố*, this says in one call
    whether the window itself is short a quarter or only that one line item is absent.
    Computes nothing new — it is the same code path the metrics take.
    """
    reader = TtmReader(session, ticker_id, as_of)
    out: dict[str, Any] = {
        "ticker_id": ticker_id,
        "as_of": str(as_of),
        "quarters_available": [q.label for q in reader.quarters()],
    }
    try:
        window = reader.window()
    except MissingInput as exc:
        out["window"] = None
        out["unavailable_reason"] = exc.reason
        return out

    out["window"] = window.as_inputs()
    items: list[dict[str, Any]] = []
    for code in sorted(FLOW_FACT_CODES | STOCK_FACT_CODES):
        row: dict[str, Any] = {"fact_code": code, "kind": str(classify(code))}
        try:
            resolved = reader.fact(code)
        except MissingInput as exc:
            row["value"] = None
            row["unavailable_reason"] = exc.reason
        else:
            row["value"] = str(resolved.value)
            row["basis"] = resolved.basis
            row["fact_ids"] = list(resolved.fact_ids)
        items.append(row)
    out["items"] = items
    return out
