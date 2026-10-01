"""Test fixtures.

Tests run against a real PostgreSQL (`quantumedge_test`), never SQLite: half this schema
— native enums, partial indexes, triggers, CHECK constraints, a privilege-limited view —
cannot be expressed in SQLite, and those are precisely the guarantees worth testing.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

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
