"""Stage 1 — ingest: fetch external sources and store them immutably.

Reads:  external HTTP sources only.
Writes: raw_document, filing, ingest_run.

Rules:
  - One module per source. A provider breaking must touch exactly one file.
  - Store bytes as received. Parsing belongs to the extract stage, not here.
  - Hash before writing; UNIQUE (source_url, content_hash) makes a re-fetch of
    unchanged content a no-op rather than a duplicate row.
  - raw_document is append-only, enforced by a database trigger.
  - News: headline, link, publisher, timestamp only. Never article bodies.
"""
