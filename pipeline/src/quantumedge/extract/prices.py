"""Raw UDF price payloads into `price_bar`.

This is where the provider's two awkward conventions get normalised, once, with the
reasoning recorded:

  * Prices arrive in THOUSANDS of VND (2.1 means 2,100). Scaling happens here rather than
    at ingest so a units bug is fixable without re-fetching anything.
  * `volume = 0` is a real zero-trade session and is written through verbatim. Dropping or
    interpolating it would destroy exactly the signal Agent 5 exists to measure, and would
    make a frozen stock look like a normally-traded one.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.extract.base import ExtractResult, latest_documents
from quantumedge.ingest.vndirect import DOC_TYPE

log = structlog.get_logger(__name__)

EXTRACTOR_VERSION = "prices/v1"

# The provider quotes in thousands of VND. One constant, one place.
PRICE_SCALE = Decimal(1000)


def _to_vnd(raw: float | int | None) -> Decimal | None:
    if raw is None:
        return None
    # Via str() so the float's binary representation never leaks into a stored decimal.
    return (Decimal(str(raw)) * PRICE_SCALE).quantize(Decimal("0.01"))


def _bar_date(epoch: int | float) -> date:
    """UTC, matching how the provider stamps sessions. The business timezone is applied
    only where a trading-day boundary is actually being decided, never here."""
    return datetime.fromtimestamp(int(epoch), tz=UTC).date()


def extract_prices(
    session: Session,
    extract_run_id: int,
    *,
    symbols: list[str] | None = None,
) -> ExtractResult:
    """Parse the newest price document per symbol into bars."""
    result = ExtractResult()

    for doc in latest_documents(session, DOC_TYPE, symbols=symbols):
        result.documents_read += 1
        payload: dict[str, Any] = doc["payload"]
        symbol = doc["symbol"]

        if doc["ticker_id"] is None:
            result.skipped += 1
            result.notes.append(f"{symbol}: no ticker row")
            continue

        times = payload.get("t") or []
        closes = payload.get("c") or []
        if not times or not closes:
            result.skipped += 1
            result.notes.append(f"{symbol}: no bars in payload")
            continue

        # Ragged arrays would silently mis-pair prices with dates, so refuse rather than
        # guess which series is authoritative.
        lengths = {k: len(payload.get(k) or []) for k in ("t", "o", "h", "l", "c", "v")}
        if len(set(lengths.values())) != 1:
            result.skipped += 1
            result.notes.append(f"{symbol}: ragged arrays {lengths}")
            log.warning("extract.prices.ragged", symbol=symbol, lengths=lengths)
            continue

        rows = []
        for i in range(len(times)):
            volume = payload["v"][i]
            rows.append(
                {
                    "ticker_id": doc["ticker_id"],
                    "trade_date": _bar_date(times[i]),
                    "open": _to_vnd(payload["o"][i]),
                    "high": _to_vnd(payload["h"][i]),
                    "low": _to_vnd(payload["l"][i]),
                    "close": _to_vnd(closes[i]),
                    # 0 is written through, deliberately.
                    "volume": int(volume if volume is not None else 0),
                    "src": doc["id"],
                    "run": extract_run_id,
                }
            )

        session.execute(
            text(
                """
                INSERT INTO price_bar
                    (ticker_id, trade_date, open, high, low, close, volume,
                     turnover_vnd, source_raw_document_id, extract_run_id)
                VALUES
                    (:ticker_id, :trade_date, :open, :high, :low, :close, :volume,
                     :close * :volume, :src, :run)
                ON CONFLICT (ticker_id, trade_date) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                    close = EXCLUDED.close, volume = EXCLUDED.volume,
                    turnover_vnd = EXCLUDED.turnover_vnd,
                    source_raw_document_id = EXCLUDED.source_raw_document_id,
                    extract_run_id = EXCLUDED.extract_run_id
                """
            ),
            rows,
        )
        result.rows_written += len(rows)
        log.info("extract.prices", symbol=symbol, bars=len(rows))

    return result
