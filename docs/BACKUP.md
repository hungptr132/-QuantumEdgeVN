# Backup and recovery

Run `.\scripts\backup.ps1` at the end of a working session. It mirrors git and dumps the
database to `D:`, verifying both.

## Why this exists

On **2026-10-04** the `E:` drive — a USB SanDisk Extreme SSD, formatted exFAT —
disconnected mid-session. At that moment it held the only copy of this repository.

It came back intact: `git fsck --full` was clean and the working tree was unchanged. But
exFAT has no journal (already recorded in `DECISIONS.md` as the reason the *database* was
kept off this drive), the volume briefly reported `HealthStatus: Warning` while
remounting, and a disconnect landing inside a `git commit` would have cost the object
store. The disk is healthy; the connection is the risk, and USB connections are not
something to design around.

## What is backed up

| What | Where | Covers |
|---|---|---|
| Git mirror | `D:\QuantumEdge-backup.git` | All history: code, `rules/*.yaml`, migrations, committed fact sheet snapshots, docs |
| Database dump | `D:\QuantumEdge-backup-db\quantumedge-<timestamp>.dump` | Every table, including the review audit trail. Last 10 kept |
| Plain files | `D:\QuantumEdge-backup-files\` | The working tree, excluding only what rebuilds itself. ~290 files, ~12 MB |

All three land on `D:` — NTFS, journalled, internal, not removable. Local only: no cloud,
no account, nothing leaves the machine.

### What the plain-file copy is for

The git mirror covers everything tracked; the dump covers the database. Between them they
left four kinds of file in **no backup at all**:

* **`.env`** — the database passwords. Included deliberately; see below.
* **`pipeline/data/filings/`** and **`filing_store/`** — statements a person downloaded,
  and the content-addressed archive of their bytes that every extracted figure's
  provenance points at. Re-downloadable in principle, tedious in practice.
* **The exported conversation transcripts** (`*.txt` at the repo root) — gitignored because
  they contain credential-shaped lines, and not reproducible at all.
* **`.idea/`** — editor settings. Low value, but it costs nothing.

It doubles as a copy that does not need git to read anything, which is worth having when
the thing you are recovering from might be a corrupt object store.

**Deliberately excluded**, because they rebuild exactly and are 34 GB between them:
`pipeline/.venv` (`uv sync`), `web/node_modules` (`npm ci`), and the `__pycache__`,
`.mypy_cache`, `.pytest_cache`, `.ruff_cache` and `.next` directories.

The copy is `/MIR`, so a file deleted in the project is deleted in the backup. A backup
that accumulates files the project no longer has is one nobody can reason about.

### Why the database is backed up at all

Almost everything in it is reproducible — that is what M13 proves. Twelve quarters of
backfilled metrics, every benchmark and every flag instance can be recomputed exactly
from stored price data.

**The review stamps cannot.** `fact_sheet.reviewed_by` / `reviewed_at` / `review_note`
and the 19 rows of `fact_sheet_review_log` record what a person did: who read a sheet,
when, and what they said they had checked. No rerun recreates that, and without it the
product's central claim — that nothing publishes without a human stamp — has no evidence
behind it. The backfilled history is merely expensive; the audit trail is irreplaceable.

## `.env` — included, and the trade behind that

It holds the database passwords. Leaving it out was the original call, on the grounds that
copying a secret widens its exposure.

That was reconsidered. Both copies sit on this one machine, so anything that can read one
can read the other and the exposure widens by almost nothing — while the exclusion cost a
real recovery step, the `pg_hba` dance below. If the trade ever looks wrong (the backup
drive leaving this machine would do it), pass `-SkipFiles`, or drop `.env` from the
robocopy call, and the reset procedure still works.

`.env.example` stays tracked, so the *shape* of the file is in git regardless.

To rebuild it after a loss:

1. Copy `.env.example` to `.env`.
2. Choose new passwords for `qe_owner` and `qe_web`.
3. Set them on the cluster. The superuser password is also in the lost file, so reach the
   cluster locally instead — `C:\QuantumEdgePg\data\pg_hba.conf`, temporarily change the
   local `host` lines to `trust`, then `.\pipeline\scripts\pg.ps1 restart`:

   ```sql
   ALTER ROLE qe_owner WITH PASSWORD '<new>';
   ALTER ROLE qe_web   WITH PASSWORD '<new>';
   ```

4. Put `pg_hba.conf` back to `scram-sha-256` and restart again.
5. Write the new DSNs into `.env` and `web\.env.local`, then `qe doctor` to confirm.

Never commit the result, and never paste a DSN into a terminal that is being logged.

## What rebuilds itself, and is therefore excluded

### `pipeline/data/` — now covered

Previously excluded as re-fetchable, which holds for the price payloads
(`qe ingest prices`, `qe ingest hose-ids`) but not for filings a person downloaded by
hand. It is in the plain-file copy.

### `pipeline/.venv/`

`cd pipeline && uv sync` rebuilds it from `uv.lock`. 9.8 GB, so it is not worth copying to
save one command.

### `web/node_modules/`

`cd web && npm ci`. 24 GB.

## Restoring

### The repository

```powershell
git clone D:\QuantumEdge-backup.git E:\QuantumEdge
cd E:\QuantumEdge
git remote set-url origin D:\QuantumEdge-backup.git   # or remove it
.\scripts\backup.ps1                                   # re-establish the mirror remote
```

Then rebuild the ignored directories (`uv sync`, `npm ci`) and recreate `.env` as above.

### The database

The cluster itself lives at `C:\QuantumEdgePg`. If only the *data* is lost but the cluster
runs:

```powershell
# Into a fresh database, so a bad restore cannot damage a working one.
C:\QuantumEdgePg\pgsql\bin\createdb.exe -p 55432 -U qe_owner quantumedge_restored
C:\QuantumEdgePg\pgsql\bin\pg_restore.exe -p 55432 -U qe_owner `
    --dbname=quantumedge_restored --no-owner `
    D:\QuantumEdge-backup-db\quantumedge-<timestamp>.dump
