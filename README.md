# QuantumEdge

A lookup and verification platform for Vietnamese stocks — including the small and
mid-caps nobody else covers. Built to answer one question fast: *is this tip worth anything?*

Product spec and pipeline diagram: [`docs/spec/`](docs/spec/).
Every design decision and its reversal condition: [`docs/DECISIONS.md`](docs/DECISIONS.md).
What data comes from where, and what does not work: [`docs/data-sources.md`](docs/data-sources.md).

## Shape of the thing

Two codebases, one Postgres database.

- **`pipeline/`** — Python. The only writer. Ingests raw sources immutably, extracts
  numbers deterministically, computes metrics/flags, and assembles one **Fact Sheet JSON**
  per ticker. Owns the schema (SQLAlchemy + Alembic).
- **`web/`** — Next.js. Read-only consumer, and not yet scaffolded. Queries a single
  Postgres view through a role with no privilege on any base table.

The governing rule, carried from the spec: **code computes, models only ever express.**
Phase 1 contains no LLM at all — the deterministic layer is proven first.

## Why a fact sheet sits in the middle

Every downstream consumer reads one artifact, never the database directly. That makes
each output reproducible: same ticker plus same data must produce the same conclusion
every time. Sheets are hashed, versioned, and committed under `factsheets/`, so changing
a flag threshold produces a reviewable diff of exactly which numbers moved on which pages.

## Nothing publishes itself

A fact sheet is born `draft`. Reaching `published` requires passing machine QA *and* a
human review stamp, and both are enforced by database constraints rather than application
code — the web role literally cannot read an unpublished row. The
"reviewed by X on Y" stamp is the product.

## Getting set up

```powershell
# Prerequisites: git, uv, and a local Postgres (see docs/DECISIONS.md for why it must
# live on an NTFS volume, not this repo's exFAT drive).

cd pipeline
uv venv                      # Python 3.12 — see docs/DECISIONS.md
uv pip install -e ".[dev]"

cd ..
copy .env.example .env       # then fill in the passwords

pipeline\.venv\Scripts\qe doctor
```

`qe doctor` reports what is configured and what is missing. It exits non-zero until the
environment is usable, which makes it safe to run first in any script.

### Checks

```powershell
cd pipeline
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy src
.venv\Scripts\python -m pytest
```

If `uv` warns about falling back from hardlinks to full copies, that is because the
package cache sits on `C:` while this repo is on `E:`. Harmless; silence it with
`$env:UV_LINK_MODE = "copy"`.
