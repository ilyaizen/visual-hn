<#
.SYNOPSIS
    Removes the Visual-HN Windows service.

.DESCRIPTION
    This script removes the Visual-HN Windows service that was installed using NSSM (Non-Sucking Service Manager).
    It stops the service if it's running and then completely removes it from the system.

.NOTES
    - Requires NSSM to be installed at "C:\Tools\nssm\nssm.exe"
    - Requires administrative privileges
#>

# Ensure script is running with administrative privileges
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "This script requires administrative privileges. Please run as Administrator."
    exit 1
}

# Configuration
$ServiceName = "Visual-HN"
$NssmPaths = @(
    "C:\Tools\nssm\nssm.exe",
    "C:\Program Files\nssm\nssm.exe",
    "C:\Program Files (x86)\nssm\nssm.exe"
)

# Find NSSM
$NssmPath = $null
foreach ($path in $NssmPaths) {
    if (Test-Path $path) {
        $NssmPath = $path
        Write-Host "Found NSSM at: $NssmPath"
        break
    }
}

if (-NOT $NssmPath) {
    Write-Warning "NSSM not found in standard locations. Will try using sc.exe instead."
}

# Check if service exists
$ServiceExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-NOT $ServiceExists) {
    Write-Warning "Service '$ServiceName' does not exist. Nothing to remove."
    exit 0
}

# Stop the service if it's running
$Service = Get-Service -Name $ServiceName
if ($Service.Status -eq "Running") {
    Write-Host "Stopping $ServiceName service..."
    Stop-Service -Name $ServiceName
    # Wait for the service to stop
    $timeout = 30
    $elapsed = 0
    while ((Get-Service -Name $ServiceName).Status -ne "Stopped" -and $elapsed -lt $timeout) {
        Start-Sleep -Seconds 1
        $elapsed++
    }
    
    if ((Get-Service -Name $ServiceName).Status -ne "Stopped") {
        Write-Warning "Service did not stop gracefully. Forcing removal..."
    }
    else {
        Write-Host "Service stopped successfully."
    }
}

# Remove the service
Write-Host "Removing $ServiceName service..."

# Try NSSM first if available
if ($NssmPath) {
    try {
        & $NssmPath remove $ServiceName confirm
    }
    catch {
        Write-Warning "NSSM removal failed: $_"
    }
}

# Wait a moment for the service to be marked for deletion
Start-Sleep -Seconds 3

# Verify service was removed
$ServiceExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-NOT $ServiceExists) {
    Write-Host "Service '$ServiceName' was successfully removed!" -ForegroundColor Green
}
else {
    # Service still exists, try using sc.exe to force delete
    Write-Warning "Service still exists. Attempting force removal using sc.exe..."
    try {
        $result = & sc.exe delete $ServiceName 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Service '$ServiceName' was successfully removed using sc.exe!" -ForegroundColor Green
        }
        else {
            Write-Warning "sc.exe removal returned: $result"
            Write-Host "Service is marked for deletion. Waiting for deletion to complete..."
            
            # Wait and retry multiple times
            $maxRetries = 10
            $retryCount = 0
            while ($retryCount -lt $maxRetries) {
                Start-Sleep -Seconds 5
                $ServiceExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
                if (-NOT $ServiceExists) {
                    Write-Host "Service deletion completed after $((($retryCount + 1) * 5)) seconds." -ForegroundColor Green
                    break
                }
                $retryCount++
                Write-Host "Still waiting... ($retryCount/$maxRetries)"
            }
            
            # Check if service is finally gone
            $ServiceExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($ServiceExists) {
                Write-Error "Failed to remove service '$ServiceName'."
                Write-Host "`nThe service is stuck in a 'marked for deletion' state."
                Write-Host "Please try one of the following:"
                Write-Host "  1. Restart your computer and run this script again"
                Write-Host "  2. Close all Service Manager windows (services.msc) and try again"
                Write-Host "  3. Run: sc.exe delete $ServiceName (may require restart)"
                exit 1
            }
        }
    }
    catch {
        Write-Error "Failed to remove service '$ServiceName'. Error: $_"
        Write-Host "`nThe service may be in a 'marked for deletion' state."
        Write-Host "Please try one of the following:"
        Write-Host "  1. Restart your computer and run this script again"
        Write-Host "  2. Close all Service Manager windows (services.msc) and try again"
        Write-Host "  3. Run: sc.exe delete $ServiceName (may require restart)"
        exit 1
    }
}

Write-Host "`nVisual-HN service has been completely removed from your system."