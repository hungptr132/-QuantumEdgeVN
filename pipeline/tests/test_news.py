"""News tests. Fully offline: every feed response comes from a recorded or constructed
fixture under `tests/fixtures/rss/`, served through an `httpx.MockTransport`. Nothing here
opens a socket, so the suite stays deterministic and still fails the day a publisher
changes shape — at which point re-recording the fixture is how you find out what moved.

Four of the fixtures were recorded from the live feeds on 2026-10-07 and trimmed to their
first three `<item>` elements, otherwise byte-for-byte. The rest are constructed, because
the behaviour they pin (an unparseable date, a missing link, an Atom feed, an HTML error
page served under HTTP 200) is behaviour the live feeds do not exhibit today and must not
be left untested until they do.

The load-bearing test in this file is `TestNoBodyEverStored`. Everything else is
correctness; that one is the licence.
"""

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tenacity import stop_after_attempt, wait_none

from quantumedge.config import Settings, load_settings
from quantumedge.db.enums import PipelineStage
from quantumedge.extract import news
from quantumedge.extract.news import FeedFormatError
from quantumedge.ingest import rss_news
from quantumedge.ingest.base import RawPayload, store_raw
from quantumedge.runs import start_run

FIXTURES = Path(__file__).parent / "fixtures" / "rss"

# Planted in every constructed fixture's <description>/<summary>/<content>. If this string
# reaches any column of news_item, the copyright guarantee is broken.
BODY_MARKER = "KHONGLUUTRUNOIDUNG"

RECORDED = (
    "cafef_chungkhoan.xml",
    "vnexpress_kinhdoanh.xml",
    "vietstock_chungkhoan.xml",
    "tinnhanhchungkhoan_home.xml",
)

TEST_FEED_CODE = "kiemthu_rss"
TEST_PUBLISHER = "Báo Kiểm Thử"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ───────────────────────── fixtures ─────────────────────────


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def specs(settings: Settings) -> list[rss_news.FeedSpec]:
    return rss_news.load_feed_specs(settings)


@pytest.fixture
def news_tickers(session: Session) -> dict[str, int]:
    """Two tickers with aliases, chosen so one exact match and one near miss are possible.

    NKG carries both "Thép Nam Kim" and the shorter "Nam Kim", because the near-miss case
    worth pinning is a headline containing "Nam Kimono" — a prefix collision that any
    similarity-ranked match would happily accept.
    """
    session.execute(
        text(
            "INSERT INTO exchange (code, name) VALUES ('HOSE', 'Ho Chi Minh Stock Exchange') "
            "ON CONFLICT (code) DO NOTHING"
        )
    )
    sector_id = session.execute(
        text(
            "INSERT INTO sector (code, name_vi) VALUES ('news_test_sector', 'Ngành kiểm thử') "
            "ON CONFLICT (code) DO UPDATE SET name_vi = EXCLUDED.name_vi RETURNING id"
        )
    ).scalar_one()

    ids: dict[str, int] = {}
    companies = {
        "NKG": ("Nam Kim", "Công ty Cổ phần Thép Nam Kim"),
        "HAX": ("Hàng Xanh", "Công ty Cổ phần Dịch vụ Ô tô Hàng Xanh"),
    }
    for symbol, (short_name, legal) in companies.items():
        company_id = session.execute(
            text(
                "INSERT INTO company (legal_name_vi, short_name, sector_id) "
                "VALUES (:legal, :short, :sector) RETURNING id"
            ),
            {"legal": legal, "short": short_name, "sector": sector_id},
        ).scalar_one()
        ids[symbol] = int(
            session.execute(
                text(
                    "INSERT INTO ticker (symbol, company_id, exchange_code, is_covered) "
                    "VALUES (:symbol, :company, 'HOSE', true) RETURNING id"
                ),
                {"symbol": symbol, "company": company_id},
            ).scalar_one()
        )

    aliases = [
        (ids["NKG"], "Thép Nam Kim", "brand"),
        (ids["NKG"], "Nam Kim", "brand"),
        (ids["HAX"], "Hàng Xanh", "brand"),
        # Two characters after normalisation. Must be ignored outright, or it matches
        # every third Vietnamese headline.
        (ids["HAX"], "HX", "nickname"),
    ]
    for ticker_id, alias, kind in aliases:
        session.execute(
            text(
                "INSERT INTO ticker_alias (ticker_id, alias, alias_norm, alias_kind) "
                "VALUES (:t, :a, unaccent(lower(:a)), :k)"
            ),
            {"t": ticker_id, "a": alias, "k": kind},
        )
    return ids


