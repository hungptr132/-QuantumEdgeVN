"""`event_coverage`: the row that lets an absence mean something.

An `event_presence` rule that finds nothing has two possible readings, and they are very
different statements about a named company:

    "no auditor change was disclosed"   - a claim about the company
    "we have no way to know"            - a claim about us

Only the second is true unless someone actually read the disclosures. `event_coverage` is
where that reading is recorded, so the first reading becomes available only where it is
earned. Everything here is about keeping those two apart.
"""

from datetime import date
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.config import Settings, load_settings
from quantumedge.db.seed import seed_events, seed_reference


def _settings_with_seeds(seeds_dir: Path) -> Settings:
    return load_settings().model_copy(update={"seeds_dir": seeds_dir})


def _write(seeds_dir: Path, payload: dict[str, object]) -> Settings:
    seeds_dir.mkdir(parents=True, exist_ok=True)
    (seeds_dir / "events.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return _settings_with_seeds(seeds_dir)


@pytest.fixture
def reviewer(session: Session, seed_ticker: int) -> str:
    """`reviewed_by` is a real foreign key, for the same reason `fact_sheet.reviewed_by`
    is: an audit stamp pointing at free text is not an audit stamp."""
    session.execute(
        text(
            "INSERT INTO app_user (slug, display_name, role) "
            "VALUES ('tester', 'Test Reviewer', 'reviewer') ON CONFLICT (slug) DO NOTHING"
        )
    )
    return "tester"


COVERAGE_ROW = {
    "symbol": "TST",
    "event_type": "auditor_change",
    "reviewed_through": date(2026, 9, 30),
    "reviewed_by": "tester",
    "source_note": "Disclosure archive for TST, 2022-01-01 to 2026-09-30, read in full",
}


class TestTheShippedSeedFileClaimsNothing:
    def test_the_repo_file_is_empty_in_both_blocks(self) -> None:
        """It ships empty on purpose. A coverage row is a human review stamp; writing one
        on the owner's behalf would assert that somebody read HOSE's archive when nobody
        has. Same rule as not approving a fact sheet for them."""
        data = yaml.safe_load(
            (load_settings().seeds_dir / "events.yaml").read_text(encoding="utf-8")
        )

        assert data["coverage"] == []
        assert data["events"] == []

    def test_seeding_an_empty_file_writes_nothing_and_still_reports(
        self, session: Session, tmp_path: Path
    ) -> None:
        settings = _write(tmp_path / "seeds", {"coverage": [], "events": []})

        counts = seed_events(session, settings)

        assert counts == {"event_coverage": 0, "event": 0}
        assert session.execute(text("SELECT count(*) FROM event_coverage")).scalar_one() == 0


class TestLoadingIsIdempotent:
    def test_a_second_run_updates_rather_than_duplicating(
        self, session: Session, reviewer: str, tmp_path: Path
    ) -> None:
        """Invariant 7: re-running changes nothing. Re-seeding is the normal way to apply
        an edit to one of these files, so convergence is not optional."""
        settings = _write(tmp_path / "seeds", {"coverage": [COVERAGE_ROW], "events": []})

        seed_events(session, settings)
        settings = _write(
            tmp_path / "seeds",
            {
                "coverage": [{**COVERAGE_ROW, "reviewed_through": date(2026, 10, 31)}],
                "events": [],
            },
        )
        seed_events(session, settings)

        rows = session.execute(text("SELECT reviewed_through FROM event_coverage")).scalars().all()
        assert rows == [date(2026, 10, 31)]

    def test_an_event_round_trips_with_its_excerpt(
        self, session: Session, reviewer: str, tmp_path: Path
    ) -> None:
        settings = _write(
            tmp_path / "seeds",
            {
                "coverage": [],
                "events": [
                    {
                        "symbol": "TST",
                        "event_type": "auditor_change",
                        "event_date": date(2025, 4, 18),
                        "severity": "high",
                        "source_url": "https://example.invalid/tst/cbtt-1",
                        "excerpt": "Trich dan tu cong bo thong tin.",
                    }
                ],
            },
        )

        counts = seed_events(session, settings)

        assert counts["event"] == 1
        method = session.execute(text("SELECT extraction_method FROM event")).scalar_one()
        assert method == "manual", "a hand-entered row must not claim an agent produced it"


class TestATypoCannotSilentlyDropARow:
    def test_an_unknown_symbol_raises_and_names_the_file(
        self, session: Session, reviewer: str, tmp_path: Path
    ) -> None:
        """These files are small and hand-written, which is exactly where a quiet skip is
        unrecoverable: the event would never appear and nothing would say why."""
        settings = _write(
            tmp_path / "seeds",
            {"coverage": [{**COVERAGE_ROW, "symbol": "NOSUCH"}], "events": []},
        )

        with pytest.raises(LookupError, match="events.yaml.*NOSUCH"):
            seed_events(session, settings)

    def test_claiming_coverage_of_event_type_none_is_refused(
        self, session: Session, reviewer: str, tmp_path: Path
    ) -> None:
        """`none` is the enum's sentinel for "no event", not a disclosure category anyone
        can go and review. Caught in Python so the message names the file."""
        settings = _write(
            tmp_path / "seeds",
            {"coverage": [{**COVERAGE_ROW, "event_type": "none"}], "events": []},
        )

        with pytest.raises(ValueError, match="sentinel"):
            seed_events(session, settings)


class TestTheDatabaseEnforcesIt:
    def test_a_coverage_row_needs_a_real_reviewer(self, session: Session, seed_ticker: int) -> None:
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO event_coverage (ticker_id, event_type, reviewed_through, "
                    " reviewed_by, source_note) VALUES (:t, 'auditor_change', '2026-09-30', "
                    " 'nobody-by-that-slug', 'read the whole archive end to end')"
                ),
                {"t": seed_ticker},
            )

    def test_a_token_source_note_is_refused(
        self, session: Session, seed_ticker: int, reviewer: str
    ) -> None:
        """The note is what makes the review repeatable by someone else. 'ok' is not a
        record of what was read."""
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO event_coverage (ticker_id, event_type, reviewed_through, "
                    " reviewed_by, source_note) VALUES (:t, 'auditor_change', '2026-09-30', "
                    " :by, 'ok')"
                ),
                {"t": seed_ticker, "by": reviewer},
            )

    def test_one_coverage_row_per_ticker_and_type(
        self, session: Session, seed_ticker: int, reviewer: str
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        for _ in range(2):
            try:
                session.execute(
                    text(
                        "INSERT INTO event_coverage (ticker_id, event_type, reviewed_through, "
                        " reviewed_by, source_note) VALUES (:t, 'auditor_change', "
                        " '2026-09-30', :by, 'read the whole archive end to end')"
                    ),
                    {"t": seed_ticker, "by": reviewer},
                )
            except IntegrityError:
                return
        pytest.fail("a second coverage row for the same ticker and type should be rejected")


class TestSeedAllIncludesIt:
    def test_seed_all_reports_both_event_tables(self, session: Session) -> None:
        """So `qe db seed` cannot quietly stop loading them."""
        from quantumedge.db.seed import seed_all

        settings = load_settings()
        seed_reference(session, settings)
        counts = seed_all(session, settings)

        assert "event_coverage" in counts
        assert "event" in counts
