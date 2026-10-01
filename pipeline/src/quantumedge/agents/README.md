# Agent map

**Phase 1 builds none of these.** This file exists so the boundaries are settled before
any model is wired in, and so the schema seams stay visible rather than being rediscovered.

Governing principle from the product spec:

> Agents may **read, extract, cross-check, and express**. Agents may **not judge**.

Practically: a model may never compute a number, infer a missing one, or reach a
conclusion. Numbers come from deterministic code; a model's only job is to put existing,
sourced numbers into readable Vietnamese.

## Where each agent plugs in

| Agent | Role | Input it may read | Where it would live | Schema seam (already exists) |
|---|---|---|---|---|
| 1 | Event extraction | `raw_document`, `filing` | `agents/agent1_events.py` | `event` table, `extraction_method='llm_agent1'`, `extractor_model`, `prompt_version` |
| 2 | Analysis expression | **`fact_sheet.payload` only** | `agents/agent2_expression.py` | a drafts table carrying the same lifecycle columns as `fact_sheet` |
| 3 | Tip parser | `fact_sheet.payload`, `event`, `metric_value` | `agents/agent3_tip_parser.py` | reuses the drafts table with a different agent name |
| 4 | Portfolio inspection | `fact_sheet.payload` per holding | `agents/agent4_portfolio.py` | needs a portfolio input table, not yet designed |
| 5 | Liquidity risk | `price_bar`, `market_calendar`, `quote_snapshot` | **`calc/liquidity.py`** — pure code, not here | ordinary `metric_value` rows keyed `liquidity_*` |

Spec's own build order: **5 → 2 → 3 → 1 → 4.**

## Hard limits per agent

**Agent 1** emits structured JSON against a closed enum (`dilution`, `auditor_change`,
`insider_sale`, `related_party`, `profit_warning`, `board_change`, `share_pledge`, `none`),
enforced as a Postgres enum. No commentary, no sentiment score, no price-impact guess.
Free-text categories would break every downstream consumer, which is why the enum is rigid
rather than extensible.

**Agent 2** reads the fact sheet and nothing else — not the database, not a raw document.
It may use only numbers already present in that payload, and missing data is written
*không công bố*. Its validator is **already implemented**: `fact_sheet.allowed_numbers`
enumerates every legitimate numeral, so the check is "tokenise the draft, assert every
numeral is in that set." A draft containing an unsourced number is rejected and logged,
and the rejection log is how the prompt improves.

**Agent 3** extracts claims from a pasted tip, then verifies them **in code** against the
event table and implied multiples. It fires *all* flags for the ticker, including ones the
tip never mentioned — often the most valuable part of the output. An unmatched claim is
*"no corresponding disclosure found"*, **never** "false" and never "scam".

**Agent 4** describes what a portfolio *is* — concentration, overlapping risk, share of
thin-liquidity names. It must never suggest buying, selling, or target weights.
Describing what exists is a tool; prescribing what should exist is licensed investment
advice.

## Why Agent 5 is not in this directory

The pipeline diagram classes it as deterministic, alongside the calc layer. It is
arithmetic over price bars with no model involved, and it writes ordinary metric rows.
Putting it here would imply a model is reading prices, which is exactly the confusion
the spec's governing principle exists to prevent. It is also framed as **exit risk**,
never as a buy or sell signal — no MACD, no RSI, no direction.
