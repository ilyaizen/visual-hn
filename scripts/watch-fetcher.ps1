# Visual-HN Residential Fetcher — watchdog
# Checks /health every 5 minutes (via Task Scheduler repeat trigger).
# If /health fails 3 consecutive times, restarts the VHN-ResidentialFetcher task.
#
# Scheduled by register-task.ps1 as a separate task: VHN-ResidentialFetcher-Watchdog
# Logs every action to watch-fetcher.log next to this script. The prior version
# was silent on the happy path, which left no forensic trail when restarts
# failed silently — debugging a 0x00000001 LastTaskResult with no log was
# impossible. Now we log both paths.

$TaskName   = "VHN-ResidentialFetcher"
$Port       = 8765
$HealthUrl  = "http://localhost:$Port/health"
$MaxRetries = 3
$logPath    = Join-Path $PSScriptRoot "watch-fetcher.log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    $line | Out-File -FilePath $logPath -Append -Encoding utf8
}

function Test-FetcherHealth {
    try {
        $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
        return $resp.StatusCode -eq 200 -and $resp.Content -match '"status"\s*:\s*"ok"'
    } catch {
        return $false
    }
}

# Check health up to $MaxRetries times with 10s spacing
$failures = 0
for ($i = 1; $i -le $MaxRetries; $i++) {
    if (Test-FetcherHealth) {
        # Healthy — exit silently. Happy path is still silent in the event log;
        # we log it to watch-fetcher.log only for forensic completeness.
        Log "healthy (probe $i of $MaxRetries) -- no action."
        exit 0
    }
    $failures++
    if ($i -lt $MaxRetries) {
        Start-Sleep -Seconds 10
    }
}

# All retries failed — restart the fetcher task
Log "unhealthy ($failures consecutive failures), restarting $TaskName..."

# Stop the fetcher task (safe if already stopped; the process may be dead
# while the task object lingers in Running state -- Stop clears that)
try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Log "Stop-ScheduledTask OK."
} catch {
    Log "Stop-ScheduledTask threw (continuing): $_"
}

# 5s gap: 2s was too short -- under load, Task Scheduler has not finished
# transitioning the task out of Running when Start fires, and the Start
# becomes a no-op (task stays Ready, fetcher never launches, watchdog
# exits 0 but the fetcher stays down). 5s gives the scheduler room.
Start-Sleep -Seconds 5

# Start it fresh
try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Log "Start-ScheduledTask OK. Will verify on next watchdog tick."
} catch {
    Log "FAILED to restart ${TaskName}: $_"
    exit 1
}
