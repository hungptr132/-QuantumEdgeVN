"""Valuation, leverage, profitability and growth metrics, computed from filed statements.

These are implemented, not stubbed, even though statement coverage is still partial. The
difference matters: a metric with no implementation produces **no row at all**, so the fact
sheet cannot say *không công bố* for it — it simply has nothing to say, which is a hole in
the honesty model. Implemented and data-gated, each one produces a row naming the exact
fact it is waiting for (`missing_fact_in_quarter:ebitda:2025Q3`), and the day a line item
is mapped in `rules/vas_lines.yaml` it starts working with no change here.

Every twelve-month figure comes from `calc/periods.py`, which resolves TTM at read time by
summing four consecutive quarters (flows) or taking the window's closing balance (stocks).
This module previously read `period_type = 'TTM'` rows, which nothing in the pipeline ever
writes — so each TTM metric was unavailable regardless of how many quarters had been
extracted. See that module's docstring for why TTM is a view rather than a stored period.

Guards that appear repeatedly and are deliberate:

* **A non-positive denominator is unavailable, never a negative ratio.** A loss-making
  company has no meaningful P/E, and a company with negative equity has no meaningful P/B
  or ROE. Printing `-4.2x` invites a reader to treat it as cheap. Several covered tickers
  are loss-makers, so this is the normal path, not an edge case.
* **Zero interest expense is unavailable, not infinite coverage.** A company with no debt
  is not "infinitely safe"; the ratio is simply undefined and the page should say so.
* **A growth figure needs a real prior-year comparable.** No eight-quarter history, or a
  non-positive base, means unavailable with the missing quarter named — growth measured
  off a loss is a number with no interpretation.

A negative result is published where it is genuinely informative and cannot be misread as
attractive: EPS, book value per share, a margin and a growth rate are all meaningful below
zero, and withholding them would hide the bad news rather than the ambiguity.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from quantumedge.calc.periods import QUARTERS_PER_YEAR, TtmFact, TtmReader
from quantumedge.calc.registry import MetricContext, MetricResult, MissingInput, metric

CALC_VERSION = "fundamentals/v2"

# Rounding applied before the row is written. `metric_value.value` is numeric(24,6); these
# say what the figure actually claims, so a reader is not shown precision nobody filed.
_TIMES = Decimal("0.01")  # a multiple: "3,50 lần"
_RATE = Decimal("0.0001")  # a rate rendered as a percentage: 15,00%
_VND_PER_SHARE = Decimal("0.01")


def _reader(ctx: MetricContext) -> TtmReader:
    """A fresh TTM reader per metric call.

    Per call, not per process: the cache exists to stop one metric resolving the same
    window four times, and nothing more. A cache outliving the call would let a figure
    from an earlier run reach a later one, and `qe calc` would stop being reproducible
    from the database alone.
    """
    return TtmReader(ctx.session, ctx.ticker_id, ctx.as_of)


def _divide(numerator: Decimal, denominator: TtmFact, places: Decimal) -> Decimal:
    """A ratio, or nothing. The denominator guard lives here so no metric can forget it."""
    if denominator.value <= 0:
        raise MissingInput(f"non_positive_denominator:{denominator.fact_code}")
    return (numerator / denominator.value).quantize(places)


def _latest_close(ctx: MetricContext) -> tuple[Decimal, date]:
    """Share price, for the market-cap based metrics.

    Reuses the same bounded lookback as the liquidity metrics, so a single missing bar does
    not blank a valuation ratio either.
    """
    from quantumedge.calc.liquidity import MAX_PRICE_LOOKBACK, _latest_traded

    found = _latest_traded(ctx)
    if found is None or found[0].close is None:
        raise MissingInput(f"no_price_bar_within_{MAX_PRICE_LOOKBACK}_sessions")
    return found[0].close, found[0].trade_date


def _market_cap(ctx: MetricContext, reader: TtmReader) -> tuple[Decimal, dict[str, Any]]:
    close, trade_date = _latest_close(ctx)
    shares = reader.fact("shares_outstanding")
    if shares.value <= 0:
        raise MissingInput("non_positive_shares_outstanding")
    cap = close * shares.value
    return cap, {
        "close_vnd": str(close),
        "trade_date": str(trade_date),
        "market_cap_vnd": str(cap),
        **shares.as_inputs(),
    }


def _net_debt(reader: TtmReader) -> tuple[Decimal, dict[str, Any]]:
    short_term = reader.fact("short_term_debt")
    long_term = reader.fact("long_term_debt")
    cash = reader.fact("cash_and_equivalents")
    net_debt = short_term.value + long_term.value - cash.value
    return net_debt, {
        **short_term.as_inputs(),
        **long_term.as_inputs(),
        **cash.as_inputs(),
        "net_debt_vnd": str(net_debt),
    }


def _growth(ctx: MetricContext, fact_code: str) -> MetricResult:
    """Year-on-year change in a twelve-month flow.

    TTM against TTM a year earlier, which needs eight consecutive quarters. Comparing a
    TTM figure against a single prior quarter, or against whatever older period happened
    to exist, would change the metric's meaning per ticker without saying so.
    """
    reader = _reader(ctx)
    now = reader.fact(fact_code)
    try:
        prior = reader.fact(fact_code, shift_quarters=QUARTERS_PER_YEAR)
    except MissingInput as exc:
        # Keep the inner reason: it names the quarter that is missing, which is the one
        # piece of information that tells a reader when this will start working.
        raise MissingInput(f"no_prior_year_comparable:{fact_code}:{exc.reason}") from exc

    if prior.value <= 0:
        # Growth measured from a loss has no interpretation: a swing from -10bn to -1bn
        # would read as +90%, and from -10bn to +1bn as -110%.
        raise MissingInput(f"non_positive_denominator:{fact_code}_prior_year")

    change = (now.value / prior.value - Decimal(1)).quantize(_RATE)
    return MetricResult(
        value=change,
        inputs={
            **reader.window().as_inputs(),
            **now.as_inputs(),
            **reader.window(shift_quarters=QUARTERS_PER_YEAR).as_inputs(prefix="prior_year_"),
            **prior.as_inputs(prefix="prior_year_"),
        },
    )


# ───────────────────────── valuation ─────────────────────────


@metric("pe", requires_facts=("net_profit_after_tax", "shares_outstanding"))
def pe(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    cap, cap_inputs = _market_cap(ctx, reader)
    profit = reader.fact("net_profit_after_tax")
    # A loss-making company has no meaningful P/E. Printing a negative one invites a
    # reader to see it as cheap, which is the opposite of what it means.
    value = _divide(cap, profit, _TIMES)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **cap_inputs, **profit.as_inputs()},
    )


@metric(
    "ev_ebitda",
    requires_facts=(
        "profit_before_tax",
        "interest_expense",
        "depreciation_amortisation",
        "short_term_debt",
        "long_term_debt",
        "cash_and_equivalents",
    ),
)
def ev_ebitda(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    cap, cap_inputs = _market_cap(ctx, reader)
    net_debt, debt_inputs = _net_debt(reader)
    ebitda = reader.fact("ebitda")
    enterprise_value = cap + net_debt
    value = _divide(enterprise_value, ebitda, _TIMES)
    return MetricResult(
        value=value,
        inputs={
            **reader.window().as_inputs(),
            **cap_inputs,
            **debt_inputs,
            "enterprise_value_vnd": str(enterprise_value),
            **ebitda.as_inputs(),
        },
    )


@metric("pb", requires_facts=("total_equity", "shares_outstanding"))
def pb(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    cap, cap_inputs = _market_cap(ctx, reader)
    equity = reader.fact("total_equity")
    # Negative equity does not make a stock infinitely cheap; the ratio has no value.
    value = _divide(cap, equity, _TIMES)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **cap_inputs, **equity.as_inputs()},
    )


@metric("eps", requires_facts=("net_profit_after_tax", "shares_outstanding"))
def eps(ctx: MetricContext) -> MetricResult:
    """Twelve-month profit per share. Published negative, unlike P/E.

    A loss per share is the plain fact and reads as bad news; a negative P/E reads as a
    low P/E. Suppressing EPS for a loss-maker would hide the loss, not the ambiguity.
    """
    reader = _reader(ctx)
    profit = reader.fact("net_profit_after_tax")
    shares = reader.fact("shares_outstanding")
    value = _divide(profit.value, shares, _VND_PER_SHARE)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **profit.as_inputs(), **shares.as_inputs()},
    )


@metric("book_value_per_share", requires_facts=("total_equity", "shares_outstanding"))
def book_value_per_share(ctx: MetricContext) -> MetricResult:
    """Equity per share at the window's close. Negative when liabilities exceed assets,
    which is exactly when a reader most needs to see it."""
    reader = _reader(ctx)
    equity = reader.fact("total_equity")
    shares = reader.fact("shares_outstanding")
    value = _divide(equity.value, shares, _VND_PER_SHARE)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **equity.as_inputs(), **shares.as_inputs()},
    )


# ───────────────────────── leverage ─────────────────────────


# `ebit` is derived in `periods.py`, so the declared requirement is the transcribed
# lines it is derived from. The registry pre-flights these before calling, and a
# derived code here would gate on a fact nothing ever writes.
@metric("interest_coverage", requires_facts=("profit_before_tax", "interest_expense"))
def interest_coverage(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    ebit = reader.fact("ebit")
    interest = reader.fact("interest_expense")
    if interest.value <= 0:
        # No debt service is not infinite safety - the ratio is undefined. Saying so is
        # more useful than a number that cannot be compared to a threshold.
        raise MissingInput("no_interest_expense_ratio_undefined")
    return MetricResult(
        value=(ebit.value / interest.value).quantize(_TIMES),
        inputs={**reader.window().as_inputs(), **ebit.as_inputs(), **interest.as_inputs()},
    )


@metric(
    "net_debt_ebitda",
    requires_facts=(
        "short_term_debt",
        "long_term_debt",
        "cash_and_equivalents",
        "profit_before_tax",
        "interest_expense",
        "depreciation_amortisation",
    ),
)
def net_debt_ebitda(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    net_debt, debt_inputs = _net_debt(reader)
    ebitda = reader.fact("ebitda")
    value = _divide(net_debt, ebitda, _TIMES)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **debt_inputs, **ebitda.as_inputs()},
    )


@metric("debt_to_equity", requires_facts=("short_term_debt", "long_term_debt", "total_equity"))
def debt_to_equity(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    short_term = reader.fact("short_term_debt")
    long_term = reader.fact("long_term_debt")
    equity = reader.fact("total_equity")
    total_debt = short_term.value + long_term.value
    value = _divide(total_debt, equity, _TIMES)
    return MetricResult(
        value=value,
        inputs={
            **reader.window().as_inputs(),
            **short_term.as_inputs(),
            **long_term.as_inputs(),
            "total_debt_vnd": str(total_debt),
            **equity.as_inputs(),
        },
    )


# ───────────────────────── profitability ─────────────────────────


@metric("roe", requires_facts=("net_profit_after_tax", "total_equity"))
def roe(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    profit = reader.fact("net_profit_after_tax")
    equity = reader.fact("total_equity")
    # Negative equity makes the ratio meaningless rather than impressive. It is itself
    # worth flagging, but not via a return-on-equity figure.
    value = _divide(profit.value, equity, _RATE)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **profit.as_inputs(), **equity.as_inputs()},
    )


@metric("roa", requires_facts=("net_profit_after_tax", "total_assets"))
def roa(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    profit = reader.fact("net_profit_after_tax")
    assets = reader.fact("total_assets")
    value = _divide(profit.value, assets, _RATE)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **profit.as_inputs(), **assets.as_inputs()},
    )


@metric("net_margin", requires_facts=("net_profit_after_tax", "revenue"))
def net_margin(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    profit = reader.fact("net_profit_after_tax")
    revenue = reader.fact("revenue")
    value = _divide(profit.value, revenue, _RATE)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **profit.as_inputs(), **revenue.as_inputs()},
    )


@metric("gross_margin", requires_facts=("gross_profit", "revenue"))
def gross_margin(ctx: MetricContext) -> MetricResult:
    reader = _reader(ctx)
    gross = reader.fact("gross_profit")
    revenue = reader.fact("revenue")
    value = _divide(gross.value, revenue, _RATE)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **gross.as_inputs(), **revenue.as_inputs()},
    )


@metric("cfo_to_net_profit", requires_facts=("cfo", "net_profit_after_tax"))
def cfo_to_net_profit(ctx: MetricContext) -> MetricResult:
    """How much of the reported profit arrived as cash.

    Only defined against a positive profit, which is the point: the question this answers
    is whether a profit that was reported was also collected. Below zero means the twelve
    months that produced an accounting profit consumed cash.
    """
    reader = _reader(ctx)
    cfo = reader.fact("cfo")
    profit = reader.fact("net_profit_after_tax")
    value = _divide(cfo.value, profit, _TIMES)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **cfo.as_inputs(), **profit.as_inputs()},
    )


# ───────────────────────── growth ─────────────────────────


@metric("revenue_growth_yoy", requires_facts=("revenue",))
def revenue_growth_yoy(ctx: MetricContext) -> MetricResult:
    return _growth(ctx, "revenue")


@metric("net_profit_growth_yoy", requires_facts=("net_profit_after_tax",))
def net_profit_growth_yoy(ctx: MetricContext) -> MetricResult:
    return _growth(ctx, "net_profit_after_tax")


# ───────────────────────── ownership and related parties ─────────────────────────


@metric(
    "related_party_receivables_to_assets",
    requires_facts=("related_party_receivables", "total_assets"),
)
def related_party_receivables_to_assets(ctx: MetricContext) -> MetricResult:
    """How much of the balance sheet is lent to related parties.

    Both inputs are stocks, read at the window's close. A ratio of two balance-sheet
    dates, so neither is summed.
    """
    reader = _reader(ctx)
    receivables = reader.fact("related_party_receivables")
    assets = reader.fact("total_assets")
    value = _divide(receivables.value, assets, _RATE)
    return MetricResult(
        value=value,
        inputs={**reader.window().as_inputs(), **receivables.as_inputs(), **assets.as_inputs()},
    )


@metric("shares_cagr_3y", requires_facts=("shares_outstanding",))
def shares_cagr_3y(ctx: MetricContext) -> MetricResult:
    """Dilution: how fast the share count is growing.

    Annual observations, not TTM windows: this is the one fundamental whose window is
    three years rather than twelve months, and the share count is a stock, so there is
    nothing to sum. Needs two observations exactly three years apart — comparing against
    whatever the oldest available figure happens to be would silently change the metric's
    meaning per ticker.
    """
    rows = (
        ctx.session.execute(
            text(
                """
                SELECT ff.id, ff.value, fp.fiscal_year
                FROM financial_fact ff
                JOIN financial_period fp ON fp.id = ff.financial_period_id
                WHERE fp.ticker_id = :t
                  AND ff.fact_code = 'shares_outstanding'
                  AND fp.period_type = CAST('Y' AS period_type)
                  AND fp.period_end <= CAST(:as_of AS date)
                  AND ff.value IS NOT NULL
                ORDER BY fp.fiscal_year DESC
                """
            ),
            {"t": ctx.ticker_id, "as_of": ctx.as_of},
        )
        .mappings()
        .all()
    )
    if not rows:
        raise MissingInput("missing_fact:shares_outstanding")

    newest = rows[0]
    target_year = int(newest["fiscal_year"]) - 3
    older = next((r for r in rows if int(r["fiscal_year"]) == target_year), None)
    if older is None:
        raise MissingInput(f"no_shares_outstanding_for_fiscal_year:{target_year}")

    now = Decimal(str(newest["value"]))
    then = Decimal(str(older["value"]))
    if then <= 0:
        raise MissingInput("non_positive_denominator:shares_outstanding_3y_ago")

    # Compound annual growth over exactly three years.
    cagr = (now / then) ** (Decimal(1) / Decimal(3)) - Decimal(1)
    return MetricResult(
        value=cagr.quantize(_RATE),
        inputs={
            "shares_now": str(now),
            "fiscal_year_now": int(newest["fiscal_year"]),
            "shares_3y_ago": str(then),
            "fiscal_year_then": target_year,
            "fact_ids": [int(newest["id"]), int(older["id"])],
        },
    )


# Every fundamental, in page order. `cli.py` should run exactly this list - a metric
# missing from it is never attempted, and a metric never attempted cannot report
# "không công bố".
FUNDAMENTAL_CODES = (
    "pe",
    "pb",
    "ev_ebitda",
    "eps",
    "book_value_per_share",
    "interest_coverage",
    "net_debt_ebitda",
    "debt_to_equity",
    "roe",
    "roa",
    "net_margin",
    "gross_margin",
    "cfo_to_net_profit",
    "revenue_growth_yoy",
    "net_profit_growth_yoy",
    "related_party_receivables_to_assets",
    "shares_cagr_3y",
)
