"""Stage 3 — calc: metrics, benchmarks, and flags. Deterministic code, no LLM.

Reads:  extracted tables (price_bar, financial_fact, market_calendar, event).
Writes: metric_value, metric_benchmark, flag_instance, flag_state_change, calc_runs.

Rules:
  - Thresholds and rule logic live in pipeline/rules/*.yaml, never hardcoded here.
    The file's content hash becomes rule_spec_version, which is what makes
    "change a rule, regenerate exactly the affected tickers" possible.
  - Every metric declares its required facts via the registry, so a missing input
    yields is_available=False with an unavailable_reason instead of an exception.
  - Missing inputs produce flag_state 'unknown', never 'inactive'. A boolean would
    make absent data indistinguishable from a clean result.
  - An 'active' flag must carry evidence; a CHECK constraint enforces it, because a
    negative statement about a named company without support is a legal risk.
  - as_of_date is always an explicit argument. Nothing here calls today().
  - metric_value.inputs records every id and value used, so any figure can be
    recomputed by hand from the row alone.

Agent 5 (liquidity risk) lives here in liquidity.py, not in agents/: the pipeline
diagram classes it as deterministic alongside the rest of this stage, and it emits
ordinary metric_value rows. It is framed as exit risk, never as a trading signal.
"""
