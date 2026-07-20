# Watchdog simulation: stop the fetcher, kill the port owner, then poll
# /health every 30s for up to 12 minutes. Report whether the watchdog task
# restarted the fetcher on its own. Logs to watchdog-sim.log.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\watchdog-sim.ps1

$ErrorActionPreference = 'Continue'
$logPath = Join-Path $PSScriptRoot 'watchdog-sim.log'
$port    = 8765
$maxMin  = 12

function Log($msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts] $msg"
    Write-Host $line
    $line | Out-File -FilePath $logPath -Append -Encoding utf8
}

Log "=== Watchdog simulation start ==="

# 1. Stop the fetcher task.
try {
    Stop-ScheduledTask -TaskName 'VHN-ResidentialFetcher' -ErrorAction Stop
    Log "Stopped fetcher task."
} catch {
    Log "Stop-ScheduledTask failed: $_"
}

# 2. Kill any python still holding the port (paranoia — the task stop should
#    have reaped the child, but PowerShell redirection sometimes orphans it).
Start-Sleep -Seconds 3
$killed = 0
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*residential_fetcher*' } |
    ForEach-Object {
        try {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
            $killed++
            Log "Killed orphaned fetcher PID $($_.ProcessId)."
        } catch {
            Log "Kill failed for PID $($_.ProcessId): $_"
        }
    }
if ($killed -eq 0) { Log "No orphaned fetcher processes found." }

# 3. Confirm port is dead.
Start-Sleep -Seconds 2
try {
    $r = Invoke-WebRequest -Uri "http://localhost:$port/health" -UseBasicParsing -TimeoutSec 3
    Log "WARN: /health still responding ($($r.StatusCode)) -- port not killed cleanly. Aborting."
    return
} catch {
    Log "Port $port confirmed dead. Waiting for watchdog tick."
}

# 4. Poll for restart. Watchdog fires every 5 min; allow up to 12 min for
#    a tick boundary + the 3x10s health-check sequence inside the watchdog.
$start = Get-Date
$found = $false
while (((Get-Date) - $start).TotalMinutes -lt $maxMin) {
    Start-Sleep -Seconds 30
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$port/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200 -and $r.Content -match '"status"\s*:\s*"ok"') {
            $elapsed = ((Get-Date) - $start).TotalSeconds
            Log "SUCCESS: fetcher restarted by watchdog after $([math]::Round($elapsed,0))s. /health = $($r.StatusCode)."
            $found = $true
            break
        }
    } catch {
        $elapsed = ((Get-Date) - $start).TotalSeconds
        Log "Still down at $([math]::Round($elapsed,0))s."
    }
}

if (-not $found) {
    Log "FAIL: fetcher NOT restarted within ${maxMin} min. Watchdog did not recover it."
}

# 5. Final state snapshot.
$ft  = Get-ScheduledTask -TaskName 'VHN-ResidentialFetcher'
$wd  = Get-ScheduledTask -TaskName 'VHN-ResidentialFetcher-Watchdog'
$wdi = $wd | Get-ScheduledTaskInfo
Log "Final fetcher state:   $($ft.State)"
Log "Final watchdog state:  $($wd.State)"
Log "Watchdog last result:  0x$('{0:X8}' -f $wdi.LastTaskResult)"
Log "Watchdog last run:     $($wdi.LastRunTime)"
Log "=== Simulation end ==="
