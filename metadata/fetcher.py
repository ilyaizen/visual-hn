"""HTML fetching — curl_cffi, residential fallback, Wayback Machine, screenshots.

Imports from safety: is_public_http_url.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress
from ssl import SSLError
from typing import Any

import aiohttp
from curl_cffi.requests import AsyncSession as CurlCffiSession

import screenshot as screenshot_module

from .safety import is_public_http_url

logger = logging.getLogger(__name__)

# Must match curl_cffi's impersonate='chrome' default UA (Chrome 146 macOS).
# Overriding to a different OS/version creates contradictions with the TLS
# fingerprint and Sec-Ch-Ua-Platform headers that curl_cffi sends automatically.
# See https://scrapfly.dev/posts/browser-math-os-fingerprint/ for why
# UA/OS consistency matters down to the Math.tanh bits.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
ENABLE_SCREENSHOT_FALLBACK = os.environ.get("VHN_SCREENSHOTS", "1").lower()
ENABLE_SCREENSHOT_FALLBACK = ENABLE_SCREENSHOT_FALLBACK not in {"0", "false", "no"}

# Layer 2: residential headful browser on the residential node (optional fallback).
# When curl_cffi gets blocked by Cloudflare JS challenges, the VPS calls a
# headful Playwright fetcher running on the residential node's IP via Tailscale.
RESIDENTIAL_FETCHER_URL = os.environ.get("VHN_RESIDENTIAL_FETCHER_URL", "")
RESIDENTIAL_FETCHER_SECRET = os.environ.get("VHN_RESIDENTIAL_FETCHER_SECRET", "")
RESIDENTIAL_FETCHER_TIMEOUT = float(
    os.environ.get("VHN_RESIDENTIAL_FETCHER_TIMEOUT", "120")
)
SCREENSHOT_TIMEOUT_SECONDS = float(os.environ.get("VHN_SCREENSHOT_TIMEOUT", "20"))
CFFI_TIMEOUT = int(os.environ.get("VHN_CFFI_TIMEOUT", "25"))
METADATA_DEADLINE_SECONDS = float(os.environ.get("VHN_METADATA_DEADLINE_SECONDS", "90"))
MAX_HTML_BYTES = int(os.environ.get("VHN_MAX_HTML_BYTES", str(2 * 1024 * 1024)))
MAX_IMAGE_BYTES = int(os.environ.get("VHN_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))


async def read_response_capped(content: Any, max_bytes: int) -> bytes | None:
    """Read a streamed aiohttp response fully up to max_bytes, returning None if too large."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await content.read(min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            return None


