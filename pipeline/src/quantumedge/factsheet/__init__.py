"""Stage 4 — fact sheet: assemble the one artifact every consumer reads.

Reads:  metric_value, metric_benchmark, flag_instance, event, news_item.
Writes: fact_sheet (always at status='draft').

The fact sheet is the only thing downstream consumers — the web app, and every future
agent — are permitted to read. Nothing downstream queries the database directly. That
single chokepoint is what makes outputs reproducible: same ticker plus same data must
produce the same sheet, every time.

Rules:
  - Every number is a Figure carrying provenance, a unit, and a precomputed display
    string. No bare floats. The web layer does no formatting, so it cannot disagree
    with the pipeline.
  - Missing values carry unavailable_reason and display as "không công bố". Gaps are
    part of the product, not an embarrassment to hide.
  - allowed_numbers enumerates every numeral that legitimately appears. It exists so
    that the future Agent 2 validator — reject any draft containing an unsourced
    number — is a function call rather than a project.
  - Content is hashed. An identical rebuild writes nothing and preserves the existing
    review stamp, so regeneration can never silently invalidate a human approval.
  - Review state (status, reviewed_by, reviewed_at) lives in columns, deliberately
    outside the hashed payload: approving must not look like a data change.
  - Sheets are written to factsheets/ and committed, so a rule change produces a
    reviewable diff of exactly which numbers moved on which pages.
"""
