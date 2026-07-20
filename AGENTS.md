# AGENTS.md

> For AI coding agents (ZCode, Hermes-Agent, Pi, OpenCode, KiloCode, Claude Code, Cursor, etc.)

## Project Overview

Visual-HN — HN w/ pics. FastAPI app that proxies hcker.news, adds preview images/Open Graph metadata, tracks position trends, and serves data for the hcker.news browser extension. The old frontend is being retired; the extension will consume the Visual-HN API for screen-capture assets, scores, and related story data.

## Two Environments

This project runs across **two machines**. Code runs in both places; commands are not portable.

|              | VPS (proxy + scraper)                                     | Residential node (Cloudflare bypass)                                                                  |
| ------------ | --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| **OS**       | Ubuntu 24.04 (Hetzner CX32)                               | Windows 11 (residential laptop)                                                                       |
| **Hostname** | *(see internal docs)*                                     | *(see internal docs)*                                                                                 |
| **Shell**    | bash                                                      | PowerShell 7                                                                                          |
| **Network**  | DC IP + Tailscale *(internal)*                            | Residential IP + Tailscale *(internal)*                                                               |
| **Runs**     | `main.py` (FastAPI proxy + scraper) as systemd service    | `residential_fetcher.py` (headful Chromium) via Task Scheduler                                        |
| **Service**  | `visual-hn.service` (`systemctl start/stop/restart`)    | `VHN-ResidentialFetcher` scheduled task                                                                |
| **Venv**     | `.venv` (Python 3.10+)                                    | `.node-venv` (Python 3.11+)                                                                           |
| **Role**     | Owns the DB, serves the public site, owns the scrape loop | Called by VPS only when curl_cffi gets 403/429/503 — solves CF JS challenges via real headful browser |

**Commands are not interchangeable.** A `systemctl restart` does nothing on Windows; `Start-ScheduledTask` does nothing on the VPS. When a command in this file looks wrong for the machine you're on, check which environment you're in before assuming the doc is stale.

Full deployment instructions for both environments: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). The residential node is intermittent by design — it's a laptop under daily use. When it's off, the VPS falls through to Wayback Machine → screenshot → favicon composite. No blocking, no alerting. See [`docs_internal/anti-scraping.md`](docs_internal/anti-scraping.md) for the full fallback chain.

## 1. Think Before Code

No assume. No hide confusion. Surface tradeoffs.

- State assumptions. Uncertain → ask.
- Multiple interpretations → present, no silent pick.
- Simpler path exist → say so. Push back when warranted.
- Unclear → stop. Name confusion. Ask.

## 2. Simplicity First

Min code that solve problem. Nothing speculative.

- No features beyond ask.
- No abstractions for single-use code.
- No "flexibility"/"configurability" not requested.
- No error handling for impossible cases.
- 200 lines could be 50 → rewrite.

Test: senior eng call this overcomplicated? Yes → simplify.

## 3. Surgical Changes

Touch only what must. Clean only own mess.

- No "improve" adjacent code/comments/format.
- No refactor things not broken.
- Match existing style even if disagree.
- Unrelated dead code → mention, no delete.
- Own changes orphan imports/vars → remove.
- Pre-existing dead code → leave unless asked.

Test: every changed line trace to user request.

## 4. Goal-Driven Execution

Define success. Loop until verified.

- "Add validation" → write failing tests, make pass.
- "Fix bug" → write reproducing test, make pass.
- "Refactor X" → tests pass before and after.

Multi-step → state plan: `[step] → verify: [check]`.

## 5. Testing / Committing

DO NOT run checks. ALWAYS ASK USER for explicit confirmation before running any verification, linting, type-check, or build commands.

