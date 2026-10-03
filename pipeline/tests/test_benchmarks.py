"""Benchmarks and the comparison block.

The rules under test are the ones that stop a comparison from misleading: a median needs
a minimum sample, an unavailable metric is excluded rather than counted as zero, the
sample members are named, and position language stays positional instead of evaluative.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.calc.benchmarks import (
    build_history_benchmarks,
    build_sector_benchmarks,
    describe,
)
from quantumedge.db.enums import PipelineStage
from quantumedge.factsheet.comparisons import (
    RANGE_LABELS,
    SECTOR_LABELS,
    _position_in_range,
    _position_vs_sector,
)
from quantumedge.runs import start_run

AS_OF = date(2026, 6, 30)


def _ticker(session: Session, symbol: str, sector_id: int) -> int:
    company_id = session.execute(
        text(
            "INSERT INTO company (legal_name_vi, short_name, sector_id) "
            "VALUES (:n, :n, :s) RETURNING id"
        ),
        {"n": symbol, "s": sector_id},
    ).scalar_one()
    return int(
        session.execute(
            text(
                "INSERT INTO ticker (symbol, company_id, exchange_code, is_covered) "
                "VALUES (:sym, :c, 'HOSE', true) RETURNING id"
            ),
            {"sym": symbol, "c": company_id},
        ).scalar_one()
    )


def _metric(
    session: Session,
    run_id: int,
    ticker_id: int,
    value: Decimal | None,
    *,
    code: str = "avg_turnover_20s_vnd",
    as_of: date = AS_OF,
) -> None:
    """One metric_value row. A None value is written unavailable, as the pipeline does."""
    session.execute(
        text(
            "INSERT INTO metric_value (ticker_id, metric_code, as_of_date, value, "
            " is_available, unavailable_reason, inputs, calc_run_id, calc_version) "
            "VALUES (:t, :c, :d, :v, :avail, :reason, '{}'::jsonb, :r, 'test/v1') "
            "ON CONFLICT (ticker_id, metric_code, as_of_date) DO UPDATE SET value = "
            " EXCLUDED.value, is_available = EXCLUDED.is_available"
        ),
        {
            "t": ticker_id,
            "c": code,
            "d": as_of,
            "v": value,
            "avail": value is not None,
            "reason": None if value is not None else "missing_input:test",
            "r": run_id,
        },
    )


@pytest.fixture
def sector_id(session: Session) -> int:
    session.execute(
        text("INSERT INTO exchange (code, name) VALUES ('HOSE', 'HOSE') ON CONFLICT DO NOTHING")
    )
    return int(
        session.execute(
            text("INSERT INTO sector (code, name_vi) VALUES ('s', 'Ngành') RETURNING id")
        ).scalar_one()
    )


class TestDistribution:
    def test_quartiles_interpolate_rather_than_collapsing_onto_the_extremes(self) -> None:
        """At n=3, picking the nearest rank would make p25 the minimum and p75 the
        maximum, so two of the three quartiles would be duplicates of the range."""
        dist = describe([Decimal(10), Decimal(20), Decimal(60)])
        assert dist is not None
        assert dist.median == Decimal(20)
        assert dist.p25 == Decimal(15)
        assert dist.p75 == Decimal(40)
        assert dist.minimum == Decimal(10)
        assert dist.maximum == Decimal(60)

    def test_a_single_value_is_its_own_every_statistic(self) -> None:
        dist = describe([Decimal(7)])
        assert dist is not None
        assert dist.median == dist.p25 == dist.p75 == Decimal(7)
        assert dist.sample_size == 1

    def test_an_empty_sample_is_none_rather_than_zeros(self) -> None:
        """Returning zeros would publish a sector median of 0 for a sector with no data."""
        assert describe([]) is None


class TestSectorBenchmarks:
    def test_three_peers_produce_an_available_median_naming_its_members(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        run_id = start_run(session, PipelineStage.CALC, "t")
        ids = [_ticker(session, s, sector_id) for s in ("AAA", "BBB", "CCC")]
        for tid, v in zip(ids, [Decimal(100), Decimal(200), Decimal(600)], strict=True):
            _metric(session, run_id, tid, v)

        build_sector_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text(
                "SELECT median, p25, p75, sample_size, is_available, member_ticker_ids "
                "FROM metric_benchmark WHERE scope = 'sector' AND sector_id = :s"
            ),
            {"s": sector_id},
        ).one()
        assert row.is_available is True
        assert row.median == Decimal("200.000000")
        assert row.sample_size == 3
        # Exactly which three companies, so the number can be audited.
        assert sorted(row.member_ticker_ids) == sorted(ids)

    def test_two_peers_are_withheld_with_a_reason(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        """A median of two is not a median. The row is still written, so a reader learns
        the comparison was attempted and why it is absent."""
        run_id = start_run(session, PipelineStage.CALC, "t")
        for s, v in (("AAA", Decimal(100)), ("BBB", Decimal(200))):
            _metric(session, run_id, _ticker(session, s, sector_id), v)

        build_sector_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text(
                "SELECT median, sample_size, is_available, unavailable_reason "
                "FROM metric_benchmark WHERE scope = 'sector'"
            )
        ).one()
        assert row.is_available is False
        assert row.unavailable_reason == "sample_below_minimum"
        assert row.sample_size == 2
        # Statistics are withheld entirely, not stored alongside the false flag, so a
        # later caller cannot read them past the availability check.
        assert row.median is None

    def test_an_unavailable_metric_is_excluded_not_counted_as_zero(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        """Treating unknown turnover as zero turnover is how a thin stock gets flattered
        and a liquid sector gets understated. They are different facts."""
        run_id = start_run(session, PipelineStage.CALC, "t")
        _metric(session, run_id, _ticker(session, "AAA", sector_id), Decimal(100))
        _metric(session, run_id, _ticker(session, "BBB", sector_id), Decimal(200))
        _metric(session, run_id, _ticker(session, "CCC", sector_id), Decimal(300))
        _metric(session, run_id, _ticker(session, "DDD", sector_id), None)

        build_sector_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text("SELECT median, sample_size FROM metric_benchmark WHERE scope = 'sector'")
        ).one()
        assert row.sample_size == 3
        # 200, not 150 — which is what including a zero would have produced.
        assert row.median == Decimal("200.000000")

    def test_rebuilding_converges_rather_than_duplicating(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        run_id = start_run(session, PipelineStage.CALC, "t")
        for s, v in (("AAA", Decimal(100)), ("BBB", Decimal(200)), ("CCC", Decimal(300))):
            _metric(session, run_id, _ticker(session, s, sector_id), v)

        for _ in range(2):
            build_sector_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        count = session.execute(
            text("SELECT count(*) FROM metric_benchmark WHERE scope = 'sector'")
        ).scalar_one()
        assert count == 1


class TestHistoryBenchmarks:
    def test_a_stocks_own_range_comes_from_its_past_as_of_dates(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        run_id = start_run(session, PipelineStage.CALC, "t")
        tid = _ticker(session, "AAA", sector_id)
        for offset, value in ((400, Decimal(900)), (200, Decimal(500)), (0, Decimal(100))):
            _metric(
                session,
                run_id,
                tid,
                value,
                as_of=date.fromordinal(AS_OF.toordinal() - offset),
            )

        build_history_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text(
                "SELECT min_value, max_value, sample_size, is_available FROM metric_benchmark "
                "WHERE scope = 'ticker_history' AND window_label = '3y' AND ticker_id = :t"
            ),
            {"t": tid},
        ).one()
        assert row.is_available is True
        assert row.sample_size == 3
        assert row.min_value == Decimal("100.000000")
        assert row.max_value == Decimal("900.000000")

    def test_a_window_excludes_dates_outside_it(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        """The 1-year window must not quietly include a 3-year-old observation, or
        "lowest in a year" would mean something different per ticker."""
        run_id = start_run(session, PipelineStage.CALC, "t")
        tid = _ticker(session, "AAA", sector_id)
        for offset, value in ((1000, Decimal(900)), (100, Decimal(500)), (0, Decimal(100))):
            _metric(
                session,
                run_id,
                tid,
                value,
                as_of=date.fromordinal(AS_OF.toordinal() - offset),
            )

        build_history_benchmarks(session, run_id, AS_OF, minimum_sample=1)

        one_year = session.execute(
            text(
                "SELECT max_value, sample_size FROM metric_benchmark "
                "WHERE scope = 'ticker_history' AND window_label = '1y' AND ticker_id = :t"
            ),
            {"t": tid},
        ).one()
        assert one_year.sample_size == 2
        assert one_year.max_value == Decimal("500.000000")


class TestPositionIsDescriptiveNotEvaluative:
    """`above` means a larger number, never a better company. Several metrics here are
    ones where higher is plainly worse, so the vocabulary has to stay positional."""

    def test_banding_uses_the_quartiles_not_the_median(self) -> None:
        bench = {"p25": Decimal(100), "p75": Decimal(300)}
        # Just above the median is still in line: at n=3 that difference is noise, and
        # reporting it as "above the sector" would dress noise as a finding.
        assert _position_vs_sector(Decimal(201), bench) == "in_line"
        assert _position_vs_sector(Decimal(301), bench) == "above"
        assert _position_vs_sector(Decimal(99), bench) == "below"
        assert _position_vs_sector(Decimal(100), bench) == "in_line"
        assert _position_vs_sector(Decimal(300), bench) == "in_line"

    def test_an_incomplete_benchmark_is_unknown_not_in_line(self) -> None:
        """`in_line` would be a claim. `unknown` is the truth."""
        assert _position_vs_sector(Decimal(200), {"p25": None, "p75": None}) == "unknown"

    def test_range_position_bands_the_edges(self) -> None:
        bench = {"min_value": Decimal(0), "max_value": Decimal(100)}
        assert _position_in_range(Decimal(5), bench) == "near_low"
        assert _position_in_range(Decimal(25), bench) == "near_low"
        assert _position_in_range(Decimal(50), bench) == "mid_range"
        assert _position_in_range(Decimal(75), bench) == "near_high"
        assert _position_in_range(Decimal(100), bench) == "near_high"

    def test_a_flat_window_is_mid_range_not_near_an_edge(self) -> None:
        """The metric did not move. Saying "near the high" implies movement that never
        happened — and a zero span would divide by zero besides."""
        flat = {"min_value": Decimal(0), "max_value": Decimal(0)}
        assert _position_in_range(Decimal(0), flat) == "mid_range"

    def test_every_position_has_vietnamese_and_none_of_it_is_a_verdict(self) -> None:
        """A label like "tốt hơn" (better) would turn a description into advice, which is
        the line this product does not cross."""
        banned = ("tốt", "xấu", "nên", "khuyến nghị", "rẻ", "đắt", "an toàn", "rủi ro")
        for label in (*SECTOR_LABELS.values(), *RANGE_LABELS.values()):
            assert label
            lowered = label.lower()
            for word in banned:
                assert word not in lowered, f"evaluative word '{word}' in '{label}'"


class TestBenchmarkNumbersAreCitable:
    """`allowed_numbers` is what will let Agent 2 be validated mechanically: every numeric
    token in a draft must appear in this set. A statistic displayed on the page but absent
    from the set would make the validator reject a figure the sheet itself shows."""

    def test_a_sector_median_on_the_page_is_in_allowed_numbers(self) -> None:
        from quantumedge.factsheet.build import collect_allowed_numbers
        from quantumedge.factsheet.schema import BenchmarkRef, Comparison, Figure

        figure = Figure(
            code="avg_turnover_20s_vnd",
            label_vi="Giá trị giao dịch bình quân 20 phiên",
            definition_vi="…",
            value=Decimal("40435850"),
            unit="VND",
            display="40,435,850 đ",
            available=True,
            comparison=Comparison(
                sector=BenchmarkRef(
                    scope="sector",
                    window_label="current",
                    label_vi="Trung vị nhóm ngành",
                    median=Decimal("6215976000"),
                    p25=Decimal("3130000000"),
                    p75=Decimal("10630000000"),
                    min_value=Decimal("40435850"),
                    max_value=Decimal("10630000000"),
                    display_median="6,215,976,000 đ",
                    display_range="40,435,850 đ – 10,630,000,000 đ",
                    sample_size=3,
                    available=True,
                ),
                position_vs_sector="below",
                position_vs_sector_label_vi="Thấp hơn nhóm ngành",
            ),
        )

        allowed = set(collect_allowed_numbers([figure], []))

        assert "6,215,976,000 đ" in allowed
        assert "6,215,976,000" in allowed
        assert "6215976000" in allowed
        # The sample size is quoted in prose as "trung vị của 3 mã".
        assert "3" in allowed

    def test_an_unavailable_benchmarks_statistics_are_not_whitelisted(self) -> None:
        """Nothing is displayed for it, so nothing about it is citable. Whitelisting a
        number the page withheld would let a draft quote a figure no reader can see."""
        from quantumedge.factsheet.build import collect_allowed_numbers
        from quantumedge.factsheet.schema import BenchmarkRef, Comparison, Figure

        figure = Figure(
            code="pe",
            label_vi="P/E",
            definition_vi="…",
            value=None,
            unit="x",
            display="không công bố",
            available=False,
            unavailable_reason="missing_facts:net_profit_after_tax",
            comparison=Comparison(
                sector=BenchmarkRef(
                    scope="sector",
                    window_label="current",
                    label_vi="Trung vị nhóm ngành",
                    median=Decimal("987654321"),
                    p25=None,
                    p75=None,
                    min_value=None,
                    max_value=None,
                    display_median="không công bố",
                    display_range="không công bố",
                    sample_size=2,
                    available=False,
                    unavailable_reason="Số mã trong nhóm ngành chưa đủ để tính trung vị",
                ),
            ),
        )

        allowed = set(collect_allowed_numbers([figure], []))
        assert "987654321" not in allowed
        assert "không công bố" in allowed
