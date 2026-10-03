"""Fact sheet assembly, machine QA, and the review gate.

The review tests are the ones that matter most: the "reviewed by X on Y" stamp is what the
product sells, so the rules protecting it are verified rather than trusted.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.db.enums import PipelineStage
from quantumedge.factsheet.build import build_fact_sheet, collect_allowed_numbers
from quantumedge.factsheet.qa import validate
from quantumedge.factsheet.schema import NOT_DISCLOSED
from quantumedge.factsheet.store import hash_of, payload_of, store_fact_sheet
from quantumedge.review.service import ReviewError, approve, publish, reject
from quantumedge.runs import start_run

AS_OF = date(2026, 6, 30)


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
        {"d": AS_OF},
    )
    session.execute(
        text(
            "INSERT INTO price_bar (ticker_id, trade_date, close, volume, turnover_vnd, "
            " source_raw_document_id, extract_run_id) "
            "VALUES (:t, :d, 2100, 1000, 2100000, :src, :r)"
        ),
        {"t": seed_ticker, "d": AS_OF, "src": doc_id, "r": run_id},
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
                "d": AS_OF,
                "v": value,
                "a": available,
                "r": reason,
                "run": run_id,
            },
        )
    return run_id, seed_ticker


class TestAssembly:
    def test_unavailable_metrics_become_disclosure_gaps(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """Gaps are content, not an error state: saying what is missing is part of the value."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)

        gap_fields = {g.field for g in sheet.disclosure_gaps}
        assert "metric.sessions_to_exit" in gap_fields
        # The company description is NULL by decision, so it must surface here too.
        assert "company.description_vi" in gap_fields

    def test_unavailable_figures_display_the_vietnamese_phrase(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        figures = {f.code: f for f in [*sheet.headline_metrics, *sheet.liquidity.figures]}
        assert figures["sessions_to_exit"].display == NOT_DISCLOSED
        assert figures["sessions_to_exit"].value is None

    def test_the_page_is_given_preformatted_strings(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """The web app must never format a number itself, or it could disagree with the
        pipeline about what a figure says."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        for figure in [*sheet.headline_metrics, *sheet.liquidity.figures]:
            assert figure.display, f"{figure.code} has no display string"

    def test_framing_note_travels_with_the_liquidity_block(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """So the numbers cannot be presented as a trading signal by omission."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        assert "RỦI RO" in sheet.liquidity.framing_note_vi

    def test_missing_metrics_is_an_error_not_an_empty_sheet(
        self, session: Session, seed_ticker: int
    ) -> None:
        run_id = start_run(session, PipelineStage.CALC, "t")
        with pytest.raises(LookupError, match="no metrics"):
            build_fact_sheet(session, "TST", AS_OF, run_id)


class TestAllowedNumbers:
    def test_both_raw_and_formatted_forms_are_listed(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """Agent 2 will reproduce the formatted form in prose, so it must be citable."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        allowed = set(sheet.allowed_numbers)
        assert NOT_DISCLOSED in allowed
        assert any("0.21" in a for a in allowed)

    def test_an_invented_number_would_not_be_allowed(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """The whole point of the whitelist: a figure that appears nowhere in the data must
        not pass as citable."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        assert "987654321" not in set(sheet.allowed_numbers)

    def test_empty_inputs_produce_only_the_sentinel(self) -> None:
        assert collect_allowed_numbers([], []) == [NOT_DISCLOSED]


class TestMachineQa:
    def test_a_clean_sheet_passes_every_check(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        report = validate(session, sheet, "TST")
        failed = [c["check"] for c in report.checks if not c["passed"]]
        assert report.passed, f"unexpected failures: {failed}"

    def test_a_symbol_mismatch_is_caught(self, session: Session, built: tuple[int, int]) -> None:
        """A sheet rendered under the wrong ticker is the worst failure available here."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        report = validate(session, sheet, "WRONG")
        assert not report.passed
        assert "symbol_matches" in report.as_dict()["failed"]

    def test_prohibited_language_is_caught(self, session: Session, built: tuple[int, int]) -> None:
        """Turns 'education, never recommendations' into a failing test rather than a habit
        someone has to remember under deadline."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        tainted = sheet.model_copy(
            update={"what_would_have_to_be_true": ["Khuyến nghị mua ở vùng giá này"]}
        )
        report = validate(session, tainted, "TST")
        assert not report.passed
        assert "no_prohibited_language" in report.as_dict()["failed"]

    def test_an_unsourced_number_in_prose_is_caught(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """This is the check Agent 2 will reuse verbatim. It is tested here, before it has a
        customer."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        tainted = sheet.model_copy(
            update={"what_would_have_to_be_true": ["Doanh thu đạt 987654321 đồng mỗi phiên"]}
        )
        report = validate(session, tainted, "TST")
        assert not report.passed
        assert "prose_numbers_are_all_allowed" in report.as_dict()["failed"]

    def test_a_figure_stripped_of_provenance_is_caught(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        stripped = [f.model_copy(update={"provenance": []}) for f in sheet.headline_metrics]
        tainted = sheet.model_copy(update={"headline_metrics": stripped})
        report = validate(session, tainted, "TST")
        assert not report.passed
        assert "every_figure_has_provenance" in report.as_dict()["failed"]


class TestHashGating:
    def test_the_hash_ignores_run_metadata(self, session: Session, built: tuple[int, int]) -> None:
        """Regression. calc_run_id changes on every build; including it made every rebuild
        look like a data change, which destroyed the review stamp each run."""
        run_id, _ = built
        first = build_fact_sheet(session, "TST", AS_OF, run_id)
        other_run = start_run(session, PipelineStage.CALC, "t2")
        second = build_fact_sheet(session, "TST", AS_OF, other_run)

        assert first.lineage.calc_run_id != second.lineage.calc_run_id
        assert hash_of(payload_of(first)) == hash_of(payload_of(second))

    def test_an_unchanged_rebuild_keeps_the_review_stamp(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """The behaviour the whole mechanism exists for: re-running the pipeline must not
        silently invalidate a human review."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        first = store_fact_sheet(session, sheet, run_id)
        approve(session, "TST", "richard", note="checked")

        rebuilt = build_fact_sheet(session, "TST", AS_OF, run_id)
        second = store_fact_sheet(session, rebuilt, run_id)

        assert second.created is False
        assert second.version == first.version
        status = session.execute(
            text("SELECT status, reviewed_by FROM fact_sheet WHERE id = :i"),
            {"i": first.fact_sheet_id},
        ).one()
        assert status.status == "approved"
        assert status.reviewed_by == "richard"

    def test_changed_content_creates_a_new_draft_requiring_fresh_review(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        run_id, ticker_id = built
        first = store_fact_sheet(session, build_fact_sheet(session, "TST", AS_OF, run_id), run_id)
        approve(session, "TST", "richard")

        session.execute(
            text(
                "UPDATE metric_value SET value = 0.99 WHERE ticker_id = :t "
                "AND metric_code = 'price_to_par'"
            ),
            {"t": ticker_id},
        )
        second = store_fact_sheet(session, build_fact_sheet(session, "TST", AS_OF, run_id), run_id)

        assert second.created is True
        assert second.version == first.version + 1
        new_status = session.execute(
            text("SELECT status FROM fact_sheet WHERE id = :i"), {"i": second.fact_sheet_id}
        ).scalar_one()
        assert new_status == "draft", "a content change must require review again"


class TestReviewGate:
    def _stored(self, session: Session, run_id: int) -> None:
        store_fact_sheet(session, build_fact_sheet(session, "TST", AS_OF, run_id), run_id)

    def test_publishing_a_draft_is_refused(self, session: Session, built: tuple[int, int]) -> None:
        run_id, _ = built
        self._stored(session, run_id)
        with pytest.raises(ReviewError, match="illegal transition draft -> published"):
            publish(session, "TST", "richard")

    def test_an_unknown_reviewer_is_refused(self, session: Session, built: tuple[int, int]) -> None:
        """A stamp naming someone who does not exist is not an audit trail."""
        run_id, _ = built
        self._stored(session, run_id)
        with pytest.raises(ReviewError, match="unknown reviewer"):
            approve(session, "TST", "nobody")

    def test_publishing_requires_a_named_person_not_system(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """Putting a page in front of readers is an act someone performs."""
        run_id, _ = built
        self._stored(session, run_id)
        approve(session, "TST", "richard")
        with pytest.raises(ReviewError, match="unknown reviewer"):
            publish(session, "TST", "system")

    def test_approving_a_sheet_that_failed_qa_is_refused(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """Machine QA gates human review rather than replacing it: a reviewer should spend
        attention on judgement, not on catching what a regex can find."""
        run_id, _ = built
        self._stored(session, run_id)
        session.execute(text("UPDATE fact_sheet SET qa_passed = false"))
        with pytest.raises(ReviewError, match="failed machine QA"):
            approve(session, "TST", "richard")

    def test_rejection_requires_a_reason(self, session: Session, built: tuple[int, int]) -> None:
        run_id, _ = built
        self._stored(session, run_id)
        with pytest.raises(ReviewError, match="must state a reason"):
            reject(session, "TST", "richard", "   ")

    def test_every_transition_is_logged_with_its_actor(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        run_id, _ = built
        self._stored(session, run_id)
        approve(session, "TST", "richard", note="checked the liquidity block")
        publish(session, "TST", "richard")

        rows = session.execute(
            text("SELECT from_status, to_status, actor FROM fact_sheet_review_log ORDER BY id")
        ).all()
        assert [(r.from_status, r.to_status) for r in rows] == [
            ("draft", "approved"),
            ("approved", "published"),
        ]
        assert all(r.actor == "richard" for r in rows)

    def test_publishing_supersedes_the_previous_version_keeping_its_stamp(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """Superseding is not unreviewing: the older version keeps who approved it."""
        run_id, ticker_id = built
        self._stored(session, run_id)
        approve(session, "TST", "richard")
        publish(session, "TST", "richard")

        session.execute(
            text(
                "UPDATE metric_value SET value = 0.55 WHERE ticker_id = :t "
                "AND metric_code = 'price_to_par'"
            ),
            {"t": ticker_id},
        )
        self._stored(session, run_id)
        approve(session, "TST", "richard")
        publish(session, "TST", "richard")

        rows = session.execute(
            text(
                "SELECT fs.version, fs.status, fs.reviewed_by FROM fact_sheet fs "
                "JOIN ticker t ON t.id = fs.ticker_id WHERE t.symbol = 'TST' ORDER BY fs.version"
            )
        ).all()
        assert [r.status for r in rows] == ["superseded", "published"]
        assert all(r.reviewed_by == "richard" for r in rows)

    def test_only_the_published_version_is_visible_through_the_view(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """The publication gate. The web role reads this view and holds no privilege on
        fact_sheet itself, so a draft cannot leak even by mistake."""
        run_id, _ = built
        self._stored(session, run_id)

        visible = session.execute(
            text("SELECT count(*) FROM published_ticker_page WHERE symbol = 'TST'")
        ).scalar_one()
        assert visible == 0, "a draft must not be visible"

        approve(session, "TST", "richard")
        publish(session, "TST", "richard")

        row = session.execute(
            text(
                "SELECT symbol, version, reviewed_by FROM published_ticker_page "
                "WHERE symbol = 'TST'"
            )
        ).one()
        assert row.symbol == "TST"
        assert row.reviewed_by == "richard"


class TestStaleness:
    def test_a_stale_page_fails_qa(self, session: Session, built: tuple[int, int]) -> None:
        """A page showing week-old prices as current is misleading even if every number in
        it was correct when computed."""
        run_id, _ = built
        far_future = AS_OF + timedelta(days=30)
        for offset in range(1, 9):
            session.execute(
                text(
                    "INSERT INTO market_calendar (exchange_code, trade_date, is_trading_day, "
                    " source) VALUES ('HOSE', :d, true, 'test') ON CONFLICT DO NOTHING"
                ),
                {"d": AS_OF + timedelta(days=offset)},
            )
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        stale_sheet = sheet.model_copy(update={"as_of_date": far_future})
        report = validate(session, stale_sheet, "TST")
        assert "price_is_fresh" in report.as_dict()["failed"]


class TestRuleVersionIsProvenanceNotContent:
    """A threshold change must not rewrite every page.

    `rule_spec_version` lives in its own `fact_sheet` column, so excluding it from the
    content hash loses nothing. Including it meant a rule affecting three companies
    created new drafts for all nine, discarding nine review stamps - and made the
    committed-snapshot diff useless for seeing what actually moved.
    """

    def test_two_sheets_differing_only_in_rule_version_hash_the_same(self) -> None:
        from quantumedge.factsheet.store import hash_of

        base = {
            "ticker": {"symbol": "TST"},
            "headline_metrics": [{"code": "pe", "value": "1.5"}],
            "lineage": {
                "rule_spec_version": "aaa",
                "calc_run_id": 1,
                "git_commit": "deadbeef",
                "code_version": "0.1.0",
                "as_of_date": "2026-06-30",
            },
            "generated_at": "2026-06-30T00:00:00Z",
        }
        other = {
            **base,
            "lineage": {**base["lineage"], "rule_spec_version": "bbb"},
            "generated_at": "2026-07-01T12:00:00Z",
        }

        assert hash_of(base) == hash_of(other)

    def test_a_changed_figure_still_changes_the_hash(self) -> None:
        """The exclusion must not be so broad that real movement stops registering."""
        from quantumedge.factsheet.store import hash_of

        base = {
            "ticker": {"symbol": "TST"},
            "headline_metrics": [{"code": "pe", "value": "1.5"}],
            "lineage": {"rule_spec_version": "aaa"},
        }
        moved = {
            **base,
            "headline_metrics": [{"code": "pe", "value": "1.6"}],
        }

        assert hash_of(base) != hash_of(moved)

    def test_a_changed_flag_state_changes_the_hash(self) -> None:
        from quantumedge.factsheet.store import hash_of

        base = {"flags": [{"code": "f", "state": "inactive"}], "lineage": {}}
        fired = {"flags": [{"code": "f", "state": "active"}], "lineage": {}}

        assert hash_of(base) != hash_of(fired)


class TestHeadlineMustLeadWithFive:
    """The spec commits the page to leading with five metrics. The check was specified and
    never implemented, and the gap was invisible: a sheet leading with three figures — two
    of them "không công bố" — passed QA and published."""

    def test_a_sheet_with_too_few_headline_metrics_fails_qa(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        starved = sheet.model_copy(update={"headline_metrics": sheet.headline_metrics[:3]})

        report = validate(session, starved, "TST")

        assert not report.passed
        assert "headline_has_at_least_five_metrics" in report.as_dict()["failed"]

    def test_presence_is_required_but_availability_is_not(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """Requiring five *available* figures would refuse to publish an honest sheet about
        a company that discloses little — inverting the product's own rule that an absent
        figure is cheaper than a wrong one. So a headline of five unavailable metrics is
        allowed, and reads as five "không công bố"."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)
        blanked = [
            f.model_copy(
                update={
                    "available": False,
                    "value": None,
                    "display": NOT_DISCLOSED,
                    "unavailable_reason": "test",
                    "provenance": [],
                }
            )
            for f in sheet.headline_metrics
        ]
        all_blank = sheet.model_copy(update={"headline_metrics": blanked})

        report = validate(session, all_blank, "TST")

        assert "headline_has_at_least_five_metrics" not in report.as_dict()["failed"]

    def test_liquidity_headline_metrics_appear_in_both_places(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """Deliberate overlap, not a duplication bug: the headline is a summary, and the
        liquidity block must stay complete because its Vietnamese framing note travels with
        the full set of figures."""
        run_id, _ = built
        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)

        headline = {f.code for f in sheet.headline_metrics}
        block = {f.code for f in sheet.liquidity.figures}

        assert "avg_turnover_20s_vnd" in headline
        assert "avg_turnover_20s_vnd" in block
        assert len(sheet.headline_metrics) >= 5