async def _curl_cffi_fetch_html(
    url: str, headers: dict[str, str], deadline: float | None = None
) -> tuple[str | None, str | None]:
    """Fetch HTML via curl_cffi with Chrome TLS fingerprint.

    Returns (html_text, final_url). Both None on total failure.
    If blocked by anti-bot (403/429/503), defers to Layer 2 residential fetcher.
    """
    try:
        async with CurlCffiSession(
            impersonate="chrome",
            timeout=CFFI_TIMEOUT,
            verify=True,
        ) as cffi_session:
            current_url = url
            response = None
            for _ in range(6):
                if not is_public_http_url(current_url):
                    logger.warning(
                        "curl_cffi redirect chain hit unsafe URL %s", current_url
                    )
                    return None, current_url
                response = await cffi_session.get(
                    current_url,
                    headers=headers,
                    allow_redirects=False,
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        break
                    from urllib.parse import urljoin

                    current_url = urljoin(current_url, location)
                    continue
                break
            else:
                logger.warning("curl_cffi: too many redirects for %s", url)
                return None, current_url

            final_url = str(response.url) or current_url
            response.raise_for_status()

            content_type = (response.headers.get("content-type") or "").lower()
            if (
                "text/html" not in content_type
                and "application/xhtml" not in content_type
            ):
                logger.warning(
                    "curl_cffi: %s returned Content-Type %s", url, content_type
                )
                return None, final_url

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_HTML_BYTES:
                logger.warning(
                    "curl_cffi: oversized HTML for %s (%s bytes)", url, content_length
                )
                return None, final_url

            raw = response.content
            if raw and len(raw) > MAX_HTML_BYTES:
                logger.warning(
                    "curl_cffi: oversized HTML for %s (> %d bytes)", url, MAX_HTML_BYTES
                )
                return None, final_url

            if raw:
                return raw.decode("utf-8", errors="ignore"), final_url

    except Exception as exc:
        status_code = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            status_code = getattr(resp, "status_code", None)

        if status_code in (401, 403, 429, 503) and RESIDENTIAL_FETCHER_URL:
            remaining = (
                (deadline - time.monotonic())
                if deadline
                else RESIDENTIAL_FETCHER_TIMEOUT
            )
            if remaining <= 0:
                logger.info(
                    "curl_cffi got %s for %s but deadline exhausted", status_code, url
                )
                return None, None
            logger.info(
                "curl_cffi got %s for %s, deferring to residential fetcher (%.0fs budget)",
                status_code,
                url,
                remaining,
            )
            return await _residential_fetch_html(url, timeout_override=remaining)

        logger.warning(
            "curl_cffi fetch failed for %s: %s - %s",
            url,
            type(exc).__name__,
            exc,
        )

    return None, None


async def _residential_fetch_html(
    url: str, timeout_override: float | None = None
) -> tuple[str | None, str | None]:
    """Call the residential node's headful browser fetcher via Tailscale.

    Returns (html_text, final_url). Both None on failure or timeout.
    Never blocks — falls through to screenshot layer if node is off.
    """
    if not RESIDENTIAL_FETCHER_URL:
        return None, None

    fetch_timeout = (
        timeout_override
        if timeout_override is not None
        else RESIDENTIAL_FETCHER_TIMEOUT
    )

    fetch_endpoint = (
        RESIDENTIAL_FETCHER_URL.rstrip("/").replace("/health", "") + "/fetch"
    )
    headers = {"Content-Type": "application/json"}
    if RESIDENTIAL_FETCHER_SECRET:
        headers["X-Fetcher-Secret"] = RESIDENTIAL_FETCHER_SECRET

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                fetch_endpoint,
                json={"url": url},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=fetch_timeout),
            ) as response:
                if response.status != 200:
                    logger.warning(
                        "residential fetcher returned %d for %s",
                        response.status,
                        url,
                    )
                    return None, None
                data = await response.json()
                html = data.get("html")
                final_url = data.get("final_url") or url
                if html:
                    logger.info("residential fetcher succeeded for %s", url)
                    return html, final_url
                logger.info("residential fetcher returned no HTML for %s", url)
                return None, final_url
    except asyncio.TimeoutError:
        logger.info("residential fetcher timed out for %s (node may be off)", url)
    except aiohttp.ClientConnectorError:
        logger.info("residential fetcher unreachable for %s (node may be off)", url)
    except Exception as exc:
        logger.warning(
            "residential fetcher error for %s: %s - %s",
            url,
            type(exc).__name__,
            exc,
        )

    return None, None


async def _wayback_fetch_html(url: str) -> tuple[str | None, str | None]:
    """Fetch HTML from the Wayback Machine archive.

    Returns (html_text, final_url). Both None if no snapshot or fetch fails.
    Used as a fallback when curl_cffi and the residential fetcher both fail
    with anti-bot 403s. Wayback serves cached pages that still contain the
    original og:image meta tags.
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Check availability first. Pass the URL as an encoded query
            # param — interpolating it raw breaks on any story URL with
            # its own query string (the story's &param gets parsed as a
            # sibling param of archive.org's API, truncating/malforming
            # the lookup URL and missing valid snapshots).
            api_url = "https://archive.org/wayback/available"
            async with session.get(
                api_url,
                params={"url": url},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()
                snap = data.get("archived_snapshots", {}).get("closest", {})
                if not snap.get("available") or snap.get("status") != "200":
                    return None, None
                snapshot_url = snap["url"]

            # Fetch the snapshot page
            async with session.get(
                snapshot_url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return None, None
                html_bytes = await resp.read()
                if len(html_bytes) > MAX_HTML_BYTES:
                    return None, None
                html = html_bytes.decode("utf-8", errors="ignore")
                logger.info("wayback fetcher succeeded for %s", url)
                return html, snapshot_url
    except asyncio.TimeoutError:
        logger.debug("wayback fetcher timed out for %s", url)
    except Exception as exc:
        logger.debug("wayback fetcher error for %s: %s", url, exc)
    return None, None


async def capture_screenshot_with_timeout(
    url: str, timeout_override: float | None = None
) -> str | None:
    """Run screenshot fallback without letting a wedged browser stall a scrape cycle."""
    import metadata

    timeout = (
        timeout_override
        if timeout_override is not None
        else metadata.SCREENSHOT_TIMEOUT_SECONDS
    )
    task = asyncio.create_task(metadata.capture_screenshot(url))
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(
            "Screenshot fallback timed out after %.1fs for %s",
            timeout,
            url,
        )
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return None


async def capture_screenshot(url: str) -> str | None:
    """Capture a browser screenshot of a URL using headless Chromium.

    Delegates to ``screenshot.py`` (Playwright + Adblock filter-list blocking).
    See that module for blocking-layer details.
    """
    if not is_public_http_url(url):
        logger.warning("Skipping screenshot for unsafe URL %s", url)
        return None
    return await screenshot_module.capture_screenshot(url)
