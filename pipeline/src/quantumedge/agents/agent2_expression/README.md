# Agent 2 — Analysis expression

**Status:** Not built. Its validator, however, already exists and is tested.

## What it does

Turns one fact sheet into plain Vietnamese prose. The division of labour is the entire
point: **Python computes every number; the model only ever phrases them.**

## Boundaries

**May read:** **`fact_sheet.payload` and nothing else.** Not the database, not a raw document, not a price bar.

**May write:** An `analysis_draft` row (to be added), carrying the same review lifecycle as `fact_sheet`.

## What it may never do

- **No arithmetic.** Not a sum, not a ratio, not a rounding. If a number is not already in
  the fact sheet, it does not go in the prose.
- **No number not in `allowed_numbers`.** Enforced mechanically, see below.
- **No recommendation, price target or position size.** Describe, never prescribe.
- **Missing data is written `không công bố`** — never omitted, never filled in.

## Where the code lives

Not yet written. When it is: `agents/agent2_expression/express.py` plus
`agents/agent2_expression/prompts/v1.md`, versioned in git so a wording change is a
reviewable diff.

**The hard part is already done.** Every fact sheet carries `allowed_numbers`: the flat set
of numeric strings that legitimately appear in it. The validator is
`factsheet/qa.py::_unlisted_numbers`, already written and under test — so "reject any draft
containing a number that is not in the fact sheet" is a function call, not a project.

## Worth knowing

Why this is built second rather than first: reproducibility is what the product sells.
The same ticker and the same data must produce the same conclusion every time, and a model
in the computation path makes that untestable. With the model confined to phrasing, the
conclusion is deterministic and the prose is reviewable.
