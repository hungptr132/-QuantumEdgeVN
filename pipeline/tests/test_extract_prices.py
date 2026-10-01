"""Extract-stage tests: unit conversion, and the three liquidity facts staying distinct."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.db.enums import PipelineStage, RunStatus
from quantumedge.extract.calendar import rebuild_calendar, session_count
from quantumedge.extract.prices import PRICE_SCALE, extract_prices
from quantumedge.ingest.base import RawPayload, store_raw
from quantumedge.runs import run_scope, start_run

FIXTURES = Path(__file__).parent / "fixtures" / "vndirect"


@pytest.fixture
def agm_body() -> dict[str, Any]:
    return json.loads((FIXTURES / "agm_prices.json").read_text(encoding="utf-8"))


@pytest.fixture
def stored(session: Session, seed_ticker: int, agm_body: dict[str, Any]) -> int:
    """Store the fixture against the TST ticker and return the extract run id."""
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) "
            "VALUES ('vndirect_dchart', 'market_data', 'VNDirect') ON CONFLICT DO NOTHING"
        )
    )
    ingest_id = start_run(session, PipelineStage.INGEST, "t", source_code="vndirect_dchart")
    store_raw(
        session,
        ingest_id,
        [
            RawPayload(
                source_code="vndirect_dchart",
                doc_type="price_history",
                body=agm_body,
                symbol="TST",
                period_key="w",
            )
        ],
    )
    return start_run(session, PipelineStage.EXTRACT, "t")


class TestUnitConversion:
    def test_thousands_become_dong(
        self, session: Session, stored: int, agm_body: dict[str, Any]
    ) -> None:
        """The provider quotes in thousands. A bar stored at face value would understate
        every price by 1000x and silently corrupt every valuation metric downstream."""
        extract_prices(session, stored, symbols=["TST"])
        close = session.execute(
            text(
                "SELECT close FROM price_bar pb JOIN ticker t ON t.id = pb.ticker_id "
                "WHERE t.symbol = 'TST' ORDER BY trade_date LIMIT 1"
            )
        ).scalar_one()
        assert close == Decimal(str(agm_body["c"][0])) * PRICE_SCALE

    def test_prices_land_in_a_plausible_vnd_range(self, session: Session, stored: int) -> None:
        extract_prices(session, stored, symbols=["TST"])
        lo, hi = session.execute(
            text(
                "SELECT min(close), max(close) FROM price_bar pb "
                "JOIN ticker t ON t.id = pb.ticker_id WHERE t.symbol = 'TST'"
            )
        ).one()
        # AGM traded near 1,800-2,100 VND over the fixture window.
        assert Decimal(1000) < lo <= hi < Decimal(10000)


class TestZeroVolumeSurvives:
    def test_zero_volume_bars_are_written_not_dropped(
        self, session: Session, stored: int, agm_body: dict[str, Any]
    ) -> None:
        """Dropping these would make a frozen stock look normally traded - the precise
        failure Agent 5 exists to prevent."""
        extract_prices(session, stored, symbols=["TST"])
        written, zero = session.execute(
            text(
                "SELECT count(*), count(*) FILTER (WHERE volume = 0) FROM price_bar pb "
                "JOIN ticker t ON t.id = pb.ticker_id WHERE t.symbol = 'TST'"
            )
        ).one()
        assert written == len(agm_body["c"])
        assert zero == sum(1 for v in agm_body["v"] if v == 0)
        assert zero > 0, "fixture should contain zero-volume sessions"

    def test_turnover_is_zero_not_null_on_a_zero_volume_session(
        self, session: Session, stored: int
    ) -> None:
        """Zero turnover is a measured fact; NULL would read as 'unknown'."""
        extract_prices(session, stored, symbols=["TST"])
        turnovers = (
            session.execute(
                text(
                    "SELECT turnover_vnd FROM price_bar pb JOIN ticker t ON t.id = pb.ticker_id "
                    "WHERE t.symbol = 'TST' AND volume = 0"
                )
            )
            .scalars()
            .all()
        )
        assert turnovers
        assert all(t == 0 for t in turnovers)


class TestExtractionIsIdempotent:
    def test_second_pass_writes_no_duplicates(self, session: Session, stored: int) -> None:
        first = extract_prices(session, stored, symbols=["TST"])
        before = session.execute(text("SELECT count(*) FROM price_bar")).scalar_one()
        second = extract_prices(session, stored, symbols=["TST"])
        after = session.execute(text("SELECT count(*) FROM price_bar")).scalar_one()
        assert first.rows_written == second.rows_written
        assert before == after


class TestRaggedArraysAreRefused:
    def test_mismatched_series_lengths_skip_rather_than_mispair(
        self, session: Session, seed_ticker: int, agm_body: dict[str, Any]
    ) -> None:
        """Ragged arrays would pair prices with the wrong dates. Refusing is the only safe
        option: a wrong number is worse than an absent one."""
        session.execute(
            text(
                "INSERT INTO source (code, kind, name) "
                "VALUES ('vndirect_dchart', 'market_data', 'V') ON CONFLICT DO NOTHING"
            )
        )
        ingest_id = start_run(session, PipelineStage.INGEST, "t", source_code="vndirect_dchart")
        broken = {**agm_body, "v": agm_body["v"][:-3]}
        store_raw(
            session,
            ingest_id,
            [
                RawPayload(
                    source_code="vndirect_dchart",
                    doc_type="price_history",
                    body=broken,
                    symbol="TST",
                    period_key="ragged",
                )
            ],
        )
        run_id = start_run(session, PipelineStage.EXTRACT, "t")
        result = extract_prices(session, run_id, symbols=["TST"])
        assert result.rows_written == 0
        assert result.skipped == 1
        assert any("ragged" in n for n in result.notes)


class TestTradingCalendar:
    def test_calendar_is_derived_from_observed_bars(
        self, session: Session, stored: int, agm_body: dict[str, Any]
    ) -> None:
        extract_prices(session, stored, symbols=["TST"])
        rebuild_calendar(session)
        days = session_count(session, "HOSE")
        assert days == len({date.fromtimestamp(t) for t in agm_body["t"]})

    def test_calendar_separates_missing_bars_from_zero_volume(
        self, session: Session, stored: int
    ) -> None:
        """The distinction the calendar exists for. A session the market held but this
        ticker has no bar for is an absence; a bar with volume 0 is a zero-trade session.
        Those are different facts and the page reports them differently."""
        extract_prices(session, stored, symbols=["TST"])
        rebuild_calendar(session)

        sessions = session_count(session, "HOSE")
        present, zero_vol = session.execute(
            text(
                "SELECT count(*), count(*) FILTER (WHERE volume = 0) FROM price_bar pb "
                "JOIN ticker t ON t.id = pb.ticker_id WHERE t.symbol = 'TST'"
            )
        ).one()

        missing = sessions - present
        assert missing >= 0
        # With one ticker driving the calendar, every session is covered by a bar, so the
        # absences are zero while zero-trade sessions are plentiful - showing the two
        # quantities are genuinely independent rather than the same number twice.
        assert missing == 0
        assert zero_vol > 0


class TestFailedRunsAreStillRecorded:
    def test_a_failing_run_leaves_a_failed_manifest_row(self, session: Session) -> None:
        """Regression: the run row is committed when opened. Without that, a rollback
        erased the row the failure needed to be recorded against, so a crashed stage left
        no trace that anything had been attempted."""
        with (
            pytest.raises(RuntimeError, match="deliberate"),
            run_scope(session, PipelineStage.EXTRACT, "doomed") as (_run_id, stats),
        ):
            stats["rows_written"] = 3
            raise RuntimeError("deliberate failure")

        row = session.execute(
            text(
                "SELECT status, error FROM ingest_run WHERE run_key LIKE :pattern "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"pattern": "%:doomed"},
        ).one_or_none()
        assert row is not None, "the failed run row must survive the rollback"
        assert row.status == RunStatus.FAILED.value
        assert "deliberate" in row.error
