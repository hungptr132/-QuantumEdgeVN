"""The invariants, asserted against a real database.

Each test here corresponds to an invariant in CLAUDE.md. They exist because "enforced by
the database" is a claim that should fail loudly if it ever stops being true.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session


def _raw_document(session: Session, run_id: int) -> int:
    source_id = session.execute(
        text(
            "INSERT INTO source (code, kind, name) VALUES ('test_src', 'market_data', 'Test') "
            "RETURNING id"
        )
    ).scalar_one()
    return int(
        session.execute(
            text(
                "INSERT INTO raw_document "
                "(source_id, ingest_run_id, doc_type, content_hash, payload, byte_size) "
                "VALUES (:s, :r, 'price_history', 'abc123', '{}'::jsonb, 2) RETURNING id"
            ),
            {"s": source_id, "r": run_id},
        ).scalar_one()
    )


class TestRawDataIsAppendOnly:
    """Invariant 1: raw data is append-only, enforced by trigger."""

    def test_update_is_rejected(self, session: Session, seed_run: int) -> None:
        doc_id = _raw_document(session, seed_run)
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(
                text("UPDATE raw_document SET byte_size = 99 WHERE id = :i"), {"i": doc_id}
            )

    def test_delete_is_rejected(self, session: Session, seed_run: int) -> None:
        doc_id = _raw_document(session, seed_run)
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(text("DELETE FROM raw_document WHERE id = :i"), {"i": doc_id})

    def test_identical_content_does_not_duplicate(self, session: Session, seed_run: int) -> None:
        """Idempotency: the same payload from the same place inserts once."""
        _raw_document(session, seed_run)
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO raw_document "
                    "(source_id, ingest_run_id, doc_type, content_hash, payload, byte_size) "
                    "SELECT source_id, ingest_run_id, doc_type, content_hash, payload, byte_size "
                    "FROM raw_document LIMIT 1"
                )
            )


class TestPublicationRequiresAHumanStamp:
    """Invariant 5: nothing publishes without review. This is the product's core promise."""

    def _draft(self, session: Session, ticker_id: int, run_id: int, version: int = 1) -> int:
        return int(
            session.execute(
                text(
                    "INSERT INTO fact_sheet (ticker_id, as_of_date, version, schema_version, "
                    "payload, payload_hash, calc_run_id, rule_spec_version, git_commit) "
                    "VALUES (:t, '2026-09-30', :v, 'factsheet/v1', '{}'::jsonb, 'h1', :r, "
                    "'rs1', 'deadbeef') RETURNING id"
                ),
                {"t": ticker_id, "v": version, "r": run_id},
            ).scalar_one()
        )

    def test_publish_without_reviewer_is_rejected(
        self, session: Session, seed_ticker: int, seed_run: int
    ) -> None:
        fs_id = self._draft(session, seed_ticker, seed_run)
        with pytest.raises(IntegrityError, match="fs_publish_requires_stamp"):
            session.execute(
                text(
                    "UPDATE fact_sheet SET status='published', qa_passed=true, "
                    "published_at=now() WHERE id=:i"
                ),
                {"i": fs_id},
            )

    def test_publish_without_passing_qa_is_rejected(
        self, session: Session, seed_ticker: int, seed_run: int
    ) -> None:
        session.execute(
            text("INSERT INTO app_user (slug, display_name) VALUES ('tester', 'Tester')")
        )
        fs_id = self._draft(session, seed_ticker, seed_run)
        with pytest.raises(IntegrityError, match="fs_publish_requires_stamp"):
            session.execute(
                text(
                    "UPDATE fact_sheet SET status='published', qa_passed=false, "
                    "reviewed_by='tester', reviewed_at=now(), published_at=now() WHERE id=:i"
                ),
                {"i": fs_id},
            )

    def test_fully_stamped_publish_succeeds(
        self, session: Session, seed_ticker: int, seed_run: int
    ) -> None:
        session.execute(
            text("INSERT INTO app_user (slug, display_name) VALUES ('tester', 'Tester')")
        )
        fs_id = self._draft(session, seed_ticker, seed_run)
        session.execute(
            text(
                "UPDATE fact_sheet SET status='published', qa_passed=true, "
                "reviewed_by='tester', reviewed_at=now(), published_at=now() WHERE id=:i"
            ),
            {"i": fs_id},
        )
        status = session.execute(
            text("SELECT status FROM fact_sheet WHERE id=:i"), {"i": fs_id}
        ).scalar_one()
        assert status == "published"

    def test_only_one_published_version_per_ticker(
        self, session: Session, seed_ticker: int, seed_run: int
    ) -> None:
        session.execute(
            text("INSERT INTO app_user (slug, display_name) VALUES ('tester', 'Tester')")
        )
        first = self._draft(session, seed_ticker, seed_run, version=1)
        second = self._draft(session, seed_ticker, seed_run, version=2)
        publish = (
            "UPDATE fact_sheet SET status='published', qa_passed=true, reviewed_by='tester', "
            "reviewed_at=now(), published_at=now() WHERE id=:i"
        )
        session.execute(text(publish), {"i": first})
        with pytest.raises(IntegrityError, match="fact_sheet_one_published_per_ticker"):
            session.execute(text(publish), {"i": second})

    def test_rejection_requires_a_reason(
        self, session: Session, seed_ticker: int, seed_run: int
    ) -> None:
        fs_id = self._draft(session, seed_ticker, seed_run)
        with pytest.raises(IntegrityError, match="fs_reject_requires_reason"):
            session.execute(
                text("UPDATE fact_sheet SET status='rejected' WHERE id=:i"), {"i": fs_id}
            )


