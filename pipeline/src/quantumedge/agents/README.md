# The agent map

One folder per agent. Each holds that agent's contract — what it may read, what it may
never do, and where its code lives.

**Code is organised by pipeline stage, not by agent**, and these folders do not change that.
A stage is a boundary with a single writer and a declared set of tables (see `CLAUDE.md`);
an agent is a *role* that may sit inside one. Moving Agent 5's implementation here, for
instance, would detach it from the metric registry it shares with the rest of the calc
stage and from the `metric_value` rows it writes — the folder would read tidier and the
architecture would be worse.

| Agent | Role | Status | Implementation |
|---|---|---|---|
| [1](agent1_events/) | Event extraction from disclosures | not built | `ingest/` + `extract/` → `event` |
| [2](agent2_expression/) | Fact sheet → plain Vietnamese | not built | `agents/agent2_expression/` |
| [3](agent3_tip_parser/) | Parse and verify a pasted tip | not built | `agents/agent3_tip_parser/` |
| [4](agent4_portfolio/) | Describe a pasted portfolio | not built | `agents/agent4_portfolio/` |
| [5](agent5_liquidity/) | Liquidity / exit risk | **LIVE** | `calc/liquidity.py` |

## The rule they all serve

**Agents may READ, EXTRACT, CROSS-CHECK and EXPRESS. They may never JUDGE.**

Phase 1 contains no LLM at all. Agent 5 is live because it is pure arithmetic; the four
that need a language model are specified here and deliberately unbuilt, so the
deterministic layer is proven before a model can obscure it.

## Build order

Carried from the product spec: **5 → 2 → 3 → 1 → 4**. Agent 5 first because it is pure
code and needs no model. Agent 2 next because its validator already exists — see
`agent2_expression/`.
