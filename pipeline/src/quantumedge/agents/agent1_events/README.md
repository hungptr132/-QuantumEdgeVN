# Agent 1 — Event extraction

**Status:** Not built. No LLM exists in Phase 1.

## What it does

Reads HOSE disclosures, IR pages and RSS items; emits **structured events only**.

Output is one row per event in the `event` table, against a **closed enum**:
`dilution`, `auditor_change`, `insider_sale`, `related_party`, `profit_warning`,
`board_change`, `share_pledge`, `none`.

## Boundaries

**May read:** `raw_document`, `filing`, `news_item` — never the calc or fact sheet layers.

**May write:** `event` only, with `extraction_method='llm_agent1'`, `extractor_model` and `prompt_version` recorded.

## What it may never do

- **No commentary.** No prose, no interpretation, no summary beyond a sourced excerpt.
- **No price-impact guessing.** Whether an event matters is a flag rule's job, not this one's.
- **No sentiment score.** Vietnamese financial media is thin and often promotional; a
  sentiment number over it is noise wearing the costume of signal.
- **No new event types.** The enum is closed in the database. Free-text categories would
  break every downstream rule that reads it.

## Where the code lives

Not yet written. When it is: `agents/agent1_events/extract.py`, plus a versioned prompt
beside it.

Its destination table already exists and is queried today. `event_presence` flag rules read
it and currently report `unknown` with reason `no_event_source_configured` — so the day this
agent produces rows, those rules start working with no schema change.

Rows can be hand-seeded with `extraction_method='manual'` to exercise the path first.

## Worth knowing

The `excerpt` column is capped at 500 characters by a CHECK constraint, and there is no
body column anywhere. That is the copyright line enforced structurally: headline, link,
publisher, timestamp and a short sourced excerpt — never an article body.
