# Agent 3 — Tip parser

**Status:** Not built. The standout feature, per the product spec.

## What it does

Someone pastes a tip from a Zalo or Facebook group. The agent extracts its claims, the
**code** verifies them, and the output is three blocks:

1. what the tip claims
2. what the data shows
3. what it does not tell you

Then it fires **every** flag for that ticker — including ones the tip never mentioned. Those
are usually the most valuable part of the answer.

## Boundaries

**May read:** The pasted text, plus `fact_sheet.payload`, `event`, and `metric_value` for cross-checking.

**May write:** An `analysis_draft` row with `agent_name='agent3_tip_parser'`.

## What it may never do

- **Never says "false".** An unmatched claim is *"không tìm thấy công bố tương ứng"* —
  no corresponding disclosure found. The distinction is both honest and legally necessary:
  absence of a disclosure is not proof of a lie.
- **Never says "scam", "lừa đảo", or "don't buy".** Screened by the QA language check.
- **Never verifies in the model.** Extraction is the model's job; checking a claim against
  the event table and implied multiples is code's.

## Where the code lives

Not yet written. When it is: `agents/agent3_tip_parser/parse.py` (claim extraction) and
`verify.py` (deterministic checking).

Needs Agent 1's `event` table populated to cross-check catalyst claims, which is why it
follows Agent 2 in the build order.

## Worth knowing

Log every parsed tip. Which tickers get pumped, in what language, and when, is a dataset
nobody else will have — and it is a by-product of the feature working.
