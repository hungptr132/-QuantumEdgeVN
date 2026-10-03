"""Benchmarks: a metric against its sector, and against the stock's own history.

A bare number tells a reader almost nothing. *180 million VND of daily turnover* means
one thing for a large-cap and another entirely for a small-cap — the figure only becomes
information next to something. Two comparisons are computed here, and deliberately only
two:

* **sector** — the median and quartiles across covered peers at the same `as_of` date.
* **ticker_history** — the stock's own range over a trailing window, so "worst liquidity
  in three years" is a statement the data can support.

Both are **descriptive**. Nothing here ranks, scores, or concludes; position language is
decided in the fact sheet and is limited to above / below / in line / unknown.

Three rules that are not negotiable, each of which exists because the alternative would
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
    """Linear-interpolated quantile over a pre-sorted list.

    Interpolating rather than picking the nearest rank matters at a sample size of three,
    where "the 25th percentile" would otherwise just be the minimum and two of the three
    quartiles would report the same number.
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
    """Per sector and metric: the distribution across covered peers at `as_of`."""
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
            window_label="current",
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
