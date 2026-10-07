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
    MIN_EXACT_QUARTILE_SAMPLE,
    SAMPLE_INCLUDES_SUBJECT,
    build_exchange_benchmarks,
    build_history_benchmarks,
    build_sector_benchmarks,
    describe,
    exchange_window_label,
    parse_exchange_window_label,
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


def _ticker(
    session: Session,
    symbol: str,
    sector_id: int | None,
    *,
    exchange: str = "HOSE",
) -> int:
    """A covered ticker. `sector_id=None` is a real state — a company whose sector is not
    yet classified — and the exchange benchmark is the only cross-section it can appear in.
    """
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
                "VALUES (:sym, :c, :x, true) RETURNING id"
            ),
            {"sym": symbol, "c": company_id, "x": exchange},
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


@pytest.fixture
def two_exchanges(session: Session) -> None:
    """HOSE and HNX. Every covered ticker is on HOSE today, which is exactly why the
    two-venue case needs a test: the key collision it guards against cannot show up in
    production data until coverage widens, and by then it would be silent."""
    session.execute(
        text(
            "INSERT INTO exchange (code, name) VALUES ('HOSE', 'HOSE'), ('HNX', 'HNX') "
            "ON CONFLICT DO NOTHING"
        )
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


class TestQuartileHonesty:
    """What a quartile of three numbers actually is.

    The convention is R type 7 (numpy's default): position = fraction * (n - 1), then a
    linear weight between the two straddling values. It is pinned by these tests because a
    quartile a reader cannot reproduce from the published min / median / max is a figure
    they have to take on trust, which is the opposite of what this product sells.
    """

    def test_hand_computed_five_point_quartiles_are_exact_order_statistics(self) -> None:
        # sorted: 100, 300, 400, 900, 1000   (n = 5, so n - 1 = 4)
        #   median: 0.50 * 4 = 2.0 -> element[2]            =  400
        #   p25:    0.25 * 4 = 1.0 -> element[1]            =  300
        #   p75:    0.75 * 4 = 3.0 -> element[3]            =  900
        # Every position lands on a whole index, so nothing is interpolated.
        dist = describe([Decimal(900), Decimal(100), Decimal(1000), Decimal(300), Decimal(400)])
        assert dist is not None
        assert dist.median == Decimal(400)
        assert dist.p25 == Decimal(300)
        assert dist.p75 == Decimal(900)
        assert dist.minimum == Decimal(100)
        assert dist.maximum == Decimal(1000)
        assert dist.quartiles_are_interpolated is False

    def test_at_three_points_p25_is_exactly_the_midpoint_of_the_min_and_the_median(self) -> None:
        """The overclaim this flag exists to mark.

        At n=3 the "25th percentile" is an arithmetic restatement of two numbers printed
        in the same row, and the fraction of the sample below it is 1/3, not 1/4. It is a
        sound *band* boundary and an unsound *statistic*, and only the label can tell them
        apart — so the label is the fact sheet's problem, and the flag is how it knows.
        """
        # sorted: 10, 20, 60   (n = 3, so n - 1 = 2)
        #   median: 0.50 * 2 = 1.0 -> element[1]                        = 20
        #   p25:    0.25 * 2 = 0.5 -> 10 + (20 - 10) * 0.5 = (10+20)/2  = 15
        #   p75:    0.75 * 2 = 1.5 -> 20 + (60 - 20) * 0.5 = (20+60)/2  = 40
        dist = describe([Decimal(10), Decimal(20), Decimal(60)])
        assert dist is not None
        assert dist.p25 == Decimal(15) == (dist.minimum + dist.median) / 2
        assert dist.p75 == Decimal(40) == (dist.median + dist.maximum) / 2
        assert dist.quartiles_are_interpolated is True

    def test_four_points_interpolate_too(self) -> None:
        # sorted: 10, 20, 30, 100   (n = 4, so n - 1 = 3)
        #   median: 0.50 * 3 = 1.5  -> 20 + (30 - 20) * 0.5  = 25
        #   p25:    0.25 * 3 = 0.75 -> 10 + (20 - 10) * 0.75 = 17.5
        #   p75:    0.75 * 3 = 2.25 -> 30 + (100 - 30) * 0.25 = 47.5
        dist = describe([Decimal(10), Decimal(20), Decimal(30), Decimal(100)])
        assert dist is not None
        assert dist.median == Decimal(25)
        assert dist.p25 == Decimal("17.5")
        assert dist.p75 == Decimal("47.5")
        assert dist.quartiles_are_interpolated is True

    def test_five_is_the_smallest_honest_quartile_sample(self) -> None:
        """Not an arbitrary constant: 0.25*(n-1) is first a whole number at n=5."""
        assert MIN_EXACT_QUARTILE_SAMPLE == 5
        assert describe([Decimal(1)] * 4) is not None
        assert describe([Decimal(i) for i in range(4)]).quartiles_are_interpolated is True  # type: ignore[union-attr]
        assert describe([Decimal(i) for i in range(5)]).quartiles_are_interpolated is False  # type: ignore[union-attr]


class TestExchangeKeyEncoding:
    """`metric_benchmark`'s natural key COALESCEs both id columns to 0, and an exchange row
    has neither, so `window_label` is the only thing separating HOSE from HNX."""

    def test_the_label_carries_the_venue_and_reads_back(self) -> None:
        assert exchange_window_label("HOSE") == "current@HOSE"
        assert exchange_window_label("HNX", "current") == "current@HNX"
        assert parse_exchange_window_label("current@HOSE") == ("current", "HOSE")

    def test_an_unencoded_label_is_none_rather_than_a_guess(self) -> None:
        """`'current'` is a sector/history label. Returning ("current", "") would invent a
        venue with an empty code and let it match a row."""
        assert parse_exchange_window_label("current") is None
        assert parse_exchange_window_label("3y") is None
        assert parse_exchange_window_label("current@") is None


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


class TestExchangeBenchmarks:
    """The market comparison. At nine tickers the sector sample is three, so the venue-wide
    cross-section is the one most likely to say something — and nothing wrote it until now.
    """

    def test_two_venues_produce_two_rows_rather_than_overwriting_each_other(
        self, session: Session, sector_id: int, two_exchanges: None, metric_defs: int
    ) -> None:
        """The collision the key encoding exists to stop. Both id columns are NULL for
        exchange scope, so without the venue in `window_label` the second INSERT's
        ON CONFLICT would update the first row and a HOSE page would quote an HNX median
        with nothing anywhere to show it had happened.
        """
        run_id = start_run(session, PipelineStage.CALC, "t")

        # HOSE, sorted: 100, 300, 400, 900, 1000   (n = 5, n - 1 = 4)
        #   median: 0.50 * 4 = 2.0 -> element[2] = 400
        #   p25:    0.25 * 4 = 1.0 -> element[1] = 300
        #   p75:    0.75 * 4 = 3.0 -> element[3] = 900
        for symbol, value in (
            ("HAA", Decimal(100)),
            ("HBB", Decimal(300)),
            ("HCC", Decimal(400)),
            ("HDD", Decimal(900)),
            ("HEE", Decimal(1000)),
        ):
            _metric(session, run_id, _ticker(session, symbol, sector_id), value)

        # HNX, sorted: 200, 500, 800   (n = 3, n - 1 = 2)
        #   median: 0.50 * 2 = 1.0 -> element[1]                  = 500
        #   p25:    0.25 * 2 = 0.5 -> 200 + (500 - 200) * 0.5     = 350
        #   p75:    0.75 * 2 = 1.5 -> 500 + (800 - 500) * 0.5     = 650
        for symbol, value in (("NAA", Decimal(200)), ("NBB", Decimal(500)), ("NCC", Decimal(800))):
            _metric(
                session,
                run_id,
                _ticker(session, symbol, sector_id, exchange="HNX"),
                value,
            )

        build_exchange_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        rows = {
            r.window_label: r
            for r in session.execute(
                text(
                    "SELECT window_label, median, p25, p75, min_value, max_value, "
                    "       sample_size, is_available, sector_id, ticker_id "
                    "FROM metric_benchmark WHERE scope = 'exchange'"
                )
            )
        }
        assert sorted(rows) == ["current@HNX", "current@HOSE"]

        hose = rows["current@HOSE"]
        assert hose.is_available is True
        assert hose.sample_size == 5
        assert hose.median == Decimal("400.000000")
        assert hose.p25 == Decimal("300.000000")
        assert hose.p75 == Decimal("900.000000")
        assert hose.min_value == Decimal("100.000000")
        assert hose.max_value == Decimal("1000.000000")
        # The CHECK allows exchange scope with neither id, and that is what is written —
        # the venue lives in the label, not in a borrowed foreign key.
        assert hose.sector_id is None
        assert hose.ticker_id is None

        hnx = rows["current@HNX"]
        assert hnx.sample_size == 3
        assert hnx.median == Decimal("500.000000")
        assert hnx.p25 == Decimal("350.000000")
        assert hnx.p75 == Decimal("650.000000")

    def test_a_company_with_no_sector_still_gets_a_comparison(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        """`build_sector_benchmarks` filters `sector_id IS NOT NULL`, so an unclassified
        company has no cross-sectional comparison at all without this one."""
        run_id = start_run(session, PipelineStage.CALC, "t")
        for symbol, value in (("AAA", Decimal(100)), ("BBB", Decimal(200)), ("CCC", Decimal(300))):
            _metric(session, run_id, _ticker(session, symbol, None), value)

        build_sector_benchmarks(session, run_id, AS_OF, minimum_sample=3)
        build_exchange_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        assert (
            session.execute(
                text("SELECT count(*) FROM metric_benchmark WHERE scope = 'sector'")
            ).scalar_one()
            == 0
        )
        row = session.execute(
            text(
                "SELECT median, sample_size, is_available FROM metric_benchmark "
                "WHERE scope = 'exchange'"
            )
        ).one()
        assert row.is_available is True
        assert row.sample_size == 3
        assert row.median == Decimal("200.000000")

    def test_below_the_minimum_sample_the_statistics_are_withheld_with_a_reason(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        """A two-name "market median" is not a market median. The row is still written so
        a reader learns the comparison was attempted."""
        run_id = start_run(session, PipelineStage.CALC, "t")
        for symbol, value in (("AAA", Decimal(100)), ("BBB", Decimal(200))):
            _metric(session, run_id, _ticker(session, symbol, sector_id), value)

        build_exchange_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text(
                "SELECT median, p25, p75, min_value, max_value, sample_size, is_available, "
                "       unavailable_reason, member_ticker_ids "
                "FROM metric_benchmark WHERE scope = 'exchange'"
            )
        ).one()
        assert row.is_available is False
        assert row.unavailable_reason == "sample_below_minimum"
        assert row.sample_size == 2
        assert (row.median, row.p25, row.p75, row.min_value, row.max_value) == (
            None,
            None,
            None,
            None,
            None,
        )
        # The members are still named: which two companies fell short is itself the fact.
        assert len(row.member_ticker_ids) == 2

    def test_members_whose_metric_is_unavailable_are_in_neither_the_sample_nor_the_list(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        """`member_ticker_ids` must name the contributors, not the candidates. Listing a
        ticker that contributed nothing would make the published median unreproducible by
        the reader who tried to check it from exactly those members."""
        run_id = start_run(session, PipelineStage.CALC, "t")
        contributors = [_ticker(session, s, sector_id) for s in ("AAA", "BBB", "CCC", "DDD", "EEE")]
        for tid, value in zip(
            contributors,
            [Decimal(100), Decimal(300), Decimal(400), Decimal(900), Decimal(1000)],
            strict=True,
        ):
            _metric(session, run_id, tid, value)
        silent = _ticker(session, "FFF", sector_id)
        _metric(session, run_id, silent, None)

        build_exchange_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text(
                "SELECT median, sample_size, member_ticker_ids FROM metric_benchmark "
                "WHERE scope = 'exchange'"
            )
        ).one()
        assert row.sample_size == 5
        assert sorted(row.member_ticker_ids) == sorted(contributors)
        assert silent not in row.member_ticker_ids
        # Still 400, the median of the five that reported. Counting FFF as zero would have
        # given 300 — a market that looks a third less liquid because one name is missing.
        assert row.median == Decimal("400.000000")

    def test_a_venue_where_nothing_resolved_gets_an_honest_row_not_no_row(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        run_id = start_run(session, PipelineStage.CALC, "t")
        for symbol in ("AAA", "BBB", "CCC"):
            _metric(session, run_id, _ticker(session, symbol, sector_id), None)

        build_exchange_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text(
                "SELECT sample_size, is_available, unavailable_reason, member_ticker_ids "
                "FROM metric_benchmark WHERE scope = 'exchange'"
            )
        ).one()
        assert row.sample_size == 0
        assert row.is_available is False
        # Distinct from sample_below_minimum: nothing was there, rather than too little.
        assert row.unavailable_reason == "no_sample"
        assert list(row.member_ticker_ids) == []

    def test_rebuilding_converges_rather_than_duplicating(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        run_id = start_run(session, PipelineStage.CALC, "t")
        for symbol, value in (("AAA", Decimal(100)), ("BBB", Decimal(200)), ("CCC", Decimal(300))):
            _metric(session, run_id, _ticker(session, symbol, sector_id), value)

        for _ in range(2):
            build_exchange_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        assert (
            session.execute(
                text("SELECT count(*) FROM metric_benchmark WHERE scope = 'exchange'")
            ).scalar_one()
            == 1
        )

    def test_only_covered_tickers_are_in_the_venue_sample(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        """Coverage is the promise that a figure has been checked. An uncovered listing has
        not been, so it cannot be part of a number the page stands behind."""
        run_id = start_run(session, PipelineStage.CALC, "t")
        for symbol, value in (("AAA", Decimal(100)), ("BBB", Decimal(200)), ("CCC", Decimal(300))):
            _metric(session, run_id, _ticker(session, symbol, sector_id), value)
        uncovered = _ticker(session, "ZZZ", sector_id)
        session.execute(
            text("UPDATE ticker SET is_covered = false WHERE id = :t"), {"t": uncovered}
        )
        _metric(session, run_id, uncovered, Decimal(9_000))

        build_exchange_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text("SELECT median, sample_size FROM metric_benchmark WHERE scope = 'exchange'")
        ).one()
        assert row.sample_size == 3
        assert row.median == Decimal("200.000000")


class TestTheSampleIncludesItsSubject:
    """A cross-sectional benchmark is a property of the population, not of the viewer.

    Leave-one-out was the alternative and is rejected: it would publish a different
    "trung vị nhóm ngành" on each page of the same sector under the same label, neither
    reproducible from the three numbers a reader can see, and at n=3 it would drop every
    sector to n=2 — below `min_benchmark_sample`, making sector comparison unavailable by
    construction for the entire current universe.
    """

    def test_the_decision_is_a_named_constant_not_an_accident_of_the_sql(self) -> None:
        assert SAMPLE_INCLUDES_SUBJECT is True

    def test_a_ticker_is_one_of_the_members_of_its_own_sector_median(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        run_id = start_run(session, PipelineStage.CALC, "t")
        ids = [_ticker(session, s, sector_id) for s in ("AAA", "BBB", "CCC")]
        for tid, value in zip(ids, [Decimal(100), Decimal(200), Decimal(300)], strict=True):
            _metric(session, run_id, tid, value)

        build_sector_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text(
                "SELECT median, sample_size, member_ticker_ids FROM metric_benchmark "
                "WHERE scope = 'sector'"
            )
        ).one()
        # One row for the sector, n=3, and every member of the sector is in it — including
        # whichever one a reader happens to be looking at.
        assert row.sample_size == 3
        assert sorted(row.member_ticker_ids) == sorted(ids)
        # Leave-one-out would have made this 150, 200 or 250 depending on the page.
        assert row.median == Decimal("200.000000")

    def test_a_ticker_is_one_of_the_members_of_its_own_exchange_median(
        self, session: Session, sector_id: int, metric_defs: int
    ) -> None:
        run_id = start_run(session, PipelineStage.CALC, "t")
        ids = [_ticker(session, s, sector_id) for s in ("AAA", "BBB", "CCC")]
        for tid, value in zip(ids, [Decimal(100), Decimal(200), Decimal(300)], strict=True):
            _metric(session, run_id, tid, value)

        build_exchange_benchmarks(session, run_id, AS_OF, minimum_sample=3)

        row = session.execute(
            text(
                "SELECT sample_size, member_ticker_ids FROM metric_benchmark "
                "WHERE scope = 'exchange'"
            )
        ).one()
        assert row.sample_size == 3
        assert sorted(row.member_ticker_ids) == sorted(ids)


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
