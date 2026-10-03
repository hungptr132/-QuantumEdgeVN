"""HOSE identity resolution, and the one property that makes it safe to rely on.

The HOSE security id is a join key: every statement, disclosure and attachment fetched
later is addressed by it. Bind a ticker to the wrong id and the pipeline produces another
company's numbers carrying flawless provenance — a wrong figure that looks more credible
than a right one. So the match must be exact, and that is what most of this file tests.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.db.enums import PipelineStage
from quantumedge.extract.identifiers import extract_identifiers, security_id
from quantumedge.ingest.base import RawPayload, store_raw
from quantumedge.ingest.hose import DOC_TYPE, SOURCE_CODE, _exact_match
from quantumedge.runs import start_run

FIXTURES = Path(__file__).parent / "fixtures" / "hose"


@pytest.fixture
def agm_search() -> dict[str, Any]:
    """A real recorded response, so the suite stays offline and deterministic."""
    return json.loads((FIXTURES / "agm_search.json").read_text(encoding="utf-8"))


def _store(session: Session, symbol: str, item: dict[str, Any]) -> int:
    """Store one identity document and return the ingest run id."""
    session.execute(
        text(
            "INSERT INTO source (code, kind, name) "
            "VALUES (:c, 'market_data', 'HOSE search') ON CONFLICT DO NOTHING"
        ),
        {"c": SOURCE_CODE},
    )
    run_id = start_run(session, PipelineStage.INGEST, "t", source_code=SOURCE_CODE)
    store_raw(
        session,
        run_id,
        [RawPayload(source_code=SOURCE_CODE, doc_type=DOC_TYPE, body=item, symbol=symbol)],
    )
    return run_id


class TestTheMatchMustBeExact:
    """A full-text search over four fields returns near misses. Taking the top hit would
    be the single most damaging bug available in this pipeline."""

    def test_the_exact_code_is_chosen_over_a_higher_ranked_near_miss(self) -> None:
        body = {
            "total": 2,
            "items": [
                # A company whose description mentions AGM, ranked first.
                {"id": 9999, "code": "AAM", "isin": "VN000000AAM1", "figi": "BBG00AAM0001"},
                {"id": 2518, "code": "AGM", "isin": "VN000000AGM6", "figi": "BBG003NWY9K8"},
            ],
        }
        match = _exact_match(body, "AGM")
        assert match is not None
        assert match["id"] == 2518

    def test_no_exact_code_means_no_match_rather_than_the_best_guess(self) -> None:
        body = {
            "total": 1,
            "items": [{"id": 9999, "code": "AAM", "isin": "VN000000AAM1"}],
        }
        assert _exact_match(body, "AGM") is None

    def test_an_empty_result_set_is_handled(self) -> None:
        assert _exact_match({"total": 0, "items": []}, "AGM") is None

    def test_a_malformed_body_does_not_raise(self) -> None:
        """The endpoint answers 200 for inputs it does not accept, so the shape is not
        guaranteed and a surprise must not take down a nine-symbol run."""
        assert _exact_match({}, "AGM") is None
        assert _exact_match({"items": "not a list"}, "AGM") is None
        assert _exact_match({"items": ["not an object"]}, "AGM") is None

    def test_the_recorded_response_resolves(self, agm_search: dict[str, Any]) -> None:
        match = _exact_match(agm_search, "AGM")
        assert match is not None
        assert match == {
            "id": 2518,
            "version": None,
            "figi": "BBG003NWY9K8",
            "code": "AGM",
            "isin": "VN000000AGM6",
            "timestamp": "0001-01-01T00:00:00+00:00",
            "securitiesTypeId": 1,
        }


class TestExtraction:
    def test_one_document_yields_three_schemes_each_naming_its_field(
        self, session: Session, seed_ticker: int, agm_search: dict[str, Any]
    ) -> None:
        item = agm_search["items"][0]
        _store(session, "TST", item)
        run_id = start_run(session, PipelineStage.EXTRACT, "t")

        result = extract_identifiers(session, run_id)

        assert result.rows_written == 3
        rows = session.execute(
            text(
                "SELECT scheme, value, source_field_path FROM security_identifier "
                "WHERE ticker_id = :t ORDER BY scheme"
            ),
            {"t": seed_ticker},
        ).all()
        assert [(r.scheme, r.value, r.source_field_path) for r in rows] == [
            ("figi", "BBG003NWY9K8", "$.figi"),
            ("hose_security_id", "2518", "$.id"),
            ("isin", "VN000000AGM6", "$.isin"),
        ]

    def test_re_extracting_converges_rather_than_duplicating(
        self, session: Session, seed_ticker: int, agm_search: dict[str, Any]
    ) -> None:
        """Invariant 7: a second run reports work done but leaves the same rows."""
        _store(session, "TST", agm_search["items"][0])
        for _ in range(2):
            extract_identifiers(session, start_run(session, PipelineStage.EXTRACT, "t"))

        count = session.execute(
            text("SELECT count(*) FROM security_identifier WHERE ticker_id = :t"),
            {"t": seed_ticker},
        ).scalar_one()
        assert count == 3

    def test_a_corrected_document_updates_the_value_and_repoints_provenance(
        self, session: Session, seed_ticker: int
    ) -> None:
        _store(session, "TST", {"id": 1111, "code": "TST"})
        extract_identifiers(session, start_run(session, PipelineStage.EXTRACT, "t"))
        first = session.execute(
            text(
                "SELECT value, source_raw_document_id FROM security_identifier "
                "WHERE ticker_id = :t AND scheme = 'hose_security_id'"
            ),
            {"t": seed_ticker},
        ).one()

        _store(session, "TST", {"id": 2222, "code": "TST"})
        extract_identifiers(session, start_run(session, PipelineStage.EXTRACT, "t"))
        second = session.execute(
            text(
                "SELECT value, source_raw_document_id FROM security_identifier "
                "WHERE ticker_id = :t AND scheme = 'hose_security_id'"
            ),
            {"t": seed_ticker},
        ).one()

        assert first.value == "1111"
        assert second.value == "2222"
        # The row must point at the document that corrected it, not the one it superseded.
        assert second.source_raw_document_id != first.source_raw_document_id

    def test_a_missing_identifier_yields_no_row_rather_than_an_empty_one(
        self, session: Session, seed_ticker: int
    ) -> None:
        """A row claiming the ISIN is '' is a false statement. No row is the true one."""
        _store(session, "TST", {"id": 2518, "code": "TST", "isin": "", "figi": None})
        result = extract_identifiers(session, start_run(session, PipelineStage.EXTRACT, "t"))

        assert result.rows_written == 1
        schemes = list(
            session.execute(
                text("SELECT scheme FROM security_identifier WHERE ticker_id = :t"),
                {"t": seed_ticker},
            ).scalars()
        )
        assert schemes == ["hose_security_id"]

    def test_a_document_for_an_uncovered_symbol_is_skipped_with_a_note(
        self, session: Session, seed_ticker: int
    ) -> None:
        _store(session, "NOPE", {"id": 4242, "code": "NOPE"})
        result = extract_identifiers(session, start_run(session, PipelineStage.EXTRACT, "t"))

        assert result.rows_written == 0
        assert result.skipped == 1
        assert any("not a covered ticker" in n for n in result.notes)

    def test_the_lookup_returns_none_before_resolution(
        self, session: Session, seed_ticker: int
    ) -> None:
        """Callers must be able to tell "never resolved" from a wrong id, so the lookup
        answers None rather than raising or inventing one."""
        assert security_id(session, "TST") is None
        _store(session, "TST", {"id": 2518, "code": "TST"})
        extract_identifiers(session, start_run(session, PipelineStage.EXTRACT, "t"))
        assert security_id(session, "TST") == "2518"


class TestTheWebRoleCannotSeeIt:
    def test_qe_web_has_no_privilege_on_the_new_table(self, session: Session) -> None:
        """Invariant 5. A new base table must not quietly become readable: the grant model
        is allowlist-only, and this asserts the new table did not slip through it."""
        granted = session.execute(
            text("SELECT has_table_privilege('qe_web', 'security_identifier', 'SELECT')")
        ).scalar_one()
        assert granted is False