```

Check it before trusting it — the review trail is the point:

```sql
SELECT count(*) FROM fact_sheet_review_log;        -- expect the audit entries
SELECT symbol, version, reviewed_by, published_at
FROM published_ticker_page;                        -- expect the published sheet(s)
```

Then point `QE_DATABASE_URL` at it, or rename the databases.

If the cluster is gone entirely, `qe db bootstrap` recreates it from
`infra/postgres/init/*.sql` and `alembic upgrade head` rebuilds the schema; restore the
dump with `--data-only` on top of that, or restore the whole dump into a fresh database
as above.

## Verifying the restore path before you need it

`scriptserify-cold-start.ps1` runs the drill against a throwaway database: createdb,
`alembic upgrade head`, seed, then assert the guarantees a recovered database must have —
the published view, `qe_web` denied on every base table, the append-only trigger, the
extensions, the seeded tickers. It drops the scratch database afterwards and never touches
the real one.

Run it after changing a migration, and before trusting a backup.

`scriptserify-restore.ps1` is its companion for the dump. `backup.ps1` proves a dump is
*readable* with `pg_restore --list`; that is not the same as proving it restores. This one
restores the newest dump into a throwaway database and checks what survived — weighted
deliberately towards the audit trail, since everything else can be recomputed. It also
checks that no published sheet came back without its stamp, which would mean the restore
had dropped the `CHECK` and the next publish could bypass the gate.

This is not hypothetical. The first time the drill was actually run it **failed**:
`ticker_alias_trgm` needs an operator class from `pg_trgm`, nothing in the migration
created the extension, and `alembic upgrade head` on a fresh database died with
"operator class gin_trgm_ops does not exist" — an error that says nothing about the cause.
The extension creation now lives in revision 0001, where the plan always said it belonged.
Until then, the recovery procedure written below would not have worked.

## What a restore cannot give back

Nothing, as long as a dump exists. Without one: every number returns via
`qe run` and `qe calc backfill`, and **only the review stamps are lost** — the sheets come
back as drafts and would need reading and approving again by a person. That is the whole
argument for the dump.
