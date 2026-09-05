"""Playwright fetcher for anti-bot circumvention.

Runs on the residential node (residential IP) behind Tailscale. The VPS calls
this when curl_cffi gets 403/429/503 — a real Chrome can solve Cloudflare
JS challenges that no HTTP client can.

Uses Playwright with a persistent browser context:
- Headless by default; set RESIDENTIAL_FETCHER_HEADLESS=0 for a visible window.
- Persistent browser profile (cookies including cf_clearance survive).
- New context per request for isolation, shared browser process for efficiency.
- When a managed CF/Turnstile challenge appears, the fetcher searches all
  frames for the "verify you are human" checkbox and clicks it. If it can't
  solve it in CF_CHALLENGE_MAX_WAIT, the fetch returns error and the VPS
  falls through to the fallback chain.

Requirements (residential node):
    pip install fastapi uvicorn playwright
    python -m playwright install chromium

Run:
    python residential_fetcher.py
    # or with custom port:
    RESIDENTIAL_FETCHER_PORT=18080 python residential_fetcher.py

The server listens on 0.0.0.0 so it's reachable via the Tailscale IP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)

# Mirror logs to a rotating file. When launched hidden by Task Scheduler the
# console handler is invisible, so browser pre-warm/relaunch failures (the
# root cause of zombie driver processes) left no trace on disk. fetcher.log
# is gitignored; rotation keeps it bounded.
_file_handler = RotatingFileHandler(
    Path(__file__).parent / "fetcher.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=2,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)

PORT = int(os.environ.get("RESIDENTIAL_FETCHER_PORT", "18080"))
SHARED_SECRET = os.environ.get("RESIDENTIAL_FETCHER_SECRET", "")
HEADLESS = os.environ.get("RESIDENTIAL_FETCHER_HEADLESS", "1").lower() not in {
    "0",
    "false",
    "no",
}
BROWSER_MODE = "headless" if HEADLESS else "headful"
MIN_SECRET_LENGTH = 24
CF_SETTLE_SECONDS = 3.0
CF_CHALLENGE_TITLES = ("just a moment", "attention required")
# Timeout for CF auto-solve + JS execution. If it doesn't resolve, the VPS
# falls through to Wayback → screenshot → favicon composite.
CF_CHALLENGE_MAX_WAIT = float(os.environ.get("CF_CHALLENGE_MAX_WAIT", "60"))
MAX_HTML_CHARS = 2_000_000
PROFILE_DIR = Path(
    os.environ.get(
        "RESIDENTIAL_FETCHER_PROFILE",
        str(Path(__file__).parent / ".browser-profile"),
    )
)

# Persistent browser: one Playwright instance, one persistent context.
# cf_clearance and other cookies persist in PROFILE_DIR across restarts.
_playwright: Any = None
_browser: Any = None  # playwright.async_api.BrowserContext (persistent)
_lock = asyncio.Lock()
_sem = asyncio.Semaphore(1)  # one fetch at a time

# Lightweight metrics for /health and the admin dashboard.
_last_fetch: dict[str, Any] = {
    "url": None,
    "status": "idle",
    "at": None,
    "final_url": None,
    "bytes": 0,
    "error": None,
}


class FetchRequest(BaseModel):
    url: str


class FetchResult(BaseModel):
    html: str | None = None
    final_url: str | None = None
    status: str = "ok"
    error: str | None = None


def _browser_is_alive() -> bool:
    """True if the browser process is running and connected."""
    if not _browser:
        return False
    try:
        # _browser is a BrowserContext (from launch_persistent_context).
        # Use the public .browser property to reach the underlying Browser
        # and check its connection state. The old code accessed a private
        # _browser attribute that doesn't exist on BrowserContext, so this
        # always raised AttributeError → returned False → health reported
        # "degraded" forever and _ensure_browser tore down + relaunched the
        # browser on every single /fetch call.
        browser = _browser.browser
        return browser is not None and browser.is_connected()
    except Exception:
        return False


async def _teardown_browser() -> None:
    """Tear down dead browser state so a fresh launch can proceed."""
    global _browser, _playwright
    if _browser:
        with suppress(Exception):
            await _browser.close()
    if _playwright:
        with suppress(Exception):
            await _playwright.stop()
    _browser = None
    _playwright = None


async def _ensure_browser() -> Any:
    """Launch the browser once. Returns the persistent BrowserContext.

    Uses launch_persistent_context so cf_clearance and other cookies survive
    in PROFILE_DIR across restarts, matching the old nodriver behavior.
    Auto-relaunches if the browser crashed.
    """
    global _browser, _playwright
    if _browser_is_alive():
        return _browser
    async with _lock:
        if _browser_is_alive():
            return _browser
        if _browser or _playwright:
            # Teardown must run when EITHER is stale: a failed launch leaves
            # _browser=None but _playwright set, and the running Playwright
            # instance still holds a driver node process. Skipping teardown
            # here leaks one driver node (~30 MB) per failed relaunch.
            logger.warning("Browser process died — relaunching")
            await _teardown_browser()

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            args=[
                "--no-first-run",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        logger.info(
            "Browser launched (%s Playwright, profile=%s)", BROWSER_MODE, PROFILE_DIR
        )
        return _browser


async def _get_title(page: Any) -> str:
    """Get the page title (lowercased)."""
    try:
        title = await page.title()
        return title.lower() if isinstance(title, str) else ""
    except Exception:
        return ""


def _is_cf_challenge(title: str) -> bool:
    return any(marker in title for marker in CF_CHALLENGE_TITLES)


async def _try_solve_cf_challenge(page: Any) -> bool:
    """Attempt to find and click a CF/Turnstile 'verify' checkbox.

    Searches all frames (including cross-origin iframes like the Turnstile
    widget from challenges.cloudflare.com). Returns True if a checkbox was
    clicked.
    """
    for frame in page.frames:
        try:
            # Turnstile renders a checkbox input or a clickable div
            checkbox = await asyncio.wait_for(
                frame.wait_for_selector(
                    "input[type='checkbox'], div[role='checkbox']",
                    timeout=3000,
                ),
                timeout=5,
            )
            if checkbox:
                await checkbox.click()
                logger.info("CF challenge checkbox clicked in frame %s", frame.url)
                await asyncio.sleep(3)
                return True
        except asyncio.TimeoutError:
            continue
        except Exception as exc:
            logger.debug("CF checkbox search in frame %s: %s", frame.url, exc)
            continue
    return False


def _record(status: str, **fields: Any) -> None:
    _last_fetch.update(status=status, at=time.time(), **fields)


async def _fetch_with_browser(url: str) -> FetchResult:
    """Navigate to URL, wait for CF challenges, return HTML.

    Uses the persistent browser context so cf_clearance cookies survive
    across requests. Validates the final URL against SSRF targets.
    """
    browser = await _ensure_browser()
    _record("navigating", url=url, error=None)

    page: Any = None
    try:
        page = await browser.new_page()

        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)

        # CF challenge detection + auto-solve loop.
        cf_deadline = asyncio.get_event_loop().time() + CF_CHALLENGE_MAX_WAIT
        challenged = False
        while asyncio.get_event_loop().time() < cf_deadline:
            title = await _get_title(page)
            if not _is_cf_challenge(title):
                break
            challenged = True
            await _try_solve_cf_challenge(page)
            await asyncio.sleep(3)

        if challenged:
            final_title = await _get_title(page)
            if _is_cf_challenge(final_title):
                logger.warning(
                    "CF challenge did not resolve in %.0fs for %s",
                    CF_CHALLENGE_MAX_WAIT,
                    url,
                )
            else:
                logger.info("CF challenge solved for %s", url)

        await asyncio.sleep(CF_SETTLE_SECONDS)

        final_url = page.url
        html = await page.content()

        # SSRF guard: reject if the browser was redirected to an unsafe target.
        from urllib.parse import urlparse

        parsed = urlparse(final_url)
        host = (parsed.hostname or "").lower()
        if (
            host in {"localhost", "localhost.localdomain"}
            or host.endswith(".local")
            or host.startswith("127.")
            or host.startswith("10.")
            or host.startswith("192.168.")
            or host.startswith("169.254.")
        ):
            logger.warning(
                "Residential fetch redirected to unsafe URL %s — rejecting", final_url
            )
            _record("error", url=url, error=f"SSRF blocked: {final_url}")
            return FetchResult(status="error", error=f"SSRF blocked: {final_url}")

        if len(html) > MAX_HTML_CHARS:
            html = html[:MAX_HTML_CHARS]
        nbytes = len(html.encode("utf-8", errors="ignore"))
        logger.info("Returning %.0f KB for %s", nbytes / 1024, final_url)
        _record("ok", url=url, final_url=final_url, bytes=nbytes)
        return FetchResult(html=html, final_url=final_url)
    except Exception as exc:
        logger.warning("Fetch failed for %s: %s - %s", url, type(exc).__name__, exc)
        _record("error", url=url, error=f"{type(exc).__name__}: {exc}")
        return FetchResult(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if page:
            with suppress(Exception):
                await page.close()


def _verify_auth(secret: str | None) -> None:
    if not SHARED_SECRET:
        return
    if secret is None or not secrets.compare_digest(secret, SHARED_SECRET):
        raise HTTPException(status_code=403, detail="unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if SHARED_SECRET and len(SHARED_SECRET) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"RESIDENTIAL_FETCHER_SECRET must be at least {MIN_SECRET_LENGTH} chars"
        )
    logger.info("Residential fetcher starting on 0.0.0.0:%d (%s)", PORT, BROWSER_MODE)
    logger.info(
        "CF challenge max wait: %.0fs (%s mode)",
        CF_CHALLENGE_MAX_WAIT,
        BROWSER_MODE,
    )
    # Pre-warm the browser so health reports "ok" immediately after startup,
    # rather than "degraded" until the first /fetch request arrives (~15 min).
    try:
        await _ensure_browser()
        logger.info("Browser pre-warmed at startup")
    except Exception as exc:
        logger.warning("Browser pre-warm failed: %s — will retry on first /fetch", exc)
    yield
    await _teardown_browser()


app = FastAPI(title="Visual-HN Residential Fetcher", lifespan=lifespan)


@app.get("/health")
async def health():
    """Health check with browser connection state and last-fetch metrics."""
    browser_connected = _browser_is_alive()
    return {
        "status": "ok" if browser_connected else "degraded",
        "browser_connected": browser_connected,
        "browser_mode": BROWSER_MODE,
        "port": PORT,
        "last_fetch": dict(_last_fetch),
        "cf_challenge_max_wait": CF_CHALLENGE_MAX_WAIT,
    }


@app.post("/fetch", response_model=FetchResult)
async def fetch(req: FetchRequest, x_fetcher_secret: str | None = Header(None)):
    _verify_auth(x_fetcher_secret)
    async with _sem:
        logger.info("Fetching: %s", req.url)
        return await _fetch_with_browser(req.url)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
