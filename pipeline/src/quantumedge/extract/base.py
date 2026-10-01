"""Shared machinery for the extract stage.

Extraction reads `raw_document` and nothing else. It never fetches, so a parsing fix is
always re-runnable against bytes already captured — which is what makes it safe to ship an
imperfect extractor and improve it later.

Every row written here records `source_raw_document_id` and `extract_run_id`. That is not
bookkeeping: it is what lets any number on a page be traced back to the exact document and
the exact code run that produced it.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class ExtractResult:
    """What one extraction pass did. `skipped` counts documents that parsed to nothing."""

    rows_written: int = 0
    documents_read: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "documents_read": self.documents_read,
            "rows_written": self.rows_written,
            "skipped": self.skipped,
        }
        if self.notes:
            stats["notes"] = self.notes
        return stats


def latest_documents(
    session: Session,
    doc_type: str,
    *,
    symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The newest document per symbol for a given type.

    Several documents can exist for one symbol — different fetch windows, or a corrected
    response — because the raw layer is append-only. Extraction wants the most recent, but
    the older rows remain for audit.
    """
    clause = ""
    params: dict[str, Any] = {"doc_type": doc_type}
    if symbols:
        clause = "AND symbol = ANY(:symbols)"
        params["symbols"] = symbols

    rows = session.execute(
        text(
            f"""
            SELECT DISTINCT ON (symbol)
                   id, symbol, ticker_id, payload, period_key, fetched_at
            FROM raw_document
            WHERE doc_type = :doc_type AND symbol <> '' {clause}
            ORDER BY symbol, fetched_at DESC, id DESC
            """
        ),
        params,
    ).mappings()
    return [dict(r) for r in rows]
