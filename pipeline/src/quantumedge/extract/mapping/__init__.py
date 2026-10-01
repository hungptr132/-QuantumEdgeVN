"""Explicit source-field to fact_code mappings, maintained by hand.

One mapping table per source, because providers name the same line item differently
and rename without notice. Keeping this declarative means a provider's rename is a
data edit here, not a code change in the extractors.

No fuzzy matching, no similarity scoring, no inference. An unmapped field is reported
as unmapped so it can be added deliberately — silently guessing a mapping would put a
wrong number on a page, which is the one outcome this product cannot afford.
"""