@pytest.fixture
def news_source(session: Session, specs: list[rss_news.FeedSpec]) -> None:
    """`source` rows for every seeded feed plus the synthetic test feed.

    store_raw refuses an unknown source code by design, so this is also a standing check
    that every code in news_sources.yaml is one the coordinator must seed.
    """
    rows = [(s.code, s.name, s.base_url, s.rate_limit_per_min) for s in specs]
    rows.append((TEST_FEED_CODE, "Báo Kiểm Thử (RSS)", "https://kiemthu.example", 10))
    for code, name, base_url, limit in rows:
        session.execute(
            text(
                "INSERT INTO source (code, kind, name, base_url, rate_limit_per_min) "
                "VALUES (:code, 'rss', :name, :base_url, :limit) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "name": name, "base_url": base_url, "limit": limit},
        )


@pytest.fixture
def ingest_run_id(session: Session, news_source: None) -> int:
    return start_run(session, PipelineStage.INGEST, "test_news", source_code=TEST_FEED_CODE)


@pytest.fixture
def extract_run_id(session: Session, news_source: None) -> int:
    return start_run(session, PipelineStage.EXTRACT, "test_news_extract")


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the failure paths honest without paying tenacity's backoff in test time."""
    monkeypatch.setattr(rss_news._get.retry, "wait", wait_none())
    monkeypatch.setattr(rss_news._get.retry, "stop", stop_after_attempt(2))


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rss_news.time, "sleep", lambda _s: None)


def store_feed(
    session: Session,
    run_id: int,
    xml_text: str,
    *,
    code: str = TEST_FEED_CODE,
    publisher: str = TEST_PUBLISHER,
    extra: dict[str, Any] | None = None,
) -> int:
    """Put one feed document into raw_document the way ingest would, and return its id."""
    body: dict[str, Any] = {
        "feed_code": code,
        "feed_url": f"https://kiemthu.example/{code}.rss",
        "publisher": publisher,
        "lang": "vi",
        "content_type": "application/rss+xml; charset=utf-8",
        "encoding": "utf-8",
        "looks_like_xml": True,
        "feed_xml": xml_text,
    }
    if extra:
        body.update(extra)
    result = store_raw(
        session,
        run_id,
        [RawPayload(source_code=code, doc_type=rss_news.DOC_TYPE, body=body)],
    )
    assert result.document_ids, "fixture document was not inserted"
    return result.document_ids[0]


def mock_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


# ───────────────────────── the seed file ─────────────────────────


class TestFeedSpecs:
    def test_four_feeds_declared(self, specs: list[rss_news.FeedSpec]) -> None:
        assert [s.code for s in specs] == [
            "cafef_rss",
            "vnexpress_kinhdoanh_rss",
            "vietstock_chungkhoan_rss",
            "tinnhanhchungkhoan_rss",
        ]

    def test_every_feed_records_its_robots_position(self, specs: list[rss_news.FeedSpec]) -> None:
        """A feed whose access basis nobody wrote down is a feed nobody checked."""
        for spec in specs:
            assert len(spec.robots_note) > 80, spec.code
            assert "robots.txt" in spec.robots_note, spec.code
            assert spec.kind == "rss"
            assert spec.feed_url.startswith("https://")
            assert spec.rate_limit_per_min > 0

    def test_seed_file_declares_no_body_field(self, settings: Settings) -> None:
        """Nothing in the feed list may invite a body/summary/description column."""
        raw = (settings.seeds_dir / rss_news.SEED_FILE).read_text(encoding="utf-8")
        for line in raw.splitlines():
            if line.lstrip().startswith("#"):
                continue  # the comment block explains the rule; it may name the fields
            assert not re.match(r"\s*-?\s*(body|summary|description|content)\s*:", line), line

    def test_missing_required_field_is_an_error(self, settings: Settings, tmp_path: Path) -> None:
        (tmp_path / rss_news.SEED_FILE).write_text(
            "feeds:\n"
            "  - code: x\n"
            "    name: X\n"
            "    publisher: X\n"
            "    base_url: https://x.example\n"
            "    feed_url: https://x.example/f.rss\n",
            encoding="utf-8",
        )
        bad = settings.model_copy(update={"seeds_dir": tmp_path})
        with pytest.raises(rss_news.FeedSpecError, match="robots_note"):
            rss_news.load_feed_specs(bad)

    def test_duplicate_code_is_an_error(self, settings: Settings, tmp_path: Path) -> None:
        entry = (
            "  - code: dup\n"
            "    name: D\n"
            "    publisher: D\n"
            "    base_url: https://d.example\n"
            "    feed_url: https://d.example/f.rss\n"
            "    robots_note: checked\n"
        )
        (tmp_path / rss_news.SEED_FILE).write_text(f"feeds:\n{entry}{entry}", encoding="utf-8")
        bad = settings.model_copy(update={"seeds_dir": tmp_path})
        with pytest.raises(rss_news.FeedSpecError, match="duplicate"):
            rss_news.load_feed_specs(bad)

    def test_unknown_code_filter_is_an_error(self, settings: Settings) -> None:
        with pytest.raises(rss_news.FeedSpecError, match="unknown feed code"):
            rss_news.load_feed_specs(settings, codes=["cafef_rss", "nope_rss"])

    def test_code_filter_selects(self, settings: Settings) -> None:
        selected = rss_news.load_feed_specs(settings, codes=["cafef_rss"])
        assert [s.code for s in selected] == ["cafef_rss"]


