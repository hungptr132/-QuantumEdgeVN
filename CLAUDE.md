# QuantumEdge — working conventions

Read `docs/ARCHITECTURE.md` to find *where* a change belongs, and `docs/ROADMAP.md` for
what is built, what is blocked, and on what. This file states *how* changes are made.
They exist so a fix is a lookup, not an investigation.

**Run `scripts\backup.ps1` at the end of a session.** The repo lives on a removable
exFAT drive that has already disconnected once mid-session; `docs/BACKUP.md` says what is
covered and how to recover what is not.

## The one rule everything else serves

**Code computes. Models only ever express.** Every number is produced by deterministic
Python and carries a pointer to the document it came from. No model may compute, infer,
round, or retype a figure. Phase 1 contains no LLM at all.

Corollary: an **absent** figure is always cheaper than a **wrong** one. A failed
extraction is recorded as a failure; it is never guessed, interpolated, or defaulted
to zero.

## Where code goes

The package mirrors the pipeline stages, so a bug has a stage and a stage has a
directory. Never add a module that spans two stages.

| Stage | Directory | May read | May write |
|---|---|---|---|
| Ingest | `ingest/` | external sources | `raw_document`, `filing`, `ingest_run` |
| Extract | `extract/` | `raw_document` only | `price_bar`, `financial_*`, `market_calendar`, `news_item`, `security_identifier` |
| Calc | `calc/` | extracted tables | `metric_value`, `metric_benchmark`, `flag_*`, `calc_runs` |
| Fact sheet | `factsheet/` | calc output | `fact_sheet` |
| Review | `review/` | `fact_sheet` | `fact_sheet.status`, `fact_sheet_review_log` |
| Agents | `agents/` | see `agents/README.md` | nothing in Phase 1 |

One module per external source in `ingest/`. This is load-bearing: when a provider
breaks — and one already did, see `docs/data-sources.md` — the blast radius must be
a single file.

Shared helpers live at package root (`config.py`, `hashing.py`, `time.py`, `runs.py`)
and are imported by stages, never the reverse.

## Invariants a change must not break

These are enforced by database constraints and tests, not by discipline. If a change
requires relaxing one, that is a design discussion, not a refactor.

> **Status as of M1:** only #5's privilege half is live — the `qe_web` role genuinely
> cannot read any base table. The constraints backing #1–#4, #6 and #7 arrive with the
> schema in M2. Until then these are design commitments, not active guarantees, so do
> not assume the database is protecting you yet.

1. **Raw data is append-only.** A trigger rejects `UPDATE`/`DELETE` on `raw_document`.
2. **Every figure traces to its source.** `financial_fact.source_field_path` is `NOT NULL`.
   No row may exist without naming the exact field it came from.
3. **Every derived row names its producer.** `calc_run_id` and `rule_spec_version` are
   recorded, so "why did this change" is one join away.
4. **Missing data is `unknown`, never `inactive`.** `flag_state` is three-valued because
   a boolean makes an absent input indistinguishable from a clean result.
5. **Nothing publishes without a human stamp.** Enforced by `CHECK` constraints, and the
   web role has no privilege on any base table — only the published view.
6. **Review state never changes `payload_hash`.** Approving must not look like a data change.
7. **Re-running changes nothing.** Every stage is idempotent via content hashing and
   natural unique keys. A second run reports `unchanged`, not duplicate rows.
8. **`as_of_date` is always explicit.** No calc function calls `today()`. Only `time.py`
   may ask the clock.

## Conventions

- **Python 3.12** (not the system 3.14 — wheel availability). Config via `pydantic-settings`,
  CLI via `typer`, logs via `structlog` keyed by `run_key` so a log line joins to its run row.
- `ruff check`, `ruff format`, and `mypy --strict` must all pass. Run them before committing.
- Rules and thresholds live in **YAML under `pipeline/rules/`**, never hardcoded. Their
  content hash is the `rule_spec_version` that drives regeneration.
- Never print or log a DSN — it carries a password. `qe doctor` masks it.
- Timestamps are `timestamptz` in UTC. `Asia/Ho_Chi_Minh` appears in exactly one module.
- Money is `numeric(24,4)`; VND trillions overflow lesser types. Never `float` for money.

## Commits

One stage per commit where possible, prefixed with the milestone: `feat(M6): …`.
The body says *why*, since the diff already says what. If a decision was made,
add a row to `docs/DECISIONS.md` with its reversal condition in the same commit.

## Guardrails that are not negotiable

The product sells verification, so these are product requirements, not preferences:

- **Never** publish a recommendation, price target, or position size. Describe, don't prescribe.
- An unmatched claim is *"no corresponding disclosure found"* — **never** "false" or "scam".
- A negative statement about a named company requires attached evidence. A `CHECK`
  constraint enforces this on `flag_instance`.
- News: headline, link, publisher, timestamp only. Never article bodies. `news_item`
  deliberately has no body column.
