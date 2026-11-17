<#
Registers a Windows Task Scheduler task that starts the scraper's scheduler at logon and restarts
it if it stops. The schedules themselves stay in config.yaml, per site.

Usage (PowerShell, from the project folder):
    powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
Remove:
    Unregister-ScheduledTask -TaskName "Crawl4AIPriceScraper" -Confirm:$false
#>
param(
    [string]$TaskName = "Crawl4AIPriceScraper"
)

$ErrorActionPreference = "Stop"
$projectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$batch = Join-Path $projectDir "scripts\start_scheduler.bat"
if (-not (Test-Path (Join-Path $projectDir ".venv\Scripts\python.exe"))) {
    throw "Virtual environment not found in $projectDir\.venv - complete the installation steps first."
}

$action = New-ScheduledTaskAction -Execute $batch -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Crawl4AI price scraper: every site on the schedule in config.yaml" -Force | Out-Null

Write-Host "Task '$TaskName' registered: starts at logon."
Write-Host "Start it now:  Start-ScheduledTask -TaskName '$TaskName'"
