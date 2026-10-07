"""Flag engine tests.

The three-valued state is the subject of most of these. Getting it wrong is not a cosmetic
bug: reporting `inactive` when an input was never available tells a reader a rule was
checked and passed, when in truth nothing was checked at all.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc.flags import evaluate_flags
from quantumedge.calc.rule_engine import RuleContext, evaluate
from quantumedge.config import load_settings
from quantumedge.db.enums import FlagState, PipelineStage
from quantumedge.db.seed import rule_spec_version
from quantumedge.runs import start_run

AS_OF = date(2026, 6, 30)

# Synthetic rules inserted by hand still have to carry the CURRENT rules-file hash,
# because `evaluate_flags` refuses to run against definitions the YAML has moved past.
# A hardcoded "spec1" looked harmless and made every one of these tests exercise the
# exact state that guard exists to prevent.
CURRENT_SPEC = rule_spec_version(load_settings())


def _metric(
    session: Session,
    ticker_id: int,
    code: str,
    value: Decimal | None,
    *,
    available: bool = True,
    reason: str | None = None,
    as_of: date = AS_OF,
) -> int:
    run_id = start_run(session, PipelineStage.CALC, "t")
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
                "d": as_of,
                "v": value,
                "a": available,
                "r": reason,
                "run": run_id,
            },
        ).scalar_one()
    )


def _threshold_rule(code: str = "test_flag", metric: str = "price_to_par") -> dict:  # type: ignore[type-arg]
    return {
        "code": code,
        "category": "valuation",
        "severity": "watch",
        "rule_kind": "metric_threshold",
        "rule_expr": {"metric": metric, "op": "<", "value": 1.0},
        "requires_metrics": [metric],
        "on_missing": "unknown",
        "rule_version": "v1",
        "rule_spec_version": CURRENT_SPEC,
    }


@pytest.fixture
def ctx(session: Session, seed_ticker: int) -> RuleContext:
    return RuleContext(session=session, ticker_id=seed_ticker, symbol="TST", as_of=AS_OF)


class TestThreeValuedState:
    def test_fires_and_attaches_resolvable_evidence(
        self, session: Session, ctx: RuleContext, metric_defs: int
    ) -> None:
        mv_id = _metric(session, ctx.ticker_id, "price_to_par", Decimal("0.21"))
        result = evaluate(ctx, _threshold_rule())

        assert result.state is FlagState.ACTIVE
        assert result.evidence, "an active flag must carry evidence"
        assert result.evidence[0]["ref"] == f"metric_value:{mv_id}"
        # The label has to stand alone: a reader should see the comparison, not a bare code.
        assert "0.21" in result.evidence[0]["label"]

    def test_a_tested_negative_is_inactive_and_records_what_was_tested(
        self, session: Session, ctx: RuleContext, metric_defs: int
    ) -> None:
        _metric(session, ctx.ticker_id, "price_to_par", Decimal("4.99"))
        result = evaluate(ctx, _threshold_rule())

        assert result.state is FlagState.INACTIVE
        assert result.triggered_values["value"] == "4.990000"
        assert result.triggered_values["threshold"] == "1.0"

    def test_a_metric_that_was_never_computed_is_unknown(
        self, session: Session, ctx: RuleContext
    ) -> None:
        """Not inactive. Nothing was tested, so claiming the rule passed would be false."""
        result = evaluate(ctx, _threshold_rule())
        assert result.state is FlagState.UNKNOWN
        assert result.reason == "metric_not_computed:price_to_par"
        assert result.evidence == []

    def test_a_metric_computed_as_unavailable_is_unknown_with_the_inner_reason(
        self, session: Session, ctx: RuleContext, metric_defs: int
    ) -> None:
        """The underlying reason is carried through, so the page can say *why* rather than
        showing one undifferentiated blank."""
        _metric(
            session,
            ctx.ticker_id,
            "sessions_to_exit",
            None,
            available=False,
            reason="no_turnover_in_a_typical_session_cannot_estimate_exit",
        )
        rule = _threshold_rule(metric="sessions_to_exit")
        result = evaluate(ctx, rule)

        assert result.state is FlagState.UNKNOWN
        assert "no_turnover_in_a_typical_session" in (result.reason or "")

    def test_an_unknown_operator_does_not_silently_pass(
        self, session: Session, ctx: RuleContext, metric_defs: int
    ) -> None:
        _metric(session, ctx.ticker_id, "price_to_par", Decimal("0.5"))
        rule = _threshold_rule()
        rule["rule_expr"] = {"metric": "price_to_par", "op": "~=", "value": 1.0}
        result = evaluate(ctx, rule)
        assert result.state is FlagState.UNKNOWN
        assert "unknown_operator" in (result.reason or "")


class TestRulesWithNoSourceYet:
    def test_event_rules_are_unknown_not_inactive_without_an_event_source(
        self, session: Session, ctx: RuleContext
    ) -> None:
        """'No auditor change was disclosed' and 'we cannot know whether one was' are
        different statements. Only the second is true today, and the page must say so."""
        rule = {
            "code": "auditor_change_within_24m",
            "rule_kind": "event_presence",
            "severity": "high",
            "rule_expr": {"event_type": "auditor_change", "within_months": 24},
            "on_missing": "unknown",
            "rule_spec_version": CURRENT_SPEC,
        }
        result = evaluate(ctx, rule)
        assert result.state is FlagState.UNKNOWN
        assert result.reason == "no_event_source_configured"

    def test_absence_rules_are_unknown_without_a_statement_source(
        self, session: Session, ctx: RuleContext
    ) -> None:
        """Otherwise every ticker would look non-compliant purely because nothing has been
        ingested - firing for the wrong reason is as bad as not firing."""
        rule = {
            "code": "no_audited_annual_statement_18m",
            "rule_kind": "data_absence",
            "severity": "high",
            "rule_expr": {
                "absent": "financial_period",
                "period_type": "Y",
                "is_audited": True,
                "within_months": 18,
            },
            "on_missing": "unknown",
            "rule_spec_version": CURRENT_SPEC,
        }
        result = evaluate(ctx, rule)
        assert result.state is FlagState.UNKNOWN
        assert result.reason == "no_statement_source_configured"

    def test_an_unimplemented_rule_kind_is_unknown(
        self, session: Session, ctx: RuleContext
    ) -> None:
        rule = {
            "code": "future",
            "rule_kind": "composite",
            "severity": "info",
            "rule_expr": {},
            "on_missing": "unknown",
            "rule_spec_version": CURRENT_SPEC,
        }
        result = evaluate(ctx, rule)
        assert result.state is FlagState.UNKNOWN
        assert "not_implemented" in (result.reason or "")


class TestPersistence:
    def _seeded_rule(self, session: Session) -> None:
        # rule_expr goes in as a bind parameter, not inlined: text() would read the colon in
        # a JSON pair like "value":1.0 as a bind placeholder.
        session.execute(
            text(
                "INSERT INTO flag_def (code, name_vi, category, severity, rule_kind, "
                " rule_expr, requires_metrics, on_missing, explanation_vi, rule_version, "
                " rule_spec_version) "
                "VALUES ('price_below_par_value', 'Giá dưới mệnh giá', 'valuation', 'watch', "
                " 'metric_threshold', CAST(:expr AS jsonb), "
                " ARRAY['price_to_par'], 'unknown', 'Giải thích', 'v1', :spec) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {
                "expr": json.dumps({"metric": "price_to_par", "op": "<", "value": 1.0}),
                "spec": CURRENT_SPEC,
            },
        )

    def test_every_rule_produces_a_row_for_every_ticker(
        self, session: Session, seed_ticker: int, metric_defs: int
    ) -> None:
        """Including inactive and unknown ones: the set of rules considered is part of the
        record, and an absent row could not distinguish 'did not fire' from 'not run'."""
        self._seeded_rule(session)
        _metric(session, seed_ticker, "price_to_par", Decimal("0.21"))
        run_id = start_run(session, PipelineStage.CALC, "t")

        outcome = evaluate_flags(session, run_id, AS_OF, symbols=["TST"])
        assert outcome.written == 1
        assert outcome.active == 1

        row = session.execute(
            text(
                "SELECT state, jsonb_array_length(evidence) AS ev FROM flag_instance "
                "WHERE ticker_id = :t"
            ),
            {"t": seed_ticker},
        ).one()
        assert row.state == "active"
        assert row.ev == 1

    def test_rerunning_updates_rather_than_duplicating(
        self, session: Session, seed_ticker: int, metric_defs: int
    ) -> None:
        self._seeded_rule(session)
        _metric(session, seed_ticker, "price_to_par", Decimal("0.21"))
        for _ in range(2):
            run_id = start_run(session, PipelineStage.CALC, "t")
            evaluate_flags(session, run_id, AS_OF, symbols=["TST"])
        count = session.execute(
            text("SELECT count(*) FROM flag_instance WHERE ticker_id = :t"), {"t": seed_ticker}
        ).scalar_one()
        assert count == 1

    def test_a_state_change_between_dates_is_recorded(
        self, session: Session, seed_ticker: int, metric_defs: int
    ) -> None:
        """What the alerting layer will read: the spec's point is that the transition is
        more interesting than the snapshot."""
        self._seeded_rule(session)
        earlier = AS_OF - timedelta(days=1)

        # Day one: trading above par, so the rule does not fire.
        _metric(session, seed_ticker, "price_to_par", Decimal("1.50"), as_of=earlier)
        run_id = start_run(session, PipelineStage.CALC, "t")
        evaluate_flags(session, run_id, earlier, symbols=["TST"])

        # Day two: now below par.
        _metric(session, seed_ticker, "price_to_par", Decimal("0.90"), as_of=AS_OF)
        run_id = start_run(session, PipelineStage.CALC, "t")
        outcome = evaluate_flags(session, run_id, AS_OF, symbols=["TST"])

        assert outcome.transitions == 1
        change = session.execute(
            text(
                "SELECT from_state, to_state, changed_on FROM flag_state_change "
                "WHERE ticker_id = :t"
            ),
            {"t": seed_ticker},
        ).one()
        assert change.from_state == "inactive"
        assert change.to_state == "active"
        assert change.changed_on == AS_OF

    def test_no_active_rules_is_an_error_not_a_silent_pass(
        self, session: Session, seed_ticker: int
    ) -> None:
        """An empty rule set would otherwise report every ticker as having no flags, which
        reads identically to a clean bill of health."""
        session.execute(text("UPDATE flag_def SET is_active = false"))
        run_id = start_run(session, PipelineStage.CALC, "t")
        with pytest.raises(LookupError, match="no active flag rules"):
            evaluate_flags(session, run_id, AS_OF, symbols=["TST"])


class TestEvidenceLabelsAreExactAndLegible:
    """The evidence block is the audit trail, so a label must carry the value actually
    compared — never a rounded one. But `numeric(24,6)` scale is not precision."""

    def test_column_scale_is_stripped_without_changing_the_number(self) -> None:
        from quantumedge.calc.rule_engine import _exact

        assert _exact(Decimal("40435850.000000")) == "40,435,850"
        assert _exact(Decimal("6215976000.000000")) == "6,215,976,000"
        assert _exact(Decimal("0.000000")) == "0"

    def test_real_precision_is_kept(self) -> None:
        from quantumedge.calc.rule_engine import _exact

        assert _exact(Decimal("0.0158")) == "0.0158"
        assert _exact(Decimal("-0.4324")) == "-0.4324"
        assert _exact(Decimal("0.25")) == "0.25"

    def test_large_values_never_render_in_exponent_form(self) -> None:
        """Decimal.normalize() turns 40435850.000000 into 4.043585E+7, which is less
        readable than the trailing zeros it removed, not more."""
        from quantumedge.calc.rule_engine import _exact

        for raw in ("1000000000", "40435850.000000", "6215976000.000000"):
            assert "E" not in _exact(Decimal(raw))