# ───────────────────────── ingest ─────────────────────────


class TestFetch:
    def test_stores_feed_text_verbatim_and_parses_nothing(
        self, specs: list[rss_news.FeedSpec], no_sleep: None
    ) -> None:
        body = fixture_text("cafef_chungkhoan.xml")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=body, headers={"content-type": "text/xml; charset=utf-8"}
            )

        with mock_client(handler) as client:
            results = rss_news.fetch_feeds(specs[:1], client=client)

        assert len(results) == 1
        payload = results[0].payload
        assert payload is not None
        assert isinstance(payload.body, dict)
        assert payload.body["feed_xml"] == body
        assert payload.body["publisher"] == "CafeF"
        assert payload.body["looks_like_xml"] is True
        # The envelope carries provenance, never interpretation: no item count, no titles,
        # no dates. Those are extract's to decide, against the stored bytes.
        assert set(payload.body) == {
            "feed_code",
            "feed_url",
            "publisher",
            "lang",
            "content_type",
            "encoding",
            "looks_like_xml",
            "feed_xml",
        }

    def test_empty_body_is_a_legitimate_answer_not_a_failure(
        self, specs: list[rss_news.FeedSpec]
    ) -> None:
        """A feed with nothing in it must not read as a broken feed in the manifest."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        with mock_client(handler) as client:
            results = rss_news.fetch_feeds(specs[:1], client=client)

        assert results[0].payload is None
        assert results[0].failed is False
        assert results[0].http_status == 200
        assert "empty" in results[0].note

        stats = rss_news.fetch_stats(results)
        assert stats["empty"] == 1
        assert stats["failed"] == 0

    def test_http_failure_is_distinguished_from_empty(
        self, specs: list[rss_news.FeedSpec], no_retry_delay: None
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        with mock_client(handler) as client:
            results = rss_news.fetch_feeds(specs[:1], client=client)

        assert results[0].payload is None
        assert results[0].failed is True
        assert results[0].note == "HTTP 503"
        stats = rss_news.fetch_stats(results)
        assert stats == {
            "feeds": 1,
            "fetched": 0,
            "empty": 0,
            "failed": 1,
            "bytes": 0,
            "notes": {"cafef_rss": "HTTP 503"},
        }

    def test_transport_error_is_a_failure(
        self, specs: list[rss_news.FeedSpec], no_retry_delay: None
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns", request=request)

        with mock_client(handler) as client:
            results = rss_news.fetch_feeds(specs[:1], client=client)

        assert results[0].failed is True
        assert "ConnectError" in results[0].note

    def test_one_feed_failing_does_not_cost_the_others(
        self, specs: list[rss_news.FeedSpec], no_sleep: None, no_retry_delay: None
    ) -> None:
        body = fixture_text("vietstock_chungkhoan.xml")

        def handler(request: httpx.Request) -> httpx.Response:
            if "vietstock" in str(request.url):
                return httpx.Response(500)
            return httpx.Response(200, text=body)

        with mock_client(handler) as client:
            results = rss_news.fetch_feeds(specs, client=client)

        stats = rss_news.fetch_stats(results)
        assert stats["feeds"] == 4
        assert stats["fetched"] == 3
        assert stats["failed"] == 1

    def test_db_rate_limit_overrides_the_seeded_one(
        self, session: Session, news_source: None, specs: list[rss_news.FeedSpec]
    ) -> None:
        session.execute(text("UPDATE source SET rate_limit_per_min = 2 WHERE code = 'cafef_rss'"))
        applied = {
            s.code: s.rate_limit_per_min for s in rss_news.apply_db_rate_limits(session, specs)
        }
        assert applied["cafef_rss"] == 2
        # Unchanged rows keep their seeded value rather than being reset to a default.
        assert applied["vietstock_chungkhoan_rss"] == 6

    def test_rate_limit_spacing_is_requested_between_feeds(
        self, specs: list[rss_news.FeedSpec], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr(rss_news.time, "sleep", lambda s: slept.append(s))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=fixture_text("empty_feed.xml"))

        with mock_client(handler) as client:
            rss_news.fetch_feeds(specs, client=client)

        # One gap per feed after the first, each derived from that feed's own limit.
        assert len(slept) == 3
        assert slept == [60.0 / s.rate_limit_per_min for s in specs[1:]]

    def test_storing_twice_is_unchanged_not_duplicated(
        self, session: Session, ingest_run_id: int
    ) -> None:
        body = fixture_text("cafef_chungkhoan.xml")
        first = store_raw(
            session,
            ingest_run_id,
            [
                RawPayload(
                    source_code=TEST_FEED_CODE,
                    doc_type=rss_news.DOC_TYPE,
                    body={"feed_code": TEST_FEED_CODE, "feed_xml": body},
                )
            ],
        )
        second = store_raw(
            session,
            ingest_run_id,
            [
                RawPayload(
                    source_code=TEST_FEED_CODE,
                    doc_type=rss_news.DOC_TYPE,
                    body={"feed_code": TEST_FEED_CODE, "feed_xml": body},
                )
            ],
        )
        assert first.inserted == 1
        assert second.inserted == 0
        assert second.unchanged == 1


# ───────────────────────── parsing ─────────────────────────


class TestParsing:
    @pytest.mark.parametrize("name", RECORDED)
    def test_recorded_feed_yields_its_items(self, name: str) -> None:
        items = news.parse_feed(fixture_text(name))
        assert len(items) == 3, name
        for item in items:
            assert item.headline
            assert item.url and item.url.startswith("http")
            assert item.published_raw

    def test_atom_parses_rather_than_yielding_nothing(self) -> None:
        """The failure to avoid is a parser that answers an Atom feed with zero items."""
        items = news.parse_feed(fixture_text("atom_sample.xml"))
        assert len(items) == 2
        # rel="alternate" wins over the enclosure listed before it.
        assert items[0].url == "https://atom.kiemthu.example/nkg-phat-hanh-rieng-le"
        # A <link> with no rel defaults to alternate per RFC 4287.
        assert items[1].url == "https://atom.kiemthu.example/hax-dai-ly-thu-20"
        assert items[0].published_raw == "2026-10-07T02:15:00Z"

    def test_empty_but_well_formed_feed_is_zero_items(self) -> None:
        assert news.parse_feed(fixture_text("empty_feed.xml")) == []

    def test_html_error_page_raises_rather_than_reading_as_quiet_news(self) -> None:
        """Real-world HTML is not well-formed XML (unclosed <meta>), so this trips the
        parse check rather than the root check. Either is a loud failure; what matters is
        that it is never an empty item list."""
        with pytest.raises(FeedFormatError):
            news.parse_feed(fixture_text("not_a_feed.html"))

    def test_well_formed_non_feed_raises_on_its_root(self) -> None:
        """The other half: valid XML that simply is not a feed."""
        with pytest.raises(FeedFormatError, match="unrecognised feed root"):
            news.parse_feed('<opml version="2.0"><body/></opml>')
        with pytest.raises(FeedFormatError, match="unrecognised feed root"):
            news.parse_feed("<html><body><h1>Vui lòng xác minh</h1></body></html>")

    def test_malformed_xml_raises(self) -> None:
        with pytest.raises(FeedFormatError, match="not well-formed"):
            news.parse_feed("<rss><channel><item><title>x</title></channel></rss>")

    def test_empty_document_raises(self) -> None:
        with pytest.raises(FeedFormatError, match="empty"):
            news.parse_feed("   ")

    def test_rss_without_channel_raises(self) -> None:
        with pytest.raises(FeedFormatError, match="without a <channel>"):
            news.parse_feed('<rss version="2.0"></rss>')

    def test_rss1_rdf_parses(self) -> None:
        rdf = (
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns="http://purl.org/rss/1.0/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<item rdf:about="https://x.example/a">'
            "<title>Thép Nam Kim</title><link>https://x.example/a</link>"
            "<dc:date>2026-10-07T09:00:00+07:00</dc:date></item></rdf:RDF>"
        )
        items = news.parse_feed(rdf)
        assert len(items) == 1
        assert items[0].url == "https://x.example/a"

    def test_non_permalink_guid_is_not_a_link(self) -> None:
        items = news.parse_feed(fixture_text("news_edge_cases.xml"))
        no_link = [i for i in items if i.url is None]
        assert len(no_link) == 1
        assert "Bản tin tài chính" in no_link[0].headline

    def test_parsed_items_carry_no_article_text(self) -> None:
        """The parser must not even read <description>, <summary> or <content>."""
        for name in (*RECORDED, "news_edge_cases.xml", "atom_sample.xml"):
            source = fixture_text(name)
            for item in news.parse_feed(source):
                blob = " ".join(
                    str(v) for v in (item.headline, item.url, item.published_raw, item.element_path)
                )
                assert BODY_MARKER not in blob, name
                # Every recorded feed's description is long prose; a headline is not.
                assert len(item.headline) <= news.MAX_HEADLINE


# ───────────────────────── dates ─────────────────────────


class TestDates:
    def test_colon_offset_is_repaired_not_silently_dropped(self) -> None:
        """Tin nhanh chứng khoán writes "+07:00". `parsedate_to_datetime` accepts the
        string and returns a NAIVE datetime, so the offset vanishes without an error.
        Inheriting that would date every one of its articles seven hours early."""
        parsed, reason = news.parse_published("Wed, 07 Oct 2026 09:00:00 +07:00")
        assert reason == ""
        assert parsed is not None
        assert parsed.isoformat() == "2026-10-07T02:00:00+00:00"

    def test_two_digit_year_rfc822(self) -> None:
        """CafeF's form."""
        parsed, _ = news.parse_published("Wed, 07 Oct 26 15:52:00 +0700")
        assert parsed is not None
        assert parsed.isoformat() == "2026-10-07T08:52:00+00:00"

    def test_four_digit_year_rfc822(self) -> None:
        parsed, _ = news.parse_published("Wed, 07 Oct 2026 14:15:00 +0700")
        assert parsed is not None
        assert parsed.isoformat() == "2026-10-07T07:15:00+00:00"

    def test_rfc3339_zulu(self) -> None:
        parsed, _ = news.parse_published("2026-10-07T02:15:00Z")
        assert parsed is not None
        assert parsed.isoformat() == "2026-10-07T02:15:00+00:00"

    def test_gmt_keyword(self) -> None:
        parsed, _ = news.parse_published("Wed, 07 Oct 2026 14:15:00 GMT")
        assert parsed is not None
        assert parsed.isoformat() == "2026-10-07T14:15:00+00:00"

    @pytest.mark.parametrize(
        "raw",
        [
            "Wed, 07 Oct 2026 14:15:00",  # no offset at all
            "Wed, 07 Oct 2026 14:15:00 -0000",  # RFC 822 for "offset unknown"
            "hôm qua lúc 14:00",
            "07/10/2026",
            "",
            None,
        ],
    )
    def test_anything_without_a_real_offset_is_a_failure(self, raw: str | None) -> None:
        """An offset we were not given is not an offset we may supply."""
        parsed, reason = news.parse_published(raw)
        assert parsed is None
        assert reason


