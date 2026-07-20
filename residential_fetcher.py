"""Headful Playwright fetcher for anti-bot circumvention.

Runs on the residential node (residential IP) behind Tailscale. The VPS calls
this when curl_cffi gets 403/429/503 — a real headful Chrome can solve Cloudflare
JS challenges that no HTTP client can.

Requirements (residential node):
    pip install fastapi uvicorn playwright
    python -m playwright install chromium

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
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

PORT = int(os.environ.get("RESIDENTIAL_FETCHER_PORT", "8765"))
SHARED_SECRET = os.environ.get("RESIDENTIAL_FETCHER_SECRET", "")
NAV_TIMEOUT_MS = 30_000
NETWORK_IDLE_MS = 24_000
CF_SETTLE_SECONDS = 3.0
CF_CHALLENGE_TITLE = "just a moment"
CF_CHALLENGE_MAX_WAIT = 40.0
MAX_HTML_CHARS = 2_000_000
# UA must match the OS the fetcher runs on (Windows 11). Since Chrome 148,
# Math.tanh reads the host libm, so Windows returns UCRT bits. Claiming a
# different OS in the UA while returning Windows math bits is an instant
# tell for any anti-bot that probes Math.tanh.
# See https://scrapfly.dev/posts/browser-math-os-fingerprint/
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# Single browser instance reused across requests. Each request gets a fresh
# incognito context so cookies/localStorage don't leak between fetches.
_browser: Any = None
_pw: Any = None
_lock = asyncio.Lock()
_sem = asyncio.Semaphore(1)  # one tab at a time — browser is heavyweight


class FetchRequest(BaseModel):
    url: str


class FetchResult(BaseModel):
    html: str | None = None
    final_url: str | None = None
    status: str = "ok"
    error: str | None = None


async def _ensure_browser() -> Any:
    """Launch Chromium once, reuse for all subsequent requests."""
    global _browser, _pw
    if _browser and _browser.is_connected():
        return _browser
    async with _lock:
        if _browser and _browser.is_connected():
            return _browser
        from playwright.async_api import async_playwright

        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
            ],
        )
        logger.info("Browser launched (headful)")
        return _browser


async def _fetch_with_browser(url: str) -> FetchResult:
    """Navigate to URL with headful Chrome, wait for CF challenges, return HTML."""
    browser = await _ensure_browser()
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )
    # Hide webdriver flag for extra stealth.
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        logger.info("DOM loaded for %s, waiting for network idle...", url)

        # Wait for networkidle — CF challenges make network requests while
        # resolving (token fetch, JS exec, redirect). networkidle fires when
        # no requests for 500ms, which reliably catches challenge completion.
        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
            logger.info("Network idle for %s", url)
        except Exception:
            logger.info("Network idle timeout for %s (may have continuous pings)", url)

        # Secondary check: if title still shows CF challenge, poll until resolved.
        cf_deadline = asyncio.get_event_loop().time() + CF_CHALLENGE_MAX_WAIT
        while asyncio.get_event_loop().time() < cf_deadline:
            title = (await page.title() or "").lower()
            if CF_CHALLENGE_TITLE not in title:
                break
            await asyncio.sleep(1.0)
        else:
            logger.warning(
                "CF challenge did not resolve in %.0fs for %s",
                CF_CHALLENGE_MAX_WAIT,
                url,
            )

        # Final settle for dynamic content (SPA hydration, lazy images, etc.)
        await asyncio.sleep(CF_SETTLE_SECONDS)

        html = await page.content()
        final_url = page.url
        if len(html) > MAX_HTML_CHARS:
            html = html[:MAX_HTML_CHARS]
        logger.info(
            "Returning %.0f KB for %s",
            len(html.encode("utf-8", errors="ignore")) / 1024,
            final_url,
        )
        return FetchResult(html=html, final_url=final_url)
    except Exception as exc:
        logger.warning("Fetch failed for %s: %s - %s", url, type(exc).__name__, exc)
        return FetchResult(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        await context.close()


def _verify_auth(secret: str | None) -> None:
    if SHARED_SECRET and secret != SHARED_SECRET:
        raise HTTPException(status_code=403, detail="unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Residential fetcher starting on 0.0.0.0:%d", PORT)
    yield
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()


app = FastAPI(title="Visual-HN Residential Fetcher", lifespan=lifespan)

# Reference Math.tanh values per OS (since Chrome 148, V8 calls host libm).
# Used by /health to self-verify that the browser's math bits match the UA's
# claimed OS. If these diverge, an anti-bot probe will catch the mismatch.
# Ref: https://scrapfly.dev/posts/browser-math-os-fingerprint/
_TANH_PROBES = [0.7, 0.8, 0.9]
_TANH_EXPECTED = {
    "linux": {
        0.7: 0.6043677771171636,
        0.8: 0.6640367702678491,
        0.9: 0.7162978701990245,
    },
    "macos": {
        0.7: 0.6043677771171635,
        0.8: 0.664036770267849,
        0.9: 0.7162978701990245,
    },
    "windows": {
        0.7: 0.6043677771171635,
        0.8: 0.6640367702678489,
        0.9: 0.7162978701990244,
    },
}


def _detect_os_from_ua(ua: str) -> str:
    """Infer the OS the UA claims, for math-fingerprint verification."""
    if "Windows" in ua:
        return "windows"
    if "Macintosh" in ua or "Mac OS" in ua:
        return "macos"
    if "Linux" in ua or "X11" in ua:
        return "linux"
    return "unknown"


@app.get("/health")
async def health():
    """Health check with Math.tanh fingerprint self-verification.

    Opens a browser tab, evaluates Math.tanh on probe inputs, and compares
    against reference values for the OS claimed in the UA. If the bits don't
    match, the fingerprint is inconsistent and anti-bot systems can detect it.
    """
    result: dict[str, Any] = {"status": "ok", "user_agent": USER_AGENT}
    claimed_os = _detect_os_from_ua(USER_AGENT)
    result["claimed_os"] = claimed_os

    if claimed_os == "unknown" or not _browser or not _browser.is_connected():
        result["fingerprint_check"] = "skipped"
        return result

    try:
        context = await _browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 800, "height": 600},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        try:
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=5_000)
            actual = await page.evaluate(
                f"() => {{ const probes = {_TANH_PROBES}; return probes.map(p => Math.tanh(p)); }}"
            )
            expected = _TANH_EXPECTED[claimed_os]
            mismatches = []
            for probe, val in zip(_TANH_PROBES, actual):
                exp = expected[probe]
                if val != exp:
                    mismatches.append(
                        f"tanh({probe}): got {val!r}, expected {exp!r} for {claimed_os}"
                    )
            if mismatches:
                result["fingerprint_check"] = "FAIL"
                result["mismatches"] = mismatches
                result["status"] = "fingerprint_mismatch"
                logger.warning("Math.tanh fingerprint mismatch: %s", mismatches)
            else:
                result["fingerprint_check"] = "pass"
                result["tanh_values"] = {
                    str(p): v for p, v in zip(_TANH_PROBES, actual)
                }
        finally:
            await context.close()
    except Exception as exc:
        result["fingerprint_check"] = f"error: {type(exc).__name__}: {exc}"
        logger.warning("Fingerprint check error: %s", exc)

    return result


@app.post("/fetch", response_model=FetchResult)
async def fetch(req: FetchRequest, x_fetcher_secret: str | None = Header(None)):
    _verify_auth(x_fetcher_secret)
    async with _sem:
        logger.info("Fetching: %s", req.url)
        return await _fetch_with_browser(req.url)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
