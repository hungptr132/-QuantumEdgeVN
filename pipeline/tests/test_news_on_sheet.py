"""Does an attributed headline actually reach the page?

`FactSheet.news` sat in the contract as `list[Provenance]` and nothing ever populated it,
and the web app had no component that rendered it. So news could be fetched live, parsed,
attributed and stored — and still never appear anywhere a reader could see. The two halves
each worked; the path between them did not exist.

These tests cover that path, plus the two rules that constrain it: only headlines
attributed to *this* ticker may appear, and the sheet must not acquire news published after
its own `as_of`.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.factsheet.build import MAX_NEWS_ITEMS, build_fact_sheet

AS_OF = date(2026, 6, 30)
# Late enough in the Vietnamese day that a UTC reading would land on the previous date.
EVENING = datetime(2026, 6, 29, 23, 30, tzinfo=UTC)


@pytest.fixture
def feed_source(session: Session) -> int:
    return int(
        session.execute(
            text(
                "INSERT INTO source (code, kind, name) "
                "VALUES ('test_rss', 'rss', 'Test feed') RETURNING id"
            )
        ).scalar_one()
    )


def _headline(
    session: Session,
    source_id: int,
    run_id: int,
    *,
    ticker_id: int | None,
    headline: str,
    url: str,
    published_at: datetime,
) -> None:
    doc_id = session.execute(
        text(
            "INSERT INTO raw_document (source_id, ingest_run_id, doc_type, content_hash, "
            " payload, byte_size) VALUES (:s, :r, 'news_feed', :h, '{}'::jsonb, 2) "
            "RETURNING id"
        ),
        {"s": source_id, "r": run_id, "h": url},
    ).scalar_one()
    session.execute(
        text(
            "INSERT INTO news_item (raw_document_id, ticker_id, headline, url, publisher, "
            " published_at) VALUES (:d, :t, :hl, :u, 'Test Publisher', :p)"
        ),
        {"d": doc_id, "t": ticker_id, "hl": headline, "u": url, "p": published_at},
    )


class TestAnAttributedHeadlineReachesTheSheet:
    def test_it_appears_with_publisher_and_a_preformatted_date(
        self, session: Session, built: tuple[int, int], feed_source: int
    ) -> None:
        run_id, ticker_id = built
        _headline(
            session,
            feed_source,
            run_id,
            ticker_id=ticker_id,
            headline="Công ty Kiểm Thử công bố nghị quyết hội đồng quản trị",
            url="https://example.invalid/tst-1",
            published_at=EVENING,
        )

        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)

        assert len(sheet.news) == 1
        item = sheet.news[0]
        assert item.publisher == "Test Publisher"
        assert item.url == "https://example.invalid/tst-1"
        # 23:30 UTC on the 29th is 06:30 on the 30th in Ho Chi Minh City. Formatting the
        # stored instant in UTC would date every evening headline a day early.
        assert item.display_date == "30/06/2026"

    def test_the_sheet_carries_no_body_text_for_it(
        self, session: Session, built: tuple[int, int], feed_source: int
    ) -> None:
        """`news_item` has no body column, and `NewsRef` must not reintroduce one by
        another name. Asserted on the model's own fields so adding `summary` later fails
        here rather than on a page."""
        run_id, ticker_id = built
        _headline(
            session,
            feed_source,
            run_id,
            ticker_id=ticker_id,
            headline="Tiêu đề",
            url="https://example.invalid/tst-2",
            published_at=EVENING,
        )

        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)

        assert set(sheet.news[0].model_dump()) == {
            "headline",
            "url",
            "publisher",
            "published_at",
            "display_date",
            "editor_note_vi",
        }

    def test_newest_first_and_capped(
        self, session: Session, built: tuple[int, int], feed_source: int
    ) -> None:
        """A ticker page is a verification record, not a feed: one noisy week must not bury
        the figures under it."""
        run_id, ticker_id = built
        for i in range(MAX_NEWS_ITEMS + 4):
            _headline(
                session,
                feed_source,
                run_id,
                ticker_id=ticker_id,
                headline=f"Tiêu đề số {i}",
                url=f"https://example.invalid/tst-n{i}",
                published_at=datetime(2026, 6, 1, 3, 0, tzinfo=UTC) + timedelta(days=i),
            )

        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)

        assert len(sheet.news) == MAX_NEWS_ITEMS
        dates = [n.published_at for n in sheet.news]
        assert dates == sorted(dates, reverse=True)
        assert sheet.news[0].headline.endswith(str(MAX_NEWS_ITEMS + 3))


class TestWhatMayNotAppear:
    def test_an_unattributed_headline_stays_off_the_page(
        self, session: Session, built: tuple[int, int], feed_source: int
    ) -> None:
        """The whole reason attribution is exact-match. A market headline with no matched
        company is stored with a NULL ticker; putting it on a company's page would assert
        it is about them, which is the claim the matcher deliberately refuses to make."""
        run_id, ticker_id = built
        _headline(
            session,
            feed_source,
            run_id,
            ticker_id=None,
            headline="Thị trường chứng khoán phiên sáng",
            url="https://example.invalid/market-1",
            published_at=EVENING,
        )

        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)

        assert sheet.news == []

    def test_another_company_s_headline_stays_off_the_page(
        self, session: Session, built: tuple[int, int], feed_source: int
    ) -> None:
        run_id, ticker_id = built
        other = session.execute(
            text(
                "INSERT INTO ticker (symbol, company_id, exchange_code, is_covered) "
                "SELECT 'OTH', company_id, exchange_code, true FROM ticker WHERE id = :t "
                "RETURNING id"
            ),
            {"t": ticker_id},
        ).scalar_one()
        _headline(
            session,
            feed_source,
            run_id,
            ticker_id=int(other),
            headline="Tin của mã khác",
            url="https://example.invalid/oth-1",
            published_at=EVENING,
        )

        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)

        assert sheet.news == []

    def test_a_headline_published_after_as_of_stays_off_the_page(
        self, session: Session, built: tuple[int, int], feed_source: int
    ) -> None:
        """Invariant 7 reaches the news section too. A sheet rebuilt for a past date must
        not acquire headlines published since, or the same `as_of` would render differently
        depending on when the rebuild happened."""
        run_id, ticker_id = built
        _headline(
            session,
            feed_source,
            run_id,
            ticker_id=ticker_id,
            headline="Tin sau ngày chốt",
            url="https://example.invalid/tst-future",
            published_at=datetime(2026, 7, 15, 3, 0, tzinfo=UTC),
        )

        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)

        assert sheet.news == []


class TestAnEmptySectionIsStillASection:
    def test_the_field_is_present_and_empty_rather_than_absent(
        self, session: Session, built: tuple[int, int]
    ) -> None:
        """The web guard lists `news` in REQUIRED_ARRAYS, so an absent key refuses to
        render rather than producing a page quietly missing a section."""
        run_id, _ = built

        sheet = build_fact_sheet(session, "TST", AS_OF, run_id)

        assert sheet.news == []
        assert "news" in sheet.model_dump()
