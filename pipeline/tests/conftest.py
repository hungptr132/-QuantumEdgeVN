"""Test fixtures.

Tests run against a real PostgreSQL (`quantumedge_test`), never SQLite: half this schema
— native enums, partial indexes, triggers, CHECK constraints, a privilege-limited view —
cannot be expressed in SQLite, and those are precisely the guarantees worth testing.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from quantumedge.db.enums import PipelineStage
from quantumedge.runs import start_run

PIPELINE_ROOT = Path(__file__).resolve().parents[1]


def _test_dsn() -> str:
    from quantumedge.config import load_settings

    settings = load_settings()
    if not settings.test_database_url:
        pytest.skip("QE_TEST_DATABASE_URL is not configured")
    return settings.test_database_url


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Migrate the test database to head once per session, then hand back an engine."""
    dsn = _test_dsn()
    env = {**os.environ, "QE_DATABASE_URL": dsn}
    # sys.executable, not bare "python": the latter resolves outside this venv.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PIPELINE_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stderr}")

    eng = create_engine(dsn, future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session rolled back after every test, so tests never see each other's rows."""
    conn = engine.connect()
    tx = conn.begin()
    s = Session(bind=conn, expire_on_commit=False)
    try:
        yield s
    finally:
        s.close()
        tx.rollback()
        conn.close()


@pytest.fixture
def seed_ticker(session: Session) -> int:
    """Minimal rows to hang a ticker off: exchange, sector, company, ticker.

    Returns the ticker id.
    """
    session.execute(
        text("INSERT INTO exchange (code, name) VALUES ('HOSE', 'Ho Chi Minh Stock Exchange')")
    )
    sector_id = session.execute(
        text(
            "INSERT INTO sector (code, name_vi) VALUES ('test_sector', 'Ngành kiểm thử') "
            "RETURNING id"
        )
    ).scalar_one()
    company_id = session.execute(
        text(
            "INSERT INTO company (legal_name_vi, short_name, sector_id) "
            "VALUES ('Công ty Kiểm Thử', 'KIEMTHU', :s) RETURNING id"
        ),
        {"s": sector_id},
    ).scalar_one()
    ticker_id = session.execute(
        text(
            "INSERT INTO ticker (symbol, company_id, exchange_code, is_covered) "
            "VALUES ('TST', :c, 'HOSE', true) RETURNING id"
        ),
        {"c": company_id},
    ).scalar_one()
    return int(ticker_id)


@pytest.fixture
def seed_run(session: Session) -> int:
    """An ingest_run row, since every derived row must name its producer."""
    return int(
        session.execute(
            text(
                "INSERT INTO ingest_run (run_key, stage, status, git_commit, code_version) "
                "VALUES ('test-run', 'calc', 'succeeded', 'deadbeef', '0.1.0') RETURNING id"
            )
        ).scalar_one()
    )


@pytest.fixture
def metric_defs(session: Session) -> int:
    """Seed the real metric definitions from rules/metrics.yaml.

    metric_value.metric_code is a foreign key, so a metric cannot be written for a code that
    was never defined. Tests use the real definitions rather than inventing stubs, which
    means a metric implemented without a matching rules entry fails here.
    """
    from quantumedge.config import load_settings
    from quantumedge.db.seed import seed_metrics

    return seed_metrics(session, load_settings())


@pytest.fixture
def fact_defs(session: Session) -> int:
    """Seed the real line-item registry from rules/fact_codes.yaml.

    `financial_fact.fact_code` is a foreign key, so a fact cannot be written for a code the
    registry never declared. Tests use the real codes, which means a metric reading a
    line item nobody defined fails here rather than in production.
    """
    from quantumedge.config import load_settings
    from quantumedge.db.seed import seed_fact_codes

    return seed_fact_codes(session, load_settings())


# The date every fact-sheet test builds for. Pinned rather than derived from the clock:
# invariant 8, and a sheet whose `as_of` moved with the calendar would make its own
# assertions unreproducible tomorrow.
FACTSHEET_AS_OF = date(2026, 6, 30)


@pytest.fixture
def built(session: Session, seed_ticker: int, metric_defs: int) -> tuple[int, int]:
    """A ticker with one available and one unavailable metric, and a calendar bar.

    Returns (run_id, ticker_id).
    """
    session.execute(text("INSERT INTO app_user (slug, display_name) VALUES ('richard', 'Richard')"))
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) VALUES ('t', 'market_data', 't') "
            "ON CONFLICT DO NOTHING"
        )
    )
    run_id = start_run(session, PipelineStage.CALC, "t")
    doc_id = session.execute(
        text(
            "INSERT INTO raw_document (source_id, ingest_run_id, ticker_id, symbol, doc_type, "
            " content_hash, payload, byte_size) "
            "VALUES ((SELECT id FROM source WHERE code='t'), :r, :tk, 'TST', 'p', 'h', "
            " '{}'::jsonb, 2) RETURNING id"
        ),
        {"r": run_id, "tk": seed_ticker},
    ).scalar_one()
    session.execute(
        text(
            "INSERT INTO market_calendar (exchange_code, trade_date, is_trading_day, source) "
            "VALUES ('HOSE', :d, true, 'test')"
        ),
        {"d": FACTSHEET_AS_OF},
    )
    session.execute(
        text(
            "INSERT INTO price_bar (ticker_id, trade_date, close, volume, turnover_vnd, "
            " source_raw_document_id, extract_run_id) "
            "VALUES (:t, :d, 2100, 1000, 2100000, :src, :r)"
        ),
        {"t": seed_ticker, "d": FACTSHEET_AS_OF, "src": doc_id, "r": run_id},
    )
    # Every is_headline metric, because QA requires the page to lead with at least five -
    # which is the spec's own commitment. A fixture with fewer would test a sheet the
    # pipeline is not allowed to publish.
    for code, value, available, reason in (
        ("price_to_par", Decimal("0.21"), True, None),
        ("avg_turnover_20s_vnd", Decimal("40435850"), True, None),
        ("median_turnover_20s_vnd", Decimal(0), True, None),
        ("zero_trade_ratio_3m", Decimal("0.7937"), True, None),
        ("turnover_concentration_3m", Decimal("0.5221"), True, None),
        ("pe", None, False, "missing_facts:net_profit_after_tax,shares_outstanding"),
        ("interest_coverage", None, False, "missing_facts:ebit,interest_expense"),
        ("sessions_to_exit", None, False, "no_turnover_in_a_typical_session"),
    ):
        session.execute(
            text(
                "INSERT INTO metric_value (ticker_id, metric_code, as_of_date, value, "
                " is_available, unavailable_reason, inputs, calc_run_id, calc_version) "
                "VALUES (:t, :c, :d, :v, :a, :r, '{}'::jsonb, :run, 'test/v1')"
            ),
            {
                "t": seed_ticker,
                "c": code,
                "d": FACTSHEET_AS_OF,
                "v": value,
                "a": available,
                "r": reason,
                "run": run_id,
            },
        )
    return run_id, seed_ticker
