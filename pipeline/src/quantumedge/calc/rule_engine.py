"""Evaluate `flag_def.rule_expr` into a state, evidence, and a reason.

Three properties this module exists to guarantee:

1. **A missing input yields `unknown`, never `inactive`.** `inactive` is a positive claim
   that the rule was tested and did not fire. If the input was never there, saying
   `inactive` would present absence as a clean bill of health, which the spec forbids.

2. **An `active` flag always carries evidence.** A negative statement about a named
   company needs something attached to it — that is a legal position, not a UI nicety, and
   a CHECK constraint refuses the row without it.

3. **Rules are data, not code.** Thresholds live in `rules/flags.yaml`; this file only
   knows how to apply the shapes. Changing a threshold is a reviewable one-line diff, and
   its content hash marks every derived row stale.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc.benchmarks import exchange_window_label
from quantumedge.db.enums import BenchmarkScope, FlagState

log = structlog.get_logger(__name__)

RULE_ENGINE_VERSION = "rules/v1"

_OPS: dict[str, Any] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


@dataclass
class Evaluation:
    state: FlagState
    evidence: list[dict[str, Any]] = field(default_factory=list)
    triggered_values: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


@dataclass
class RuleContext:
    session: Session
    ticker_id: int
    symbol: str
    as_of: date


def evaluate(ctx: RuleContext, rule: dict[str, Any]) -> Evaluation:
    """Dispatch on `rule_kind`. An unimplemented kind is `unknown`, never a silent pass."""
    kind = rule["rule_kind"]
    expr = rule["rule_expr"]

    if kind == "metric_threshold":
        return _metric_threshold(ctx, rule, expr)
    if kind == "metric_vs_benchmark":
        return _metric_vs_benchmark(ctx, rule, expr)
    if kind == "event_presence":
        return _event_presence(ctx, expr)
    if kind == "data_absence":
        return _data_absence(ctx, expr)
    return Evaluation(state=FlagState.UNKNOWN, reason=f"rule_kind_not_implemented:{kind}")


def _load_metric(ctx: RuleContext, code: str) -> dict[str, Any] | None:
    row = (
        ctx.session.execute(
            text(
                "SELECT id, value, is_available, unavailable_reason FROM metric_value "
                "WHERE ticker_id = :t AND metric_code = :c AND as_of_date = CAST(:d AS date)"
            ),
            {"t": ctx.ticker_id, "c": code, "d": ctx.as_of},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _metric_threshold(ctx: RuleContext, rule: dict[str, Any], expr: dict[str, Any]) -> Evaluation:
    code, op, threshold = expr["metric"], expr["op"], Decimal(str(expr["value"]))

    metric = _load_metric(ctx, code)
    if metric is None:
        # Never computed. Distinct from computed-and-unavailable: one means the pipeline
        # has not run that metric, the other means the data does not support it.
        return Evaluation(state=FlagState.UNKNOWN, reason=f"metric_not_computed:{code}")
    if not metric["is_available"]:
        return Evaluation(
            state=FlagState.UNKNOWN,
            reason=f"metric_unavailable:{code}:{metric['unavailable_reason']}",
        )

    if op not in _OPS:
        return Evaluation(state=FlagState.UNKNOWN, reason=f"unknown_operator:{op}")

    value = Decimal(str(metric["value"]))
    fired = bool(_OPS[op](value, threshold))

    triggered = {"metric": code, "value": str(value), "op": op, "threshold": str(threshold)}
    if not fired:
        # A genuine negative: the rule was tested against real data and did not fire.
        return Evaluation(state=FlagState.INACTIVE, triggered_values=triggered)

    return Evaluation(
        state=FlagState.ACTIVE,
        triggered_values=triggered,
        evidence=[
            {
                "kind": "metric",
                "ref": f"metric_value:{metric['id']}",
                "label": f"{code} = {_exact(value)} ({op} {_exact(threshold)})",
                "source_url": None,
            }
        ],
    )


def _exact(value: Any) -> str:
    """A figure for the evidence trail: exact, with thousands separators.

    The evidence block is the audit trail, so the value must be the one actually compared
    - never a rounded display. But `numeric(24,6)` renders 40435850 as "40435850.000000",
    and those trailing zeros are column scale rather than measured precision. Stripping
    them and grouping the digits changes nothing about the number and makes it legible.
    """
    d = Decimal(str(value))
    if d == d.to_integral_value():
        return f"{int(d):,}"
    # normalize() alone would render 40435850.000000 as 4.043585E+7 - worse than the
    # trailing zeros it removes. Formatting with "f" first forces plain notation, so the
    # zeros go and the magnitude stays readable.
    return f"{Decimal(format(d.normalize(), 'f')):,f}"


# The only statistic a `metric_vs_benchmark` rule may compare against today. p25/p75 are
# deliberately absent: at the sample sizes this universe produces they are interpolations
# between two adjacent values rather than order statistics of the sample (see
# calc.benchmarks.MIN_EXACT_QUARTILE_SAMPLE), so a flag keyed to one would fire on an
# arithmetic artefact. An unrecognised stat is `unknown`, never a silent fallback to the
# median — a rule asking for p75 and quietly getting the median is a wrong number.
_BENCHMARK_STATS = frozenset({"median"})

_BENCHMARK_COLUMNS = (
    "b.id, b.median, b.sample_size, b.is_available, b.unavailable_reason, b.window_label"
)


def _sector_benchmark(ctx: RuleContext, code: str, window: str) -> dict[str, Any] | None:
    """The row for this ticker's own sector. NULL `sector_id` matches nothing, by design:
    `sector_id = c.sector_id` is never true when the company has no sector, so a
    sector-less company reports `benchmark_not_computed` rather than matching some
    arbitrary row."""
    row = (
        ctx.session.execute(
            text(
                f"SELECT {_BENCHMARK_COLUMNS} "
                "FROM metric_benchmark b "
                "JOIN ticker t ON t.id = :t "
                "JOIN company c ON c.id = t.company_id "
                "WHERE b.metric_code = :c AND b.as_of_date = CAST(:d AS date) "
                "  AND b.scope = 'sector' "
                "  AND b.window_label = :window AND b.sector_id = c.sector_id"
            ),
            {"t": ctx.ticker_id, "c": code, "d": ctx.as_of, "window": window},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _exchange_benchmark(
    ctx: RuleContext, code: str, window: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The row for the venue this ticker is listed on, and that venue's code.

    An exchange-scope row carries neither `sector_id` nor `ticker_id`, so the exchange is
    encoded into `window_label` (`calc.benchmarks.exchange_window_label`) and that label is
    the whole of the match. Both id columns are asserted NULL as well: if a future writer
    ever puts an exchange-scope row against a sector, this returns nothing and the flag
    reads `unknown`, rather than comparing against a population it cannot name.
    """
    exchange_code = ctx.session.execute(
        text("SELECT exchange_code FROM ticker WHERE id = :t"),
        {"t": ctx.ticker_id},
    ).scalar_one_or_none()
    if exchange_code is None:
        return None, None

    row = (
        ctx.session.execute(
            text(
                f"SELECT {_BENCHMARK_COLUMNS} "
                "FROM metric_benchmark b "
                "WHERE b.metric_code = :c AND b.as_of_date = CAST(:d AS date) "
                "  AND b.scope = 'exchange' "
                "  AND b.window_label = :label "
                "  AND b.sector_id IS NULL AND b.ticker_id IS NULL"
            ),
            {"c": code, "d": ctx.as_of, "label": exchange_window_label(exchange_code, window)},
        )
        .mappings()
        .first()
    )
    return (dict(row) if row else None), str(exchange_code)


