<#
.SYNOPSIS
    Mirror the repository to D: (NTFS), and verify the copy.

.DESCRIPTION
    On 2026-10-04 the E: drive — an external exFAT SSD — disconnected mid-session while
    this repo was the only copy of the project. It came back intact and `git fsck` was
    clean, but exFAT has no journal, the volume now reports HealthStatus "Warning", and
    one unlucky disconnect during a write would have cost every commit.

    So: a second copy, on a journalled filesystem, on a drive that is not removable.
    Local only — no cloud, no account, nothing leaves this machine.

    What this DOES cover: all git history, which includes the committed fact sheet
    snapshots, rules YAML, migrations and docs.

    It also dumps the database, because one thing in there cannot be regenerated: the
    **review stamps**. Re-running the pipeline reproduces every number exactly — that is
    what M13 proves — but `fact_sheet.reviewed_by/reviewed_at/review_note` and
    `fact_sheet_review_log` record human acts. Nobody can recompute who approved what.
    The twelve quarters of backfilled history are merely expensive to rebuild; the audit
    trail is irreplaceable.

    What this does NOT cover, deliberately:
      * .env               — holds the database password. Duplicating a secret to widen
                             its exposure is a poor trade when recovery is cheap; see
                             docs/BACKUP.md for resetting the role passwords instead.
      * pipeline/data/     — raw payloads, re-fetchable, and large.
      * pipeline/.venv/    — reproducible from uv.lock.

.EXAMPLE
    .\scripts\backup.ps1

.EXAMPLE
    .\scripts\backup.ps1 -SkipDatabase
#>

[CmdletBinding()]
param(
    [string]$Mirror = 'D:\QuantumEdge-backup.git',
    [string]$DbDir = 'D:\QuantumEdge-backup-db',
    [string]$FilesDir = 'D:\QuantumEdge-backup-files',
    [switch]$SkipDatabase,
    [switch]$SkipFiles,
    [int]$Keep = 10
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

Write-Host "repo   : $repo"
Write-Host "mirror : $Mirror"

if (-not (Test-Path $Mirror)) {
    Write-Host "`nMirror does not exist. Creating it." -ForegroundColor Yellow
    git clone --mirror $repo $Mirror
    if (-not $?) { throw "clone failed" }
}

# Check the source first. Pushing a corrupt object graph into the backup would turn one
# bad copy into two.
Write-Host "`nVerifying the source repository..."
$fsck = git -C $repo fsck --full 2>&1 | Where-Object { $_ -notmatch '^dangling' }
if ($LASTEXITCODE -ne 0) {
    Write-Host ($fsck -join "`n") -ForegroundColor Red
    throw "source repository failed fsck - NOT pushing to the backup"
}
Write-Host "  source ok" -ForegroundColor Green

Write-Host "`nPushing all refs..."
git -C $repo push --mirror $Mirror
if (-not $?) { throw "push failed" }

# Verify the copy independently rather than trusting the push's exit code.
$srcHead  = (git -C $repo rev-parse HEAD).Trim()
$dstHead  = (git --git-dir=$Mirror rev-parse HEAD).Trim()
$srcCount = (git -C $repo rev-list --count HEAD).Trim()
$dstCount = (git --git-dir=$Mirror rev-list --count HEAD).Trim()

Write-Host "`nsource HEAD $srcHead  ($srcCount commits)"
Write-Host "mirror HEAD $dstHead  ($dstCount commits)"

if ($srcHead -ne $dstHead -or $srcCount -ne $dstCount) {
    throw "mirror does not match the source"
}

Write-Host "`nGit mirror verified." -ForegroundColor Green

if ($SkipDatabase) {
    Write-Host "`nDatabase dump skipped (-SkipDatabase)." -ForegroundColor Yellow
    Write-Host "Note: the review stamps live only in the database."
    exit 0
}

# --- database ---------------------------------------------------------------------
# The DSN carries a password, so it is read from .env, handed to pg_dump through the
# environment, and never printed. See the CLAUDE.md convention.

$envFile = Join-Path $repo '.env'
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile - cannot reach the database" }

$dsn = $null
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*QE_DATABASE_URL\s*=\s*(.+?)\s*$') { $dsn = $Matches[1] }
}
if (-not $dsn) { throw "QE_DATABASE_URL not found in .env" }

# The pipeline's DSN is SQLAlchemy's, carrying a driver: postgresql+psycopg://...
# libpq does not understand the "+psycopg" part, and pg_dump's failure mode is not an
# error — it silently discards the whole URL and connects to the default host and port
# 5432, which here is nothing at all. Strip the driver rather than keeping a second DSN
# in .env for one script to read.
$dsn = $dsn -replace '^postgresql\+[a-z0-9]+://', 'postgresql://'

$pgDump = 'C:\QuantumEdgePg\pgsql\bin\pg_dump.exe'
if (-not (Test-Path $pgDump)) { throw "pg_dump not found at $pgDump" }

