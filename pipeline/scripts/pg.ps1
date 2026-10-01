<#
.SYNOPSIS
  Start, stop, or inspect the local QuantumEdge Postgres cluster.

.DESCRIPTION
  The cluster lives outside the repo on purpose: this repo sits on an exFAT volume,
  which lacks the journaling and fsync guarantees PostgreSQL needs. See docs/DECISIONS.md.

  `pg_ctl start` is launched via Start-Process rather than called directly, because the
  server inherits the console's output handles and a direct call never returns.

.EXAMPLE
  .\pipeline\scripts\pg.ps1 start
  .\pipeline\scripts\pg.ps1 status
  .\pipeline\scripts\pg.ps1 stop
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('start', 'stop', 'status', 'restart', 'log')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'

$PgBin  = 'C:\QuantumEdgePg\pgsql\bin'
$PgData = 'C:\QuantumEdgePg\data'
$PgLog  = 'C:\QuantumEdgePg\logs\postgres.log'
$Port   = 55432

if (-not (Test-Path $PgBin)) {
    Write-Error "Postgres binaries not found at $PgBin. See README.md for setup."
}

function Test-Listening {
    $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

switch ($Action) {
    'start' {
        if (Test-Listening) { Write-Host "already running on port $Port" -ForegroundColor Yellow; break }
        New-Item -ItemType Directory -Force (Split-Path $PgLog) | Out-Null
        # The server must not inherit this console's stdout/stderr handles, or the
        # launching shell never returns even though the server came up fine.
        # Start-Process leaks them even with -NoNewWindow or explicit redirection,
        # because postgres.exe inherits from pg_ctl. UseShellExecute inherits nothing,
        # which fully detaches the server. pg_ctl's own -l flag handles logging.
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = "$PgBin\pg_ctl.exe"
        $psi.Arguments = "-D `"$PgData`" -l `"$PgLog`" start"
        $psi.UseShellExecute = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        [void][System.Diagnostics.Process]::Start($psi)
        # Poll rather than trusting a fixed sleep.
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 400
            if (Test-Listening) { Write-Host "started on port $Port" -ForegroundColor Green; exit 0 }
        }
        Write-Error "did not come up within ~12s. Last log lines:`n$(Get-Content $PgLog -Tail 15 | Out-String)"
    }
    'stop' {
        if (-not (Test-Listening)) { Write-Host "not running" -ForegroundColor Yellow; break }
        & "$PgBin\pg_ctl.exe" -D $PgData -m fast stop
        Write-Host "stopped" -ForegroundColor Green
    }
    'restart' {
        & $PSCommandPath stop
        & $PSCommandPath start
    }
    'status' {
        if (Test-Listening) {
            Write-Host "listening on port $Port" -ForegroundColor Green
            & "$PgBin\pg_ctl.exe" -D $PgData status
        } else {
            Write-Host "not listening on port $Port" -ForegroundColor Red
            exit 1
        }
    }
    'log' { Get-Content $PgLog -Tail 40 }
}