def _metric_vs_benchmark(
    ctx: RuleContext, rule: dict[str, Any], expr: dict[str, Any]
) -> Evaluation:
    """`{metric, op, benchmark: {scope, window, stat}, multiple}` against a stored row.

    Every path out of here that lacks a real number on both sides is `unknown` with a
    reason naming which side was missing. There are five such paths — the metric was never
    computed, the metric is unavailable, the stat is unsupported, the benchmark was never
    computed, the benchmark is unavailable — and none of them may return `inactive`, which
    would assert that the rule was tested against real data and passed.
    """
    code = expr["metric"]
    metric = _load_metric(ctx, code)
    if metric is None or not metric["is_available"]:
        reason = (
            f"metric_not_computed:{code}"
            if metric is None
            else f"metric_unavailable:{code}:{metric['unavailable_reason']}"
        )
        return Evaluation(state=FlagState.UNKNOWN, reason=reason)

    spec = expr["benchmark"]
    scope, window = spec["scope"], spec["window"]
    stat = spec.get("stat", "median")
    if stat not in _BENCHMARK_STATS:
        return Evaluation(state=FlagState.UNKNOWN, reason=f"benchmark_stat_not_supported:{stat}")

    bench: dict[str, Any] | None
    if scope == BenchmarkScope.SECTOR.value:
        bench, label = _sector_benchmark(ctx, code, window), scope
    elif scope == BenchmarkScope.EXCHANGE.value:
        bench, exchange_code = _exchange_benchmark(ctx, code, window)
        # The venue goes in the reason and the evidence: "the HOSE median" is a claim a
        # reader can check, "the exchange median" is not once coverage spans HNX.
        label = f"{scope}:{exchange_code}" if exchange_code else scope
    else:
        # ticker_history included: comparing a figure against a quantile of its own past
        # is a different rule shape, and resolving it here would have silently matched a
        # sector row on a NULL sector_id comparison that is never true.
        return Evaluation(state=FlagState.UNKNOWN, reason=f"benchmark_scope_not_supported:{scope}")

    if bench is None:
        return Evaluation(state=FlagState.UNKNOWN, reason=f"benchmark_not_computed:{code}:{label}")
    if not bench["is_available"] or bench["median"] is None:
        # Both halves are load-bearing. `is_available=false` is the stated contract, and
        # the NULL check is the belt: a row written available-but-empty by some future
        # writer must not reach the comparison below, where `Decimal(str(None))` would
        # raise and `None * multiple` would be worse.
        return Evaluation(
            state=FlagState.UNKNOWN,
            reason=f"benchmark_unavailable:{code}:{label}:{bench['unavailable_reason']}",
        )

    multiple = Decimal(str(expr.get("multiple", 1)))
    comparand = Decimal(str(bench["median"])) * multiple
    value = Decimal(str(metric["value"]))
    op = expr["op"]
    if op not in _OPS:
        return Evaluation(state=FlagState.UNKNOWN, reason=f"unknown_operator:{op}")

    triggered = {
        "metric": code,
        "value": str(value),
        "op": op,
        "benchmark_scope": label,
        "benchmark_stat": stat,
        "benchmark_median": str(bench["median"]),
        "multiple": str(multiple),
        "comparand": str(comparand),
        "sample_size": str(bench["sample_size"]),
    }
    if not bool(_OPS[op](value, comparand)):
        return Evaluation(state=FlagState.INACTIVE, triggered_values=triggered)

    return Evaluation(
        state=FlagState.ACTIVE,
        triggered_values=triggered,
        evidence=[
            {
                "kind": "metric",
                "ref": f"metric_value:{metric['id']}",
                "label": f"{code} = {_exact(value)}",
                "source_url": None,
            },
            {
                "kind": "benchmark",
                "ref": f"metric_benchmark:{bench['id']}",
                "label": (
                    f"{label} median {_exact(bench['median'])} x{_exact(multiple)} "
                    f"(n={bench['sample_size']})"
                ),
                "source_url": None,
            },
        ],
    )