if (-not (Test-Path $DbDir)) { New-Item -ItemType Directory -Path $DbDir | Out-Null }
$stamp = Get-Date -Format 'yyyy-MM-dd-HHmmss'
$dump = Join-Path $DbDir "quantumedge-$stamp.dump"

Write-Host "`nDumping the database..."
# Custom format (-Fc): compressed, and restorable table-by-table with pg_restore.
& $pgDump --dbname=$dsn --format=custom --file=$dump --no-password
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }

$size = [math]::Round((Get-Item $dump).Length / 1MB, 1)
Write-Host "  wrote $dump ($size MB)" -ForegroundColor Green

# Prove the dump is readable rather than trusting that it was written. A silently
# corrupt backup is worse than none, because it is trusted.
$toc = & 'C:\QuantumEdgePg\pgsql\bin\pg_restore.exe' --list $dump 2>&1
if ($LASTEXITCODE -ne 0) { throw "the dump just written is not readable by pg_restore" }
$tables = ($toc | Select-String -Pattern 'TABLE DATA' -SimpleMatch).Count
Write-Host "  pg_restore reads it: $tables tables with data" -ForegroundColor Green

# Keep a window of dumps. One dump is a single point of failure in its own right: a
# corruption noticed late needs an older copy to fall back to.
$old = Get-ChildItem $DbDir -Filter 'quantumedge-*.dump' |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
foreach ($f in $old) {
    Remove-Item $f.FullName -Confirm:$false
    Write-Host "  pruned $($f.Name)" -ForegroundColor DarkGray
}

# --- plain files ------------------------------------------------------------------------
# Everything in the working tree that is not in git and not reproducible, plus a plain copy
# of the tracked files so a recovery does not need git to read anything.
#
# This is small: ~290 files, ~12 MB. The two big directories are deliberately out —
# pipeline\.venv is 9.8 GB and web\node_modules is 24 GB, and both rebuild exactly from
# uv.lock and package-lock.json, so copying them would be 34 GB of churn to save two
# commands.
#
# .env IS included. It holds the database passwords, and keeping it out means a loss costs
# the pg_hba trust-then-ALTER-ROLE dance in docs/BACKUP.md. Both copies are on this one
# machine, so a second local copy widens the exposure by almost nothing while removing a
# real recovery step. If that trade ever looks wrong, exclude it here and the documented
# reset still works.

if (-not $SkipFiles) {
    Write-Host "`nMirroring plain files..."
    if (-not (Test-Path $FilesDir)) { New-Item -ItemType Directory -Path $FilesDir | Out-Null }

    $excludeDirs = @(
        (Join-Path $repo '.git'),
        (Join-Path $repo 'pipeline\.venv'),
        (Join-Path $repo 'pipeline\.mypy_cache'),
        (Join-Path $repo 'pipeline\.pytest_cache'),
        (Join-Path $repo 'pipeline\.ruff_cache'),
        (Join-Path $repo 'web\node_modules'),
        (Join-Path $repo 'web\.next'),
        '__pycache__'
    )

    # /MIR so a file deleted here is deleted there: a backup that accumulates files the
    # project no longer has is a backup nobody can reason about.
    & robocopy $repo $FilesDir /MIR /XD @excludeDirs /XF '*.pyc' /R:1 /W:1 /NFL /NDL /NJH /NJS | Out-Null

    # robocopy's exit code is a BIT FIELD, not a status: 0-7 all mean success (1 = files
    # copied, 2 = extra files present, 4 = mismatches), and only >= 8 is a real failure.
    # Treating non-zero as an error here would fail on every normal run.
    $code = $LASTEXITCODE
    if ($code -ge 8) { throw "robocopy failed with exit code $code" }

    $copied = (Get-ChildItem $FilesDir -Recurse -File -Force -ErrorAction SilentlyContinue).Count
    Write-Host "  $copied files mirrored to $FilesDir" -ForegroundColor Green

    # Spot-check the two things that exist ONLY here - the git mirror has neither, so if
    # this copy is wrong they are not stored anywhere.
    foreach ($critical in @('.env', 'pipeline\data\filings')) {
        $target = Join-Path $FilesDir $critical
        if (Test-Path $target) {
            Write-Host "  [ok]   $critical" -ForegroundColor Green
        } else {
            Write-Host "  [FAIL] $critical did not copy" -ForegroundColor Red
            throw "$critical is not in the file backup, and it is in no other backup either"
        }
    }
}

Write-Host "`nBackup complete." -ForegroundColor Green
Write-Host "git    -> $Mirror"
Write-Host "db     -> $DbDir"
if (-not $SkipFiles) { Write-Host "files  -> $FilesDir" }
Write-Host "Excluded as reproducible: pipeline\.venv (uv sync), web\node_modules (npm ci),"
Write-Host "caches. See docs/BACKUP.md."

# Exit 0 explicitly. robocopy's exit code is a bit field where 1 means "files were copied"
# - a success - and it lingers in $LASTEXITCODE, so without this the script reported
# success and then exited 1. Anything reading the exit code would have seen every normal
# backup as a failure.
exit 0
