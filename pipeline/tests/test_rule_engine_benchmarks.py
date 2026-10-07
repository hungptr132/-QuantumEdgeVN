"""`metric_vs_benchmark` against an exchange-scope benchmark.

The subject of most of these is the third state. A flag comparing a figure to a market
median has *two* inputs that can go missing independently — the figure and the median —
and either one absent must produce `unknown` with a reason naming which. `inactive` would
assert that the comparison was made against real data on both sides and did not fire,
which is a claim about the company, not about the pipeline.

The second subject is the `window_label` encoding. An exchange-scope row carries neither
`sector_id` nor `ticker_id`, so the venue has to live in `window_label` or HOSE and HNX
collide on the natural key. The writer (`calc.benchmarks`) and the reader (`calc.rule_engine`)
have to agree on that encoding exactly, and `test_the_writer_and_the_reader_agree_on_the_label`
is the test that would fail if either side changed it alone.

These exercise `rule_engine.evaluate` directly rather than going through `evaluate_flags`,
which keeps `flag_def`, `rules/flags.yaml` and the staleness guard out of the way: the
subject here is the resolution logic, not the rule loader. One test at the end does go
through `evaluate_flags`, to prove an `unknown` benchmark outcome actually persists as an
`unknown` flag_instance row carrying its reason.
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc.benchmarks import build_exchange_benchmarks, exchange_window_label
from quantumedge.calc.flags import evaluate_flags
from quantumedge.calc.rule_engine import RuleContext, evaluate
from quantumedge.config import load_settings
from quantumedge.db.enums import FlagState, PipelineStage
from quantumedge.db.seed import rule_spec_version
from quantumedge.runs import start_run

AS_OF = date(2026, 6, 30)
METRIC = "avg_turnover_20s_vnd"

# Synthetic flag_def rows have to carry the real rules-file hash: `evaluate_flags` refuses
# to run against definitions the YAML on disk has moved past, and a placeholder would make
# every test here exercise the exact state that guard exists to prevent.
CURRENT_SPEC = rule_spec_version(load_settings())


@pytest.fixture
def ctx(session: Session, seed_ticker: int) -> RuleContext:
    return RuleContext(session=session, ticker_id=seed_ticker, symbol="TST", as_of=AS_OF)


@pytest.fixture
def run_id(session: Session) -> int:
    return start_run(session, PipelineStage.CALC, "t")


def _metric(
    session: Session,
    run_id: int,
    ticker_id: int,
    value: Decimal | None,
    *,
    code: str = METRIC,
    reason: str | None = None,
) -> int:
    return int(
        session.execute(
            text(
                "INSERT INTO metric_value (ticker_id, metric_code, as_of_date, value, "
                " is_available, unavailable_reason, inputs, calc_run_id, calc_version) "
                "VALUES (:t, :c, :d, :v, :a, :r, '{}'::jsonb, :run, 'test/v1') RETURNING id"
            ),
            {
                "t": ticker_id,
                "c": code,
                "d": AS_OF,
                "v": value,
                "a": value is not None,
                "r": reason if value is None else None,
                "run": run_id,
            },
        ).scalar_one()
    )


def _exchange_benchmark(
    session: Session,
    run_id: int,
    *,
    exchange: str = "HOSE",
    median: Decimal | None = Decimal(1_000),
    is_available: bool = True,
    unavailable_reason: str | None = None,
    sample_size: int = 9,
    members: list[int] | None = None,
    code: str = METRIC,
    window: str = "current",
) -> int:
    """An exchange-scope row written by hand, so availability can be set independently of
    what a real sample would have produced."""
    return int(
        session.execute(
            text(
                "INSERT INTO metric_benchmark (metric_code, as_of_date, scope, window_label, "
                " median, p25, p75, min_value, max_value, sample_size, is_available, "
                " unavailable_reason, member_ticker_ids, calc_run_id) "
                "VALUES (:c, :d, 'exchange', :label, :median, NULL, NULL, NULL, NULL, :n, "
                " :avail, :reason, :members, :run) RETURNING id"
            ),
            {
                "c": code,
                "d": AS_OF,
                "label": exchange_window_label(exchange, window),
                "median": median,
                "n": sample_size,
                "avail": is_available,
                "reason": unavailable_reason,
                "members": members or [],
                "run": run_id,
            },
        ).scalar_one()
    )


def _rule(
    *,
    scope: str = "exchange",
    window: str = "current",
    stat: str = "median",
    op: str = "<",
    multiple: float = 0.25,
    code: str = "turnover_below_quarter_of_exchange_median",
) -> dict[str, object]:
    return {
        "code": code,
        "category": "liquidity",
        "severity": "watch",
        "rule_kind": "metric_vs_benchmark",
        "rule_expr": {
            "metric": METRIC,
            "op": op,
            "benchmark": {"scope": scope, "window": window, "stat": stat},
            "multiple": multiple,
        },
        "requires_metrics": [METRIC],
        "on_missing": "unknown",
        "rule_version": "v1",
        "rule_spec_version": CURRENT_SPEC,
    }


class TestExchangeBenchmarkResolves:
    def test_it_fires_and_the_evidence_names_the_venue_and_the_sample(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        mv_id = _metric(session, run_id, ctx.ticker_id, Decimal(100))
        b_id = _exchange_benchmark(session, run_id, median=Decimal(1_000), sample_size=9)

        # 100 < 1,000 * 0.25 = 250
        result = evaluate(ctx, _rule())

        assert result.state is FlagState.ACTIVE
        assert result.triggered_values["benchmark_scope"] == "exchange:HOSE"
        assert result.triggered_values["benchmark_median"] == "1000.000000"
        # numeric(24,6) gives the median 6 decimal places, and Decimal multiplication adds
        # the multiple's 2 — so the comparand carries 8. Asserting the exact string is the
        # point: `triggered_values` is the audit trail, and it has to hold the number that
        # was actually compared, not a rounded rendering of it.
        assert result.triggered_values["comparand"] == "250.00000000"
        assert result.triggered_values["sample_size"] == "9"

        refs = [e["ref"] for e in result.evidence]
        assert f"metric_value:{mv_id}" in refs
        assert f"metric_benchmark:{b_id}" in refs
        # "the HOSE median" is checkable; "the exchange median" stops being so the moment
        # coverage spans a second venue.
        label = next(e["label"] for e in result.evidence if e["kind"] == "benchmark")
        assert "exchange:HOSE" in label
        assert "n=9" in label

    def test_a_tested_negative_is_inactive_and_records_both_sides(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        """Both inputs were real and the rule did not fire. This is the one case where
        `inactive` is a true statement."""
        _metric(session, run_id, ctx.ticker_id, Decimal(900))
        _exchange_benchmark(session, run_id, median=Decimal(1_000))

        # 900 < 250 is false.
        result = evaluate(ctx, _rule())

        assert result.state is FlagState.INACTIVE
        assert result.triggered_values["value"] == "900.000000"
        assert result.triggered_values["benchmark_median"] == "1000.000000"
        assert result.evidence == []

    def test_the_writer_and_the_reader_agree_on_the_label(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        """End to end through the real writer, not a hand-written row.

        This is the test that fails if either side of the `window_label` encoding changes
        alone — and that failure mode is otherwise silent, because a mismatched label
        simply finds no row and reports `benchmark_not_computed`, which looks exactly like
        a benchmark nobody computed.
        """
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        for symbol, value in (("AAA", Decimal(1_000)), ("BBB", Decimal(2_000))):
            company_id = session.execute(
                text(
                    "INSERT INTO company (legal_name_vi, short_name) VALUES (:n, :n) RETURNING id"
                ),
                {"n": symbol},
            ).scalar_one()
            peer = int(
                session.execute(
                    text(
                        "INSERT INTO ticker (symbol, company_id, exchange_code, is_covered) "
                        "VALUES (:s, :c, 'HOSE', true) RETURNING id"
                    ),
                    {"s": symbol, "c": company_id},
                ).scalar_one()
            )
            _metric(session, run_id, peer, value)

        build_exchange_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        # Sample is 100, 1000, 2000 -> median 1000; comparand 1000 * 0.25 = 250; 100 < 250.
        result = evaluate(ctx, _rule())
        assert result.state is FlagState.ACTIVE
        assert result.triggered_values["benchmark_median"] == "1000.000000"
        assert result.triggered_values["sample_size"] == "3"

    def test_sector_scope_still_resolves_unchanged(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        """The existing rules in flags.yaml all use sector scope, so the refactor that
        added exchange scope has to leave them alone."""
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        sector_id = session.execute(
            text("SELECT sector_id FROM company c JOIN ticker t ON t.company_id = c.id "),
        ).scalar_one()
        session.execute(
            text(
                "INSERT INTO metric_benchmark (metric_code, as_of_date, scope, sector_id, "
                " window_label, median, sample_size, is_available, calc_run_id) "
                "VALUES (:c, :d, 'sector', :s, 'current', 1000, 3, true, :run)"
            ),
            {"c": METRIC, "d": AS_OF, "s": sector_id, "run": run_id},
        )

        result = evaluate(ctx, _rule(scope="sector"))
        assert result.state is FlagState.ACTIVE
        assert result.triggered_values["benchmark_scope"] == "sector"


class TestAnAbsentBenchmarkIsUnknown:
    """Invariant 4. Every path here must be `unknown` with a reason, never `inactive`, and
    never a comparison against a NULL median."""

    def test_a_benchmark_nobody_computed_is_unknown(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        result = evaluate(ctx, _rule())

        assert result.state is FlagState.UNKNOWN
        assert result.reason == f"benchmark_not_computed:{METRIC}:exchange:HOSE"
        assert result.evidence == []
        assert result.triggered_values == {}

    def test_a_sample_below_the_minimum_is_unknown_and_carries_the_inner_reason(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        """The small-sample path end to end: `build_exchange_benchmarks` writes
        `is_available=false, unavailable_reason='sample_below_minimum'`, and a flag
        depending on it comes out `unknown` naming that reason rather than `inactive`.
        """
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        _exchange_benchmark(
            session,
            run_id,
            median=None,
            is_available=False,
            unavailable_reason="sample_below_minimum",
            sample_size=2,
        )

        result = evaluate(ctx, _rule())

        assert result.state is FlagState.UNKNOWN
        assert result.reason == (
            f"benchmark_unavailable:{METRIC}:exchange:HOSE:sample_below_minimum"
        )

    def test_an_empty_sample_is_unknown_and_distinguishable_from_a_small_one(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        _exchange_benchmark(
            session,
            run_id,
            median=None,
            is_available=False,
            unavailable_reason="no_sample",
            sample_size=0,
        )

        result = evaluate(ctx, _rule())
        assert result.state is FlagState.UNKNOWN
        assert (result.reason or "").endswith(":no_sample")

    def test_an_available_row_with_a_null_median_is_still_unknown(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        """The belt behind the braces. `is_available` is the contract, but a row written
        available-and-empty by some future writer must not reach the comparison, where
        `Decimal(str(None))` raises and `None * multiple` would be worse.
        """
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        _exchange_benchmark(session, run_id, median=None, is_available=True, sample_size=9)

        result = evaluate(ctx, _rule())
        assert result.state is FlagState.UNKNOWN
        assert (result.reason or "").startswith("benchmark_unavailable:")

    def test_another_venues_median_does_not_satisfy_this_ticker(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        """The collision test from the reader's side. The ticker is on HOSE; only an HNX
        row exists. Matching it would compare a HOSE figure against an HNX population —
        two different listing, float and disclosure regimes — and say nothing about it.
        """
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        _exchange_benchmark(session, run_id, exchange="HNX", median=Decimal(1_000))

        result = evaluate(ctx, _rule())
        assert result.state is FlagState.UNKNOWN
        assert result.reason == f"benchmark_not_computed:{METRIC}:exchange:HOSE"

    def test_a_missing_figure_is_unknown_even_when_the_median_is_there(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        _exchange_benchmark(session, run_id, median=Decimal(1_000))
        result = evaluate(ctx, _rule())

        assert result.state is FlagState.UNKNOWN
        assert result.reason == f"metric_not_computed:{METRIC}"

    def test_an_unavailable_figure_carries_its_own_reason_through(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        _metric(session, run_id, ctx.ticker_id, None, reason="missing_input:no_price_bars")
        _exchange_benchmark(session, run_id, median=Decimal(1_000))

        result = evaluate(ctx, _rule())
        assert result.state is FlagState.UNKNOWN
        assert "missing_input:no_price_bars" in (result.reason or "")


class TestTheSpecIsNotGuessedAt:
    """An unrecognised part of a rule expression is `unknown`. Falling back to a default
    would answer a question the rule did not ask."""

    def test_a_stat_other_than_the_median_is_refused_rather_than_approximated(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        """p25/p75 are interpolations at these sample sizes (see
        calc.benchmarks.MIN_EXACT_QUARTILE_SAMPLE), so a flag keyed to one would fire on an
        arithmetic artefact. Quietly substituting the median would be worse: the rule as
        written and the rule as applied would differ, with nothing to show it.
        """
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        _exchange_benchmark(session, run_id, median=Decimal(1_000))

        result = evaluate(ctx, _rule(stat="p75"))
        assert result.state is FlagState.UNKNOWN
        assert result.reason == "benchmark_stat_not_supported:p75"

    def test_an_unsupported_scope_is_refused(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        """`ticker_history` rows exist but comparing a figure to a quantile of its own past
        is a different rule shape. Before this was explicit it fell through to the sector
        query and matched nothing, which reported `benchmark_not_computed` — the right
        state for the wrong reason, and only by accident."""
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        result = evaluate(ctx, _rule(scope="ticker_history", window="3y"))

        assert result.state is FlagState.UNKNOWN
        assert result.reason == "benchmark_scope_not_supported:ticker_history"

    def test_a_window_nobody_wrote_is_unknown_not_a_nearby_window(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        _exchange_benchmark(session, run_id, window="current", median=Decimal(1_000))

        result = evaluate(ctx, _rule(window="1y"))
        assert result.state is FlagState.UNKNOWN
        assert result.reason == f"benchmark_not_computed:{METRIC}:exchange:HOSE"

    def test_an_unknown_operator_does_not_silently_pass(
        self, session: Session, ctx: RuleContext, run_id: int, metric_defs: int
    ) -> None:
        _metric(session, run_id, ctx.ticker_id, Decimal(100))
        _exchange_benchmark(session, run_id, median=Decimal(1_000))

        result = evaluate(ctx, _rule(op="~="))
        assert result.state is FlagState.UNKNOWN
        assert result.reason == "unknown_operator:~="


class TestItPersistsAsAnUnknownFlag:
    """The state has to survive the write, not just the evaluation. A reason computed and
    then dropped on the floor would leave a page with an unexplained blank, which invites
    the assumption that the comparison was unfavourable and withheld."""

    def test_an_unavailable_exchange_benchmark_writes_an_unknown_row_with_its_reason(
        self, session: Session, seed_ticker: int, run_id: int, metric_defs: int
    ) -> None:
        _metric(session, run_id, seed_ticker, Decimal(100))
        _exchange_benchmark(
            session,
            run_id,
            median=None,
            is_available=False,
            unavailable_reason="sample_below_minimum",
            sample_size=2,
        )
        rule = _rule()
        session.execute(
            text(
                "INSERT INTO flag_def (code, name_vi, category, severity, rule_kind, "
                " rule_expr, requires_metrics, on_missing, explanation_vi, rule_version, "
                " rule_spec_version) "
                "VALUES (:code, 'Giá trị giao dịch dưới trung vị sàn', 'liquidity', 'watch', "
                " 'metric_vs_benchmark', CAST(:expr AS jsonb), :req, 'unknown', "
                " 'Giải thích', 'v1', :spec)"
            ),
            {
                "code": rule["code"],
                "expr": json.dumps(rule["rule_expr"]),
                "req": [METRIC],
                "spec": CURRENT_SPEC,
            },
        )

        outcome = evaluate_flags(session, run_id, AS_OF, symbols=["TST"])
        assert outcome.unknown == 1
        assert outcome.active == 0

        row = session.execute(
            text(
                "SELECT state::text AS state, reason, evidence FROM flag_instance "
                "WHERE ticker_id = :t AND flag_code = :c"
            ),
            {"t": seed_ticker, "c": rule["code"]},
        ).one()
        assert row.state == "unknown"
        assert row.reason == (f"benchmark_unavailable:{METRIC}:exchange:HOSE:sample_below_minimum")
        # An unknown flag carries no evidence, and the CHECK only demands it of active
        # ones — there is nothing to cite when the comparison never happened.
        assert list(row.evidence or []) == []
