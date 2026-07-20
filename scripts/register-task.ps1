# Registers or unregisters Windows Scheduled Tasks for the
# Visual-HN Residential Fetcher.
#
# Two tasks:
#   1. VHN-ResidentialFetcher         — runs on login, starts the fetcher
#   2. VHN-ResidentialFetcher-Watchdog — runs every 5 min, restarts the
#                                          fetcher if /health fails 3x
#
# The watchdog is the fix for the reliability gap where a dead Chromium
# process leaves the fetcher task in "Running" state without actually
# serving requests. See docs/DEPLOYMENT.md → "Known reliability gap".
#
# Usage:
#   .\scripts\register-task.ps1              # register both
#   .\scripts\register-task.ps1 -Uninstall   # remove both

param(
    [switch]$Uninstall
)

# Halt on any registration error — without this, Register-ScheduledTask
# failures print to stderr but execution continues, producing misleading
# "Registered" success messages for tasks that don't actually exist.
$ErrorActionPreference = 'Stop'

$FetcherTaskName  = "VHN-ResidentialFetcher"
$WatchdogTaskName = "VHN-ResidentialFetcher-Watchdog"

if ($Uninstall) {
    foreach ($name in @($WatchdogTaskName, $FetcherTaskName)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed scheduled task: $name"
        } else {
            Write-Host "Task not found: $name (nothing to remove)"
        }
    }
    exit 0
}

# Resolve launcher scripts (VBS wrappers for silent execution — no console window flash)
$FetcherLauncher  = (Resolve-Path "$PSScriptRoot\run-fetcher-silent.vbs").Path
$WatchdogLauncher = (Resolve-Path "$PSScriptRoot\run-watchdog-silent.vbs").Path

foreach ($script in @($FetcherLauncher, $WatchdogLauncher)) {
    if (-not (Test-Path $script)) {
        Write-Error "Missing script: $script"
        exit 1
    }
}

# --- Register the fetcher task (AtLogOn + 3 min retries for start failures) ---

if (Get-ScheduledTask -TaskName $FetcherTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $FetcherTaskName -Confirm:$false
}

$FetcherAction   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//nologo `"$FetcherLauncher`""
$FetcherTrigger  = New-ScheduledTaskTrigger -AtLogOn
$FetcherSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $FetcherTaskName -Action $FetcherAction -Trigger $FetcherTrigger -Settings $FetcherSettings -Description "Visual-HN headful residential fetcher — Cloudflare bypass (called by VPS)" -Force | Out-Null
Write-Host "Registered fetcher task: $FetcherTaskName (triggers AtLogOn)"

# --- Register the watchdog task (every 5 min, independent of logon) ---

if (Get-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $WatchdogTaskName -Confirm:$false
}

# Watchdog runs every 5 minutes with a 15-minute repeat trigger.
# It checks /health; if it fails 3x (with 10s spacing inside the script),
# it stops and restarts the fetcher task. Silent on happy path.
$WatchdogAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//nologo `"$WatchdogLauncher`""
$WatchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
                    -RepetitionInterval (New-TimeSpan -Minutes 5) `
                    -RepetitionDuration (New-TimeSpan -Days 3650)

# Watchdog should survive sleep/wake and run whether or not anyone is logged in
$WatchdogSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $WatchdogTaskName -Action $WatchdogAction -Trigger $WatchdogTrigger -Settings $WatchdogSettings -Description "Visual-HN watchdog — restarts the fetcher if /health fails 3x (5-min interval)" -Force | Out-Null
Write-Host "Registered watchdog task: $WatchdogTaskName (every 5 min)"

# --- Summary ---

Write-Host ""
Write-Host "Both tasks registered. The fetcher will start on next login; the watchdog"
Write-Host "starts in 1 minute and runs every 5 minutes thereafter."
Write-Host ""
Write-Host "Manual control:"
Write-Host "  Fetcher:"
Write-Host "    Start:  Start-ScheduledTask -TaskName '$FetcherTaskName'"
Write-Host "    Stop:   Stop-ScheduledTask -TaskName '$FetcherTaskName'"
Write-Host "    Status: Get-ScheduledTask -TaskName '$FetcherTaskName' | Get-ScheduledTaskInfo"
Write-Host "  Watchdog:"
Write-Host "    Run now: Start-ScheduledTask -TaskName '$WatchdogTaskName'"
Write-Host "    Last result: (Get-ScheduledTask -TaskName '$WatchdogTaskName' | Get-ScheduledTaskInfo).LastTaskResult"
Write-Host "  Remove both:"
Write-Host "    .\scripts\register-task.ps1 -Uninstall"
