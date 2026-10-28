"""Playwright-based screenshot capture with Adblock filter-list blocking.

Replaces the Selenium + hand-rolled content_blocker approach. Two blocking
layers run before the screenshot is taken:

1. **Network blocking** via ``page.route()`` — requests whose URLs match the
   Fanboy Cookie filter list are aborted before they load. Cookie consent
   scripts never execute, so banners never render. This is fundamentally
   better than hiding banners with CSS after-the-fact.

2. **Cosmetic hiding** via ``page.add_style_tag()`` — element-hiding rules
   (``##.cookie-banner`` etc.) injected as a stylesheet for first-party /
   inline banners that aren't loaded from a blockable domain.

A persistent browser singleton is reused across screenshots — each call gets
its own isolated context with its own route handler. This eliminates the 2-5s
Chrome process spawn overhead per screenshot.

Output contract matches the old Selenium path: saves a JPEG to
``static/images/{hash}_screenshot.jpg`` and returns the filename, or None.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from contextlib import suppress
from io import BytesIO

from PIL import Image

import filter_lists

logger = logging.getLogger(__name__)

IMAGE_DIR = "static/images"
MAX_STORED_IMAGE_WIDTH = int(os.environ.get("VHN_MAX_IMAGE_WIDTH", "800"))
JPEG_QUALITY = int(os.environ.get("VHN_JPEG_QUALITY", "72"))
SCREENSHOT_MIN_BYTES = 4 * 1024
MIN_IMAGE_WIDTH = 400
MIN_IMAGE_HEIGHT = 100
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900

# Wall-clock deadline for the whole per-page capture phase (goto + style
# injection + screenshot). A page whose JS never settles previously wedged a
# renderer at ~100% CPU for 3.5 days (2026-10-25 incident): page.goto's own
# timeout doesn't cover post-load JS, and add_style_tag/screenshot protocol
# calls can hang indefinitely waiting for a busy renderer.
SCREENSHOT_DEADLINE_S = int(os.environ.get("VHN_SCREENSHOT_DEADLINE_S", "60"))
# Hard cap on context.close() — close itself hangs on a wedged renderer.
_CLOSE_TIMEOUT_S = 5
# Recycle the browser singleton after this many screenshots or this much
# uptime, so leaked/wedged renderers can't accumulate for weeks.
BROWSER_MAX_SHOTS = int(os.environ.get("VHN_BROWSER_MAX_SHOTS", "50"))
BROWSER_MAX_AGE_S = int(os.environ.get("VHN_BROWSER_MAX_AGE_S", "3600"))

# Headless Chrome on Linux defaults to UA "HeadlessChrome/148" which is an
# instant bot tell. Use a regular Linux Chrome UA. The VPS runs Linux glibc, so
# Math.tanh returns glibc bits — the UA must claim Linux to stay consistent.
# See https://scrapfly.dev/posts/browser-math-os-fingerprint/
SCREENSHOT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# One screenshot at a time — Playwright contexts are lightweight but the
# screenshot path is a worst-case fallback, not the hot path.
screenshot_lock = asyncio.Lock()

_COSMETIC_CSS = filter_lists.get_cosmetic_css()

# --- Browser singleton ---
_browser: object | None = None  # playwright.async_api.Browser
_playwright: object | None = None  # playwright.async_api.Playwright
_browser_started_at: float = 0.0  # time.monotonic() at launch
_screenshots_since_launch = 0
_browser_lock = asyncio.Lock()


async def _get_browser():
    """Return the shared browser singleton, launching it if needed.

    The browser process persists across screenshot calls. Each screenshot
    creates its own context (isolated cookies, route handlers, viewport).
    Auto-relaunches if the process crashed, or proactively recycles the
    browser after ``BROWSER_MAX_SHOTS`` screenshots or ``BROWSER_MAX_AGE_S``
    uptime so a leaked/wedged renderer cannot survive for weeks.
    """
    global _browser, _playwright, _browser_started_at, _screenshots_since_launch
    if _browser and _browser.is_connected():
        age = time.monotonic() - _browser_started_at
        if _screenshots_since_launch >= BROWSER_MAX_SHOTS or age >= BROWSER_MAX_AGE_S:
            logger.info(
                "Recycling screenshot browser (age %ds, %d screenshots)",
                int(age),
                _screenshots_since_launch,
            )
            with suppress(Exception):
                await _browser.close()
            _browser = None
            with suppress(Exception):
                await _playwright.stop()
            _playwright = None
        else:
            return _browser
    async with _browser_lock:
        if _browser and _browser.is_connected():
            return _browser
        if _browser:
            logger.warning("Screenshot browser disconnected — relaunching")
            with suppress(Exception):
                await _browser.close()
            _browser = None
            with suppress(Exception):
                await _playwright.stop()
            _playwright = None

        from playwright.async_api import async_playwright

        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        _browser_started_at = time.monotonic()
        _screenshots_since_launch = 0
        logger.info("Screenshot browser singleton launched")
        return _browser


async def _close_context(context) -> None:
    """Close a capture context with a hard cap.

    ``context.close()`` is a protocol call that waits on the renderer; on a
    page whose JS never yields it can hang forever, leaking the renderer and
    holding the screenshot lock. Bound it.
    """
    if context is None:
        return
    with suppress(Exception):
        await asyncio.wait_for(context.close(), timeout=_CLOSE_TIMEOUT_S)


async def shutdown_browser():
    """Clean shutdown of the browser singleton. Call on app shutdown."""
    global _browser, _playwright
    if _browser:
        with suppress(Exception):
            await _browser.close()
    if _playwright:
        with suppress(Exception):
            await _playwright.stop()
    _browser = None
    _playwright = None


def is_image_too_small(image_filename: str) -> bool:
    """Return True when a saved image is missing or too small for a card."""
    image_path = os.path.join(IMAGE_DIR, image_filename)
    if not os.path.exists(image_path):
        return True
    try:
        with Image.open(image_path) as img:
            width, height = img.size
        return width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT
    except Exception as exc:
        logger.warning(
            "Could not open image file for size check %s: %s", image_filename, exc
        )
        return True


async def capture_screenshot(url: str) -> str | None:
    """Capture a screenshot of ``url`` using headless Chromium.

    Network requests matching the Fanboy Cookie filter list are aborted; a
    cosmetic stylesheet hides inline banners. The whole per-page phase is
    bounded by ``SCREENSHOT_DEADLINE_S`` and the context is always closed with
    a hard cap. Saves a resized JPEG and returns the filename, or None on
    failure.
    """
    from playwright.async_api import Error as PlaywrightError

    global _screenshots_since_launch

    async with screenshot_lock:
        image_filename: str | None = None
        context = None
        try:
            browser = await _get_browser()
            _screenshots_since_launch += 1
            context = await browser.new_context(
                user_agent=SCREENSHOT_UA,
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()

            # Layer 1: abort requests to cookie-consent / annoyance domains
            # + SSRF guard: block redirects to private/internal targets
            async def _block_route(route):
                req_url = route.request.url
                if filter_lists.is_blocked_url(req_url):
                    await route.abort()
                    return
                # SSRF: validate every navigation/request target
                from urllib.parse import urlparse

                parsed = urlparse(req_url)
                host = (parsed.hostname or "").lower()
                if (
                    host in {"localhost", "localhost.localdomain"}
                    or host.endswith(".local")
                    or host.startswith("127.")
                    or host.startswith("10.")
                    or host.startswith("192.168.")
                    or host.startswith("169.254.")
                ):
                    logger.warning("Screenshot route blocked SSRF target: %s", req_url)
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", _block_route)

            async def _capture_page():
                response = None
                try:
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=35_000
                    )
                except PlaywrightError as exc:
                    logger.warning("Page load issue for %s: %s", url, exc)

                if response is not None and not response.ok:
                    logger.warning(
                        "Skipping screenshot for %s — HTTP %d (non-2xx)",
                        url,
                        response.status,
                    )
                    return None

                # Layer 2: hide inline / first-party banners via CSS
                if _COSMETIC_CSS:
                    try:
                        await page.add_style_tag(content=_COSMETIC_CSS)
                    except PlaywrightError as exc:
                        logger.debug(
                            "Failed to inject cosmetic CSS for %s: %s", url, exc
                        )

                await asyncio.sleep(1.5)
                return await page.screenshot(type="png", full_page=False)

            try:
                screenshot_png = await asyncio.wait_for(
                    _capture_page(), timeout=SCREENSHOT_DEADLINE_S
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Screenshot deadline %ss exceeded for %s — abandoning page",
                    SCREENSHOT_DEADLINE_S,
                    url,
                )
                return None
            except PlaywrightError as exc:
                logger.error("Playwright screenshot error for %s: %s", url, exc)
                return None
            if screenshot_png is None:
                return None
        except Exception as exc:
            logger.error(
                "Unexpected screenshot error for %s: %s", url, exc, exc_info=True
            )
            return None
        finally:
            await _close_context(context)

        # --- Save + resize (same logic as the old Selenium path) ---
        image_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        image_filename = f"{image_hash}_screenshot.jpg"
        image_path = os.path.join(IMAGE_DIR, image_filename)
        os.makedirs(IMAGE_DIR, exist_ok=True)
        with BytesIO(screenshot_png) as screenshot_buffer:
            with Image.open(screenshot_buffer) as opened_image:
                image = opened_image.convert("RGB")
                if image.width > MAX_STORED_IMAGE_WIDTH:
                    ratio = MAX_STORED_IMAGE_WIDTH / float(image.width)
                    height = int(float(image.height) * ratio)
                    resized = image.resize(
                        (MAX_STORED_IMAGE_WIDTH, height), Image.Resampling.LANCZOS
                    )
                    image.close()
                    image = resized
                try:
                    image.save(image_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
                finally:
                    image.close()

        if os.path.getsize(image_path) < SCREENSHOT_MIN_BYTES or is_image_too_small(
            image_filename
        ):
            logger.warning(
                "Saved screenshot seems too small or invalid: %s", image_path
            )
            with suppress(OSError):
                os.remove(image_path)
            return None

        return image_filename
