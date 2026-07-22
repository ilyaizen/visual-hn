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

# Install Playwright's bundled Chromium
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

The browser runs **headless** — no visible window, no taskbar button, no focus
stealing. It uses [Playwright](https://playwright.dev/) to drive a bundled
Chromium, which auto-passes most Cloudflare managed challenges.

When Cloudflare throws an interactive challenge, the fetcher searches all
frames for the "verify you are human" checkbox and clicks it. If it doesn't
resolve within `CF_CHALLENGE_MAX_WAIT` seconds, the fetch returns an error
and the VPS falls through to Wayback → screenshot → favicon composite.

| Env var                          | Default                  | Purpose                                        |
| -------------------------------- | ------------------------ | ---------------------------------------------- |
| `RESIDENTIAL_FETCHER_PORT`       | `8765`                   | Port to listen on                              |
| `RESIDENTIAL_FETCHER_SECRET`     | _(disabled)_             | Shared secret matching the VPS (min 24 chars)  |
| `CF_CHALLENGE_MAX_WAIT`          | `60`                     | Seconds to wait for headless CF auto-solve     |

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
