"""HOSE identity documents → `security_identifier`.

One stored search result becomes up to three rows: the exchange's internal numeric id,
the ISIN, and the FIGI. Each names the exact JSON field it came from, because an
identifier used to fetch a company's statements is load-bearing in the same way a figure
is — bind a ticker to the wrong id and every number that follows is the wrong company's,
with provenance that looks immaculate.
"""

from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.extract.base import ExtractResult, latest_documents
from quantumedge.ingest.hose import DOC_TYPE, SCHEMES

log = structlog.get_logger(__name__)

EXTRACTOR_VERSION = "identifiers/v1"


def _values(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(scheme, value, field_path) for every identifier actually present.

    An absent or blank identifier yields no row rather than an empty one. A row saying a
    security's ISIN is `''` would be a claim; no row is the honest absence.
    """
    out: list[tuple[str, str, str]] = []
    for scheme, field_name in SCHEMES.items():
        raw = payload.get(field_name)
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        out.append((scheme, value, f"$.{field_name}"))
    return out


def extract_identifiers(
    session: Session,
    run_id: int,
    *,
    symbols: list[str] | None = None,
) -> ExtractResult:
    """Write identifiers for every stored HOSE identity document."""
    result = ExtractResult()
    documents = latest_documents(session, DOC_TYPE, symbols=symbols)

    for doc in documents:
        result.documents_read += 1
        payload = doc["payload"]
        if not isinstance(payload, dict):
            result.skipped += 1
            result.notes.append(f"{doc['symbol']}: payload is not an object")
            continue
        if doc["ticker_id"] is None:
            # The raw layer accepts documents for symbols we do not cover, so this is a
            # legitimate state rather than an error.
            result.skipped += 1
            result.notes.append(f"{doc['symbol']}: not a covered ticker")
            continue

        rows = _values(payload)
        if not rows:
            result.skipped += 1
            result.notes.append(f"{doc['symbol']}: no identifiers in payload")
            continue

        for scheme, value, field_path in rows:
            # Converging upsert: re-running changes nothing, but a corrected document
            # does update the value and repoints provenance at the document that fixed it.
            session.execute(
                text(
                    "INSERT INTO security_identifier "
                    "(ticker_id, scheme, value, source_raw_document_id, source_field_path, "
                    " extract_run_id) "
                    "VALUES (:ticker_id, :scheme, :value, :doc_id, :field_path, :run_id) "
                    "ON CONFLICT (ticker_id, scheme) DO UPDATE SET "
                    " value = EXCLUDED.value, "
                    " source_raw_document_id = EXCLUDED.source_raw_document_id, "
                    " source_field_path = EXCLUDED.source_field_path, "
                    " extract_run_id = EXCLUDED.extract_run_id"
                ),
                {
                    "ticker_id": doc["ticker_id"],
                    "scheme": scheme,
                    "value": value,
                    "doc_id": doc["id"],
                    "field_path": field_path,
                    "run_id": run_id,
                },
            )
            result.rows_written += 1

        log.info("identifiers.extracted", symbol=doc["symbol"], schemes=len(rows))

    return result


def security_id(session: Session, symbol: str) -> str | None:
    """A covered ticker's HOSE internal id, or None if it was never resolved.

    This is the join key every other HOSE endpoint needs, so it is exposed as a lookup
    rather than left for each caller to re-query.
    """
    return session.execute(
        text(
            "SELECT si.value FROM security_identifier si "
            "JOIN ticker t ON t.id = si.ticker_id "
            "WHERE t.symbol = :symbol AND si.scheme = 'hose_security_id'"
        ),
        {"symbol": symbol},
    ).scalar_one_or_none()
