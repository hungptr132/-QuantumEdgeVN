"""Agent slots. Empty in Phase 1 by design — see README.md in this directory.

Governing principle, carried from the product spec:

    Agents may READ, EXTRACT, CROSS-CHECK, and EXPRESS.
    Agents may NOT JUDGE.

Phase 1 ships no LLM at all. Agent 5 (liquidity risk) is pure code and therefore lives
in calc/liquidity.py, not here. This package stays empty until Agent 1 or 2 is built,
and the schema already reserves their columns so neither needs a migration rewrite.
"""
