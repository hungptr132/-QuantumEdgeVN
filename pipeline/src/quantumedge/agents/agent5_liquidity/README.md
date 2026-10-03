# Agent 5 — Liquidity risk

**Status:** **LIVE.** Pure code, no model, 97 metrics across 9 tickers.

## What it does

Quantifies how hard a position would be to **exit**. For a Vietnamese small-cap, being
unable to sell is the risk retail investors discover last.

Produces: 20-session average and **median** turnover, zero-trade sessions over three months,
missing-bar sessions (counted separately), the 52-week range, bid-ask spread, and an
estimated number of sessions to exit a given position.

## Boundaries

**May read:** `price_bar`, `market_calendar`, `quote_snapshot`.

**May write:** `metric_value` rows with `liquidity_*` and price codes — no table of its own.

## What it may never do

- **No signal, no direction, no recommendation.** Framed as exit *risk* throughout, with a
  fixed Vietnamese framing note that travels with the numbers so they cannot read as a
  trading signal by omission.
- **No MACD, no RSI, no indicators.** Deliberately absent. This is the narrowed technical
  analysis the spec asked for, not technical analysis in general.

## Where the code lives

**`calc/liquidity.py`** — and it lives there on purpose.

The pipeline diagram classes Agent 5 as *deterministic*, alongside the rest of the calc
stage. It shares the metric registry with every other metric and emits ordinary
`metric_value` rows. Moving the file into this folder would detach it from both for the sake
of a tidier directory listing.

Tests: `tests/test_liquidity.py`.

## Worth knowing

Two decisions here were forced by real data and are worth not undoing:

**Exit capacity uses the median session, not the mean.** POM showed 16 of 20 sessions with
zero volume and four block trades. The mean implied a position could be exited in under one
session; there is no such session. A zero median now reports *cannot estimate* instead.

**Windows count calendar trading sessions, never rows.** Dividing by bars present would
shorten the window for exactly the illiquid stocks these metrics describe, and flatter them.
