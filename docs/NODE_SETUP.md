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

# Optional: custom port (default 18080)
# $env:RESIDENTIAL_FETCHER_PORT = "18080"

# Optional: run headful so you can solve Cloudflare / Turnstile manually.
# This opens a real Chromium window on the residential laptop.
# $env:RESIDENTIAL_FETCHER_HEADLESS = "0"

python residential_fetcher.py
```

Default mode is **headless** — no visible window, no taskbar button, no focus
stealing. It uses [Playwright](https://playwright.dev/) to drive a bundled
Chromium, which auto-passes some Cloudflare managed challenges.

If you need to solve an interactive Cloudflare / Turnstile checkbox yourself,
run the fetcher in **headful** mode by setting:

```powershell
$env:RESIDENTIAL_FETCHER_HEADLESS = "0"
python residential_fetcher.py
```

That opens a visible Chromium window using the persistent browser profile, so
cookies like `cf_clearance` survive after you solve the challenge once.

When Cloudflare throws an interactive challenge, the fetcher still searches all
frames for the "verify you are human" checkbox and clicks it first. If that
fails, headful mode lets you take over manually on the laptop.

| Env var                          | Default       | Purpose                                                  |
| -------------------------------- | ------------- | -------------------------------------------------------- |
| `RESIDENTIAL_FETCHER_PORT`       | `18080`       | Port to listen on                                        |
| `RESIDENTIAL_FETCHER_SECRET`     | _(disabled)_  | Shared secret matching the VPS (min 24 chars)            |
| `RESIDENTIAL_FETCHER_HEADLESS`   | `1`           | `1` = hidden browser, `0` = visible browser for manual solve |
| `CF_CHALLENGE_MAX_WAIT`          | `60`          | Seconds to wait for CF auto-solve before giving up       |

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
curl http://<tailscale-ip>:18080/health
# Should return: {"status":"ok"}
```
