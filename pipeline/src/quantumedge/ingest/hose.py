"""HOSE (hsx.vn) — the exchange itself, and the only authoritative source in the stack.

Everything else here is a broker's convenience endpoint. This is the venue.

`hsx.vn` is a React SPA talking to `api.hsx.vn` behind several prefixed bases. Its
`robots.txt` is `User-agent: * / Disallow:` — an empty Disallow, i.e. full crawl permission
explicitly granted — and the endpoint used below is the one the site's own search box calls
when a visitor types a ticker. Both the path and its parameters were read out of the
bundle the site serves to every visitor (`static/js/main.*.js`), not guessed at: see
`docs/data-sources.md` for the decode and for what remains unresolved.

**What this module does today: resolve a symbol to HOSE's internal numeric security id.**

That sounds minor and is not. Every other HOSE endpoint — financial statements, disclosure
listings, attachment media files — is keyed by that numeric id, never by the ticker symbol.
Without it the rest of the exchange is unreachable; with it, it is a path parameter. The
same response also carries the security's ISIN and FIGI, which are the international
standard identifiers and worth keeping for their own sake.

A caution that cost real time: the finance endpoint answers **HTTP 200 with
`{"data":[],"success":true}`** to a caller it does not accept, rather than 401 or 404. So an
empty result proves nothing about whether the id, the period, or the request itself was
wrong. Any future module here must treat an empty `data` as *unknown*, never as "this
company published nothing" — reporting absence off the back of it would be exactly the kind
of false negative this product exists to avoid.
"""

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from quantumedge.ingest.base import RawPayload

log = structlog.get_logger(__name__)

SOURCE_CODE = "hose_search"
DOC_TYPE = "security_identity"

# The Elasticsearch index behind the public search box.
SEARCH_URL = "https://api.hsx.vn/q/api/v1/search"
INDEX_NAME = "securities"
# Copied verbatim from the bundle's own index config. `code^2` boosts the ticker field, so
# an exact symbol outranks a company whose description merely mentions it.
SEARCH_FIELDS = "code^2,isin,figi,introduction"

# The schemes this response yields, mapped to their JSON field.
SCHEMES = {"hose_security_id": "id", "isin": "isin", "figi": "figi"}


class SourceUnavailable(RuntimeError):
    """The exchange failed in a way retrying will not fix."""


@dataclass
class IdentityFetch:
    symbol: str
    payload: RawPayload | None
    security_id: int | None = None
    note: str = ""


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _search(client: httpx.Client, symbol: str) -> tuple[dict[str, Any] | None, int, str]:
    response = client.get(
        SEARCH_URL,
        params={
            "indexName": INDEX_NAME,
            "field": SEARCH_FIELDS,
            "query": symbol,
            "page": 1,
            "pageSize": 20,
        },
    )
    response.raise_for_status()
    if not response.content.strip():
        return None, response.status_code, str(response.url)
    try:
        parsed = response.json()
    except ValueError:
        return None, response.status_code, str(response.url)
    if not isinstance(parsed, dict):
        return None, response.status_code, str(response.url)
    return parsed, response.status_code, str(response.url)


def _exact_match(body: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """The item whose `code` is exactly this symbol.

    A full-text search over four fields returns near misses: querying AGM also matches
    companies whose description contains it. Taking the top hit would silently bind a
    ticker to the wrong security id, and every statement fetched afterwards would be the
    wrong company's — a wrong number with perfect provenance, which is the worst failure
    this pipeline can produce. So the match must be exact or there is no match.
    """
    items = body.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("code") == symbol:
            return item
    return None


def fetch_identities(
    symbols: list[str],
    *,
    rate_limit_per_min: int = 20,
    timeout: float = 30.0,
) -> list[IdentityFetch]:
    """Resolve each symbol to its HOSE identity record.

    Returns one result per symbol including the failures, so a caller can tell "asked and
    HOSE does not list it" from "never asked".
    """
    delay = 60.0 / max(rate_limit_per_min, 1)
    results: list[IdentityFetch] = []

    headers = {
        "User-Agent": "QuantumEdge/0.1 (Vietnamese equity research; contact via repository)",
        "Accept": "*/*",
    }

    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(delay)
            try:
                body, status, url = _search(client, symbol)
            except httpx.HTTPError as exc:
                raise SourceUnavailable(f"{symbol}: {type(exc).__name__}: {exc}") from exc

            if body is None:
                note = "empty or non-JSON response"
                log.info("hose.no_identity", symbol=symbol, note=note)
                results.append(IdentityFetch(symbol=symbol, payload=None, note=note))
                continue

            match = _exact_match(body, symbol)
            if match is None:
                note = f"no exact code match among {body.get('total', 0)} hits"
                log.info("hose.no_identity", symbol=symbol, note=note)
                results.append(IdentityFetch(symbol=symbol, payload=None, note=note))
                continue

            # Store the matched item alone, not the whole result set. The other hits are an
            # artifact of how the query was phrased, so including them would make the
            # content hash depend on unrelated listings and defeat the dedupe.
            security_id = match.get("id")
            results.append(
                IdentityFetch(
                    symbol=symbol,
                    payload=RawPayload(
                        source_code=SOURCE_CODE,
                        doc_type=DOC_TYPE,
                        body=match,
                        symbol=symbol,
                        source_url=url,
                        http_status=status,
                    ),
                    security_id=int(security_id) if isinstance(security_id, int) else None,
                )
            )
            log.info("hose.identity", symbol=symbol, security_id=security_id)

    return results
