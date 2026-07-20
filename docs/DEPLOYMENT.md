# Deployment — Two Environments

Visual-HN runs across two machines. This doc covers how each is set up, how code reaches each, and how services are controlled. Infrastructure details (IPs, hostnames, topology) are in internal docs — not committed to the public repo.

|                     | VPS                                      | Residential node                                 |
| ------------------- | ---------------------------------------- | ------------------------------------------------ |
| **OS**              | Ubuntu 24.04 LTS (Hetzner CX32)          | Windows 11 (residential laptop)                  |
| **Shell**           | bash                                     | PowerShell 7                                     |
| **Runs**            | `main.py` — FastAPI proxy + scraper + DB | `residential_fetcher.py` — headful Chromium      |
| **Service manager** | systemd (`visual-hn.service`)            | Windows Task Scheduler (`VHN-ResidentialFetcher`) |
| **Venv**            | `.venv/`                                 | `.node-venv/`                                    |

Both machines pull from the same `git` repo (`github.com/ilyaizen/visual-hn`). Code changes deploy by `git pull` + service restart on each machine. There is no build step.

---

## VPS (Ubuntu) — proxy + scraper

### First-time setup

```bash
cd /srv/apps/visual-hn
git clone https://github.com/ilyaizen/visual-hn.git .
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # screenshot fallback (Layer 3)
```

### systemd unit

The unit lives at `/etc/systemd/system/visual-hn.service` and is **not committed** (it carries the single-writer constraint + env vars specific to this box). The repo's `AGENTS.md` documents the invariants; the skill reference has the exact env var list.

Key settings that must survive any unit edits:

- `ExecStart=... uvicorn main:app --workers 1` — **single-writer SQLite**. Multiple workers corrupt trend arrows (see AGENTS.md → Architecture). Do not bump to 2+.
- `ExecStartPre=... playwright install chromium` — self-heals the headless Chromium binary on every restart. Without this, screenshots die silently after a Playwright pip upgrade or disk cleanup.
- `Environment=VHN_RESIDENTIAL_FETCHER_URL=http://<tailscale-ip>:<port>` — enables Layer 2. Comment out to disable.
- `Environment=VHN_RESIDENTIAL_FETCHER_SECRET=<shared-secret>` — shared secret with the node.

### Routine operations

```bash
# Restart after code changes
cd /srv/apps/visual-hn && git pull
sudo systemctl restart visual-hn

# Check health
sudo systemctl status visual-hn
sudo journalctl -u visual-hn -f          # live logs
curl -s http://localhost:8091/api/health  # if health endpoint exists

# Verify the residential node is reachable from VPS
curl -s --connect-timeout 5 http://<tailscale-ip>:<port>/health
# Expected: {"status":"ok"}
# If empty/timeout → the node is off (graceful degradation; see below)
```

### Cloudflare → VPS routing

The public URL is routed through Cloudflare to the VPS. The service binds to the Tailscale IP, not `0.0.0.0`. If the public site goes down but the service is active, check Cloudflare's origin health and DNS before touching the service.

---

## Residential node (Windows 11) — Cloudflare bypass

### What it does

When the VPS's curl_cffi fetch gets 403/429/503 from a story URL (Cloudflare/Edge-hardened sites blocking the Hetzner DC IP), the VPS calls the residential node over Tailscale:

```
VPS  →  POST http://<tailscale-ip>:<port>/fetch {"url": "..."}
NODE →  launches a fresh incognito context in a persistent headful Chromium
NODE →  navigates to URL, waits 3s for CF JS challenges to auto-solve
NODE →  returns the fully-rendered HTML
VPS  →  parses og:image from the returned HTML
```

A real headful browser on a residential IP passes challenges no HTTP client can — this is the entire reason the node exists. See [`docs_internal/anti-scraping.md`](../docs_internal/anti-scraping.md) *(internal; git-ignored)* for the full fallback chain.

### First-time setup

Follow [`docs/NODE_SETUP.md`](NODE_SETUP.md) — it has the step-by-step PowerShell instructions. Summary:

```powershell
cd <your-clone-path>
git clone https://github.com/ilyaizen/visual-hn.git .
python -m venv .node-venv
.\.node-venv\Scripts\Activate.ps1
pip install fastapi uvicorn playwright
python -m playwright install chromium
```

### Running manually (foreground)

```powershell
.\scripts\start-fetcher.ps1
```

A Chromium window opens — that's normal. It's the headful browser solving CF challenges. Close the PowerShell window or the Chromium window to stop it.

### Auto-start on login (Task Scheduler)

```powershell
.\scripts\register-task.ps1            # register
.\scripts\register-task.ps1 -Uninstall # remove
```

This registers `VHN-ResidentialFetcher` — a scheduled task that fires `AtLogOn` and runs `start-fetcher.ps1` hidden in the background. Manual control:

```powershell
Start-ScheduledTask -TaskName 'VHN-ResidentialFetcher'
Stop-ScheduledTask  -TaskName 'VHN-ResidentialFetcher'
Get-ScheduledTask   -TaskName 'VHN-ResidentialFetcher' | Get-ScheduledTaskInfo
```

### ⚠️ Reliability — process death mid-session (solved by watchdog)

