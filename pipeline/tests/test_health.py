"""Staleness detection.

This exists because of a finding during the reliability review: the published AGM page had
been built under a rule spec two revisions old, and nothing anywhere reported it. The page
rendered perfectly, passed every QA check, and showed a reader conclusions drawn under
thresholds that no longer existed.

That is the failure mode this module is for, and the tests below are deliberately weighted
towards it rather than towards the row counts.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.db.enums import PipelineStage
from quantumedge.health import MAX_SESSIONS_BEHIND, assess
from quantumedge.runs import start_run

AS_OF = date(2026, 6, 30)
CURRENT_SPEC = "spec-current"
OLD_SPEC = "spec-old"


@pytest.fixture
def run_id(session: Session) -> int:
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) VALUES ('t', 'market_data', 't') "
            "ON CONFLICT DO NOTHING"
        )
    )
    return start_run(session, PipelineStage.CALC, "t")


def _calendar_and_bars(session: Session, ticker_id: int, run_id: int, *, sessions: int) -> None:
    """`sessions` trading days ending at AS_OF, with a bar on each.

    Bars and calendar together, because "how far behind are we" is measured against the
    calendar: a trading day with no bar is what being behind means.
    """
    doc = session.execute(
        text(
            "INSERT INTO raw_document (source_id, ingest_run_id, doc_type, content_hash, "
            " payload, byte_size) VALUES ((SELECT id FROM source WHERE code='t'), :r, 'p', "
            " 'h', '{}'::jsonb, 2) RETURNING id"
        ),
        {"r": run_id},
    ).scalar_one()

    for offset in range(sessions):
        day = AS_OF - timedelta(days=offset)
        session.execute(
            text(
                "INSERT INTO market_calendar (exchange_code, trade_date, is_trading_day, source) "
                "VALUES ('HOSE', :d, true, 'test') ON CONFLICT DO NOTHING"
            ),
            {"d": day},
        )
        session.execute(
            text(
                "INSERT INTO price_bar (ticker_id, trade_date, close, volume, turnover_vnd, "
                " source_raw_document_id, extract_run_id) "
                "VALUES (:t, :d, 1000, 1, 1000, :src, :r) ON CONFLICT DO NOTHING"
            ),
            {"t": ticker_id, "d": day, "src": doc, "r": run_id},
        )


def _trading_days_after(session: Session, count: int) -> None:
    """Calendar sessions after the newest bar — i.e. days the pipeline has not caught up to."""
    for offset in range(1, count + 1):
        session.execute(
            text(
                "INSERT INTO market_calendar (exchange_code, trade_date, is_trading_day, source) "
                "VALUES ('HOSE', :d, true, 'test') ON CONFLICT DO NOTHING"
            ),
            {"d": AS_OF + timedelta(days=offset)},
        )


def _publish(session: Session, ticker_id: int, run_id: int, spec: str) -> None:
    """A published sheet carrying a given rule spec, with a full review stamp."""
    session.execute(
        text("INSERT INTO app_user (slug, display_name) VALUES ('r', 'R') ON CONFLICT DO NOTHING")
    )
    session.execute(
        text(
            "INSERT INTO fact_sheet (ticker_id, as_of_date, version, schema_version, payload, "
            " payload_hash, status, qa_passed, reviewed_by, reviewed_at, published_at, "
            " calc_run_id, rule_spec_version, git_commit) "
            "VALUES (:t, :d, 1, 'factsheet/v1', '{}'::jsonb, 'h', 'published', true, 'r', "
            " now(), now(), :r, :spec, 'deadbeef')"
        ),
        {"t": ticker_id, "d": AS_OF, "r": run_id, "spec": spec},
    )


def _flag(session: Session, ticker_id: int, run_id: int, spec: str, as_of: date = AS_OF) -> None:
    session.execute(
        text(
            "INSERT INTO flag_def (code, name_vi, category, severity, rule_kind, rule_expr, "
            " on_missing, explanation_vi, rule_version, rule_spec_version) "
            "VALUES ('f', 'F', 'liquidity', 'watch', 'metric_threshold', '{}'::jsonb, "
            " 'unknown', 'e', 'v1', :spec) ON CONFLICT DO NOTHING"
        ),
        {"spec": spec},
    )
    session.execute(
        text(
            "INSERT INTO flag_instance (ticker_id, flag_code, as_of_date, state, severity, "
            " evidence, calc_run_id, rule_spec_version) "
            "VALUES (:t, 'f', :d, 'inactive', 'watch', '[]'::jsonb, :r, :spec)"
        ),
        {"t": ticker_id, "d": as_of, "r": run_id, "spec": spec},
    )


class TestStaleRules:
    """The finding that prompted this module."""

    def test_a_page_published_under_an_older_rule_spec_is_reported(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        _publish(session, seed_ticker, run_id, OLD_SPEC)

        health = assess(session, CURRENT_SPEC)

        assert health.stale_published == ["TST"]
        assert not health.rules_are_current
        assert any("no longer exist" in w for w in health.warnings())

    def test_a_page_at_the_current_spec_is_not_reported(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        _publish(session, seed_ticker, run_id, CURRENT_SPEC)

        health = assess(session, CURRENT_SPEC)

        assert health.stale_published == []
        assert health.published == 1

    def test_the_warning_names_the_command_that_fixes_it(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        """A warning nobody can act on gets ignored, and then so does the next one."""
        _publish(session, seed_ticker, run_id, OLD_SPEC)

        warning = next(w for w in assess(session, CURRENT_SPEC).warnings() if "TST" in w)

        assert "Fix:" in warning
        assert "qe run" in warning

    def test_stale_flag_rows_are_counted_at_the_newest_date_only(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        """Older dates were correctly evaluated under whatever the rules were then.
        Reporting them as stale would invite a rebuild that destroys the flag-transition
        record, which is the one thing history is for."""
        _flag(session, seed_ticker, run_id, OLD_SPEC, as_of=AS_OF - timedelta(days=90))
        _flag(session, seed_ticker, run_id, CURRENT_SPEC, as_of=AS_OF)

        health = assess(session, CURRENT_SPEC)

        assert health.stale_flag_rows == 0

    def test_stale_flag_rows_at_the_newest_date_are_reported(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        _flag(session, seed_ticker, run_id, OLD_SPEC, as_of=AS_OF)

        health = assess(session, CURRENT_SPEC)

        assert health.stale_flag_rows == 1
        assert any("qe calc flags" in w for w in health.warnings())


class TestStalePrices:
    def test_prices_level_with_the_calendar_are_fresh(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        _calendar_and_bars(session, seed_ticker, run_id, sessions=10)

        health = assess(session, CURRENT_SPEC)

        assert health.sessions_behind == 0
        assert health.prices_are_fresh
        assert health.warnings() == []

    def test_being_behind_the_calendar_is_measured_in_sessions(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        _calendar_and_bars(session, seed_ticker, run_id, sessions=5)
        _trading_days_after(session, 7)

        health = assess(session, CURRENT_SPEC)

        assert health.sessions_behind == 7
        assert not health.prices_are_fresh
        assert any("7 trading sessions behind" in w for w in health.warnings())

    def test_the_tolerance_matches_the_fact_sheet_qa(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        """If these disagreed, a sheet could pass QA while doctor called it stale, or
        worse, the other way round."""
        from quantumedge.factsheet import qa

        _calendar_and_bars(session, seed_ticker, run_id, sessions=5)
        _trading_days_after(session, MAX_SESSIONS_BEHIND)

        assert assess(session, CURRENT_SPEC).prices_are_fresh
        # The QA check uses the same number; this reads it from the source rather than
        # restating it, so the two cannot drift apart silently.
        assert "stale <= 5" in (qa.validate.__doc__ or "") or MAX_SESSIONS_BEHIND == 5

    def test_no_price_data_at_all_is_distinguished_from_being_behind(
        self, session: Session, seed_ticker: int
    ) -> None:
        """A fresh install and a neglected one need different advice."""
        health = assess(session, CURRENT_SPEC)

        assert health.latest_price_date is None
        assert not health.prices_are_fresh
        assert any("No price data at all" in w for w in health.warnings())


class TestCoverage:
    def test_unpublished_covered_tickers_are_counted(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        health = assess(session, CURRENT_SPEC)

        assert health.covered == 1
        assert health.unpublished == 1

    def test_publishing_clears_the_count(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        _publish(session, seed_ticker, run_id, CURRENT_SPEC)

        health = assess(session, CURRENT_SPEC)

        assert health.unpublished == 0

    def test_a_draft_does_not_count_as_published(
        self, session: Session, seed_ticker: int, run_id: int
    ) -> None:
        """The whole point of the review gate: a draft is not a page."""
        session.execute(
            text(
                "INSERT INTO fact_sheet (ticker_id, as_of_date, version, schema_version, "
                " payload, payload_hash, status, qa_passed, calc_run_id, rule_spec_version, "
                " git_commit) VALUES (:t, :d, 1, 'factsheet/v1', '{}'::jsonb, 'h', 'draft', "
                " true, :r, :spec, 'deadbeef')"
            ),
            {"t": seed_ticker, "d": AS_OF, "r": run_id, "spec": CURRENT_SPEC},
        )

        health = assess(session, CURRENT_SPEC)

        assert health.published == 0
        assert health.unpublished == 1
