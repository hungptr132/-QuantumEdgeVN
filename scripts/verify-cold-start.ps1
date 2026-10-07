<#
.SYNOPSIS
    Rebuild the schema from nothing in a throwaway database, and check the guarantees hold.

.DESCRIPTION
    The test suite migrates a database that already exists, so it cannot notice a migration
    that only works against a database someone prepared by hand. This runs the real thing:
    createdb, `alembic upgrade head`, seed, and then assert the properties a recovered
    database must have.

    It is not academic. The first time this drill was actually run, it failed:
    `ticker_alias_trgm` needs an operator class from pg_trgm and nothing in the migration
    created the extension, so `alembic upgrade head` on a fresh database died with
    "operator class gin_trgm_ops does not exist" - an error that says nothing about the
    cause. That path is the documented recovery procedure in docs/BACKUP.md.

    Run this after changing a migration, and before trusting a backup.

    The scratch database is dropped at the end unless -Keep is given. Nothing touches the
    real `quantumedge` database.

.EXAMPLE
    .\scripts\verify-cold-start.ps1
#>

[CmdletBinding()]
param(
    [string]$Database = 'quantumedge_coldstart',
    [switch]$Keep
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$pipeline = Join-Path $repo 'pipeline'
$pgBin = 'C:\QuantumEdgePg\pgsql\bin'

# The DSN carries a password: read it, use it, never print it.
$envFile = Join-Path $repo '.env'
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile" }
$dsn = $null
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*QE_DATABASE_URL\s*=\s*(.+?)\s*$') { $dsn = $Matches[1] }
}
if (-not $dsn) { throw 'QE_DATABASE_URL not found in .env' }

# libpq does not understand SQLAlchemy's "+psycopg"; it silently ignores the whole URL and
# falls back to port 5432, where nothing is listening.
$libpq = $dsn -replace '^postgresql\+[a-z0-9]+://', 'postgresql://'
$scratchSqlAlchemy = $dsn -replace '/[^/]+$', "/$Database"
$scratchLibpq = $libpq -replace '/[^/]+$', "/$Database"

$failures = @()
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) {
        Write-Host ("  [ok]   " + $name) -ForegroundColor Green
    } else {
        Write-Host ("  [FAIL] " + $name + ' ' + $detail) -ForegroundColor Red
        $script:failures += $name
    }
}

Write-Host "Scratch database: $Database (the real one is untouched)"

Write-Host "`n1. Creating an empty database..."
& "$pgBin\psql.exe" $libpq -q -c "DROP DATABASE IF EXISTS $Database;" | Out-Null
& "$pgBin\psql.exe" $libpq -q -c "CREATE DATABASE $Database;"
if ($LASTEXITCODE -ne 0) { throw 'could not create the scratch database' }

Write-Host "`n2. alembic upgrade head, and nothing else..."
$env:QE_DATABASE_URL = $scratchSqlAlchemy
try {
    Push-Location $pipeline
    & '.venv\Scripts\python.exe' -m alembic upgrade head
    $migrated = $LASTEXITCODE -eq 0
    Check 'migrations apply to an empty database' $migrated

    if ($migrated) {
        Write-Host "`n3. Seeding reference data..."
        & '.venv\Scripts\python.exe' -m quantumedge.cli db seed | Out-Null
        Check 'seeds load' ($LASTEXITCODE -eq 0)
    }
} finally {
    Pop-Location
    Remove-Item Env:\QE_DATABASE_URL -ErrorAction SilentlyContinue
}

if ($migrated) {
    Write-Host "`n4. Checking the guarantees a recovered database must have..."

    function Scalar([string]$sql) {
        (& "$pgBin\psql.exe" $scratchLibpq -q -t -A -c $sql).Trim()
    }

    Check 'the published view exists' `
        ((Scalar "SELECT count(*) FROM pg_views WHERE viewname='published_ticker_page'") -eq '1')

    # Invariant 5, both halves. The web role must read the view and nothing else, or a
    # forgotten WHERE clause in TypeScript becomes a leaked draft.
    Check 'qe_web can read the published view' `
        ((Scalar "SELECT has_table_privilege('qe_web','published_ticker_page','SELECT')") -eq 't')

    foreach ($table in @('fact_sheet', 'price_bar', 'ticker', 'financial_fact',
                         'metric_value', 'security_identifier')) {
        Check "qe_web is denied $table" `
            ((Scalar "SELECT has_table_privilege('qe_web','$table','SELECT')") -eq 'f')
    }

    # Invariant 1. Append-only is a property of the database, not of anyone's discipline.
    Check 'the raw_document append-only trigger exists' `
        ((Scalar "SELECT count(*) FROM pg_trigger WHERE tgname='raw_document_immutable'") -eq '1')

    Check 'pg_trgm is installed' `
        ((Scalar "SELECT count(*) FROM pg_extension WHERE extname='pg_trgm'") -eq '1')
    Check 'unaccent is installed' `
        ((Scalar "SELECT count(*) FROM pg_extension WHERE extname='unaccent'") -eq '1')

    $tables = [int](Scalar "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
    Check "schema has its tables ($tables)" ($tables -ge 30) "expected >= 30, got $tables"

    $covered = [int](Scalar 'SELECT count(*) FROM ticker WHERE is_covered')
    Check "covered tickers seeded ($covered)" ($covered -ge 9)
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

Write-Host "`nCold start verified: the repo alone rebuilds a correct database." -ForegroundColor Green
