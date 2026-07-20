# Rebrand Migration: YAHNC / hn-clone → Visual-HN / visual-hn

This runbook covers the **live infrastructure migration** that must accompany the code rename. The repo's code, docs, scripts, and tests already reference the new names; the boxes it runs on still use the old ones until you run the steps below.

Run order: **VPS first, then the residential node.** Either side will keep working on the old names until the new code is deployed, so there's no need to coordinate minute-by-minute — just don't `git pull` on a box without then running its migration steps.

## Renames at a glance

| Surface | Old | New |
|---|---|---|
| systemd unit | `hn-clone.service` | `visual-hn.service` |
| Repo dir (VPS) | `/srv/apps/hn-clone` | `/srv/apps/visual-hn` |
| SQLite DB file | `hn_clone.db` | `visual_hn.db` |
| Env vars (12) | `YAHNC_*` | `VHN_*` |
| Repo clone URL | `github.com/ilyaizen/hn-clone` | `github.com/ilyaizen/visual-hn` |
| NSSM service (alt path) | `YAHNC` | `Visual-HN` |
| Scheduled tasks | `YAHNC-ResidentialFetcher(-Watchdog)` | `VHN-ResidentialFetcher(-Watchdog)` |
| Repo dir (node) | `D:\GitHub\hn-clone` | `D:\GitHub\visual-hn` |
| Extension dir + asset URL path | `hcker-news-previews/` served at `/hcker-news-previews/*` | `visual-hn-previews/` served at `/visual-hn-previews/*` |

## VPS (Ubuntu, production)

### 1. Stop the old service

```bash
sudo systemctl stop hn-clone
```

### 2. Move the repo dir

```bash
sudo mv /srv/apps/hn-clone /srv/apps/visual-hn
cd /srv/apps/visual-hn
```

The `.venv`, `.git`, and on-disk DB come along. Symlinks elsewhere that point at `/srv/apps/hn-clone` will need repointing — none are known to exist in the repo, but check the systemd unit's `ExecStart`/`ExecStartPre` paths.

### 3. Move the DB file

```bash
mv /srv/apps/visual-hn/hn_clone.db /srv/apps/visual-hn/visual_hn.db
```

`database.py` now opens `visual_hn.db`; the old filename will be ignored and a fresh empty one created on next start if you skip this.

### 4. Rename + edit the systemd unit

```bash
sudo mv /etc/systemd/system/hn-clone.service /etc/systemd/system/visual-hn.service
sudo $EDITOR /etc/systemd/system/visual-hn.service
```

Inside the unit, update every reference:

- `WorkingDirectory=/srv/apps/hn-clone` → `/srv/apps/visual-hn`
- `ExecStart=` / `ExecStartPre=` paths → `/srv/apps/visual-hn/...`
- `Environment=YAHNC_RESIDENTIAL_FETCHER_URL=...` → `Environment=VHN_RESIDENTIAL_FETCHER_URL=...`
- `Environment=YAHNC_RESIDENTIAL_FETCHER_SECRET=...` → `Environment=VHN_RESIDENTIAL_FETCHER_SECRET=...`
- Any other `YAHNC_*` env var → `VHN_*` (full list in `metadata.py`, `hn_scraper.py`, `screenshot.py` — 12 vars total)

Then reload + start:

```bash
sudo systemctl daemon-reload
sudo systemctl start visual-hn
sudo systemctl status visual-hn       # confirm active
sudo journalctl -u visual-hn -f       # watch for "visual_hn.db" open + scraper cycle
```

### 5. Pull the new code (if not already)

```bash
cd /srv/apps/visual-hn
git pull
# If requirements changed:
# source .venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart visual-hn
```

### 6. Decommission the old unit name

```bash
# Sanity check: nothing should still reference hn-clone.service
sudo systemctl list-units --all | grep hn-clone   # expect no output
```

## Residential node (Windows 11, Cloudflare bypass)

### 1. Stop the fetcher

```powershell
Stop-ScheduledTask -TaskName 'YAHNC-ResidentialFetcher'
```

### 2. Move the repo dir

```powershell
# From the parent of your clone
Move-Item D:\GitHub\hn-clone D:\GitHub\visual-hn
cd D:\GitHub\visual-hn
```

`.node-venv` and `.git` come along. The venv's hard-coded absolute paths inside `.node-venv/Scripts/activate.ps1` may need a re-create if PowerShell activation fails after the move:

```powershell
Remove-Item -Recurse -Force .node-venv
python -m venv .node-venv
.\.node-venv\Scripts\Activate.ps1
pip install fastapi uvicorn playwright
python -m playwright install chromium
```

### 3. Pull the new code

```powershell
git pull
```

### 4. Unregister the old tasks, register the new ones

