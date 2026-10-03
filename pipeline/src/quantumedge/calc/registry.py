"""Metric registry and runner.

The important behaviour here is what happens when an input is missing. A metric must not
raise, and must not quietly return zero — it has to produce a row saying *it is not
available* and *why*. The database enforces the shape (`metric_value_consistent`), and this
module is what makes satisfying it the default rather than an afterthought.

Every result also records the exact ids and values it consumed in `inputs`, so any figure
on a page can be recomputed by hand from its own row.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

log = structlog.get_logger(__name__)


class MissingInput(Exception):
    """Raised by a metric when a required input is absent.

    Carries a machine-readable reason so the page can distinguish "no EBITDA reported" from
    "fewer than 20 sessions of history" rather than showing one generic blank.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class MetricResult:
    value: Decimal | None
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricSpec:
    code: str
    fn: Callable[["MetricContext"], MetricResult]
    # Statement line items the metric cannot run without. Checked by the runner before the
    # function is called, so a metric need not repeat the guard and the reported reason
    # names the first missing input rather than whichever one happened to be read first.
    requires_facts: tuple[str, ...] = ()


REGISTRY: dict[str, MetricSpec] = {}


def metric(
    code: str, *, requires_facts: tuple[str, ...] = ()
) -> Callable[
    [Callable[["MetricContext"], MetricResult]], Callable[["MetricContext"], MetricResult]
]:
    """Register a metric implementation under its `metric_def.code`."""

    def decorator(
        fn: Callable[["MetricContext"], MetricResult],
    ) -> Callable[["MetricContext"], MetricResult]:
        if code in REGISTRY:
            raise ValueError(f"metric '{code}' is already registered")
        REGISTRY[code] = MetricSpec(code=code, fn=fn, requires_facts=requires_facts)
        return fn

    return decorator


def _missing_facts(ctx: "MetricContext", codes: tuple[str, ...]) -> list[str]:
    """Which of the declared statement facts this ticker has no value for."""
    if not codes:
        return []
    present = set(
        ctx.session.execute(
            text(
                "SELECT DISTINCT ff.fact_code FROM financial_fact ff "
                "JOIN financial_period fp ON fp.id = ff.financial_period_id "
                "WHERE fp.ticker_id = :t AND ff.fact_code = ANY(:codes) "
                "  AND ff.value IS NOT NULL AND fp.period_end <= CAST(:as_of AS date)"
            ),
            {"t": ctx.ticker_id, "codes": list(codes), "as_of": ctx.as_of},
        ).scalars()
    )
    return [c for c in codes if c not in present]


@dataclass
class MetricContext:
    """What a metric is allowed to see.

    `as_of` is always explicit — no metric may ask the clock, or the same inputs would stop
    producing the same answer and reproducibility would be gone.
    """

    session: Session
    ticker_id: int
    symbol: str
    exchange_code: str
    as_of: date
    par_value_vnd: Decimal


@dataclass
class CalcOutcome:
    written: int = 0
    available: int = 0
    unavailable: int = 0
    notes: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "metrics_written": self.written,
            "available": self.available,
            "unavailable": self.unavailable,
        }
        if self.notes:
            stats["notes"] = self.notes[:40]
        return stats


def run_metrics(
    session: Session,
    calc_run_id: int,
    calc_version: str,
    contexts: list[MetricContext],
    *,
    codes: list[str] | None = None,
) -> CalcOutcome:
    """Evaluate registered metrics for each context and persist every outcome.

    An unavailable metric is still written. A row that says "not available, because fewer
    than 20 trading sessions exist" is information; a silently absent row is not, and would
    be indistinguishable from the metric never having been attempted.
    """
    outcome = CalcOutcome()
    selected = [REGISTRY[c] for c in (codes or REGISTRY.keys()) if c in REGISTRY]

    for ctx in contexts:
        for spec in selected:
            try:
                # Pre-flight: a metric whose declared inputs are absent reports that
                # plainly, without running and without each metric repeating the check.
                absent = _missing_facts(ctx, spec.requires_facts)
                if absent:
                    raise MissingInput("missing_facts:" + ",".join(absent))
                result = spec.fn(ctx)
                value, inputs, reason = result.value, result.inputs, None
                if value is None:
                    reason = "computed_null"
            except MissingInput as exc:
                value, inputs, reason = None, {}, exc.reason
            except ZeroDivisionError:
                value, inputs, reason = None, {}, "division_by_zero"

            available = value is not None and reason is None
            if available:
                outcome.available += 1
            else:
                outcome.unavailable += 1
                outcome.notes.append(f"{ctx.symbol}/{spec.code}: {reason}")

            session.execute(
                text(
                    """
                    INSERT INTO metric_value
                        (ticker_id, metric_code, as_of_date, value, is_available,
                         unavailable_reason, inputs, calc_run_id, calc_version)
                    VALUES
                        (:ticker_id, :code, :as_of, :value, :is_available,
                         :reason, CAST(:inputs AS jsonb), :run, :version)
                    ON CONFLICT (ticker_id, metric_code, as_of_date) DO UPDATE SET
                        value = EXCLUDED.value,
                        is_available = EXCLUDED.is_available,
                        unavailable_reason = EXCLUDED.unavailable_reason,
                        inputs = EXCLUDED.inputs,
                        calc_run_id = EXCLUDED.calc_run_id,
                        calc_version = EXCLUDED.calc_version,
                        computed_at = now()
                    """
                ),
                {
                    "ticker_id": ctx.ticker_id,
                    "code": spec.code,
                    "as_of": ctx.as_of,
                    "value": value,
                    "is_available": available,
                    "reason": None if available else (reason or "unknown"),
                    "inputs": _json(inputs),
                    "run": calc_run_id,
                    "version": calc_version,
                },
            )
            outcome.written += 1

    log.info(
        "calc.metrics",
        written=outcome.written,
        available=outcome.available,
        unavailable=outcome.unavailable,
    )
    return outcome


def load_contexts(
    session: Session, as_of: date, *, symbols: list[str] | None = None
) -> list[MetricContext]:
    """One context per covered ticker."""
    clause = "AND symbol = ANY(:symbols)" if symbols else ""
    params: dict[str, Any] = {"symbols": symbols} if symbols else {}
    rows = session.execute(
        text(
            f"SELECT id, symbol, exchange_code, par_value_vnd FROM ticker "
            f"WHERE is_covered {clause} ORDER BY symbol"
        ),
        params,
    ).mappings()
    return [
        MetricContext(
            session=session,
            ticker_id=r["id"],
            symbol=r["symbol"],
            exchange_code=r["exchange_code"],
            as_of=as_of,
            par_value_vnd=r["par_value_vnd"],
        )
        for r in rows
    ]


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
