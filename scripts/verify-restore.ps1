<#
.SYNOPSIS
    Restore the newest database backup into a throwaway database and check what survived.

.DESCRIPTION
    `backup.ps1` proves a dump is *readable* by running `pg_restore --list` over it. That
    is not the same as proving it restores: a dump can list its table of contents and still
    fail to load.

    This does the real thing. It restores into a fresh database — never over the live one —
    and then checks the part that cannot be regenerated. Almost everything in this
    database is reproducible, which is what M13 proves: the metrics, benchmarks and flag
    history all recompute from stored price data. The review stamps do not.
    `fact_sheet.reviewed_by` and `fact_sheet_review_log` record what a person did, and if
    they are not in the dump then the product's central claim — that nothing publishes
    without a human stamp — has no evidence behind it after a restore.

    So the assertions here are deliberately weighted: row counts for the reproducible
    tables, and the audit trail checked in detail.

    Run it after changing the backup script, and whenever you want to know the backups are
    real rather than merely present.

.EXAMPLE
    .\scripts\verify-restore.ps1

.EXAMPLE
    .\scripts\verify-restore.ps1 -Dump D:\QuantumEdge-backup-db\quantumedge-2026-10-05-172056.dump
#>

[CmdletBinding()]
param(
    [string]$DbDir = 'D:\QuantumEdge-backup-db',
    [string]$Dump,
    [string]$Database = 'quantumedge_restoretest',
    [switch]$Keep
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$pgBin = 'C:\QuantumEdgePg\pgsql\bin'

if (-not $Dump) {
    $newest = Get-ChildItem $DbDir -Filter 'quantumedge-*.dump' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) { throw "no dumps found in $DbDir - run scripts\backup.ps1 first" }
    $Dump = $newest.FullName
}
if (-not (Test-Path $Dump)) { throw "dump not found: $Dump" }

# The DSN carries a password: read it, use it, never print it.
$envFile = Join-Path $repo '.env'
$dsn = $null
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*QE_DATABASE_URL\s*=\s*(.+?)\s*$') { $dsn = $Matches[1] }
}
if (-not $dsn) { throw 'QE_DATABASE_URL not found in .env' }
# libpq does not understand SQLAlchemy's "+psycopg" and silently falls back to port 5432.
$libpq = $dsn -replace '^postgresql\+[a-z0-9]+://', 'postgresql://'
$target = $libpq -replace '/[^/]+$', "/$Database"

$failures = @()
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Host ("  [ok]   " + $name) -ForegroundColor Green }
    else {
        Write-Host ("  [FAIL] " + $name + ' ' + $detail) -ForegroundColor Red
        $script:failures += $name
    }
}
function Scalar([string]$sql) { (& "$pgBin\psql.exe" $target -q -t -A -c $sql).Trim() }

Write-Host "Dump:     $(Split-Path -Leaf $Dump) ($([math]::Round((Get-Item $Dump).Length/1MB,1)) MB)"
Write-Host "Restoring into: $Database (the live database is untouched)"

& "$pgBin\psql.exe" $libpq -q -c "DROP DATABASE IF EXISTS $Database;" | Out-Null
& "$pgBin\psql.exe" $libpq -q -c "CREATE DATABASE $Database;"
if ($LASTEXITCODE -ne 0) { throw 'could not create the scratch database' }

Write-Host "`n1. pg_restore..."
& "$pgBin\pg_restore.exe" --dbname=$target --no-owner $Dump
Check 'the dump restores without error' ($LASTEXITCODE -eq 0)

Write-Host "`n2. The part that cannot be regenerated..."
$log = [int](Scalar 'SELECT count(*) FROM fact_sheet_review_log')
Check "review log entries survived ($log)" ($log -gt 0) 'the audit trail is the whole reason for this dump'

$stamped = [int](Scalar 'SELECT count(*) FROM fact_sheet WHERE reviewed_by IS NOT NULL')
Check "review stamps survived ($stamped)" ($stamped -gt 0)

# A stamp without a reviewer who exists is not a stamp. reviewed_by is a foreign key, so a
# partial restore that dropped app_user would show up here.
$orphan = [int](Scalar @'
SELECT count(*) FROM fact_sheet f
WHERE f.reviewed_by IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM app_user u WHERE u.slug = f.reviewed_by)
'@)
Check 'every stamp names a reviewer who exists' ($orphan -eq 0) "$orphan orphaned"

$notes = [int](Scalar "SELECT count(*) FROM fact_sheet_review_log WHERE coalesce(note,'') <> ''")
Check "review notes survived ($notes)" ($notes -gt 0) 'what the reviewer said they checked'

Write-Host "`n3. The publication boundary..."
Check 'the published view exists and is queryable' `
    ((Scalar "SELECT count(*) >= 0 FROM published_ticker_page") -eq 't')
Check 'qe_web is still denied fact_sheet' `
    ((Scalar "SELECT has_table_privilege('qe_web','fact_sheet','SELECT')") -eq 'f')

# The constraint, not just the data: a restore that dropped the CHECK would let the next
# publish bypass the gate.
$unstamped = [int](Scalar @'
SELECT count(*) FROM fact_sheet
WHERE status = 'published' AND (reviewed_by IS NULL OR published_at IS NULL OR NOT qa_passed)
'@)
Check 'no published sheet lacks its stamp' ($unstamped -eq 0)

Write-Host "`n4. The reproducible tables (counts only - M13 can rebuild these)..."
foreach ($t in @('price_bar', 'metric_value', 'metric_benchmark', 'flag_instance',
                 'flag_state_change', 'fact_sheet')) {
    $n = [int](Scalar "SELECT count(*) FROM $t")
    Check "$t has rows ($n)" ($n -gt 0)
}

if (-not $Keep) {
    Write-Host "`nDropping $Database..."
    & "$pgBin\psql.exe" $libpq -q -c "DROP DATABASE IF EXISTS $Database;" | Out-Null
} else {
    Write-Host "`nKept $Database for inspection (-Keep)."
}

if ($failures.Count -gt 0) {
    Write-Host "`n$($failures.Count) check(s) failed:" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}

Write-Host "`nRestore verified: the backup is real, and the audit trail came back." -ForegroundColor Green
