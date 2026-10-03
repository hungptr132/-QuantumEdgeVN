# Agent 4 — Portfolio inspection

**Status:** Not built.

## What it does

A user pastes their holdings. The system **describes what exists**: sector
concentration, total active flags across the portfolio, overlapping risk (several names on
one commodity cycle), and the share sitting in thin-liquidity small-caps.

## Boundaries

**May read:** A pasted holdings list, plus `fact_sheet.payload` and `flag_instance` per ticker.

**May write:** A portfolio report; needs a `portfolio` input table not yet designed.

## What it may never do

- **No buy or sell suggestion.**
- **No target weights.**
- **No rebalancing advice.**

This boundary is regulatory, not stylistic. Describing a portfolio that exists is a tool.
Prescribing the portfolio someone *should* hold is personalised investment advice and
SSC-licensed territory.

## Where the code lives

Not yet written. When it is: `agents/agent4_portfolio/inspect.py`.

Last in the build order: it needs flags across many tickers to be meaningful, so it depends
on broad coverage rather than on any one agent.

## Worth knowing

The name matters. "Inspection", not "construction" — and the distinction should survive
into every label the feature ever shows a user.
