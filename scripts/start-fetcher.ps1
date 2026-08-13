# Visual-HN Residential Fetcher -- launcher for Windows 11 PowerShell
# Activates the venv, sets env vars, and starts residential_fetcher.py
# Used by Task Scheduler for auto-start on login, or can be run manually

# --- Config --- Edit these to match your residential node ---
$RepoDir = "C:\dev\visual-hn"
$Port    = "18080"
# -----------------------------------------------------------

# Resolve repo directory (script may live in scripts/ subdirectory)
if (Test-Path "$PSScriptRoot\..\residential_fetcher.py") {
    $RepoDir = (Resolve-Path "$PSScriptRoot\..\").Path
}

$VenvPython = "$RepoDir\.node-venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error "Python venv not found at $VenvPython. Run scripts\NODE_SETUP.md first."
    exit 1
}

# Secret: read from env var, then from gitignored scripts/.fetcher-secret file.
# The fetcher requires >= 24 chars (MIN_SECRET_LENGTH in residential_fetcher.py).
# The VPS must send the same value via VHN_RESIDENTIAL_FETCHER_SECRET env var.
$Secret = $env:VHN_FETCHER_SECRET
$SecretFile = Join-Path $PSScriptRoot ".fetcher-secret"
if (-not $Secret -and (Test-Path $SecretFile)) {
    $Secret = (Get-Content $SecretFile -Raw).Trim()
}
if (-not $Secret -or $Secret.Length -lt 24) {
    Write-Error "Fetcher secret missing or too short (< 24 chars). Set `$env:VHN_FETCHER_SECRET or create scripts\.fetcher-secret (gitignored)."
    exit 1
}

$Headless = if ($null -ne $env:VHN_FETCHER_HEADLESS -and $env:VHN_FETCHER_HEADLESS -ne '') { $env:VHN_FETCHER_HEADLESS } else { "1" }

$env:RESIDENTIAL_FETCHER_SECRET   = $Secret
$env:RESIDENTIAL_FETCHER_PORT     = $Port
$env:RESIDENTIAL_FETCHER_HEADLESS = $Headless

$Mode = if ($Headless -in @("0", "false", "False", "no", "No")) { "headful" } else { "headless" }

Write-Host "Starting Visual-HN Residential Fetcher on port $Port ($Mode)..."
Write-Host "Repo: $RepoDir"
Write-Host "Python: $VenvPython"

Set-Location $RepoDir
& $VenvPython "residential_fetcher.py"