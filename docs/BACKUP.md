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

Both land on `D:` — NTFS, journalled, internal, not removable. Local only: no cloud, no
account, nothing leaves the machine.

### Why the database is backed up at all

Almost everything in it is reproducible — that is what M13 proves. Twelve quarters of
backfilled metrics, every benchmark and every flag instance can be recomputed exactly
from stored price data.

**The review stamps cannot.** `fact_sheet.reviewed_by` / `reviewed_at` / `review_note`
and the 19 rows of `fact_sheet_review_log` record what a person did: who read a sheet,
when, and what they said they had checked. No rerun recreates that, and without it the
product's central claim — that nothing publishes without a human stamp — has no evidence
behind it. The backfilled history is merely expensive; the audit trail is irreplaceable.

## What is deliberately not backed up

### `.env` — and why not

It holds the database passwords. Copying a secret to a second location widens its
exposure, and recovery here costs about two minutes, so the trade is not worth it.
`.env.example` is tracked, so the *shape* of the file survives in git.

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

### `pipeline/data/` — raw payloads

Re-fetchable: `qe ingest prices` and `qe ingest hose-ids`. Large, and the content hashes
in `raw_document` mean a re-fetch of identical bytes is recorded as `unchanged`.

### `pipeline/.venv/` — reproducible

`cd pipeline && uv sync` rebuilds it byte-for-byte from `uv.lock`.

### `web/node_modules/` — reproducible

`cd web && npm ci`.

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

## What a restore cannot give back

Nothing, as long as a dump exists. Without one: every number returns via
`qe run` and `qe calc backfill`, and **only the review stamps are lost** — the sheets come
back as drafts and would need reading and approving again by a person. That is the whole
argument for the dump.
