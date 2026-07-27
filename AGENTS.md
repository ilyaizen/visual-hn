# AGENTS.md

> For AI coding agents (ZCode, Hermes-Agent, Pi, OpenCode, KiloCode, Claude Code, Cursor, etc.)

## Project Overview

Visual-HN — HN w/ pics. FastAPI app that proxies hcker.news, adds preview images/Open Graph metadata, tracks position trends, and serves data for the hcker.news browser extension. The old frontend is being retired; the extension will consume the Visual-HN API for screen-capture assets, scores, and related story data.

## Two Environments

This project runs across **two machines**. Code runs in both places; commands are not portable.

|              | VPS (proxy + scraper)                                     | Residential node (Cloudflare bypass)                                                                                |
| ------------ | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **OS**       | Ubuntu 24.04 (Hetzner CX32)                               | Windows 11 (residential laptop)                                                                                     |
| **Hostname** | *(see internal docs)*                                     | *(see internal docs)*                                                                                               |
| **Shell**    | bash                                                      | PowerShell 7                                                                                                        |
| **Network**  | DC IP + Tailscale *(internal)*                            | Residential IP + Tailscale *(internal)*                                                                             |
| **Runs**     | `main.py` (FastAPI proxy + scraper) as systemd service    | `residential_fetcher.py` (headless Chrome via Playwright) via Task Scheduler                                        |
| **Service**  | `visual-hn.service` (`systemctl start/stop/restart`)      | `VHN-ResidentialFetcher` scheduled task                                                                             |
| **Venv**     | `.venv` (Python 3.10+)                                    | `.node-venv` (Python 3.11+)                                                                                         |
| **Role**     | Owns the DB, serves the public site, owns the scrape loop | Called by VPS only when curl_cffi gets 403/429/503 — solves CF JS challenges via real Chrome (Playwright, headless) |

**Commands are not interchangeable.** A `systemctl restart` does nothing on Windows; `Start-ScheduledTask` does nothing on the VPS. When a command in this file looks wrong for the machine you're on, check which environment you're in before assuming the doc is stale.

Full deployment instructions for both environments: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). The residential node is intermittent by design — it's a laptop under daily use. When it's off, the VPS falls through to Wayback Machine → screenshot → favicon composite. No blocking, no alerting. See [`docs_internal/anti-scraping.md`](docs_internal/anti-scraping.md) for the full fallback chain.

## Think Before Code

No assume. No hide confusion. Surface tradeoffs.

- State assumptions. Uncertain → ask.
- Multiple interpretations → present, no silent pick.
- Simpler path exist → say so. Push back when warranted.
- Unclear → stop. Name confusion. Ask.

## Simplicity First

Min code that solve problem. Nothing speculative.

- No features beyond ask.
- No abstractions for single-use code.
- No "flexibility"/"configurability" not requested.
- No error handling for impossible cases.
- 200 lines could be 50 → rewrite.

Test: senior eng call this overcomplicated? Yes → simplify.

## Goal-Driven Execution

Define success. Loop until verified.

- "Add validation" → write failing tests, make pass.
- "Fix bug" → write reproducing test, make pass.
- "Refactor X" → tests pass before and after.

Multi-step → state plan: `[step] → verify: [check]`.

## Branching, Checks, Commits

Before making any changes, create a branch from `main`:

```bash
git checkout main && git pull && git checkout -b <descriptive-name>
```

Never commit directly to `main`. Every task gets its own branch.

