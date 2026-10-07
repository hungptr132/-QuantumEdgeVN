"""Stored feed documents → `news_item`. Headline, link, publisher, timestamp. Nothing else.

Reads `raw_document` only. Writes `news_item` only.

**The copyright rule is schema, not etiquette.** `news_item` has no body column, and this
module never reads a feed's `<description>`, `<content:encoded>`, `<summary>` or
`media:*` elements — not into a variable, not into a log line. The one piece of prose
about an article we may publish is `editor_note_vi`, which a person writes and this module
leaves NULL. If a future change needs a summary, that is a licensing conversation, not a
column.

**Attribution is exact or absent.** A headline is bound to a ticker only when a stored
alias, or the bare symbol, appears in it as a whole token. `pg_trgm` is installed and would
happily rank "Nam Kimono" against "Nam Kim"; it exists for the search box, where a human
sees the candidates and picks. Attributing a headline is the opposite situation: nobody
reviews it, and the headline may be negative about a named company. A false attribution
there is the single worst thing this pipeline can emit, and it is worse than the headline
simply not appearing on any ticker page. So: exact token match, and a headline matching two
different companies is left unattributed rather than assigned to the first one found.

**A date that will not parse means the item is dropped.** Defaulting to `now()` would date
a three-day-old article today, which is both a wrong number and an invisible one. The item
is skipped and counted with a reason, so the loss is in the run manifest rather than
silent.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.ingest.rss_news import DOC_TYPE

log = structlog.get_logger(__name__)

EXTRACTOR_VERSION = "news/v1"

# The schema's own ceiling (news_headline_len). Checked here so an over-long headline is a
# counted skip with a reason rather than an IntegrityError that aborts the whole document.
MAX_HEADLINE = 400

_ATOM = "http://www.w3.org/2005/Atom"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RSS10 = "http://purl.org/rss/1.0/"
_DC = "http://purl.org/dc/elements/1.1/"

# Only an http(s) URL is a link. A feed whose <guid> is "node/18827" is not offering a
# location, and storing it would produce a news_item whose `url` resolves to nothing.
_HTTP_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)

# "+07:00" instead of "+0700". RFC 822 forbids the colon, RFC 3339 requires it, and
# Tin nhanh chứng khoán emits the RFC 3339 form inside an RSS <pubDate>.
# email.utils.parsedate_to_datetime does not reject it - it silently drops the offset and
# hands back a naive datetime, so the fix below is what stands between a stored timestamp
# and a uniform seven-hour error on one publisher's entire feed.
_COLON_OFFSET = re.compile(r"([+-]\d{2}):(\d{2})\s*$")

# Token boundary for exact alias matching over an unaccented, lowercased headline.
_WORD = "0-9a-z"

# An alias shorter than this is not matched at all. Normalisation folds diacritics, so a
# one- or two-character alias ("an", "vi") collides with ordinary Vietnamese words and
# would attribute half the feed to one company. Every real ticker symbol is >= 3.
MIN_ALIAS_LEN = 3


class FeedFormatError(ValueError):
    """The document is not a feed this module recognises.

    Raised rather than returning an empty item list. "Zero items" is a legitimate state
    for a real feed and must stay distinguishable from "this was an HTML error page" -
    otherwise a provider quietly serving a login wall reads as a quiet news week.
    """


@dataclass(frozen=True)
class FeedItem:
    """One item, reduced to the four fields that may be stored, plus the raw date text."""

    headline: str
    url: str | None
    published_raw: str | None
    element_path: str


@dataclass
class NewsExtractResult:
    """Counts the run manifest needs. Every item is in exactly one terminal bucket:
    written, unchanged, or one of the skipped_* counters."""

    documents_read: int = 0
    documents_failed: int = 0
    items_seen: int = 0
    written: int = 0
    unchanged: int = 0
    attributed: int = 0
    unattributed: int = 0
    skipped_no_date: int = 0
    skipped_no_link: int = 0
    skipped_no_headline: int = 0
    # Not a skip: an ambiguous headline is still stored, just with ticker_id NULL. It is
    # counted separately because "we saw it and declined to attribute it" is a different
    # fact from "we could not read it", and lumping the two would hide which one is
    # growing. It is therefore part of `unattributed`, never part of `skipped`.
    ambiguous_unattributed: int = 0
    notes: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "documents_read": self.documents_read,
            "documents_failed": self.documents_failed,
            "items_seen": self.items_seen,
            "written": self.written,
            "unchanged": self.unchanged,
            "attributed": self.attributed,
            "unattributed": self.unattributed,
            "skipped_no_date": self.skipped_no_date,
            "skipped_no_link": self.skipped_no_link,
            "skipped_no_headline": self.skipped_no_headline,
            "ambiguous_unattributed": self.ambiguous_unattributed,
        }
        if self.notes:
            # Capped: a malformed feed can produce one note per item, and a run row is not
            # a log file. The counters above stay exact either way.
            stats["notes"] = self.notes[:50]
            if len(self.notes) > 50:
                stats["notes_truncated"] = len(self.notes) - 50
        return stats

    @property
    def skipped(self) -> int:
        """Items that produced no row at all.

        `written + unchanged + skipped == items_seen` holds for every pass. That identity
        is the reason the ambiguous counter is deliberately outside this sum: an item left
        unattributed was still stored.
        """
        return self.skipped_no_date + self.skipped_no_link + self.skipped_no_headline


# ───────────────────────── parsing ─────────────────────────


def parse_feed(xml_text: str) -> list[FeedItem]:
    """RSS 2.0, RSS 1.0 (RDF) or Atom 1.0 → items. Unrecognised input raises.

    Deliberately hand-rolled over ElementTree rather than handed to feedparser: this is
    four known feeds and three well-specified formats, and a lenient parser's helpfulness
    is the problem here, not the solution. feedparser merges `<description>` into
    `entry.summary` as a matter of course, which is the one field that must never be in
    reach, and it answers an HTML error page with an empty item list rather than a
    complaint.
    """
    if not xml_text.strip():
        raise FeedFormatError("document is empty")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FeedFormatError(f"not well-formed XML: {exc}") from exc

    tag = root.tag
    if tag == "rss":
        return _parse_rss2(root)
    if tag == f"{{{_RDF}}}RDF":
        return _parse_rss1(root)
    if tag == f"{{{_ATOM}}}feed":
        return _parse_atom(root)
    raise FeedFormatError(f"unrecognised feed root element <{tag}>")


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = " ".join(element.text.split())
    return value or None


def _parse_rss2(root: ET.Element) -> list[FeedItem]:
    channel = root.find("channel")
    if channel is None:
        raise FeedFormatError("<rss> without a <channel>")
    items: list[FeedItem] = []
    for i, item in enumerate(channel.findall("item")):
        items.append(
            FeedItem(
                headline=_text(item.find("title")) or "",
                url=_rss2_link(item),
                published_raw=_text(item.find("pubDate")),
                element_path=f"$.feed_xml rss/channel/item[{i}]",
            )
        )
    return items


def _rss2_link(item: ET.Element) -> str | None:
    """`<link>`, else a `<guid>` that explicitly declares itself a permalink.

    RSS 2.0 defaults `isPermaLink` to true when the attribute is absent, but in practice
    feeds omit the attribute while putting an opaque id in the element, so the default is
    not honoured here: the attribute must be present and true, and the value must look like
    an http(s) URL. Both conditions, or no link.
    """
    link = _text(item.find("link"))
    if link and _HTTP_URL.match(link):
        return link
    guid = item.find("guid")
    if guid is not None and (guid.get("isPermaLink") or "").strip().lower() == "true":
        value = _text(guid)
        if value and _HTTP_URL.match(value):
            return value
    return None


def _parse_rss1(root: ET.Element) -> list[FeedItem]:
    items: list[FeedItem] = []
    for i, item in enumerate(root.findall(f"{{{_RSS10}}}item")):
        link = _text(item.find(f"{{{_RSS10}}}link")) or item.get(f"{{{_RDF}}}about")
        items.append(
            FeedItem(
                headline=_text(item.find(f"{{{_RSS10}}}title")) or "",
                url=link if link and _HTTP_URL.match(link) else None,
                published_raw=_text(item.find(f"{{{_DC}}}date")),
                element_path=f"$.feed_xml rdf:RDF/item[{i}]",
            )
        )
    return items


def _parse_atom(root: ET.Element) -> list[FeedItem]:
    items: list[FeedItem] = []
    for i, entry in enumerate(root.findall(f"{{{_ATOM}}}entry")):
        # <published> is when the article appeared; <updated> is when it was last touched
        # and is the only one Atom makes mandatory. Prefer published, because a correction
        # three days later must not re-date the article to the correction.
        when = _text(entry.find(f"{{{_ATOM}}}published")) or _text(
            entry.find(f"{{{_ATOM}}}updated")
        )
        items.append(
            FeedItem(
                headline=_text(entry.find(f"{{{_ATOM}}}title")) or "",
                url=_atom_link(entry),
                published_raw=when,
                element_path=f"$.feed_xml atom:feed/entry[{i}]",
            )
        )
    return items


def _atom_link(entry: ET.Element) -> str | None:
    """`rel="alternate"` is the article; `rel="enclosure"`/`"related"` are not."""
    fallback: str | None = None
    for link in entry.findall(f"{{{_ATOM}}}link"):
        href = (link.get("href") or "").strip()
        if not href or not _HTTP_URL.match(href):
            continue
        rel = (link.get("rel") or "").strip().lower()
        if rel == "alternate":
            return href
        if not rel and fallback is None:
            # Atom defaults a missing rel to "alternate".
            fallback = href
    return fallback


# ───────────────────────── dates ─────────────────────────


def parse_published(raw: str | None) -> tuple[datetime | None, str]:
    """RFC 822 or RFC 3339 → an aware UTC datetime. Returns (None, reason) on failure.

    A naive result is a failure, not a value. The offset is information the publisher
    either gave or did not; supplying one ourselves would be inventing the figure, and a
    seven-hour error on a "published 2 hours ago" line is exactly the kind of small wrong
    number this pipeline refuses to produce.
    """
    if raw is None or not raw.strip():
        return None, "no date element"

    candidate = raw.strip()
    repaired = _COLON_OFFSET.sub(r"\1\2", candidate)

    for attempt in (repaired, candidate):
        parsed = _try_rfc822(attempt) or _try_iso(attempt)
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC), ""

    return None, f"unparseable or offset-less date {candidate!r}"


def _try_rfc822(value: str) -> datetime | None:
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _try_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ───────────────────────── attribution ─────────────────────────


@dataclass(frozen=True)
class AliasIndex:
    """Every alias and symbol that may attribute a headline, pre-normalised and compiled.

    Built once per run: the table is small, and matching in Python against Postgres-
    normalised strings keeps `unaccent(lower(...))` as the single definition of
    "normalised" while letting the token-boundary rule be read in one place.
    """

    patterns: tuple[tuple[re.Pattern[str], int, str], ...]

    def matches(self, headline_norm: str) -> list[tuple[int, str]]:
        """Distinct (ticker_id, matched alias) pairs, one per ticker, longest alias first."""
        hits: dict[int, str] = {}
        for pattern, ticker_id, alias in self.patterns:
            if pattern.search(headline_norm) and ticker_id not in hits:
                hits[ticker_id] = alias
        return sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[1]))


def build_alias_index(session: Session) -> AliasIndex:
    """Load `ticker_alias.alias_norm` plus every bare symbol.

    The symbol is included directly rather than relying on a seeded alias row: a ticker
    with no alias rows must still be attributable from "NKG", and depending on seed
    completeness for that would make attribution silently coverage-dependent.
    """
    rows = session.execute(
        text(
            "SELECT ta.ticker_id, ta.alias_norm AS value "
            "FROM ticker_alias ta "
            "UNION ALL "
            "SELECT t.id, unaccent(lower(t.symbol)) FROM ticker t"
        )
    ).all()

    patterns: list[tuple[re.Pattern[str], int, str]] = []
    seen: set[tuple[int, str]] = set()
    for ticker_id, value in rows:
        alias = _collapse(str(value))
        if len(alias) < MIN_ALIAS_LEN:
            continue
        key = (int(ticker_id), alias)
        if key in seen:
            continue
        seen.add(key)
        patterns.append(
            (
                re.compile(rf"(?<![{_WORD}]){re.escape(alias)}(?![{_WORD}])"),
                int(ticker_id),
                alias,
            )
        )
    # Longest alias first, so "thep nam kim" is tried before "nam kim". Only affects which
    # alias is reported, never which ticker is chosen.
    patterns.sort(key=lambda p: -len(p[2]))
    return AliasIndex(patterns=tuple(patterns))


def _collapse(value: str) -> str:
    """Single-space, stripped. Applied to both sides so a headline broken across lines
    still matches an alias - that is whitespace, not fuzz."""
    return " ".join(value.split())


def normalise(session: Session, values: list[str]) -> list[str]:
    """`unaccent(lower(...))` in Postgres, in input order.

    Done in the database rather than with a Python unicodedata fold because
    `ticker_alias.alias_norm` was written by Postgres's `unaccent`, and two different
    definitions of "normalised" on the two sides of a comparison is how an exact match
    stops being exact. One round trip for the whole batch.
    """
    if not values:
        return []
    rows = session.execute(
        text(
            "SELECT unaccent(lower(v)) FROM unnest(CAST(:vals AS text[])) "
            "WITH ORDINALITY AS t(v, ord) ORDER BY ord"
        ),
        {"vals": values},
    ).scalars()
    return [_collapse(str(r)) for r in rows]


# ───────────────────────── the pass ─────────────────────────


def _documents(
    session: Session,
    *,
    feed_codes: list[str] | None,
) -> list[dict[str, Any]]:
    """Every stored feed document, oldest first.

    Oldest first is deliberate: `news_item.url` is UNIQUE, so the first document to carry
    an article is the one that keeps the provenance. Processing newest-first would make
    `raw_document_id` depend on when extract happened to run, and the whole point of the
    pointer is that it does not move.

    Every document is re-read on every run, not just the newest: a feed holds 20 items and
    rolls, so yesterday's document is the only record of yesterday's headlines. Re-reading
    is free of consequence because the UNIQUE url makes a re-seen item `unchanged`.
    """
    clause = ""
    params: dict[str, Any] = {"doc_type": DOC_TYPE}
    if feed_codes:
        clause = "AND payload->>'feed_code' = ANY(CAST(:codes AS text[]))"
        params["codes"] = feed_codes
    rows = session.execute(
        text(
            f"""
            SELECT id, payload, fetched_at
            FROM raw_document
            WHERE doc_type = :doc_type {clause}
            ORDER BY fetched_at ASC, id ASC
            """
        ),
        params,
    ).mappings()
    return [dict(r) for r in rows]


def extract_news(
    session: Session,
    run_id: int,
    *,
    feed_codes: list[str] | None = None,
) -> NewsExtractResult:
    """Write `news_item` rows for every stored feed document.

    Idempotent: `news_item.url` is UNIQUE and the insert is ON CONFLICT DO NOTHING, so a
    second run over the same documents reports `unchanged` and writes nothing.

    A document that is not a parseable feed is counted in `documents_failed` with a reason
    and does not stop the pass - one publisher serving an error page must not cost the
    other three their headlines. The caller should treat `documents_failed > 0` as a
    PARTIAL run rather than a success.
    """
    result = NewsExtractResult()
    index = build_alias_index(session)

    for doc in _documents(session, feed_codes=feed_codes):
        result.documents_read += 1
        payload = doc["payload"]
        if not isinstance(payload, dict):
            result.documents_failed += 1
            result.notes.append(f"doc {doc['id']}: payload is not an object")
            continue

        feed_code = str(payload.get("feed_code") or "")
        publisher = str(payload.get("publisher") or "").strip()
        lang = str(payload.get("lang") or "vi").strip()[:2] or "vi"
        xml_text = payload.get("feed_xml")

        if not publisher:
            # publisher is NOT NULL, and guessing it from the URL's hostname would be this
            # module inventing an attribution.
            result.documents_failed += 1
            result.notes.append(f"doc {doc['id']}: envelope names no publisher")
            continue
        if not isinstance(xml_text, str):
            result.documents_failed += 1
            result.notes.append(f"doc {doc['id']}: envelope has no feed_xml string")
            continue

        try:
            items = parse_feed(xml_text)
        except FeedFormatError as exc:
            result.documents_failed += 1
            result.notes.append(f"{feed_code} doc {doc['id']}: {exc}")
            log.warning("news.unparseable", feed=feed_code, document_id=doc["id"], error=str(exc))
            continue

        if not items:
            # A well-formed feed holding nothing. Legitimate, and distinguishable from the
            # branch above precisely because that one is counted as a failure.
            result.notes.append(f"{feed_code} doc {doc['id']}: feed parsed, 0 items")

        _write_items(session, run_id, doc, items, index, publisher, lang, feed_code, result)

    log.info("news.extracted", **result.as_stats())
    return result


def _write_items(
    session: Session,
    run_id: int,
    doc: dict[str, Any],
    items: list[FeedItem],
    index: AliasIndex,
    publisher: str,
    lang: str,
    feed_code: str,
    result: NewsExtractResult,
) -> None:
    result.items_seen += len(items)

    # Normalise every headline in one round trip, then decide item by item.
    headlines = [item.headline for item in items]
    normalised = normalise(session, headlines)

    for item, headline_norm in zip(items, normalised, strict=True):
        headline = _collapse(item.headline)
        if not headline:
            result.skipped_no_headline += 1
            result.notes.append(f"{feed_code} {item.element_path}: no title")
            continue
        if len(headline) > MAX_HEADLINE:
            result.skipped_no_headline += 1
            result.notes.append(
                f"{feed_code} {item.element_path}: headline {len(headline)} chars "
                f"exceeds {MAX_HEADLINE}"
            )
            continue
        if not item.url:
            result.skipped_no_link += 1
            result.notes.append(f"{feed_code} {item.element_path}: no usable link")
            continue

        published_at, reason = parse_published(item.published_raw)
        if published_at is None:
            result.skipped_no_date += 1
            result.notes.append(f"{feed_code} {item.element_path}: {reason}")
            continue

        hits = index.matches(headline_norm)
        ticker_id: int | None = None
        if len(hits) == 1:
            ticker_id = hits[0][0]
        elif len(hits) > 1:
            # Two companies named in one headline. Picking one would be a guess about which
            # the article is about, and the headline may be negative.
            result.ambiguous_unattributed += 1
            result.notes.append(
                f"{feed_code} {item.element_path}: {len(hits)} tickers matched "
                f"({', '.join(a for _, a in hits)}) - left unattributed"
            )

        new_id = session.execute(
            text(
                "INSERT INTO news_item "
                "(raw_document_id, ticker_id, headline, url, publisher, published_at, lang) "
                "VALUES (:doc_id, :ticker_id, :headline, :url, :publisher, :published_at, :lang) "
                "ON CONFLICT (url) DO NOTHING RETURNING id"
            ),
            {
                "doc_id": doc["id"],
                "ticker_id": ticker_id,
                "headline": headline,
                "url": item.url,
                "publisher": publisher,
                "published_at": published_at,
                "lang": lang,
            },
        ).scalar_one_or_none()

        if new_id is None:
            result.unchanged += 1
            continue

        result.written += 1
        if ticker_id is None:
            result.unattributed += 1
        else:
            result.attributed += 1