class TestNegativeClaimsNeedEvidence:
    """The defamation guardrail: an active flag against a named company must carry
    something to point at."""

    def _flag_def(self, session: Session) -> None:
        session.execute(
            text(
                "INSERT INTO flag_def (code, name_vi, category, severity, rule_kind, "
                "rule_expr, explanation_vi, rule_version, rule_spec_version) "
                "VALUES ('test_flag', 'Cờ kiểm thử', 'leverage', 'high', "
                "'metric_threshold', '{}'::jsonb, 'Giải thích', 'v1', 'rs1')"
            )
        )

    def test_active_flag_without_evidence_is_rejected(
        self, session: Session, seed_ticker: int, seed_run: int
    ) -> None:
        self._flag_def(session)
        with pytest.raises(IntegrityError, match="flag_active_needs_evidence"):
            session.execute(
                text(
                    "INSERT INTO flag_instance (ticker_id, flag_code, as_of_date, state, "
                    "severity, evidence, calc_run_id, rule_spec_version) "
                    "VALUES (:t, 'test_flag', '2026-09-30', 'active', 'high', "
                    "'[]'::jsonb, :r, 'rs1')"
                ),
                {"t": seed_ticker, "r": seed_run},
            )

    def test_unknown_state_needs_no_evidence(
        self, session: Session, seed_ticker: int, seed_run: int
    ) -> None:
        """Missing inputs must be recordable as unknown — that is the whole point of the
        three-valued state."""
        self._flag_def(session)
        session.execute(
            text(
                "INSERT INTO flag_instance (ticker_id, flag_code, as_of_date, state, "
                "severity, evidence, reason, calc_run_id, rule_spec_version) "
                "VALUES (:t, 'test_flag', '2026-09-30', 'unknown', 'high', '[]'::jsonb, "
                "'no_event_source_configured', :r, 'rs1')"
            ),
            {"t": seed_ticker, "r": seed_run},
        )
        state = session.execute(
            text("SELECT state FROM flag_instance WHERE ticker_id=:t"), {"t": seed_ticker}
        ).scalar_one()
        assert state == "unknown"


class TestMetricsAreNeverSilentlyEmpty:
    """A metric is either a value, or an explicit reason there isn't one."""

    def _metric_def(self, session: Session) -> None:
        session.execute(
            text(
                "INSERT INTO metric_def (code, label_vi, definition_vi, formula_text, "
                "unit, category) VALUES ('test_pe', 'P/E', 'Định nghĩa', 'P / E', "
                "'x', 'valuation')"
            )
        )

    def test_unavailable_without_reason_is_rejected(
        self, session: Session, seed_ticker: int, seed_run: int
    ) -> None:
        self._metric_def(session)
        with pytest.raises(IntegrityError, match="metric_value_consistent"):
            session.execute(
                text(
                    "INSERT INTO metric_value (ticker_id, metric_code, as_of_date, "
                    "is_available, inputs, calc_run_id, calc_version) "
                    "VALUES (:t, 'test_pe', '2026-09-30', false, '{}'::jsonb, :r, 'v1')"
                ),
                {"t": seed_ticker, "r": seed_run},
            )

    def test_available_without_value_is_rejected(
        self, session: Session, seed_ticker: int, seed_run: int
    ) -> None:
        self._metric_def(session)
        with pytest.raises(IntegrityError, match="metric_value_consistent"):
            session.execute(
                text(
                    "INSERT INTO metric_value (ticker_id, metric_code, as_of_date, "
                    "is_available, inputs, calc_run_id, calc_version) "
                    "VALUES (:t, 'test_pe', '2026-09-30', true, '{}'::jsonb, :r, 'v1')"
                ),
                {"t": seed_ticker, "r": seed_run},
            )


class TestCopyrightBoundary:
    """News storage is headline + link + publisher + timestamp. There must be no column
    capable of holding an article body."""

    def test_news_item_has_no_body_column(self, session: Session) -> None:
        cols = {
            r[0]
            for r in session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'news_item'"
                )
            )
        }
        for forbidden in ("body", "content", "article_text", "full_text", "html"):
            assert forbidden not in cols


class TestExchangeIsExtensible:
    """The USP spans HOSE, HNX and UPCOM, so adding a venue must not need a migration."""

    def test_can_add_a_new_exchange_without_ddl(self, session: Session) -> None:
        session.execute(text("INSERT INTO exchange (code, name) VALUES ('UPCOM', 'UPCoM')"))
        assert (
            session.execute(text("SELECT count(*) FROM exchange WHERE code='UPCOM'")).scalar_one()
            == 1
        )
