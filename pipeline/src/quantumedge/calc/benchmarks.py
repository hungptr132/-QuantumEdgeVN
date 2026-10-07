"""Benchmarks: a metric against its sector, against its exchange, and against its own past.

A bare number tells a reader almost nothing. *180 million VND of daily turnover* means
one thing for a large-cap and another entirely for a small-cap — the figure only becomes
information next to something. Three comparisons are computed here, and deliberately only
three:

* **sector** — the median and quartiles across covered peers at the same `as_of` date.
* **exchange** — the same cross-section widened to every covered ticker listed on the
  same venue. At a nine-ticker universe the sector sample is three, which is the bare
  minimum a median can be computed from; the exchange sample is every covered name, and
  it is the only comparison available at all to a company whose `sector_id` is NULL.
* **ticker_history** — the stock's own range over a trailing window, so "worst liquidity
  in three years" is a statement the data can support.

All three are **descriptive**. Nothing here ranks, scores, or concludes; position language
is decided in the fact sheet and is limited to above / below / in line / unknown.

Five rules that are not negotiable, each of which exists because the alternative would
mislead:

1. **A median needs a minimum sample.** At this universe size a "sector median" can be
   two companies, which is not a median. Below `min_benchmark_sample` the row is written
   `is_available=false, unavailable_reason='sample_below_minimum'` and renders as
   *không công bố*. Writing the row rather than skipping it is the point — a reader learns
   the comparison was attempted and why it is absent.
2. **Only available metric values count.** A ticker whose metric is unavailable is
   excluded from the sample, never treated as zero. Zero turnover and unknown turnover are
   different facts, and averaging the second as the first is how a thin stock gets
   flattered.
3. **The sample members are recorded.** `member_ticker_ids` names exactly which companies
   produced the number. Publishing "the sector median is 4.2bn" without being able to say
   which three companies that is would be indefensible.
4. **A sample includes the subject.** The sector and exchange rows are one row per
   (sector | exchange, metric, date) — a *property of the population*, not of a viewer.
   Leaving each ticker out of its own comparison would publish a different "trung vị nhóm
   ngành thép" on SMC's page than on NKG's, both under the same label, and neither
   reproducible from the three numbers a reader can see. At n=3 it would also drop every
   sector to n=2, below `min_benchmark_sample`, making sector comparison unavailable by
   construction. See `SAMPLE_INCLUDES_SUBJECT`.
5. **Quartiles are labelled honestly or not at all.** At n<5, p25 and p75 are
   interpolations between two adjacent order statistics, not independent statistics.
   See `MIN_EXACT_QUARTILE_SAMPLE` and `Distribution.quartiles_are_interpolated`.

## Why `window_label` carries the exchange code

`metric_benchmark`'s natural key is

    UNIQUE (metric_code, as_of_date, scope, COALESCE(sector_id,0), COALESCE(ticker_id,0),
            window_label)

and the `benchmark_scope_target` CHECK allows `scope='exchange'` with neither
`sector_id` nor `ticker_id`. Both COALESCE terms are therefore 0 for every exchange row,
so HOSE and HNX would collide on the same key and the second write would silently
overwrite the first — a HOSE page quoting an HNX median, with nothing to show it had
happened. `window_label` is the only remaining discriminator, so an exchange row's label
is `"<window>@<EXCHANGE_CODE>"` (`exchange_window_label` builds it, `parse_exchange_window_label`
reads it back). Nothing parses `window_label` by equality against a bare `"current"`
for exchange scope.

This is an encoding, not a design preference: the right fix is a nullable
`exchange_code` column in the key, which is a schema change and so not made here.
A whole-market row — one median spanning every venue — was rejected deliberately: HOSE
and UPCOM impose different listing, float and disclosure regimes, so their union is two
populations, and a median across it describes neither.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.db.enums import BenchmarkScope

log = structlog.get_logger(__name__)

CALC_VERSION = "benchmarks/v1"

# Trailing windows for a stock against itself, in calendar days. 3 years matches the
# price history actually ingested; 1 year gives a shorter-term reference.
HISTORY_WINDOWS: dict[str, int] = {"3y": 1095, "1y": 365}

# The window label a cross-sectional (sector or exchange) benchmark carries: the sample is
# every covered peer at one `as_of` date, so the "window" is that single date.
CURRENT_WINDOW = "current"

# Separator between the window and the exchange code inside an exchange row's
# `window_label`. See the module docstring for why the code has to live there at all.
# "@" is not used by any other label, and never appears in an exchange code (the column is
# String(10) holding HOSE / HNX / UPCOM), so the encoding round-trips unambiguously.
EXCHANGE_LABEL_SEPARATOR = "@"

# A ticker is part of its own sector and exchange sample. Rule 4 in the module docstring.
# Named rather than merely implied by the SQL, so that "did anyone decide this?" is a
# lookup and a change to it has to change a constant somebody will grep for.
SAMPLE_INCLUDES_SUBJECT = True

# Below this sample size, p25 and p75 are interpolated between two adjacent order
# statistics rather than being order statistics themselves.
#
# The convention here is linear interpolation over (n-1) positions — R type 7, which is
# also numpy's default — so p25 sits at position 0.25*(n-1):
#
#   n=3: 0.25*2 = 0.5  -> exactly midway between the minimum and the median
#   n=4: 0.25*3 = 0.75 -> three quarters of the way from the lowest to the second lowest
#   n=5: 0.25*4 = 1.0  -> exactly the second lowest value, no interpolation
#
# n=5 is therefore the smallest sample where both quartiles are real order statistics.
# Below it, p25 is an exact arithmetic restatement of numbers already published in the
# same row (at n=3, p25 == (min + median) / 2 exactly), carrying no information of its
# own — which is fine as a *band*, and overclaiming if it is labelled "the 25th
# percentile", since the fraction of a three-point sample below it is 1/3, not 1/4.
MIN_EXACT_QUARTILE_SAMPLE = 5


def exchange_window_label(exchange_code: str, window: str = CURRENT_WINDOW) -> str:
    """The `window_label` an exchange-scope row is keyed by, e.g. `"current@HOSE"`."""
    return f"{window}{EXCHANGE_LABEL_SEPARATOR}{exchange_code}"


def parse_exchange_window_label(label: str) -> tuple[str, str] | None:
    """`"current@HOSE"` -> `("current", "HOSE")`; anything unencoded -> None."""
    window, separator, code = label.partition(EXCHANGE_LABEL_SEPARATOR)
    if not separator or not code:
        return None
    return window, code


@dataclass
class BenchmarkOutcome:
    written: int = 0
    available: int = 0
    unavailable: int = 0
    notes: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "benchmarks_written": self.written,
            "available": self.available,
            "unavailable": self.unavailable,
        }
        if self.notes:
            stats["notes"] = self.notes[:20]
        return stats


def _quantile(ordered: list[Decimal], fraction: Decimal) -> Decimal:
    """Linear-interpolated quantile over a pre-sorted list (R type 7 / numpy default).

    Interpolating rather than picking the nearest rank matters at a sample size of three,
    where "the 25th percentile" would otherwise just be the minimum and two of the three
    quartiles would report the same number.

    The convention is fixed and documented because it is the difference between a figure a
    reader can reproduce and one they cannot: position = fraction * (n - 1), then linear
    weight between the two straddling values. See `MIN_EXACT_QUARTILE_SAMPLE` for what
    this does and does not entitle a caller to claim at n<5.
    """
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * Decimal(len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - Decimal(low)
    return ordered[low] + (ordered[high] - ordered[low]) * weight


@dataclass(frozen=True)
class Distribution:
    median: Decimal
    p25: Decimal
    p75: Decimal
    minimum: Decimal
    maximum: Decimal
    sample_size: int

    @property
    def quartiles_are_interpolated(self) -> bool:
        """True when p25/p75 are interpolations, not order statistics of the sample.

        At this universe size that is *always* true for a sector row (n=3), so a caller
        that renders p25/p75 as "the 25th percentile" is overclaiming. They are the
        boundaries of a conservative band — which is exactly what
        `factsheet.comparisons._position_vs_sector` uses them for, and that use is sound
        at any n. The distinction is about the label, not the arithmetic, so it is exposed
        here rather than suppressed: a figure withheld at n=3 would push the fact sheet
        back onto a median-only comparison, which reports a figure one dong above the
        midpoint as "above the sector" and is strictly the less honest of the two.
        """
        return self.sample_size < MIN_EXACT_QUARTILE_SAMPLE


def describe(values: list[Decimal]) -> Distribution | None:
    """Summarise a sample. Returns None for an empty one rather than zeros."""
    if not values:
        return None
    ordered = sorted(values)
    return Distribution(
        median=_quantile(ordered, Decimal("0.5")),
        p25=_quantile(ordered, Decimal("0.25")),
        p75=_quantile(ordered, Decimal("0.75")),
        minimum=ordered[0],
        maximum=ordered[-1],
        sample_size=len(ordered),
    )


def _write(
    session: Session,
    run_id: int,
    *,
    metric_code: str,
    as_of: date,
    scope: BenchmarkScope,
    window_label: str,
    sector_id: int | None = None,
    ticker_id: int | None = None,
    dist: Distribution | None,
    sample_size: int,
    members: list[int],
    minimum_sample: int,
    outcome: BenchmarkOutcome,
) -> None:
    """One converging upsert, available or not.

    An unavailable benchmark is still written. Absence of a row would be
    indistinguishable from a benchmark nobody tried to compute, and the rule engine
    reports those two states differently (`benchmark_not_computed` vs
    `benchmark_unavailable`) precisely so a reader can tell them apart.
    """
    available = dist is not None and sample_size >= minimum_sample
    reason: str | None = None
    if not available:
        reason = "no_sample" if sample_size == 0 else "sample_below_minimum"

    session.execute(
        text(
            "INSERT INTO metric_benchmark "
            "(metric_code, as_of_date, scope, sector_id, ticker_id, window_label, "
            " median, p25, p75, min_value, max_value, sample_size, is_available, "
            " unavailable_reason, member_ticker_ids, calc_run_id) "
            "VALUES (:metric_code, :as_of, CAST(:scope AS benchmark_scope), :sector_id, "
            " :ticker_id, :window_label, :median, :p25, :p75, :min_value, :max_value, "
            " :sample_size, :is_available, :reason, :members, :run_id) "
            "ON CONFLICT (metric_code, as_of_date, scope, COALESCE(sector_id, 0), "
            " COALESCE(ticker_id, 0), window_label) DO UPDATE SET "
            " median = EXCLUDED.median, p25 = EXCLUDED.p25, p75 = EXCLUDED.p75, "
            " min_value = EXCLUDED.min_value, max_value = EXCLUDED.max_value, "
            " sample_size = EXCLUDED.sample_size, is_available = EXCLUDED.is_available, "
            " unavailable_reason = EXCLUDED.unavailable_reason, "
            " member_ticker_ids = EXCLUDED.member_ticker_ids, "
            " calc_run_id = EXCLUDED.calc_run_id"
        ),
        {
            "metric_code": metric_code,
            "as_of": as_of,
            "scope": scope.value,
            "sector_id": sector_id,
            "ticker_id": ticker_id,
            "window_label": window_label,
            # Statistics are withheld entirely when the sample is too small. Storing them
            # alongside is_available=false would invite a later caller to read them anyway.
            "median": dist.median if available and dist else None,
            "p25": dist.p25 if available and dist else None,
            "p75": dist.p75 if available and dist else None,
            "min_value": dist.minimum if available and dist else None,
            "max_value": dist.maximum if available and dist else None,
            "sample_size": sample_size,
            "is_available": available,
            "reason": reason,
            "members": members,
            "run_id": run_id,
        },
    )

    outcome.written += 1
    if available:
        outcome.available += 1
    else:
        outcome.unavailable += 1


def build_sector_benchmarks(
    session: Session,
    run_id: int,
    as_of: date,
    *,
    minimum_sample: int = 3,
    metric_codes: list[str] | None = None,
) -> BenchmarkOutcome:
    """Per sector and metric: the distribution across covered peers at `as_of`.

    The sample includes every covered ticker in the sector, the subject among them
    (`SAMPLE_INCLUDES_SUBJECT`). One row is written per (sector, metric), not one per
    viewer, so the number on every page of a sector is the same number and is reproducible
    from `member_ticker_ids`.
    """
    outcome = BenchmarkOutcome()

    clause = "AND mv.metric_code = ANY(:codes)" if metric_codes else ""
    params: dict[str, Any] = {"as_of": as_of}
    if metric_codes:
        params["codes"] = metric_codes

    # Every (sector, metric) pair that has any covered ticker, whether or not the metric
    # resolved — so a sector where nothing is available still gets its honest row.
    pairs = session.execute(
        text(
            f"""
            SELECT c.sector_id,
                   mv.metric_code,
                   count(*) FILTER (WHERE mv.is_available)        AS n_available,
                   array_agg(t.id ORDER BY t.symbol)
                     FILTER (WHERE mv.is_available)                AS members,
                   array_agg(mv.value ORDER BY t.symbol)
                     FILTER (WHERE mv.is_available)                AS values
            FROM metric_value mv
            JOIN ticker t  ON t.id = mv.ticker_id AND t.is_covered
            JOIN company c ON c.id = t.company_id
            WHERE mv.as_of_date = :as_of AND c.sector_id IS NOT NULL {clause}
            GROUP BY c.sector_id, mv.metric_code
            ORDER BY c.sector_id, mv.metric_code
            """
        ),
        params,
    ).mappings()

    for row in pairs:
        values = [Decimal(str(v)) for v in (row["values"] or [])]
        members = list(row["members"] or [])
        _write(
            session,
            run_id,
            metric_code=row["metric_code"],
            as_of=as_of,
            scope=BenchmarkScope.SECTOR,
            window_label=CURRENT_WINDOW,
            sector_id=row["sector_id"],
            dist=describe(values),
            sample_size=len(values),
            members=members,
            minimum_sample=minimum_sample,
            outcome=outcome,
        )

    log.info(
        "benchmarks.sector",
        as_of=str(as_of),
        written=outcome.written,
        available=outcome.available,
    )
    return outcome


def build_exchange_benchmarks(
    session: Session,
    run_id: int,
    as_of: date,
    *,
    minimum_sample: int = 3,
    metric_codes: list[str] | None = None,
) -> BenchmarkOutcome:
    """Per exchange and metric: the distribution across every covered ticker on that venue.

    The comparison a nine-ticker universe most needs, and the one nothing wrote until now.
    A sector sample is three — the bare minimum `min_benchmark_sample` permits — so a
    single outlier moves the median it is being compared against. The exchange sample is
    every covered name on the venue, which is nine today and grows with coverage rather
    than staying pinned at three.

    It is also the *only* comparison available to a company whose `sector_id` is NULL:
    `build_sector_benchmarks` filters those rows out (`c.sector_id IS NOT NULL`) and so
    leaves such a ticker with nothing cross-sectional at all. This function deliberately
    does not join `company`, so sector coverage is irrelevant to it.

    Per *exchange*, not whole-market. HOSE, HNX and UPCOM impose different listing, free
    float and disclosure regimes, so a median across their union summarises two or three
    populations and describes none of them. Every covered ticker is on HOSE today, which
    makes the two identical in output and different in meaning — and the `exchange` lookup
    table exists precisely so coverage can extend to HNX and UPCOM, at which point a
    whole-market row would start quietly misleading and this one would not.

    The sample includes the subject ticker (`SAMPLE_INCLUDES_SUBJECT`), for the same
    reason as the sector row: this is a property of the venue, not of a viewer.
    """
    outcome = BenchmarkOutcome()

    clause = "AND mv.metric_code = ANY(:codes)" if metric_codes else ""
    params: dict[str, Any] = {"as_of": as_of}
    if metric_codes:
        params["codes"] = metric_codes

    # Grouped on the ticker's exchange_code, which is where listing venue lives: a listing
    # is a property of the listing, and tickers do move between venues (see models.Ticker).
    # Every (exchange, metric) pair with any covered ticker is emitted, whether or not the
    # metric resolved for any of them, so a venue where nothing is available still gets its
    # honest unavailable row rather than silently no row.
    pairs = session.execute(
        text(
            f"""
            SELECT t.exchange_code,
                   mv.metric_code,
                   array_agg(t.id ORDER BY t.symbol)
                     FILTER (WHERE mv.is_available)                AS members,
                   array_agg(mv.value ORDER BY t.symbol)
                     FILTER (WHERE mv.is_available)                AS values
            FROM metric_value mv
            JOIN ticker t ON t.id = mv.ticker_id AND t.is_covered
            WHERE mv.as_of_date = :as_of {clause}
            GROUP BY t.exchange_code, mv.metric_code
            ORDER BY t.exchange_code, mv.metric_code
            """
        ),
        params,
    ).mappings()

    for row in pairs:
        values = [Decimal(str(v)) for v in (row["values"] or [])]
        members = list(row["members"] or [])
        _write(
            session,
            run_id,
            metric_code=row["metric_code"],
            as_of=as_of,
            scope=BenchmarkScope.EXCHANGE,
            # Both id columns are NULL for exchange scope, so the venue has to be carried
            # in the one key column left. Without this, HOSE and HNX collide.
            window_label=exchange_window_label(row["exchange_code"]),
            dist=describe(values),
            sample_size=len(values),
            members=members,
            minimum_sample=minimum_sample,
            outcome=outcome,
        )

    log.info(
        "benchmarks.exchange",
        as_of=str(as_of),
        written=outcome.written,
        available=outcome.available,
    )
    return outcome


def build_history_benchmarks(
    session: Session,
    run_id: int,
    as_of: date,
    *,
    minimum_sample: int = 3,
    metric_codes: list[str] | None = None,
    windows: dict[str, int] | None = None,
) -> BenchmarkOutcome:
    """Per ticker and metric: the stock's own distribution over trailing windows.

    The sample is the ticker's own previously computed `metric_value` rows, which is why
    this is worth having: it needs no new data source, only that metrics have been run at
    past `as_of` dates. `qe calc backfill` exists to create that history.
    """
    outcome = BenchmarkOutcome()
    windows = windows or HISTORY_WINDOWS

    clause = "AND mv.metric_code = ANY(:codes)" if metric_codes else ""

    for label, days in windows.items():
        params: dict[str, Any] = {"as_of": as_of, "days": days}
        if metric_codes:
            params["codes"] = metric_codes

        rows = session.execute(
            text(
                f"""
                SELECT mv.ticker_id,
                       mv.metric_code,
                       array_agg(mv.value ORDER BY mv.as_of_date)
                         FILTER (WHERE mv.is_available) AS values
                FROM metric_value mv
                JOIN ticker t ON t.id = mv.ticker_id AND t.is_covered
                WHERE mv.as_of_date <= :as_of
                  AND mv.as_of_date > :as_of - CAST(:days AS integer)
                  {clause}
                GROUP BY mv.ticker_id, mv.metric_code
                ORDER BY mv.ticker_id, mv.metric_code
                """
            ),
            params,
        ).mappings()

        for row in rows:
            values = [Decimal(str(v)) for v in (row["values"] or [])]
            _write(
                session,
                run_id,
                metric_code=row["metric_code"],
                as_of=as_of,
                scope=BenchmarkScope.TICKER_HISTORY,
                window_label=label,
                ticker_id=row["ticker_id"],
                dist=describe(values),
                sample_size=len(values),
                # A stock compared against itself has one member by definition; the field
                # is for sector samples and stays empty rather than self-referential.
                members=[],
                minimum_sample=minimum_sample,
                outcome=outcome,
            )

    log.info(
        "benchmarks.history",
        as_of=str(as_of),
        written=outcome.written,
        available=outcome.available,
    )
    return outcome
