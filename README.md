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

Prerequisites: `git` and `uv`. Both install per-user, no administrator rights needed.

### 1. Python environment

```powershell
cd pipeline
uv venv                      # Python 3.12 — see docs/DECISIONS.md for why not 3.14
uv pip install -e ".[dev]"
```

### 2. Local Postgres

A portable PostgreSQL 17.6 cluster, no Docker and no admin rights. It lives **outside the
repo** at `C:\QuantumEdgePg\`, because this repo sits on an exFAT volume and exFAT lacks
the journaling and fsync guarantees PostgreSQL needs.

```powershell
# One-time: download and extract the binaries
curl.exe -L -o pg.zip https://get.enterprisedb.com/postgresql/postgresql-17.6-1-windows-x64-binaries.zip
Expand-Archive pg.zip -DestinationPath C:\QuantumEdgePg

# One-time: create the cluster (UTF8 + ICU vi-VN for Vietnamese collation)
C:\QuantumEdgePg\pgsql\bin\initdb.exe -D C:\QuantumEdgePg\data -U postgres `
  -A scram-sha-256 --pwfile=<file-with-password> -E UTF8 `
  --locale-provider=icu --icu-locale=vi-VN

# Then append to C:\QuantumEdgePg\data\postgresql.conf:
#   port = 55432
#   timezone = 'UTC'
#   log_timezone = 'UTC'
```

Roles and databases come from the committed bootstrap SQL. Passwords are passed as psql
variables, never stored in the files:

```powershell
$init = "infra\postgres\init"
psql -U postgres -d postgres -v owner_pw='...' -v web_pw='...' -f "$init\01_roles_databases.sql"
psql -U postgres -d quantumedge      -f "$init\02_database_setup.sql"
psql -U postgres -d quantumedge_test -f "$init\02_database_setup.sql"
```

Day-to-day the cluster is managed with:

```powershell
.\pipeline\scripts\pg.ps1 start | stop | restart | status | log
```

### 3. Configure and check

```powershell
copy .env.example .env       # then fill in the passwords
pipeline\.venv\Scripts\qe doctor
```

`qe doctor` reports repo root, `.env`, database connectivity, server timezone, required
extensions, and migration state. It exits non-zero until the environment is usable, which
makes it safe as the first line of any script.

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
