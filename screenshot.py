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

Output contract matches the old Selenium path: saves a JPEG to
``static/images/{hash}_screenshot.jpg`` and returns the filename, or None.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
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

# Headless Chrome on Linux defaults to UA "HeadlessChrome/148" which is an
# instant bot tell. Use a regular Linux Chrome UA. The VPS runs Linux glibc, so
# Math.tanh returns glibc bits — the UA must claim Linux to stay consistent.
# See https://scrapfly.dev/posts/browser-math-os-fingerprint/
SCREENSHOT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# One browser at a time — the screenshot path is a worst-case fallback, not
# the hot path, and Playwright browsers are heavyweight to spin up concurrently.
screenshot_lock = asyncio.Lock()

_COSMETIC_CSS = filter_lists.get_cosmetic_css()


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
    cosmetic stylesheet hides inline banners. Saves a resized JPEG and returns
    the filename, or None on failure.
    """
    from playwright.async_api import async_playwright, Error as PlaywrightError

    async with screenshot_lock:
        image_filename: str | None = None
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                    ]
                )
                context = await browser.new_context(
                    user_agent=SCREENSHOT_UA,
                    viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                )
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page = await context.new_page()

                # Layer 1: abort requests to cookie-consent / annoyance domains
                async def _block_route(route):
                    if filter_lists.is_blocked_url(route.request.url):
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", _block_route)

                response = None
                try:
                    # "domcontentloaded" mirrors the old Selenium "eager"
                    # strategy: don't wait for every stylesheet/image/ad
                    # request. Some pages stall on subresources long after
                    # the useful content is painted.
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=35_000
                    )
                except PlaywrightError as exc:
                    logger.warning("Page load issue for %s: %s", url, exc)
                    # Continue anyway — the page may have painted enough to
                    # screenshot even if navigation timed out.

                # Guard: only screenshot when we actually entered the story.
                # Non-2xx responses are forbidden/blank/error pages — capturing
                # them produces useless images. response is None on timeout
                # (page may still have content, so we let those through).
                if response is not None and not response.ok:
                    logger.warning(
                        "Skipping screenshot for %s — HTTP %d (non-2xx)",
                        url,
                        response.status,
                    )
                    await browser.close()
                    return None

                # Layer 2: hide inline / first-party banners via CSS
                if _COSMETIC_CSS:
                    try:
                        await page.add_style_tag(content=_COSMETIC_CSS)
                    except PlaywrightError as exc:
                        logger.debug(
                            "Failed to inject cosmetic CSS for %s: %s", url, exc
                        )

                # Brief settle for late-rendered overlays the CSS didn't catch
                await asyncio.sleep(1.5)

                screenshot_png = await page.screenshot(type="png", full_page=False)
                await browser.close()
        except PlaywrightError as exc:
            logger.error("Playwright screenshot error for %s: %s", url, exc)
            return None
        except Exception as exc:
            logger.error(
                "Unexpected screenshot error for %s: %s", url, exc, exc_info=True
            )
            return None

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
