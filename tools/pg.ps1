<#
.SYNOPSIS
    Start, stop or check the local PostgreSQL server used by the opt-in Postgres publish step.

.DESCRIPTION
    Reads PG_BIN, PG_DATA, PG_LOG, PG_HOST, PG_PORT, PG_DATABASE and the logins from .env at the repo
    root (git-ignored; copy .env.example). No admin rights and no Windows service: pg_ctl runs the server
    as the current user. The default pipeline never needs this server.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\pg.ps1 start
    powershell -ExecutionPolicy Bypass -File tools\pg.ps1 status
    powershell -ExecutionPolicy Bypass -File tools\pg.ps1 psql            # as bi_reader
    powershell -ExecutionPolicy Bypass -File tools\pg.ps1 psql -Superuser
    powershell -ExecutionPolicy Bypass -File tools\pg.ps1 stop
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'psql')]
    [string]$Command = 'status',
    [switch]$Superuser
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repo '.env'
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile (copy .env.example and fill it in)" }

$cfg = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $cfg[$Matches[1]] = $Matches[2] }
}
foreach ($k in 'PG_BIN', 'PG_DATA') {
    if (-not $cfg[$k]) { throw "$k is not set in .env" }
}
$bin = $cfg['PG_BIN']
$data = $cfg['PG_DATA']
$log = if ($cfg['PG_LOG']) { $cfg['PG_LOG'] } else { Join-Path $data 'server.log' }
$pgHost = if ($cfg['PG_HOST']) { $cfg['PG_HOST'] } else { '127.0.0.1' }
$port = if ($cfg['PG_PORT']) { $cfg['PG_PORT'] } else { '5432' }
$db = if ($cfg['PG_DATABASE']) { $cfg['PG_DATABASE'] } else { 'vsc_analytics' }
$pgCtl = Join-Path $bin 'pg_ctl.exe'

function Test-Running { & $pgCtl status -D $data *> $null; return ($LASTEXITCODE -eq 0) }

switch ($Command) {
    'start' {
        if (Test-Running) { Write-Output "already running (port $port)"; break }
        & $pgCtl start -D $data -l $log -o "-p $port" -w -t 60
        if ($LASTEXITCODE -ne 0) { throw "pg_ctl start failed; see $log" }
    }
    'stop' {
        if (-not (Test-Running)) { Write-Output 'not running'; break }
        & $pgCtl stop -D $data -m fast -w
    }
    'restart' {
        & $pgCtl restart -D $data -l $log -o "-p $port" -m fast -w -t 60
    }
    'status' {
        & $pgCtl status -D $data
        if ($LASTEXITCODE -eq 0) { & (Join-Path $bin 'pg_isready.exe') -h $pgHost -p $port }
        exit $LASTEXITCODE
    }
    'psql' {
        if ($Superuser) { $user = $cfg['PG_SUPERUSER']; $env:PGPASSWORD = $cfg['PG_SUPERUSER_PASSWORD'] }
        else { $user = $cfg['PG_BI_READER_USER']; $env:PGPASSWORD = $cfg['PG_BI_READER_PASSWORD'] }
        try { & (Join-Path $bin 'psql.exe') -h $pgHost -p $port -U $user -d $db }
        finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
    }
}
