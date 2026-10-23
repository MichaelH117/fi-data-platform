<#
.SYNOPSIS
  Windows task runner (same targets as the Makefile).
.EXAMPLE
  .\tasks.ps1 run
  .\tasks.ps1 day
  .\tasks.ps1 demo        # build + 3 daily runs incl. one injected gate failure and its recovery
  .\tasks.ps1 schedule    # register a weekday 06:00 'day' run so the 7 AM SLO builds real history
#>
param(
    [ValidateSet("setup", "lint", "test", "run", "day", "demo", "schedule", "unschedule", "dbt", "docs", "clean", "up", "down", "help")]
    [string]$Task = "help"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"
$taskName = "vsc-data-platform-daily"

function Invoke-Py { & $py @args; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }

switch ($Task) {
    "setup" { python -m venv .venv; Invoke-Py -m pip install -r requirements.txt }
    "lint"  { Invoke-Py -m ruff check . }
    "test"  { Invoke-Py -m pytest -q }
    "run"   { Invoke-Py -m pipeline.run }
    "day"   { Invoke-Py -m pipeline.run --append-day }
    "demo"  { Invoke-Py tools\demo_history.py }   # tools/demo_history.py --help: --days, --fail-day, --dry-run
    "schedule" {
        # Weekdays 06:00 local, before the 07:00 SLO deadline (docs/operating-model/SLA.md).
        $action = New-ScheduledTaskAction -Execute (Resolve-Path $py).Path -Argument "-m pipeline.run --append-day" `
            -WorkingDirectory $PSScriptRoot
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 6am
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Force | Out-Null
        "registered scheduled task '$taskName' (weekdays 06:00); remove with .\tasks.ps1 unschedule"
    }
    "unschedule" { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    "dbt"   { Invoke-Py -m pipeline.run --only dbt }
    "docs"  { Invoke-Py -m dbt.cli.main docs generate --project-dir dbt --profiles-dir dbt }
    "clean" { "data", "site", "dbt\target", "dbt\logs" | Where-Object { Test-Path $_ } | Remove-Item -Recurse -Force }
    "up"    { docker compose --profile airflow --profile bi up -d }   # needs Docker; untested
    "down"  { docker compose --profile airflow --profile bi --profile source down }
    default { Get-Help $PSCommandPath -Examples; "Tasks: setup lint test run day demo schedule unschedule dbt docs clean up down" }
}