# ───────────────────────── attribution ─────────────────────────


class TestAttribution:
    def _match(self, session: Session, headline: str) -> list[tuple[int, str]]:
        index = news.build_alias_index(session)
        (normalised,) = news.normalise(session, [headline])
        return index.matches(normalised)

    def test_exact_alias_attributes(self, session: Session, news_tickers: dict[str, int]) -> None:
        hits = self._match(session, "Thép Nam Kim báo lãi quý III tăng 42%")
        assert [t for t, _ in hits] == [news_tickers["NKG"]]

    def test_diacritics_are_folded_the_way_postgres_folded_the_alias(
        self, session: Session, news_tickers: dict[str, int]
    ) -> None:
        hits = self._match(session, "THEP NAM KIM chot quyen")
        assert [t for t, _ in hits] == [news_tickers["NKG"]]

    def test_bare_symbol_as_whole_token_attributes(
        self, session: Session, news_tickers: dict[str, int]
    ) -> None:
        hits = self._match(session, "HAX chốt quyền nhận cổ tức tiền mặt")
        assert [t for t, _ in hits] == [news_tickers["HAX"]]

    @pytest.mark.parametrize(
        "headline",
        [
            "Nam Kimono mở thêm 12 cửa hàng tại Hà Nội",  # alias is a prefix of a word
            "Cổ phiếu NKGP bị đưa vào diện cảnh báo",  # symbol is a prefix of a token
            "Chuyện của ANKG và những mã nhỏ",  # symbol is a suffix of a token
            "Thị trường hàng hoá xanh trở lại",  # alias words present but not adjacent
        ],
    )
    def test_near_miss_does_not_attribute(
        self, session: Session, news_tickers: dict[str, int], headline: str
    ) -> None:
        """pg_trgm would rank every one of these as a strong match. That is why it is not
        used here: a wrongly attributed headline about a named company is the failure this
        product exists to prevent, and nobody reviews an attribution before it ships."""
        assert self._match(session, headline) == []

    def test_short_alias_is_ignored_entirely(
        self, session: Session, news_tickers: dict[str, int]
    ) -> None:
        """ "HX" is a seeded alias and must still never match, or it attributes any headline
        containing it as a word to HAX."""
        index = news.build_alias_index(session)
        assert all(len(alias) >= news.MIN_ALIAS_LEN for _, _, alias in index.patterns)
        assert self._match(session, "Chỉ số HX tăng nhẹ") == []

    def test_whitespace_in_a_headline_does_not_break_an_exact_match(
        self, session: Session, news_tickers: dict[str, int]
    ) -> None:
        hits = self._match(session, "Thép   Nam\nKim\tbáo lãi")
        assert [t for t, _ in hits] == [news_tickers["NKG"]]

    def test_two_companies_in_one_headline_match_both(
        self, session: Session, news_tickers: dict[str, int]
    ) -> None:
        hits = self._match(session, "Thép Nam Kim và Hàng Xanh cùng lọt rổ VN30 kỳ này")
        assert sorted(t for t, _ in hits) == sorted(news_tickers.values())


