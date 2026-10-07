"""Vietnamese finance RSS feeds — fetch the document, decide nothing about it.

One module per external source, and for news that source is "RSS" rather than any one
publisher: the four feeds in `seeds/news_sources.yaml` differ only in a URL and a rate
limit, and splitting them into four near-identical modules would mean fixing a date or
encoding bug four times. A feed going away is a row edit in the seed file, which is the
blast radius this layering is for.

**This module does not parse feeds.** It fetches, records what the server said, and hands
the bytes to `ingest.base.store_raw`. Element names, dates, links and ticker attribution
all belong to `extract/news.py`, which runs against the stored document — so a parser fix
never requires re-fetching, and improving the parser cannot retroactively change what was
received.

Three outcomes are deliberately distinguished, because collapsing them makes the run
manifest lie:

  * **fetched**      - HTTP 2xx with a non-empty body. Stored (or already stored).
  * **empty**        - HTTP 2xx with a zero-length body. A legitimate answer from a feed
                       with nothing in it; recorded as such and *not* an error.
  * **failed**       - transport error or non-2xx after retries. An error.

"A feed with zero `<item>` elements" is a fourth case and is not visible from here: it
takes parsing to see, so extract counts it. Reporting it from ingest would mean this
module had read the document, which is exactly the boundary being held.

What is stored is the feed text verbatim under `feed_xml`, wrapped in a small envelope
carrying the feed code, its URL and its publisher. `raw_document.payload` is `jsonb`, so a
string is the closest thing to "bytes as received" available; the envelope exists so that
extract can read `raw_document` *alone* and still know which publisher's document it is
holding, which its stage contract requires.
"""

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from quantumedge.config import Settings
from quantumedge.ingest.base import RawPayload

log = structlog.get_logger(__name__)

DOC_TYPE = "news_feed"
SEED_FILE = "news_sources.yaml"

# Identifies us honestly and points a site operator at where to complain. A feed served to
# an anonymous client is still being served to someone, and saying who is the cheap half of
# the access basis recorded in each feed's robots_note.
USER_AGENT = "QuantumEdge/0.1 (Vietnamese equity research; contact via repository)"

# Not a parse: the envelope records whether the response even plausibly was a feed, so a
# courtesy HTML error page stored under a 200 can be told apart later from real XML.
_XML_SNIFF = re.compile(rb"^\s*(<\?xml|<rss|<feed|<rdf:RDF)", re.IGNORECASE)


class FeedSpecError(ValueError):
    """`seeds/news_sources.yaml` is missing, malformed, or missing a required field."""


@dataclass(frozen=True)
class FeedSpec:
    """One feed, as declared in `seeds/news_sources.yaml`.

    `publisher` is taken from here rather than from the feed's own `<channel><title>`:
    the channel title is editorial text that changes with a site redesign, while the
    publisher attached to a headline is an attribution we are making and must be stable.
    """

    code: str
    name: str
    publisher: str
    base_url: str
    feed_url: str
    rate_limit_per_min: int
    robots_note: str
    kind: str = "rss"
    lang: str = "vi"


@dataclass
class FeedFetch:
    """What one feed attempt produced. `payload is None` always has a `note` saying why."""

    spec: FeedSpec
    payload: RawPayload | None
    http_status: int | None = None
    byte_size: int = 0
    note: str = ""
    failed: bool = False


_REQUIRED = ("code", "name", "publisher", "base_url", "feed_url", "robots_note")