Run checks (`black .`, `pytest`) and commit as you go. Use [Conventional Commits](https://www.conventionalcommits.org/) messages.

1. `black .` — Python formatting.
2. `pytest` — run all tests.

## Push / publish / PR — ask first

**Always** get explicit user confirmation before:

- `git push` (any remote)
- Opening a PR (`gh pr create`)
- Publishing or deploying anything

Commit locally all you want. Ask before it leaves the machine.

## Efficiency

- Read before write. Each file once.
- Edit over rewrite. No write-delete-rewrite cycles.
- Test once, fix, verify once.
- Budget: 50 tool calls.
- Stuck → ask. No dead ends.
- No sycophantic openers/fluff.
- Never guess paths.

## Commands

> **Environment matters.** Commands below are tagged **[VPS]** (Ubuntu/bash, the production proxy) or **[NODE]** (Windows 11/PowerShell 7, the residential fetcher). Same repo, different machines. See `docs/DEPLOYMENT.md` for the full topology.

### Setup — VPS (Ubuntu, production)

```bash
cd /srv/apps/visual-hn
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # for screenshot fallback
```

### Setup — NODE (Windows 11, residential fetcher)

Follow [`docs/NODE_SETUP.md`](docs/NODE_SETUP.md). Uses Playwright with bundled Chromium (no system Chrome required). Summary:

```powershell
cd D:\GitHub\visual-hn
python -m venv .node-venv
.\.node-venv\Scripts\Activate.ps1
pip install fastapi uvicorn playwright
python -m playwright install chromium
```

### Run — VPS (development)

```bash
source .venv/bin/activate
uvicorn main:app --reload
```

### Run — VPS (production)

The systemd service owns this. Do not run uvicorn manually while the service is active.

```bash
sudo systemctl restart visual-hn    # after code changes
sudo systemctl status visual-hn
sudo journalctl -u visual-hn -f     # live logs
```

### Run — NODE (residential fetcher)

```powershell
.\scripts\start-fetcher.ps1                          # manual, foreground
# Or via Task Scheduler (auto-start on login):
.\scripts
egister-task.ps1
```

### CSS (rebuild Tailwind when modifying styles) [VPS]

```bash
pnpm install
pnpm exec tailwindcss -i ./static/css/input.css -o ./static/css/output.css --watch
# If pnpm unavailable:
npx tailwindcss -i ./static/css/input.css -o ./static/css/output.css --watch
```

### Tests [VPS]

```bash
source .venv/bin/activate
pytest                          # all tests
pytest test_database.py -v      # single file
```

Tests use `pytest-asyncio` with in-memory SQLite. Async test functions need `@pytest.mark.asyncio` and the `test_db` fixture for database access.

### Formatting [VPS]

```bash
source .venv/bin/activate
black .
```

> **`black` is not installed on the VPS** — run `pip install black` in the venv before relying on it.

## Architecture

The scrape pipeline (`hn_scraper.py` → `metadata.py` → `database.py`) runs every 15 minutes. Gotchas the code won't tell you:

- `database.py` renames HN API fields on the way in: `by`→`poster`, `descendants`→`comments_count`, `time`→`time_posted`.
- Position trends are inverted: a **lower** position number is a **higher** rank, so `last_position > current_position` means `"up"`.
- No migrations — `create_all` runs on startup, so schema changes to `models.py` need the DB recreated by hand.

**Web serving / extension API:** The main consumer is the `visual-hn-previews/` project, which calls the Visual-HN API for HN w/ pics. The old web frontend is being retired. The home route should stay minimal, while the legacy frontend lives behind a two-word hidden route. Scores still need to be exposed through the Visual-HN API for the extension.

## Code Style

- Python 3.10+, async throughout, type hints on function signatures
- Functional style preferred over classes (except ORM models)
- Use `async def` for I/O operations, `def` for pure functions
- Early returns for error handling, guard clauses over nested conditionals
- Use Python `logging` module, not print statements
- Pydantic for validation, SQLAlchemy ORM for persistence

<!-- rtk-instructions v2 -->
## RTK (Rust Token Killer) - Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has dedicated filter, it uses it. Else passthrough unchanged. RTK always safe. No `rtk bun`; see commands.

**Important**: Even in command chains with `&&`, use `rtk`:

```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

Full command reference (which tools have dedicated filters, and their savings): the `rtk-commands` skill in `.claude/skills/rtk-commands/`.
<!-- /rtk-instructions -->