DO NOT commit changes without explicit user confirmation. Before ending a task, ask whether to run checks and commit. If the user confirms committing, generate a suitable [Conventional Commits](https://www.conventionalcommits.org/) message that summarizes the diff concisely.

1. `black .` — Python formatting.
2. `pytest` — run all tests.

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

Follow [`docs/NODE_SETUP.md`](docs/NODE_SETUP.md). Summary:

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

**Entry point:** `main.py` — FastAPI app with a lifespan handler that initializes the DB and launches the background scraper as an `asyncio.create_task`.

**Data pipeline (runs every 15 minutes):**
1. `hn_scraper.py` — fetches top 30 story IDs from HN Firebase API, then fetches each story's details in parallel via `asyncio.gather`
2. `metadata.py` — for each story URL, fetches HTML and parses Open Graph tags (og:image, og:description); downloads/resizes images to max 640px JPEG; uses in-memory cache to avoid redundant fetches; has SSL-retry fallback
3. `database.py` — maps HN API fields to model fields (`by`→`poster`, `descendants`→`comments_count`, `time`→`time_posted`), computes position trends (lower position number = higher rank, so `last_position > current_position` = "up"), persists via async SQLAlchemy

**Web serving / extension API:** The main consumer is the `visual-hn-previews/` project, which calls the Visual-HN API for HN w/ pics. The old web frontend is being retired. The home route should stay minimal, while the legacy frontend lives behind a two-word hidden route. Scores still need to be exposed through the Visual-HN API for the extension.

**Database:** SQLite via aiosqlite + SQLAlchemy async. Schema defined in `models.py` (single `Story` table). Sessions use `async with async_session() as session:` pattern. No migrations — `create_all` on startup.

**Frontend:** `templates/index.html` with Tailwind CSS (config in `tailwind.config.js` scanning `./templates/**/*.html` and `./static/**/*.js`). Auto-refreshes every 15 minutes client-side.

## Repository Layout

- `main.py` — FastAPI app entrypoint and lifespan setup
- `hn_scraper.py` — fetches top stories from Hacker News
- `metadata.py` — Open Graph parsing, image download/resize, caching
- `database.py` — async persistence, story mapping, trend calculation
- `models.py` — SQLAlchemy ORM models
- `templates/index.html` — legacy page template while the frontend is being retired
- `static/` — favicon/assets, generated CSS, images, web manifest
- `visual-hn-previews/` — Chrome extension for hcker.news that consumes the Visual-HN API
- `test_*.py` — async pytest coverage for database and metadata behavior

## Template Layout

`templates/index.html` is a single full-page layout:

- root `<main>` wraps the page content with ambient background layers
- `<header>` contains the logo, title, short description, and the stats badge area
- `<section>` renders the story cards in a responsive 1/2-column grid
- each card contains image/fallback, rank/trend badges, title, metadata chips, and links
- `<footer>` only contains the GitHub link
- bottom scripts handle cookie consent and the 15-minute auto-refresh

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

## RTK Commands by Workflow

### Build & Compile (80-90% savings)

```bash
rtk cargo build         # Cargo build output
rtk cargo check         # Cargo check output
rtk cargo clippy        # Clippy warnings grouped by file (80%)
rtk tsc                 # TypeScript errors grouped by file/code (83%)
rtk lint                # ESLint/Biome violations grouped (84%)
rtk prettier --check    # Files needing format only (70%)
rtk next build          # Next.js build with route metrics (87%)
```

### Test (90-99% savings)

```bash
rtk cargo test          # Cargo test failures only (90%)
rtk vitest run          # Vitest failures only (99.5%)
rtk playwright test     # Playwright failures only (94%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)

```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Git passthrough works for ALL subcommands, including ones not listed.

### GitHub (26-87% savings)

```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh issue list       # Compact issue list (80%)
rtk gh api              # Compact API responses (26%)
```

### JavaScript/TypeScript Tooling (70-90% savings)

```bash
rtk pnpm list           # Compact dependency tree (70%)
rtk pnpm outdated       # Compact outdated packages (80%)
rtk pnpm install        # Compact install output (90%)
rtk npm run <script>    # Compact npm script output
rtk npx <cmd>           # Compact npx command output
rtk prisma              # Prisma without ASCII art (88%)
```

### Files & Search (60-75% savings)

```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%)
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)

```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)

```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)

```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

<!-- /rtk-instructions -->
