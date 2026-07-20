<#
.SYNOPSIS
    Installs Visual-HN as a Windows service using NSSM.

.DESCRIPTION
    This script installs Visual-HN as a Windows service using NSSM (Non-Sucking Service Manager).
    It sets up the service with proper parameters and configures it to handle system sleep/wake cycles.

.NOTES
    - Requires NSSM to be installed at "C:\Tools\nssm\nssm.exe"
    - Requires administrative privileges
    - Python 3.10 or higher must be installed
#>

# Ensure script is running with administrative privileges
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "This script requires administrative privileges. Please run as Administrator."
    exit 1
}

# Configuration
$ServiceName = "Visual-HN"
$ServiceDisplayName = "Visual-HN"
$ServiceDescription = "A visual Hacker News reader that scrapes and displays Hacker News stories"
$NssmPaths = @(
    "C:\Tools\nssm\nssm.exe",
    "C:\Program Files\nssm\nssm.exe",
    "C:\Program Files (x86)\nssm\nssm.exe"
)
# App lives one level up from this scripts/ directory
$AppDirectory = Split-Path $PSScriptRoot -Parent
$AppScript = Join-Path $AppDirectory "main.py"
$LogPath = Join-Path $AppDirectory "logs"
$StdoutLog = Join-Path $LogPath "stdout.log"
$StderrLog = Join-Path $LogPath "stderr.log"

# Determine Python path - prioritize virtual environment
$VenvPath = Join-Path $AppDirectory ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

if (Test-Path $VenvPython) {
    $PythonPath = $VenvPython
    Write-Host "Using Python from virtual environment: $PythonPath"
}
elseif ($env:VIRTUAL_ENV) {
    $VenvPython = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonPath = $VenvPython
        Write-Host "Using Python from active virtual environment: $PythonPath"
    }
    else {
        Write-Error "Virtual environment Python not found at: $VenvPython"
        exit 1
    }
}
else {
    # Try to find Python 3.12 specifically
    $Python312Paths = @(
        "C:\Users\User\AppData\Local\Python\bin\python3.12.exe",
        "C:\Users\User\AppData\Local\Python\pythoncore-3.12-64\python.exe"
    )
    foreach ($path in $Python312Paths) {
        if (Test-Path $path) {
            $PythonPath = $path
            Write-Host "Using Python 3.12: $PythonPath"
            break
        }
    }
    if (-NOT $PythonPath) {
        $PythonPath = (Get-Command python).Source
        Write-Host "Using default Python: $PythonPath"
    }
}

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
    Write-Error "NSSM not found in standard locations. Please install NSSM first."
    Write-Host "Expected locations:"
    foreach ($path in $NssmPaths) {
        Write-Host "  - $path"
    }
    exit 1
}

# Note: SYSTEM account has full access to local drives by default on Windows.
# If the service fails with access-denied errors, manually run:
#   icacls "<AppDirectory>" /grant "SYSTEM:(OI)(CI)F" /T

# Install system-wide dependencies
Write-Host "Installing dependencies using Python: $PythonPath..."
& $PythonPath -m pip install --no-input -r (Join-Path $AppDirectory "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to install dependencies"
    exit 1
}

# Create logs directory if it doesn't exist
if (-NOT (Test-Path $LogPath)) {
    New-Item -ItemType Directory -Path $LogPath | Out-Null
}

# Check if service already exists
Write-Host "Checking for existing service..."
$ServiceExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($ServiceExists) {
    Write-Warning "Service '$ServiceName' already exists. Removing it first..."
    try {
        & $NssmPath remove $ServiceName confirm
    }
    catch {
        Write-Warning "NSSM removal failed: $_"
    }
    Start-Sleep -Seconds 3
    
    # Check if service still exists and try sc.exe if needed
    $ServiceStillExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($ServiceStillExists) {
        Write-Warning "Service still exists. Attempting force removal using sc.exe..."
        $result = & sc.exe delete $ServiceName 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Service removed successfully using sc.exe."
        }
        else {
            Write-Warning "sc.exe removal returned: $result"
            Write-Host "Service is marked for deletion. Waiting for deletion to complete..."
            
            # Wait and retry multiple times
            $maxRetries = 10
            $retryCount = 0
            while ($retryCount -lt $maxRetries) {
                Start-Sleep -Seconds 5
                $ServiceStillExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
                if (-NOT $ServiceStillExists) {
                    Write-Host "Service deletion completed after $((($retryCount + 1) * 5)) seconds."
                    break
                }
                $retryCount++
                Write-Host "Still waiting... ($retryCount/$maxRetries)"
            }
            
            # Check if service is finally gone
            $ServiceStillExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if ($ServiceStillExists) {
                Write-Error "Service '$ServiceName' is stuck in 'marked for deletion' state."
                Write-Host "`nThis is a Windows service manager issue. Please try one of the following:"
                Write-Host "  1. Restart your computer and run this script again"
                Write-Host "  2. Close all Service Manager windows (services.msc) and try again"
                Write-Host "  3. Run: sc.exe delete $ServiceName (may require restart)"
                exit 1
            }
        }
    }
}

# Install the service
Write-Host "Installing $ServiceName service..."
& $NssmPath install $ServiceName $PythonPath $AppScript

# Configure service details
& $NssmPath set $ServiceName DisplayName $ServiceDisplayName
& $NssmPath set $ServiceName Description $ServiceDescription
& $NssmPath set $ServiceName AppDirectory $AppDirectory
& $NssmPath set $ServiceName AppExit Default Restart
& $NssmPath set $ServiceName Start SERVICE_AUTO_START

# Configure stdout/stderr logging
& $NssmPath set $ServiceName AppStdout $StdoutLog
& $NssmPath set $ServiceName AppStderr $StderrLog
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateBytes 10485760  # 10 MB
& $NssmPath set $ServiceName AppRotateOnline 1

# Configure service recovery options
& $NssmPath set $ServiceName AppRestartDelay 30000  # 30 seconds
& $NssmPath set $ServiceName AppThrottle 60000  # 1 minute
& $NssmPath set $ServiceName AppExit Default Restart

# Configure service to restart after system resume from sleep/hibernation
& $NssmPath set $ServiceName DependOnService "Power"

# Start the service
Write-Host "Starting $ServiceName service..."
Start-Service -Name $ServiceName

# Check if service started successfully
$Service = Get-Service -Name $ServiceName
if ($Service.Status -eq "Running") {
    Write-Host "Service '$ServiceName' installed and started successfully!" -ForegroundColor Green
    Write-Host "Service is running at http://localhost:80"
}
else {
    Write-Warning "Service '$ServiceName' installed but failed to start. Check the logs at $LogPath for details."
}

Write-Host "`nTo remove the service, run remove-service.ps1 script."