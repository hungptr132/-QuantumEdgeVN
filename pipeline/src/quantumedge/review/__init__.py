"""Stage 5 — review: the human gate between generated and published.

Reads:  fact_sheet.
Writes: fact_sheet.status / reviewed_by / reviewed_at, fact_sheet_review_log.

Lifecycle: draft -> approved -> published, with rejected and superseded as the other
terminals. Transitions are the only thing this stage may change; it never touches a
payload.

Rules:
  - Reaching approved or published requires both a passing machine QA and a human
    stamp. CHECK constraints enforce this, so no code path can bypass it.
  - At most one published sheet per ticker, enforced by a partial unique index.
    Publishing a new version supersedes the old one and preserves its original stamp.
  - A rejection requires a reason, and every transition is logged. That log is how
    the upstream rules improve — it is the feedback loop, not an audit formality.
  - The web role cannot read an unpublished sheet at all: it holds no privilege on
    this table, only on the published view. The gate is in the database, not in a
    WHERE clause somebody could forget.

The "reviewed by X on Y" stamp is the product. Everything upstream exists to make
that stamp defensible.
"""
