"""Evaluating flags against stale rule definitions.

Found while reporting status, which is the embarrassing part: `qe doctor` said 117 flag
rows were stale and printed `Fix: qe calc flags`. Running exactly that changed nothing,
because `flag_def` is a mirror of `rules/flags.yaml` refreshed only by `qe db seed`, and
`flag_instance` inherits its `rule_spec_version` from the definition row it was evaluated
against.

So editing a threshold and recomputing produced flags from the OLD threshold, stamped with
the OLD version, with no error anywhere — and the remediation the tool suggested could not
have fixed it. `qe run` did not seed either, so the documented `Fix: qe run` was no better.

The calc stage now refuses in that state rather than producing quietly wrong conclusions.
"""

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc.flags import StaleRuleDefinitions, evaluate_flags
from quantumedge.config import load_settings
from quantumedge.db.enums import PipelineStage
from quantumedge.db.seed import rule_spec_version, seed_flags
from quantumedge.runs import start_run

AS_OF = date(2026, 6, 30)


@pytest.fixture
def run_id(session: Session) -> int:
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) VALUES ('t', 'market_data', 't') "
            "ON CONFLICT DO NOTHING"
        )
    )
    return start_run(session, PipelineStage.CALC, "t")


class TestStaleDefinitionsAreRefused:
    def test_evaluating_against_an_older_rule_spec_raises(
        self, session: Session, seed_ticker: int, metric_defs: int, run_id: int
    ) -> None:
        """The exact state that went unnoticed: definitions loaded from a rules file the
        YAML on disk has since moved past."""
        settings = load_settings()
        seed_flags(session, settings)
        session.execute(
            text("UPDATE flag_def SET rule_spec_version = 'an-older-hash' WHERE is_active")
        )

        with pytest.raises(StaleRuleDefinitions, match="Run `qe db seed` first"):
            evaluate_flags(session, run_id, AS_OF)

    def test_the_message_names_both_versions(
        self, session: Session, seed_ticker: int, metric_defs: int, run_id: int
    ) -> None:
        """So the reader can tell which way round it is, rather than guessing whether the
        YAML or the database is ahead."""
        settings = load_settings()
        seed_flags(session, settings)
        session.execute(
            text("UPDATE flag_def SET rule_spec_version = 'deadbeefdeadbeef' WHERE is_active")
        )

        with pytest.raises(StaleRuleDefinitions) as caught:
            evaluate_flags(session, run_id, AS_OF)

        message = str(caught.value)
        assert "deadbeef" in message
        assert rule_spec_version(settings)[:12] in message

    def test_fresh_definitions_evaluate_normally(
        self, session: Session, seed_ticker: int, metric_defs: int, run_id: int
    ) -> None:
        """The guard must not block the ordinary case."""
        seed_flags(session, load_settings())

        outcome = evaluate_flags(session, run_id, AS_OF)

        assert outcome.written > 0

    def test_a_mix_of_versions_is_also_refused(
        self, session: Session, seed_ticker: int, metric_defs: int, run_id: int
    ) -> None:
        """A partial seed is worse than a stale one: some rules would apply new thresholds
        and others old, inside a single sheet. QA check 6 exists for the same reason."""
        seed_flags(session, load_settings())
        session.execute(
            text(
                "UPDATE flag_def SET rule_spec_version = 'half-updated' "
                "WHERE code = (SELECT min(code) FROM flag_def)"
            )
        )

        with pytest.raises(StaleRuleDefinitions):
            evaluate_flags(session, run_id, AS_OF)

    def test_an_inactive_definition_does_not_trigger_the_guard(
        self, session: Session, seed_ticker: int, metric_defs: int, run_id: int
    ) -> None:
        """A retired rule keeps whatever version it was retired under. It is not evaluated,
        so it cannot make the output wrong, and blocking on it would be a false alarm
        nobody could clear."""
        seed_flags(session, load_settings())
        session.execute(
            text(
                "UPDATE flag_def SET is_active = false, rule_spec_version = 'retired-hash' "
                "WHERE code = (SELECT min(code) FROM flag_def)"
            )
        )

        outcome = evaluate_flags(session, run_id, AS_OF)

        assert outcome.written > 0


class TestTheRemediationAdviceIsActionable:
    def test_the_stale_flag_warning_names_the_seed_step(self, session: Session) -> None:
        """The whole bug in one assertion: the advice used to say `qe calc flags`, which
        re-evaluated against the same stale definitions and cleared nothing."""
        from quantumedge.health import Health

        health = Health(rule_spec_version="abc", stale_flag_rows=117)
        warning = next(w for w in health.warnings() if "flag rows" in w)

        assert "qe db seed" in warning
        assert warning.index("qe db seed") < warning.index("qe calc flags")

    def test_the_run_chain_seeds_before_it_calculates(self) -> None:
        """`qe run` omitted seeding entirely, so the documented `Fix: qe run` could not
        clear the staleness either."""
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "src" / "quantumedge" / "cli.py"
        text_ = source.read_text(encoding="utf-8")
        chain = text_[text_.index("steps: list[tuple[str, list[str]]] = [") :]
        chain = chain[: chain.index("for label, args in steps")]

        assert '["db", "seed"]' in chain, "qe run must seed the rule definitions"
        assert chain.index('["db", "seed"]') < chain.index('["calc", "flags"]')
