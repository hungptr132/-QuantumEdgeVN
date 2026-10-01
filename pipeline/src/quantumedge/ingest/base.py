"""The one writer every source funnels through.

Idempotency is a property of this file. A source module decides *what* to fetch; it never
decides how storage works, because the moment two sources write raw rows differently the
dedupe guarantee stops being a guarantee.

Contract: hash the payload, attempt the insert, and let the database's
UNIQUE (source_id, doc_type, symbol, period_key, content_hash) decide. Unchanged content
is counted, not inserted. Nothing is ever updated — a trigger forbids it.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from quantumedge.hashing import canonical_json, sha256_hex


@dataclass(frozen=True)
class RawPayload:
    """One fetched document, ready to store.

    `period_key` distinguishes several documents of the same type for one symbol (a date
    window, a fiscal quarter). It defaults to '' rather than None because NULLs compare
    unequal in Postgres and would let duplicates slip past the unique constraint.
    """

    source_code: str
    doc_type: str
    body: dict[str, Any] | list[Any]
    symbol: str = ""
    period_key: str = ""
    source_url: str | None = None
    http_status: int | None = None
    published_at: datetime | None = None

    def content_hash(self) -> str:
        """Hash of the body alone.

        Deliberately excludes the URL and fetch time: the same prices fetched twice, or
        via a differently-ordered query string, are the same document. Including volatile
        fields would defeat the dedupe it exists to provide.
        """
        return sha256_hex(canonical_json(self.body))


@dataclass
class StoreResult:
    inserted: int = 0
    unchanged: int = 0
    document_ids: list[int] = field(default_factory=list)

    def as_stats(self) -> dict[str, int]:
        return {
            "fetched": self.inserted + self.unchanged,
            "new": self.inserted,
            "unchanged": self.unchanged,
        }


def store_raw(
    session: Session,
    run_id: int,
    payloads: list[RawPayload],
    *,
    result: StoreResult | None = None,
) -> StoreResult:
    """Persist payloads, skipping any whose content is already stored."""
    out = result or StoreResult()

    for p in payloads:
        source_id = session.execute(
            text("SELECT id FROM source WHERE code = :c"), {"c": p.source_code}
        ).scalar_one_or_none()
        if source_id is None:
            raise LookupError(f"unknown source '{p.source_code}' - add it to seeds/reference.yaml")

        ticker_id = None
        if p.symbol:
            ticker_id = session.execute(
                text("SELECT id FROM ticker WHERE symbol = :s"), {"s": p.symbol}
            ).scalar_one_or_none()

        body_json = json.dumps(p.body, ensure_ascii=False, default=str)
        digest = p.content_hash()

        # ON CONFLICT DO NOTHING returns no id when the row already exists, which is
        # exactly the signal for "unchanged" - no second query needed.
        doc_id = session.execute(
            text(
                "INSERT INTO raw_document "
                "(source_id, ingest_run_id, ticker_id, symbol, doc_type, period_key, "
                " source_url, http_status, published_at, content_hash, payload, "
                " payload_path, byte_size) "
                "VALUES (:source_id, :run_id, :ticker_id, :symbol, :doc_type, :period_key, "
                " :source_url, :http_status, :published_at, :content_hash, "
                " CAST(:payload AS jsonb), NULL, :byte_size) "
                "ON CONFLICT (source_id, doc_type, symbol, period_key, content_hash) "
                "DO NOTHING RETURNING id"
            ),
            {
                "source_id": source_id,
                "run_id": run_id,
                "ticker_id": ticker_id,
                "symbol": p.symbol,
                "doc_type": p.doc_type,
                "period_key": p.period_key,
                "source_url": p.source_url,
                "http_status": p.http_status,
                "published_at": p.published_at,
                "content_hash": digest,
                "payload": body_json,
                "byte_size": len(body_json.encode("utf-8")),
            },
        ).scalar_one_or_none()

        if doc_id is None:
            out.unchanged += 1
        else:
            out.inserted += 1
            out.document_ids.append(int(doc_id))

    return out
