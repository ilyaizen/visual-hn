# Visual-HN Residential Fetcher — launcher for Windows 11 PowerShell
# Activates the venv, sets env vars, and starts residential_fetcher.py
# Used by Task Scheduler for auto-start on login, or can be run manually

# ─── Config ─── Edit these to match your residential node ───
$RepoDir = "C:\dev\visual-hn"
$Secret  = "hello"
$Port    = "8765"
# ──────────────────────────────────────────────────

# Resolve repo directory (script may live in scripts/ subdirectory)
if (Test-Path "$PSScriptRoot\..\residential_fetcher.py") {
    $RepoDir = (Resolve-Path "$PSScriptRoot\..\").Path
}

$VenvPython = "$RepoDir\.node-venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error "Python venv not found at $VenvPython. Run scripts\NODE_SETUP.md first."
    exit 1
}

$env:RESIDENTIAL_FETCHER_SECRET = $Secret
$env:RESIDENTIAL_FETCHER_PORT   = $Port

Write-Host "Starting Visual-HN Residential Fetcher on port $Port..."
Write-Host "Repo: $RepoDir"
Write-Host "Python: $VenvPython"

Set-Location $RepoDir
& $VenvPython "residential_fetcher.py"
