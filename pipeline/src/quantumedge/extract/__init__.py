"""Stage 2 — extract: turn stored raw documents into typed rows. Deterministic only.

Reads:  raw_document.
Writes: price_bar, market_calendar, financial_period, financial_fact, news_item.

Rules:
  - No model ever retypes a number off a document. This stage is code, always.
  - Every written figure records source_raw_document_id and source_field_path. The
    path is NOT NULL: a number that cannot name its origin must not be stored.
  - A failed extraction is recorded as failed (extraction_status='failed'), never
    guessed, interpolated, or defaulted to zero. Absent beats wrong.
  - Source field names are mapped explicitly in mapping/. No fuzzy matching.
  - Re-runnable against already-stored documents, so extraction quality can improve
    without re-fetching anything.
"""
