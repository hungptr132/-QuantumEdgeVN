"""VNDirect dchart — daily OHLCV history.

The only module that knows this provider exists. When it breaks, and an unaffiliated
provider eventually will (see docs/data-sources.md for the one that already did), the
blast radius is this file.

Response shape, verified live 2026-10-01: TradingView UDF, which is *parallel arrays*
rather than records:

    {"s":"ok","t":[epoch,...],"o":[...],"h":[...],"l":[...],"c":[...],"v":[...]}

Two quirks that must not be smoothed over here:
  * Prices are in THOUSANDS of VND (2.1 means 2,100). Scaling is the extractor's job -
    this stage stores bytes as received, so a scaling bug stays fixable without re-fetching.
  * {"s":"no_data"} is a legitimate empty answer, not a failure. Conflating the two would
    make the run manifest lie about what happened.
"""

import time
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from quantumedge.ingest.base import RawPayload

log = structlog.get_logger(__name__)

SOURCE_CODE = "vndirect_dchart"
DOC_TYPE = "price_history"
BASE_URL = "https://dchart-api.vndirect.com.vn/dchart/history"


class SourceUnavailable(RuntimeError):
    """The provider failed in a way retrying will not fix."""


@dataclass
class PriceFetch:
    symbol: str
    payload: RawPayload | None
    bars: int
    note: str = ""


def _epoch(d: date) -> int:
    return int(datetime.combine(d, dtime.min).timestamp())


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _get(
    client: httpx.Client, symbol: str, start: date, end: date
) -> tuple[dict[str, Any] | None, int, str]:
    params: dict[str, str | int] = {
        "resolution": "D",
        "symbol": symbol,
        "from": _epoch(start),
        "to": _epoch(end),
    }
    response = client.get(BASE_URL, params=params)
    response.raise_for_status()

    # An unknown symbol answers HTTP 200 with a COMPLETELY EMPTY body, so .json() would
    # raise JSONDecodeError - which is not an httpx error and would otherwise escape as an
    # unhandled crash, taking down a whole multi-symbol run over one typo. Treat it as the
    # empty answer it is.
    if not response.content.strip():
        return None, response.status_code, str(response.url)
    try:
        parsed = response.json()
    except ValueError:
        return None, response.status_code, str(response.url)
    # Narrow here rather than at the call site, so this function's contract is simply
    # "a UDF dict, or nothing".
    if not isinstance(parsed, dict):
        return None, response.status_code, str(response.url)
    return parsed, response.status_code, str(response.url)


def fetch_prices(
    symbols: list[str],
    start: date,
    end: date,
    *,
    rate_limit_per_min: int = 20,
    timeout: float = 30.0,
) -> list[PriceFetch]:
    """Fetch daily bars per symbol. Returns one result each, including empty ones.

    Empty results are returned rather than dropped so the caller can tell "asked and got
    nothing" apart from "never asked".
    """
    delay = 60.0 / max(rate_limit_per_min, 1)
    results: list[PriceFetch] = []

    headers = {
        # A plain identifying UA. No spoofing, no rotation - and happily none is needed:
        # probing showed the server does not care what the UA says, only that one is
        # present (no UA at all returns 403).
        "User-Agent": "QuantumEdge/0.1 (Vietnamese equity research; contact via repository)",
        # Must be */*. Asking for application/json returns 406 Not Acceptable, because the
        # endpoint does not advertise a JSON content type even though it returns JSON.
        # This cost a confusing failure once; it is not worth rediscovering.
        "Accept": "*/*",
    }

    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(delay)
            try:
                body, status, url = _get(client, symbol, start, end)
            except httpx.HTTPError as exc:
                raise SourceUnavailable(f"{symbol}: {type(exc).__name__}: {exc}") from exc

            # Two ways to legitimately get nothing, both HTTP 200, reported distinctly
            # so the manifest records which one happened.
            reason = ""
            if body is None:
                reason = "empty body, non-JSON, or unexpected shape - unknown symbol?"
            elif body.get("s") == "no_data" or not body.get("c"):
                reason = f"state={body.get('s')}, no bars in window"

            if reason or body is None:
                log.info("vndirect.no_data", symbol=symbol, note=reason)
                results.append(PriceFetch(symbol=symbol, payload=None, bars=0, note=reason))
                continue

            bars = len(body["c"])
            results.append(
                PriceFetch(
                    symbol=symbol,
                    payload=RawPayload(
                        source_code=SOURCE_CODE,
                        doc_type=DOC_TYPE,
                        body=body,
                        symbol=symbol,
                        # The window is part of the document's identity: the same symbol
                        # over a different range is a different document.
                        period_key=f"{start.isoformat()}..{end.isoformat()}",
                        source_url=url,
                        http_status=status,
                    ),
                    bars=bars,
                )
            )
            log.info("vndirect.fetched", symbol=symbol, bars=bars)

    return results
