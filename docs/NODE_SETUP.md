# Visual-HN Residential Fetcher — Node Setup (Windows 11 PowerShell)

## Prerequisites

- Python 3.11+ installed and on PATH (`python --version`)
- Tailscale running and connected
- Git clone of visual-hn repo at your preferred location

## One-time setup

```powershell
cd D:\GitHub\visual-hn

# Create venv
python -m venv .node-venv
.\.node-venv\Scripts\Activate.ps1

# Install dependencies
pip install fastapi uvicorn playwright

# Install Chromium binary for Playwright
python -m playwright install chromium
```

If you get a PowerShell execution policy error when activating the venv:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

## Running manually

```powershell
cd D:\GitHub\visual-hn
.\.node-venv\Scripts\Activate.ps1

# Set the shared secret (must match VPS env var)
$env:RESIDENTIAL_FETCHER_SECRET = "your-secret-here"

# Optional: custom port (default 8765)
# $env:RESIDENTIAL_FETCHER_PORT = "8765"

python residential_fetcher.py
```

A Chromium window will open — that's normal. It's the headful browser that
solves Cloudflare challenges. Keep it running.

## Auto-start on login + watchdog (Task Scheduler)

```powershell
# Edit the paths in the script first, then run:
.\scripts\register-task.ps1
```

This registers **two** Windows Scheduled Tasks:

1. **`VHN-ResidentialFetcher`** — launches the fetcher on user login (`AtLogOn` trigger).
2. **`VHN-ResidentialFetcher-Watchdog`** — runs every 5 minutes, curls `/health`, and restarts the fetcher if it fails 3 consecutive checks (with 10s spacing). This is the fix for the reliability gap where a dead Chromium process leaves the fetcher task in "Running" state without actually serving requests.

To uninstall both: `.\scripts\register-task.ps1 -Uninstall`

### Why the watchdog exists

Without it, if a cleaner / laptop sleep / OOM kill / accidental close kills the Chromium process mid-session, the scheduled task stays in "Running" state from Scheduler's view but no process is serving. It won't restart until next login, and even then it may be stuck. The watchdog detects this within 5 minutes and restarts the fetcher automatically. See `docs/DEPLOYMENT.md` → "Known reliability gap" for the full story.

## Health check (from VPS or residential node)

```powershell
curl http://<tailscale-ip>:8765/health
# Should return: {"status":"ok"}
```