def load_feed_specs(
    settings: Settings,
    *,
    codes: list[str] | None = None,
) -> list[FeedSpec]:
    """Read the feed list. A missing required field is an error, never a default.

    `robots_note` is required: a feed whose access basis nobody recorded is a feed nobody
    checked, and an empty note would let that pass review silently.
    """
    path: Path = settings.seeds_dir / SEED_FILE
    if not path.exists():
        raise FeedSpecError(f"seed file missing: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("feeds"), list):
        raise FeedSpecError(f"{SEED_FILE}: expected a top-level 'feeds' list")

    specs: list[FeedSpec] = []
    seen: set[str] = set()
    for i, row in enumerate(data["feeds"]):
        if not isinstance(row, dict):
            raise FeedSpecError(f"{SEED_FILE}: feeds[{i}] is not a mapping")
        missing = [f for f in _REQUIRED if not str(row.get(f) or "").strip()]
        if missing:
            raise FeedSpecError(f"{SEED_FILE}: feeds[{i}] missing {', '.join(missing)}")
        code = str(row["code"]).strip()
        if code in seen:
            raise FeedSpecError(f"{SEED_FILE}: duplicate feed code '{code}'")
        seen.add(code)
        specs.append(
            FeedSpec(
                code=code,
                name=str(row["name"]).strip(),
                publisher=str(row["publisher"]).strip(),
                base_url=str(row["base_url"]).strip(),
                feed_url=str(row["feed_url"]).strip(),
                rate_limit_per_min=int(row.get("rate_limit_per_min", 10)),
                robots_note=" ".join(str(row["robots_note"]).split()),
                kind=str(row.get("kind", "rss")).strip(),
                lang=str(row.get("lang", "vi")).strip(),
            )
        )

    if codes is not None:
        wanted = {c.strip() for c in codes if c.strip()}
        unknown = wanted - {s.code for s in specs}
        if unknown:
            raise FeedSpecError(f"unknown feed code(s): {', '.join(sorted(unknown))}")
        specs = [s for s in specs if s.code in wanted]

    return specs


def apply_db_rate_limits(session: Session, specs: list[FeedSpec]) -> list[FeedSpec]:
    """Override each spec's rate limit with `source.rate_limit_per_min` where a row exists.

    The seed file is where a limit is *authored*; the `source` table is where it is
    *operative*, because that is what an operator can lower at 2am without editing and
    re-seeding a file. A feed with no `source` row keeps its seeded value, so this is safe
    to call before the first seed.
    """
    if not specs:
        return specs
    rows = session.execute(
        text(
            "SELECT code, rate_limit_per_min FROM source WHERE code = ANY(CAST(:codes AS text[]))"
        ),
        {"codes": [s.code for s in specs]},
    ).all()
    limits = {str(code): int(limit) for code, limit in rows}
    return [
        spec
        if spec.code not in limits
        else FeedSpec(**{**vars(spec), "rate_limit_per_min": limits[spec.code]})
        for spec in specs
    ]


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _get(client: httpx.Client, url: str) -> httpx.Response:
    response = client.get(url)
    response.raise_for_status()
    return response


def fetch_feeds(
    specs: list[FeedSpec],
    *,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> list[FeedFetch]:
    """Fetch every feed, one result per spec including the failures.

    Returns rather than raises on a single feed's failure: three feeds answering and one
    503-ing is a partial run, and aborting would discard three good documents. The caller
    decides whether `failed` entries make the run PARTIAL or FAILED.

    `client` is injectable so the test suite can drive this against recorded fixtures
    without a socket.
    """
    results: list[FeedFetch] = []
    owned = client is None
    http = client or httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, */*"},
        follow_redirects=True,
    )
    try:
        for i, spec in enumerate(specs):
            if i:
                # Per-feed spacing. Successive specs are different hosts, so this is more
                # conservative than each host requires - which is the right direction.
                time.sleep(60.0 / max(spec.rate_limit_per_min, 1))
            results.append(_fetch_one(http, spec))
    finally:
        if owned:
            http.close()
    return results


def _fetch_one(client: httpx.Client, spec: FeedSpec) -> FeedFetch:
    try:
        response = _get(client, spec.feed_url)
    except httpx.HTTPStatusError as exc:
        note = f"HTTP {exc.response.status_code}"
        log.warning("rss_news.failed", feed=spec.code, note=note)
        return FeedFetch(
            spec=spec, payload=None, http_status=exc.response.status_code, note=note, failed=True
        )
    except httpx.HTTPError as exc:
        note = f"{type(exc).__name__}: {exc}"
        log.warning("rss_news.failed", feed=spec.code, note=note)
        return FeedFetch(spec=spec, payload=None, note=note, failed=True)

    raw = response.content
    if not raw.strip():
        # A feed that answers 200 with nothing has told us something true: it currently
        # holds no document. Storing an empty payload would create a raw_document that
        # claims to be a feed, so it is recorded in the manifest instead.
        note = "empty response body"
        log.info("rss_news.empty", feed=spec.code, status=response.status_code)
        return FeedFetch(
            spec=spec, payload=None, http_status=response.status_code, note=note, failed=False
        )

    body: dict[str, Any] = {
        "feed_code": spec.code,
        "feed_url": spec.feed_url,
        "publisher": spec.publisher,
        "lang": spec.lang,
        "content_type": response.headers.get("content-type"),
        "encoding": response.encoding,
        "looks_like_xml": bool(_XML_SNIFF.match(raw)),
        "feed_xml": response.text,
    }

    log.info(
        "rss_news.fetched",
        feed=spec.code,
        status=response.status_code,
        bytes=len(raw),
        xml=body["looks_like_xml"],
    )
    return FeedFetch(
        spec=spec,
        payload=RawPayload(
            source_code=spec.code,
            doc_type=DOC_TYPE,
            body=body,
            # No symbol and no period: a feed is about no one ticker and covers no fixed
            # window. period_key stays '' so the dedupe key is (source, doc_type, hash),
            # i.e. the same feed content fetched twice is one document.
            symbol="",
            period_key="",
            source_url=str(response.url),
            http_status=response.status_code,
        ),
        http_status=response.status_code,
        byte_size=len(raw),
        note="",
    )


def fetch_stats(results: list[FeedFetch]) -> dict[str, Any]:
    """Manifest-ready counts. The three outcomes stay separate on purpose."""
    stats: dict[str, Any] = {
        "feeds": len(results),
        "fetched": sum(1 for r in results if r.payload is not None),
        "empty": sum(1 for r in results if r.payload is None and not r.failed),
        "failed": sum(1 for r in results if r.failed),
        "bytes": sum(r.byte_size for r in results),
    }
    problems = {r.spec.code: r.note for r in results if r.payload is None}
    if problems:
        stats["notes"] = problems
    return stats
