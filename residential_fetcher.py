"""Headless nodriver fetcher for anti-bot circumvention.

Runs on the residential node (residential IP) behind Tailscale. The VPS calls
this when curl_cffi gets 403/429/503 — a real Chrome via CDP can solve Cloudflare
JS challenges that no HTTP client can.

Uses nodriver (undetected-chromedriver successor) in headless mode:
- No visible window, no focus stealing, no Win32 hacks.
- Persistent browser profile (cookies including cf_clearance survive).
- Single tab navigates between URLs.
- nodriver's CDP-based approach auto-passes most CF/Turnstile challenges.
  When a managed challenge appears, the fetcher attempts to find and click
  the "verify you are human" checkbox programmatically (nodriver's find()
  searches iframes too). If it can't solve it in CF_CHALLENGE_MAX_WAIT,
  the fetch returns error and the VPS falls through to the fallback chain.

Requirements (residential node):
    pip install fastapi uvicorn nodriver
    # System Chrome must be installed (nodriver uses the real binary, not a
    # bundled Chromium like Playwright did).

Run:
    python residential_fetcher.py
    # or with custom port:
    RESIDENTIAL_FETCHER_PORT=8765 python residential_fetcher.py

The server listens on 0.0.0.0 so it's reachable via the Tailscale IP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import nodriver as uc
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("RESIDENTIAL_FETCHER_PORT", "8765"))
SHARED_SECRET = os.environ.get("RESIDENTIAL_FETCHER_SECRET", "")
CF_SETTLE_SECONDS = 3.0
CF_CHALLENGE_TITLE = "just a moment"
# In headless mode there is no window for a human to solve interactive
# challenges. This timeout is long enough for nodriver's auto-solve + CF's
# JS to execute. If it doesn't resolve, the VPS falls through to Wayback →
# screenshot → favicon composite.
CF_CHALLENGE_MAX_WAIT = float(os.environ.get("CF_CHALLENGE_MAX_WAIT", "60"))
MAX_HTML_CHARS = 2_000_000
PROFILE_DIR = Path(
    os.environ.get(
        "RESIDENTIAL_FETCHER_PROFILE",
        str(Path(__file__).parent / ".browser-profile"),
    )
)

# nodriver drives the real system Chrome, so the User-Agent and Math.tanh
# fingerprint automatically match the host OS. No hardcoded UA needed — this
# was a manual maintenance burden in the Playwright version and a mismatch
# risk (claiming Chrome 148 while the bundled Chromium was a different build).

# Persistent browser: one instance, one profile, reused for every request.
_browser: Any = None  # nodriver.Browser
_lock = asyncio.Lock()
_sem = asyncio.Semaphore(1)  # one fetch at a time — single tab

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
    """True if the browser process is running and the CDP socket is connected."""
    if not _browser:
        return False
    proc = getattr(_browser, "_process", None)
    if not proc or proc.returncode is not None:
        return False
    return getattr(_browser, "socket", None) is not None


async def _teardown_browser() -> None:
    """Tear down dead browser state so a fresh launch can proceed."""
    global _browser
    if _browser:
        try:
            await _browser.aclose()
        except Exception:
            pass
    proc = getattr(_browser, "_process", None) if _browser else None
    if proc and proc.returncode is None:
        with suppress(Exception):
            proc.kill()
    _browser = None


async def _ensure_browser() -> Any:
    """Launch the browser once. Returns the nodriver.Browser.

    The same browser and profile are reused for every subsequent request.
    Cookies, localStorage, and cf_clearance persist in PROFILE_DIR across
    restarts. Auto-relaunches if the browser process has crashed.
    """
    global _browser
    if _browser_is_alive():
        return _browser
    async with _lock:
        if _browser_is_alive():
            return _browser
        if _browser:
            logger.warning("Browser process died — relaunching")
            await _teardown_browser()

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _browser = await uc.start(
            headless=True,
            user_data_dir=str(PROFILE_DIR),
            lang="en-US",
            browser_args=["--no-first-run", "--headless=new"],
        )
        logger.info("Browser launched (headless nodriver, profile=%s)", PROFILE_DIR)
        return _browser


async def _get_title(tab: Any) -> str:
    """Get the page title (lowercased) via JS evaluation."""
    try:
        result = await tab.evaluate("document.title", return_by_value=True)
        return result.lower() if isinstance(result, str) else ""
    except Exception:
        return ""


async def _try_solve_cf_challenge(tab: Any) -> bool:
    """Attempt to find and click a CF/Turnstile 'verify' checkbox.

    nodriver's find() searches iframes too, so it can locate the Turnstile
    widget inside its iframe. Returns True if a checkbox was clicked.
    """
    try:
        checkbox = await asyncio.wait_for(
            tab.find("verify you are human", best_match=True, timeout=5),
            timeout=8,
        )
        if checkbox:
            await checkbox.click()
            logger.info("CF challenge checkbox clicked")
            await tab.wait(3)
            return True
    except Exception as exc:
        logger.debug("CF checkbox search: %s", exc)
    return False


def _record(status: str, **fields: Any) -> None:
    _last_fetch.update(status=status, at=time.time(), **fields)


async def _fetch_with_browser(url: str) -> FetchResult:
    """Navigate to URL, wait for CF challenges, return HTML."""
    browser = await _ensure_browser()
    _record("navigating", url=url, error=None)
    try:
        tab = await browser.get(url)
        await tab.wait(2)

        # CF challenge detection + auto-solve loop. nodriver auto-passes most
        # managed challenges, but interactive Turnstile widgets sometimes need
        # a click. We poll the page title — "just a moment" = CF interstitial.
        cf_deadline = asyncio.get_event_loop().time() + CF_CHALLENGE_MAX_WAIT
        challenged = False
        while asyncio.get_event_loop().time() < cf_deadline:
            title = await _get_title(tab)
            if CF_CHALLENGE_TITLE not in title:
                break
            challenged = True
            await _try_solve_cf_challenge(tab)
            await asyncio.sleep(3)

        if challenged:
            final_title = await _get_title(tab)
            if CF_CHALLENGE_TITLE in final_title:
                logger.warning(
                    "CF challenge did not resolve in %.0fs for %s",
                    CF_CHALLENGE_MAX_WAIT,
                    url,
                )
            else:
                logger.info("CF challenge solved for %s", url)

        # Final settle for dynamic content (SPA hydration, lazy images).
        await asyncio.sleep(CF_SETTLE_SECONDS)

        html = await tab.get_content()
        try:
            final_url = await tab.evaluate("window.location.href", return_by_value=True)
        except Exception:
            final_url = url

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


MIN_SECRET_LENGTH = 24


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
    logger.info("Residential fetcher starting on 0.0.0.0:%d", PORT)
    logger.info("Profile dir: %s", PROFILE_DIR)
    logger.info(
        "CF challenge max wait: %.0fs (headless auto-solve)", CF_CHALLENGE_MAX_WAIT
    )
    yield
    if _browser:
        with suppress(Exception):
            await _browser.aclose()


app = FastAPI(title="Visual-HN Residential Fetcher", lifespan=lifespan)


@app.get("/health")
async def health():
    """Health check with browser connection state and last-fetch metrics."""
    browser_connected = _browser_is_alive()
    return {
        "status": "ok" if browser_connected else "degraded",
        "browser_connected": browser_connected,
        "profile_dir": str(PROFILE_DIR),
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