def _event_presence(ctx: RuleContext, expr: dict[str, Any]) -> Evaluation:
    """Looks for a disclosed event of a given type inside a window.

    With no event source configured, this reports `unknown` rather than `inactive`. The
    difference is the whole point: "no auditor change was disclosed" and "we have no way to
    know whether one was" are different statements, and only the second is true today.
    """
    has_source = ctx.session.execute(
        text("SELECT exists (SELECT 1 FROM event LIMIT 1)")
    ).scalar_one()
    if not has_source:
        return Evaluation(state=FlagState.UNKNOWN, reason="no_event_source_configured")

    months = int(expr.get("within_months", 24))
    row = (
        ctx.session.execute(
            text(
                "SELECT id, event_date, source_url, excerpt FROM event "
                "WHERE ticker_id = :t AND event_type = CAST(:et AS event_type) "
                "  AND event_date > CAST(:d AS date) - make_interval(months => :m) "
                "  AND event_date <= CAST(:d AS date) "
                "ORDER BY event_date DESC LIMIT 1"
            ),
            {"t": ctx.ticker_id, "et": expr["event_type"], "d": ctx.as_of, "m": months},
        )
        .mappings()
        .first()
    )

    if row is None:
        return Evaluation(
            state=FlagState.INACTIVE,
            triggered_values={"event_type": expr["event_type"], "within_months": months},
        )

    return Evaluation(
        state=FlagState.ACTIVE,
        triggered_values={"event_type": expr["event_type"], "event_date": str(row["event_date"])},
        evidence=[
            {
                "kind": "event",
                "ref": f"event:{row['id']}",
                "label": row["excerpt"][:200],
                "source_url": row["source_url"],
            }
        ],
    )


