"""Valuation, leverage and profitability metrics, computed from filed statements.

These are implemented, not stubbed, even though no statement source exists yet. The
difference matters: a metric with no implementation produces **no row at all**, so the fact
sheet cannot say *không công bố* for it — it simply has nothing to say, which is a hole in
the honesty model. Implemented and data-gated, each one produces a row naming the exact
fact it is waiting for (`missing_fact:ebitda`), and the day Arc B lands they start working
with no further change.

Two guards appear repeatedly and are deliberate:

* **A non-positive denominator is unavailable, never a negative ratio.** A loss-making
  company has no meaningful P/E. Printing `-4.2x` invites a reader to treat it as cheap.
* **Zero interest expense is unavailable, not infinite coverage.** A company with no debt
  is not "infinitely safe"; the ratio is simply undefined and the page should say so.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from quantumedge.calc.registry import MetricContext, MetricResult, MissingInput, metric

CALC_VERSION = "fundamentals/v1"

# Which period a fundamental is read from. TTM where a flow makes sense over twelve months,
# the latest annual otherwise.
PERIOD_TTM = "TTM"
PERIOD_YEAR = "Y"


def _fact(ctx: MetricContext, code: str, period_type: str = PERIOD_TTM) -> tuple[Decimal, int]:
    """Latest value of one statement line item, with the id that produced it.

    Raises MissingInput naming the fact, so the page can distinguish "no EBITDA reported"
    from "no statements at all" rather than showing one undifferentiated blank.
    """
    row = (
        ctx.session.execute(
            text(
                """
                SELECT ff.id, ff.value
                FROM financial_fact ff
                JOIN financial_period fp ON fp.id = ff.financial_period_id
                WHERE fp.ticker_id = :t
                  AND ff.fact_code = :code
                  AND fp.period_type = CAST(:pt AS period_type)
                  AND fp.period_end <= CAST(:as_of AS date)
                  AND ff.value IS NOT NULL
                ORDER BY fp.period_end DESC
                LIMIT 1
                """
            ),
            {"t": ctx.ticker_id, "code": code, "pt": period_type, "as_of": ctx.as_of},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise MissingInput(f"missing_fact:{code}")
    return Decimal(str(row["value"])), int(row["id"])


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


def _market_cap(ctx: MetricContext) -> tuple[Decimal, dict[str, Any]]:
    close, trade_date = _latest_close(ctx)
    shares, shares_id = _fact(ctx, "shares_outstanding", PERIOD_YEAR)
    if shares <= 0:
        raise MissingInput("non_positive_shares_outstanding")
    return close * shares, {
        "close_vnd": str(close),
        "trade_date": str(trade_date),
        "shares_outstanding": str(shares),
        "shares_fact_id": shares_id,
    }


def _net_debt(ctx: MetricContext) -> tuple[Decimal, dict[str, Any]]:
    short_term, std_id = _fact(ctx, "short_term_debt", PERIOD_YEAR)
    long_term, ltd_id = _fact(ctx, "long_term_debt", PERIOD_YEAR)
    cash, cash_id = _fact(ctx, "cash_and_equivalents", PERIOD_YEAR)
    return short_term + long_term - cash, {
        "short_term_debt": str(short_term),
        "long_term_debt": str(long_term),
        "cash_and_equivalents": str(cash),
        "fact_ids": [std_id, ltd_id, cash_id],
    }


# ───────────────────────── valuation ─────────────────────────


@metric("pe", requires_facts=("net_profit_after_tax", "shares_outstanding"))
def pe(ctx: MetricContext) -> MetricResult:
    cap, cap_inputs = _market_cap(ctx)
    profit, profit_id = _fact(ctx, "net_profit_after_tax")
    if profit <= 0:
        # A loss-making company has no meaningful P/E. Printing a negative one invites a
        # reader to see it as cheap, which is the opposite of what it means.
        raise MissingInput("non_positive_denominator:net_profit_after_tax")
    return MetricResult(
        value=(cap / profit).quantize(Decimal("0.01")),
        inputs={
            **cap_inputs,
            "market_cap_vnd": str(cap),
            "net_profit_after_tax": str(profit),
            "profit_fact_id": profit_id,
        },
    )


@metric(
    "ev_ebitda",
    requires_facts=("ebitda", "short_term_debt", "long_term_debt", "cash_and_equivalents"),
)
def ev_ebitda(ctx: MetricContext) -> MetricResult:
    cap, cap_inputs = _market_cap(ctx)
    net_debt, debt_inputs = _net_debt(ctx)
    ebitda, ebitda_id = _fact(ctx, "ebitda")
    if ebitda <= 0:
        raise MissingInput("non_positive_denominator:ebitda")
    enterprise_value = cap + net_debt
    return MetricResult(
        value=(enterprise_value / ebitda).quantize(Decimal("0.01")),
        inputs={
            **cap_inputs,
            **debt_inputs,
            "market_cap_vnd": str(cap),
            "net_debt_vnd": str(net_debt),
            "enterprise_value_vnd": str(enterprise_value),
            "ebitda": str(ebitda),
            "ebitda_fact_id": ebitda_id,
        },
    )


# ───────────────────────── leverage ─────────────────────────


@metric("interest_coverage", requires_facts=("ebit", "interest_expense"))
def interest_coverage(ctx: MetricContext) -> MetricResult:
    ebit, ebit_id = _fact(ctx, "ebit")
    interest, interest_id = _fact(ctx, "interest_expense")
    if interest <= 0:
        # No debt service is not infinite safety - the ratio is undefined. Saying so is
        # more useful than a number that cannot be compared to a threshold.
        raise MissingInput("no_interest_expense_ratio_undefined")
    return MetricResult(
        value=(ebit / interest).quantize(Decimal("0.01")),
        inputs={
            "ebit": str(ebit),
            "interest_expense": str(interest),
            "fact_ids": [ebit_id, interest_id],
        },
    )


@metric(
    "net_debt_ebitda",
    requires_facts=("short_term_debt", "long_term_debt", "cash_and_equivalents", "ebitda"),
)
def net_debt_ebitda(ctx: MetricContext) -> MetricResult:
    net_debt, debt_inputs = _net_debt(ctx)
    ebitda, ebitda_id = _fact(ctx, "ebitda")
    if ebitda <= 0:
        raise MissingInput("non_positive_denominator:ebitda")
    return MetricResult(
        value=(net_debt / ebitda).quantize(Decimal("0.01")),
        inputs={
            **debt_inputs,
            "net_debt_vnd": str(net_debt),
            "ebitda": str(ebitda),
            "ebitda_fact_id": ebitda_id,
        },
    )


# ───────────────────────── profitability and ownership ─────────────────────────


@metric("roe", requires_facts=("net_profit_after_tax", "total_equity"))
def roe(ctx: MetricContext) -> MetricResult:
    profit, profit_id = _fact(ctx, "net_profit_after_tax")
    equity, equity_id = _fact(ctx, "total_equity", PERIOD_YEAR)
    if equity <= 0:
        # Negative equity makes the ratio meaningless rather than impressive. It is itself
        # worth flagging, but not via a return-on-equity figure.
        raise MissingInput("non_positive_denominator:total_equity")
    return MetricResult(
        value=(profit / equity).quantize(Decimal("0.0001")),
        inputs={
            "net_profit_after_tax": str(profit),
            "total_equity": str(equity),
            "fact_ids": [profit_id, equity_id],
        },
    )


@metric("shares_cagr_3y", requires_facts=("shares_outstanding",))
def shares_cagr_3y(ctx: MetricContext) -> MetricResult:
    """Dilution: how fast the share count is growing.

    Needs two annual observations three years apart. Comparing against whatever the oldest
    available figure happens to be would silently change the metric's meaning per ticker,
    so the window is required rather than approximated.
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
        value=cagr.quantize(Decimal("0.0001")),
        inputs={
            "shares_now": str(now),
            "fiscal_year_now": int(newest["fiscal_year"]),
            "shares_3y_ago": str(then),
            "fiscal_year_then": target_year,
            "fact_ids": [int(newest["id"]), int(older["id"])],
        },
    )


FUNDAMENTAL_CODES = (
    "pe",
    "ev_ebitda",
    "interest_coverage",
    "net_debt_ebitda",
    "roe",
    "shares_cagr_3y",
)