**The structural problem:** The fetcher task's `AtLogOn` trigger with `RestartCount 3` only covers *failed starts within the first 3 minutes*. If the Chromium process is killed later — Windows cleaners (CCleaner, BleachBit, Windows Disk Cleanup), laptop sleep/hibernate, a user closing the hidden window, an OOM kill — the scheduled task stays in "Running" state (from Scheduler's view) even though the actual process is dead, so it won't even fire on the next login until you explicitly stop + start it. This is the cause of "it ran a few times and then stopped."

**The fix (implemented):** `scripts/watch-fetcher.ps1` + a second scheduled task `VHN-ResidentialFetcher-Watchdog` that runs every 5 minutes. The watchdog curls `localhost:<port>/health`; if it fails 3 consecutive checks (with 10s spacing inside the script), it stops and restarts the fetcher task. Silent on the happy path — writes output only when it takes action. Registered alongside the fetcher task by `register-task.ps1`.

To verify the watchdog is registered and healthy on the node:

```powershell
# Both tasks should be listed
Get-ScheduledTask -TaskName 'VHN-ResidentialFetcher*'

# Watchdog last run result (0 = success, 267011 = task hasn't run yet)
Get-ScheduledTask -TaskName 'VHN-ResidentialFetcher-Watchdog' | Get-ScheduledTaskInfo

# Watchdog history (look for action output if the fetcher was restarted)
Get-WinEvent -ProviderName Microsoft-Windows-TaskScheduler -MaxEvents 20 |
  Where-Object { $_.Message -match 'VHN-ResidentialFetcher-Watchdog' }
```

**Manual recovery (if the watchdog itself is broken):**

```powershell
Stop-ScheduledTask -TaskName 'VHN-ResidentialFetcher'
Start-ScheduledTask -TaskName 'VHN-ResidentialFetcher'
curl http://localhost:<port>/health   # verify
```

**Design notes:**
- The watchdog runs as a separate task, not a loop inside the fetcher — if the fetcher process dies, the watchdog isn't affected.
- `MultipleInstances=IgnoreNew` + 2-minute `ExecutionTimeLimit` prevents watchdog pile-up.
- `RepetitionDuration=[TimeSpan]::MaxValue` keeps it running indefinitely after first registration (no expiry).
- 3 consecutive failures with 10s spacing (30s total) avoids restarting on a transient network blip or a single slow health response.
- The watchdog checks `localhost`, not the Tailscale IP — so it validates the server is actually serving, not just that the port is reachable from outside.

### Verification from VPS

```bash
# Health
curl -s --connect-timeout 5 http://<tailscale-ip>:<port>/health
# Expected: {"status":"ok"}

# Manual fetch test (end-to-end through the real chain)
curl -s -X POST http://<tailscale-ip>:<port>/fetch \
  -H "Content-Type: application/json" \
  -H "X-Fetcher-Secret: <shared-secret>" \
  -d '{"url": "https://www.bloomberg.com"}' | head -c 200

# Check what the VPS is actually seeing from the node
sudo journalctl -u visual-hn --since "1 hour ago" | grep -iE "residential|fetcher"
```

### When the node is off

Expected and graceful. `_residential_fetch_html()` in `metadata.py` catches `ClientConnectorError` and `TimeoutError`, logs at INFO level, returns `(None, None)`. The fetch falls through to Layer 2.5 (Wayback Machine) → Layer 3 (screenshot) → favicon composite. No alerting fires. The only signal is the log line:

```
residential fetcher timed out for <url> (node may be off)
```

If you see this for every blocked URL across multiple scrape cycles, the node is down — check Tailscale connectivity and the Task Scheduler status on the laptop.

---

## Coordinating changes across both machines

Code changes that touch `residential_fetcher.py` or its dependencies need to deploy to both machines:

```bash
# VPS
cd /srv/apps/visual-hn && git pull && sudo systemctl restart visual-hn

# Node (PowerShell)
cd <your-clone-path> ; git pull
# If requirements changed: .\.node-venv\Scripts\Activate.ps1 ; pip install -r requirements.txt
Stop-ScheduledTask -TaskName 'VHN-ResidentialFetcher'
Start-ScheduledTask -TaskName 'VHN-ResidentialFetcher'
```

Changes to `main.py`, `metadata.py`, `hcker_proxy.py`, the extension, or templates only need the VPS restart.

---

## Quick triage: "previews are missing on the front page"

1. **Check VPS service first:** `sudo systemctl status visual-hn`. If dead → `sudo systemctl restart visual-hn`.
2. **Check `journalctl` for TypeError** — a `retries` key leak in `Story(**metadata)` crashes the entire scraper loop and produces zero previews that cycle (looks like a scraping failure, is actually a DB write crash). See AGENTS.md pitfalls.
3. **Check if Chromium binary exists** on the VPS: `ls /root/.cache/ms-playwright/`. Empty → screenshots are silently failing. `sudo systemctl restart visual-hn` triggers `ExecStartPre` which reinstalls it.
4. **Check residential node health** (only relevant if the missing previews are from CF-hardened sites): `curl http://<tailscale-ip>:<port>/health`. Dead → follow the "Known reliability gap" recovery steps above.
5. **Check Wayback fallback** in logs — if Wayback is catching everything the node would have caught, Layer 2 being down is a quality regression, not an outage. Lower priority.