def _data_absence(ctx: RuleContext, expr: dict[str, Any]) -> Evaluation:
    """Flags a disclosure that should exist and does not.

    Only meaningful once the relevant source is wired up: without statement ingestion,
    everything would look absent and the flag would fire on all nine tickers for the wrong
    reason. So it reports `unknown` until there is something to be absent from.
    """
    target = expr.get("absent")
    if target != "financial_period":
        return Evaluation(state=FlagState.UNKNOWN, reason=f"absence_target_unsupported:{target}")

    has_any = ctx.session.execute(
        text("SELECT exists (SELECT 1 FROM financial_period WHERE ticker_id = :t)"),
        {"t": ctx.ticker_id},
    ).scalar_one()
    if not has_any:
        return Evaluation(state=FlagState.UNKNOWN, reason="no_statement_source_configured")

    months = int(expr.get("within_months", 18))
    found = (
        ctx.session.execute(
            text(
                "SELECT id, period_end FROM financial_period "
                "WHERE ticker_id = :t AND period_type = CAST(:pt AS period_type) "
                "  AND (:audited IS NULL OR is_audited = CAST(:audited AS boolean)) "
                "  AND period_end > CAST(:d AS date) - make_interval(months => :m) "
                "ORDER BY period_end DESC LIMIT 1"
            ),
            {
                "t": ctx.ticker_id,
                "pt": expr.get("period_type", "Y"),
                "audited": expr.get("is_audited"),
                "d": ctx.as_of,
                "m": months,
            },
        )
        .mappings()
        .first()
    )

    if found is not None:
        return Evaluation(
            state=FlagState.INACTIVE,
            triggered_values={"found_period_end": str(found["period_end"])},
        )

    # The absence itself is the evidence, so it is cited as such.
    return Evaluation(
        state=FlagState.ACTIVE,
        triggered_values={"within_months": months, "period_type": expr.get("period_type", "Y")},
        evidence=[
            {
                "kind": "data_absence",
                "ref": f"ticker:{ctx.ticker_id}",
                "label": (
                    f"no {expr.get('period_type', 'Y')} statement "
                    f"(audited={expr.get('is_audited')}) within {months} months of {ctx.as_of}"
                ),
                "source_url": None,
            }
        ],
    )
