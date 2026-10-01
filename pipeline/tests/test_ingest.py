"""Ingest-stage tests. Offline: every response comes from a recorded fixture, so the
suite stays deterministic and still passes the day the provider changes shape - at which
point re-recording the fixture is how you find out what moved.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.db.enums import PipelineStage, RunStatus
from quantumedge.ingest.base import RawPayload, store_raw
from quantumedge.runs import finish_run, git_commit, make_run_key, start_run

FIXTURES = Path(__file__).parent / "fixtures" / "vndirect"


@pytest.fixture
def agm_body() -> dict[str, Any]:
    return json.loads((FIXTURES / "agm_prices.json").read_text(encoding="utf-8"))


@pytest.fixture
def ingest_run_id(session: Session) -> int:
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) "
            "VALUES ('vndirect_dchart', 'market_data', 'VNDirect') ON CONFLICT DO NOTHING"
        )
    )
    return start_run(session, PipelineStage.INGEST, "test_prices", source_code="vndirect_dchart")


def _payload(body: dict[str, Any], symbol: str = "AGM") -> RawPayload:
    return RawPayload(
        source_code="vndirect_dchart",
        doc_type="price_history",
        body=body,
        symbol=symbol,
        period_key="2026-07-01..2026-10-01",
    )


class TestContentHashing:
    """The hash decides what counts as the same document, so its coverage matters."""

    def test_same_body_same_hash(self, agm_body: dict[str, Any]) -> None:
        a = RawPayload(source_code="s", doc_type="d", body=agm_body, symbol="AGM")
        b = RawPayload(source_code="s", doc_type="d", body=dict(agm_body), symbol="AGM")
        assert a.content_hash() == b.content_hash()

    def test_hash_ignores_url_and_fetch_metadata(self, agm_body: dict[str, Any]) -> None:
        """Identical prices fetched via a differently-ordered query string are the same
        document. Including volatile fields would defeat the dedupe they exist for."""
        a = RawPayload(
            source_code="s", doc_type="d", body=agm_body, source_url="u1", http_status=200
        )
        b = RawPayload(
            source_code="s", doc_type="d", body=agm_body, source_url="u2", http_status=304
        )
        assert a.content_hash() == b.content_hash()

    def test_changed_body_changes_hash(self, agm_body: dict[str, Any]) -> None:
        mutated = {**agm_body, "c": [*agm_body["c"][:-1], 99.9]}
        a = RawPayload(source_code="s", doc_type="d", body=agm_body)
        b = RawPayload(source_code="s", doc_type="d", body=mutated)
        assert a.content_hash() != b.content_hash()

    def test_key_order_does_not_affect_hash(self, agm_body: dict[str, Any]) -> None:
        reordered = dict(reversed(list(agm_body.items())))
        a = RawPayload(source_code="s", doc_type="d", body=agm_body)
        b = RawPayload(source_code="s", doc_type="d", body=reordered)
        assert a.content_hash() == b.content_hash()


class TestStoreIsIdempotent:
    def test_second_store_is_unchanged_not_duplicate(
        self, session: Session, ingest_run_id: int, agm_body: dict[str, Any]
    ) -> None:
        first = store_raw(session, ingest_run_id, [_payload(agm_body)])
        assert (first.inserted, first.unchanged) == (1, 0)

        second = store_raw(session, ingest_run_id, [_payload(agm_body)])
        assert (second.inserted, second.unchanged) == (0, 1)

        assert session.execute(text("SELECT count(*) FROM raw_document")).scalar_one() == 1

    def test_changed_content_stores_a_new_row(
        self, session: Session, ingest_run_id: int, agm_body: dict[str, Any]
    ) -> None:
        """A corrected response is a new document, never an overwrite: the raw layer is
        append-only, so the earlier version stays auditable."""
        store_raw(session, ingest_run_id, [_payload(agm_body)])
        revised = {**agm_body, "c": [*agm_body["c"][:-1], 2.5]}
        assert store_raw(session, ingest_run_id, [_payload(revised)]).inserted == 1
        assert session.execute(text("SELECT count(*) FROM raw_document")).scalar_one() == 2

    def test_stats_shape_matches_the_manifest(
        self, session: Session, ingest_run_id: int, agm_body: dict[str, Any]
    ) -> None:
        store_raw(session, ingest_run_id, [_payload(agm_body)])
        result = store_raw(session, ingest_run_id, [_payload(agm_body)])
        assert result.as_stats() == {"fetched": 1, "new": 0, "unchanged": 1}

    def test_unknown_source_is_refused(
        self, session: Session, ingest_run_id: int, agm_body: dict[str, Any]
    ) -> None:
        bogus = RawPayload(source_code="nope_not_seeded", doc_type="d", body=agm_body)
        with pytest.raises(LookupError, match="unknown source"):
            store_raw(session, ingest_run_id, [bogus])

    def test_symbol_links_to_the_ticker_row(
        self, session: Session, seed_ticker: int, ingest_run_id: int, agm_body: dict[str, Any]
    ) -> None:
        store_raw(session, ingest_run_id, [_payload(agm_body, symbol="TST")])
        linked = session.execute(
            text("SELECT ticker_id FROM raw_document WHERE symbol = 'TST'")
        ).scalar_one()
        assert linked == seed_ticker


class TestRunManifest:
    def test_run_key_is_sortable_and_descriptive(self) -> None:
        stamp, stage, label = make_run_key(PipelineStage.INGEST, "vndirect_prices").split(":")
        assert stage == "ingest"
        assert label == "vndirect_prices"
        assert stamp.endswith("Z")
        # Microsecond precision: run_key is UNIQUE and stages can start in the same second.
        assert len(stamp) == 23

    def test_commit_is_a_sha_or_unknown(self) -> None:
        """A run built from uncommitted files is not reproducible from any commit, so the
        manifest discloses it with a -dirty suffix rather than implying clean provenance."""
        commit = git_commit()
        assert commit == "unknown" or len(commit.split("-")[0]) == 40

    def test_failure_is_recorded_not_swallowed(self, session: Session, ingest_run_id: int) -> None:
        finish_run(session, ingest_run_id, RunStatus.FAILED, {"new": 0}, error="boom")
        row = session.execute(
            text("SELECT status, error, finished_at FROM ingest_run WHERE id = :i"),
            {"i": ingest_run_id},
        ).one()
        assert row.status == "failed"
        assert row.error == "boom"
        assert row.finished_at is not None


class TestRecordedFixtureShape:
    """Pins the assumptions the extractor relies on. If the provider changes shape these
    fail first and name the broken assumption, rather than the extractor silently
    producing nonsense."""

    def test_udf_arrays_are_parallel(self, agm_body: dict[str, Any]) -> None:
        lengths = {k: len(agm_body[k]) for k in ("t", "o", "h", "l", "c", "v")}
        assert len(set(lengths.values())) == 1, f"ragged arrays: {lengths}"

    def test_prices_are_in_thousands_of_vnd(self, agm_body: dict[str, Any]) -> None:
        """AGM traded near 1,800-2,100 VND, so raw values are small single digits. If this
        fails, the provider changed units and the extractor x1000 scaling is now wrong."""
        assert all(0 < c < 100 for c in agm_body["c"])

    def test_zero_volume_sessions_survive_in_the_raw_data(self, agm_body: dict[str, Any]) -> None:
        """The entire point of Agent 5. If these were absent or nulled upstream, liquidity
        analysis would have nothing to measure."""
        assert 0 in agm_body["v"]

    def test_timestamps_are_epoch_seconds(self, agm_body: dict[str, Any]) -> None:
        first = date.fromtimestamp(agm_body["t"][0])
        assert date(2020, 1, 1) < first < date(2030, 1, 1)