```powershell
# Remove the YAHNC-* tasks
.\scripts\register-task.ps1 -Uninstall    # run BEFORE editing the script? NO —
# the committed script now uninstalls VHN-* names. To remove the legacy tasks:
Unregister-ScheduledTask -TaskName 'YAHNC-ResidentialFetcher' -Confirm:$false
Unregister-ScheduledTask -TaskName 'YAHNC-ResidentialFetcher-Watchdog' -Confirm:$false

# Register VHN-* tasks
.\scripts\register-task.ps1
Start-ScheduledTask -TaskName 'VHN-ResidentialFetcher'

# Verify both tasks exist
Get-ScheduledTask -TaskName 'VHN-ResidentialFetcher*'

# Health
curl http://localhost:8765/health    # expect {"status":"ok"}
```

> **Gotcha:** `register-task.ps1 -Uninstall` in the new code unregisters `VHN-*` names, not `YAHNC-*`. Run the explicit `Unregister-ScheduledTask` lines above to clean up the legacy tasks first. After that, `-Uninstall` works as documented.

### 5. (Alt path only) NSSM service

If you ever installed the NSSM-based `YAHNC` service (the `install-service.ps1` path, separate from Task Scheduler):

```powershell
.\scripts\remove-service.ps1    # run the OLD version first to remove service "YAHNC"
# then, if you want the NSSM path back:
.\scripts\install-service.ps1   # installs service "Visual-HN"
```

## VPS ↔ node coordination

The two services communicate over Tailscale using a shared secret. The secret value itself does not change — only its env-var name (`YAHNC_RESIDENTIAL_FETCHER_SECRET` → `VHN_RESIDENTIAL_FETCHER_SECRET`). As long as both sides set the new var name to the same value, the handshake keeps working. There is no order dependency between the VPS and node migrations for this.

## Extension asset URL path

The Chrome extension's runtime assets (JS/CSS injected into the proxied page) moved from `/hcker-news-previews/*` to `/visual-hn-previews/*` when the extension directory was renamed. This is **repo-internal** — it only affects URLs the VPS itself generates and serves; clients don't configure it.

Because every injected `<script>`/`<link>` tag carries a cache-busting `?v={PREVIEW_RUNTIME_VERSION}` query string, the new URLs are fetched fresh on the first page load after deploy. There is no stale-cache risk and no client-side action. The Chrome Web Store extension is unaffected — it loads its own bundled content scripts, not these URLs (those URLs are for the in-page runtime injected by the proxy itself).

If you want clients to forget the old `/hcker-news-previews/*` paths entirely, you can leave them 404ing (harmless — they're never requested again once the proxy stops emitting them).

## Rollback

If the new code misbehaves and you need to revert fast:

```bash
# VPS
sudo systemctl stop visual-hn
cd /srv/apps/visual-hn && git reset --hard <last-old-sha>
sudo mv /etc/systemd/system/visual-hn.service /etc/systemd/system/hn-clone.service
sudo mv /srv/apps/visual-hn /srv/apps/hn-clone
sudo mv /srv/apps/hn-clone/visual_hn.db /srv/apps/hn-clone/hn_clone.db
# edit hn-clone.service: undo the path + env var renames
sudo systemctl daemon-reload && sudo systemctl start hn-clone
```

```powershell
# Node
Stop-ScheduledTask -TaskName 'VHN-ResidentialFetcher'
Unregister-ScheduledTask -TaskName 'VHN-ResidentialFetcher' -Confirm:$false
Unregister-ScheduledTask -TaskName 'VHN-ResidentialFetcher-Watchdog' -Confirm:$false
cd D:\GitHub\visual-hn ; git reset --hard <last-old-sha>
Move-Item D:\GitHub\visual-hn D:\GitHub\hn-clone
# re-register old tasks from the rolled-back register-task.ps1
.\scripts\register-task.ps1
```

## Post-migration checks

- [ ] `curl https://hn.is-ai-good-yet.com/` — proxied page loads, header reads **`visual HN`** (rainbow "visual" + static "HN")
- [ ] `curl https://hn.is-ai-good-yet.com/mossy-velvet` — legacy frontend still serves (template is intentionally frozen with old YAHNC branding, only the footer link repointed)
- [ ] `sudo journalctl -u visual-hn --since "5 min ago" | grep -i error` — no errors since start
- [ ] `ls /srv/apps/visual-hn/visual_hn.db` — DB present and growing (check mtime after a scrape cycle)
- [ ] `Get-ScheduledTask -TaskName 'VHN-ResidentialFetcher*'` on the node — both tasks Registered/Ready
- [ ] Trigger a CF-hardened URL fetch from the VPS and confirm the residential node handles it (log line on both sides)