# ───────────────────────── the extract pass ─────────────────────────


class TestExtract:
    def test_edge_case_feed_counts_every_item_into_exactly_one_bucket(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        result = news.extract_news(session, extract_run_id)

        assert result.documents_read == 1
        assert result.documents_failed == 0
        assert result.items_seen == 8
        assert result.written == 6
        assert result.unchanged == 0
        assert result.attributed == 3
        assert result.unattributed == 3
        assert result.skipped_no_date == 1
        assert result.skipped_no_link == 1
        assert result.skipped_no_headline == 0
        assert result.ambiguous_unattributed == 1
        # Every item lands in exactly one terminal bucket. The ambiguous one is counted in
        # `unattributed`, not in `skipped`, because it was stored.
        assert result.written + result.unchanged + result.skipped == result.items_seen

    def test_attribution_lands_on_the_right_rows(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        news.extract_news(session, extract_run_id)

        rows = dict(
            session.execute(
                text("SELECT headline, ticker_id FROM news_item ORDER BY published_at")
            ).all()
        )
        by_prefix = {h[:20]: t for h, t in rows.items()}
        assert by_prefix["Thép Nam Kim báo lãi"] == news_tickers["NKG"]
        assert by_prefix["HAX chốt quyền nhận "] == news_tickers["HAX"]
        assert by_prefix["Hàng Xanh công bố kế"] == news_tickers["HAX"]
        assert by_prefix["Nam Kimono mở thêm 1"] is None
        assert by_prefix["Cổ phiếu NKGP bị đưa"] is None
        # Two companies named: stored, attributed to neither.
        assert by_prefix["Thép Nam Kim và Hàng"] is None

    def test_colon_offset_item_is_stored_at_the_right_instant(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        news.extract_news(session, extract_run_id)
        stored = session.execute(
            text("SELECT published_at FROM news_item WHERE headline LIKE 'Hàng Xanh công bố%'")
        ).scalar_one()
        assert stored.isoformat() == "2026-10-07T02:00:00+00:00"

    def test_unparseable_date_is_absent_not_now(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        """The whole point: no row at all, rather than a stale article dated today."""
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        result = news.extract_news(session, extract_run_id)

        assert (
            session.execute(
                text("SELECT count(*) FROM news_item WHERE headline LIKE 'Thị trường phiên%'")
            ).scalar_one()
            == 0
        )
        assert result.skipped_no_date == 1
        assert any("unparseable" in n for n in result.notes)

    def test_rerun_over_the_same_document_writes_nothing(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        first = news.extract_news(session, extract_run_id)
        before = session.execute(text("SELECT count(*) FROM news_item")).scalar_one()

        second = news.extract_news(session, extract_run_id)
        after = session.execute(text("SELECT count(*) FROM news_item")).scalar_one()

        assert first.written == 6
        assert second.written == 0
        assert second.unchanged == 6
        assert before == after == 6

    def test_same_article_in_a_later_document_is_unchanged(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        """A rolling feed re-serves yesterday's articles. url is UNIQUE, so the second
        sighting must be `unchanged` and must keep pointing at the first document."""
        original = fixture_text("news_edge_cases.xml")
        first_doc = store_feed(session, ingest_run_id, original)
        news.extract_news(session, extract_run_id)

        # Same items, different channel timestamp - a genuinely different document.
        rolled = original.replace(
            "<pubDate>Wed, 07 Oct 2026 10:00:00 +0700</pubDate>",
            "<pubDate>Wed, 07 Oct 2026 11:00:00 +0700</pubDate>",
            1,
        )
        second_doc = store_feed(session, ingest_run_id, rolled)
        assert second_doc != first_doc

        second = news.extract_news(session, extract_run_id)
        assert second.documents_read == 2
        assert second.written == 0
        assert second.unchanged == 12
        assert session.execute(text("SELECT count(*) FROM news_item")).scalar_one() == 6
        # Provenance does not drift to whichever document was processed last.
        assert (
            session.execute(text("SELECT DISTINCT raw_document_id FROM news_item")).scalar_one()
            == first_doc
        )

    def test_recorded_feeds_all_extract(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
        specs: list[rss_news.FeedSpec],
    ) -> None:
        """Every live feed's real shape, including dates and link forms, round-trips."""
        for spec, name in zip(specs, RECORDED, strict=True):
            store_feed(
                session,
                ingest_run_id,
                fixture_text(name),
                code=spec.code,
                publisher=spec.publisher,
            )
        result = news.extract_news(session, extract_run_id)

        assert result.documents_read == 4
        assert result.documents_failed == 0
        assert result.items_seen == 12
        assert result.written == 12
        assert result.skipped == 0
        publishers = set(
            session.execute(text("SELECT DISTINCT publisher FROM news_item")).scalars()
        )
        assert publishers == {s.publisher for s in specs}

    def test_publisher_comes_from_the_envelope_not_the_channel_title(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("cafef_chungkhoan.xml"))
        news.extract_news(session, extract_run_id)
        assert set(session.execute(text("SELECT DISTINCT publisher FROM news_item")).scalars()) == {
            TEST_PUBLISHER
        }

    def test_empty_feed_is_zero_rows_and_zero_failures(
        self, session: Session, ingest_run_id: int, extract_run_id: int
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("empty_feed.xml"))
        result = news.extract_news(session, extract_run_id)
        assert result.documents_read == 1
        assert result.documents_failed == 0
        assert result.items_seen == 0
        assert result.written == 0
        assert any("0 items" in n for n in result.notes)

    def test_html_document_is_a_failure_not_an_empty_feed(
        self, session: Session, ingest_run_id: int, extract_run_id: int
    ) -> None:
        """The distinction the run manifest depends on: a provider serving a verification
        wall must not read the same as a quiet news day."""
        store_feed(session, ingest_run_id, fixture_text("not_a_feed.html"))
        result = news.extract_news(session, extract_run_id)
        assert result.documents_failed == 1
        assert result.items_seen == 0
        assert result.as_stats()["documents_failed"] == 1

    def test_one_bad_document_does_not_cost_the_others_their_headlines(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("not_a_feed.html"), code="cafef_rss")
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        result = news.extract_news(session, extract_run_id)
        assert result.documents_failed == 1
        assert result.written == 6

    def test_document_without_a_publisher_is_a_failure_not_a_guess(
        self, session: Session, ingest_run_id: int, extract_run_id: int
    ) -> None:
        store_feed(
            session,
            ingest_run_id,
            fixture_text("cafef_chungkhoan.xml"),
            publisher="",
        )
        result = news.extract_news(session, extract_run_id)
        assert result.documents_failed == 1
        assert result.written == 0
        assert any("publisher" in n for n in result.notes)

    def test_feed_code_filter_limits_the_pass(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        store_feed(
            session,
            ingest_run_id,
            fixture_text("cafef_chungkhoan.xml"),
            code="cafef_rss",
            publisher="CafeF",
        )
        result = news.extract_news(session, extract_run_id, feed_codes=["cafef_rss"])
        assert result.documents_read == 1
        assert result.written == 3

    def test_atom_document_extracts(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("atom_sample.xml"))
        result = news.extract_news(session, extract_run_id)
        assert result.items_seen == 2
        assert result.written == 2
        assert result.attributed == 2
        assert result.skipped == 0

    def test_editor_note_is_ours_and_starts_empty(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        """The only prose about an article we may publish is written by a person."""
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        news.extract_news(session, extract_run_id)
        assert (
            session.execute(
                text("SELECT count(*) FROM news_item WHERE editor_note_vi IS NOT NULL")
            ).scalar_one()
            == 0
        )

    def test_notes_are_capped_so_a_run_row_is_not_a_log_file(self) -> None:
        result = news.NewsExtractResult(notes=[f"n{i}" for i in range(60)])
        stats = result.as_stats()
        assert len(stats["notes"]) == 50
        assert stats["notes_truncated"] == 10


# ───────────────────────── the licence ─────────────────────────


class TestNoBodyEverStored:
    """`news_item` has no body column, and nothing may smuggle one in.

    These are the tests that make the copyright position checkable rather than cultural.
    """

    def test_table_has_exactly_the_columns_it_is_allowed(self, session: Session) -> None:
        """Pinned as an exact set, not a denylist: a future `summary` or `excerpt` column
        has to fail here and be argued for, rather than appearing in a migration."""
        columns = set(
            session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'news_item'"
                )
            ).scalars()
        )
        assert columns == {
            "id",
            "raw_document_id",
            "ticker_id",
            "headline",
            "url",
            "publisher",
            "published_at",
            "lang",
            "editor_note_vi",
            "created_at",
        }

    def test_no_article_text_reaches_any_text_column(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        """Every character-typed column of every written row is searched for the marker
        planted in the fixture's <description>. Columns are enumerated from
        information_schema, so a column added later is covered without editing this test.
        """
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        store_feed(session, ingest_run_id, fixture_text("atom_sample.xml"), code="cafef_rss")
        news.extract_news(session, extract_run_id)

        text_columns = list(
            session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'news_item' "
                    "AND data_type IN ('text', 'character varying', 'character') "
                    "ORDER BY column_name"
                )
            ).scalars()
        )
        assert text_columns, "no text columns found - the query is wrong, not the schema"

        rows = session.execute(
            text(f"SELECT {', '.join(text_columns)} FROM news_item")  # noqa: S608
        ).all()
        assert rows, "nothing was written, so this test proves nothing"
        for row in rows:
            for column, value in zip(text_columns, row, strict=True):
                if value is None:
                    continue
                assert BODY_MARKER not in str(value), f"{column} carries article body text"

    def test_the_marker_really_is_in_the_fixture(self) -> None:
        """Guards the test above from passing because the fixture lost its body text."""
        for name in ("news_edge_cases.xml", "atom_sample.xml"):
            assert fixture_text(name).count(BODY_MARKER) >= 2, name

    def test_headline_is_the_title_only_never_title_plus_summary(
        self,
        session: Session,
        ingest_run_id: int,
        extract_run_id: int,
        news_tickers: dict[str, int],
    ) -> None:
        store_feed(session, ingest_run_id, fixture_text("news_edge_cases.xml"))
        news.extract_news(session, extract_run_id)
        headlines = list(session.execute(text("SELECT headline FROM news_item")).scalars())
        assert "Thép Nam Kim báo lãi quý III tăng 42%" in headlines
        for headline in headlines:
            assert len(headline) <= news.MAX_HEADLINE

    def test_feed_item_exposes_no_body_field(self) -> None:
        """The parser's own data structure has nowhere to put article text."""
        assert set(news.FeedItem.__dataclass_fields__) == {
            "headline",
            "url",
            "published_raw",
            "element_path",
        }
