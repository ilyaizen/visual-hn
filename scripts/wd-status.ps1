# One-off diagnostic: print watchdog/fetcher task state + timing.
# Usage: powershell -NoProfile -File scripts\wd-status.ps1
$wd   = Get-ScheduledTask -TaskName 'VHN-ResidentialFetcher-Watchdog'
$ft   = Get-ScheduledTask -TaskName 'VHN-ResidentialFetcher'
$wdi  = $wd | Get-ScheduledTaskInfo
$fti  = $ft | Get-ScheduledTaskInfo
$now  = Get-Date

Write-Host "Now:                  $now"
Write-Host ""
Write-Host "Fetcher State:        $($ft.State)"
Write-Host "Fetcher Last Run:     $($fti.LastRunTime)"
Write-Host "Fetcher Last Result:  0x$('{0:X8}' -f $fti.LastTaskResult)"
Write-Host ""
Write-Host "Watchdog State:       $($wd.State)"
Write-Host "Watchdog Last Run:    $($wdi.LastRunTime)"
Write-Host "Watchdog Next Run:    $($wdi.NextRunTime)"
Write-Host "Watchdog Last Result: 0x$('{0:X8}' -f $wdi.LastTaskResult)"

# Minutes until next watchdog tick
if ($wdi.NextRunTime) {
    $delta = ($wdi.NextRunTime - $now).TotalMinutes
    Write-Host ("Minutes to next tick: {0:N1}" -f $delta)
}

# Is fetcher currently healthy?
try {
    $resp = Invoke-WebRequest -Uri 'http://localhost:8765/health' -UseBasicParsing -TimeoutSec 5
    Write-Host "Fetcher /health:      $($resp.StatusCode)"
} catch {
    Write-Host "Fetcher /health:      FAIL ($($_.Exception.Message))"
}
