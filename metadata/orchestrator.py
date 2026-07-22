"""Metadata orchestration — the main fetch_metadata pipeline.

Imports from all sub-modules to coordinate the fallback chain:
curl_cffi → residential → Wayback → screenshot → favicon composite.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict

import aiohttp
from ssl import SSLError

from .cache import (
    PLACEHOLDER_IMAGE,
    METADATA_MAX_RETRIES,
    cache_metadata,
    get_cached_metadata,
    metadata_cache,
    should_use_cached_metadata,
)
from .safety import (
    is_public_http_url,
    aiohttp_request_url,
)
from .parser import (
    truncate_description,
    extract_description_from_html,
    extract_og_image_url,
    build_fallback_description,
)
from .fetcher import (
    USER_AGENT,
    ENABLE_SCREENSHOT_FALLBACK,
    MAX_HTML_BYTES,
    SCREENSHOT_TIMEOUT_SECONDS,
    METADATA_DEADLINE_SECONDS,
    CFFI_TIMEOUT,
    read_response_capped,
    _curl_cffi_fetch_html,
    _wayback_fetch_html,
    capture_screenshot_with_timeout,
)
from .images import (
    _render_pdf_first_page,
    generate_favicon_composite,
)

logger = logging.getLogger(__name__)


async def fetch_metadata(
    url: str,
    text_snippet: str = "",
    session: aiohttp.ClientSession | None = None,
    enable_screenshot: bool = True,
    deadline: float | None = None,
) -> Dict[str, Any]:
    """
    Fetch metadata from a URL, prefer social tags, and fall back to screenshots/images.

    Description priority: OG → Twitter → standard meta description → JSON-LD → first
    substantial paragraph → HN/story text snippet → friendly domain copy.
    Image priority: OG/Twitter image download → browser screenshot → placeholder.

    ``deadline`` is a monotonic timestamp. If None, defaults to now +
    METADATA_DEADLINE_SECONDS. Each fallback stage checks remaining time and
    bails to the next cheaper stage when the budget is exhausted.
    """
    cached_metadata = get_cached_metadata(url)
    if should_use_cached_metadata(cached_metadata):
        logger.debug("Cache hit for %s", url)
        return cached_metadata
    retries = 0
    if cached_metadata:
        retries = cached_metadata.get("retries", 0)
        logger.info(
            "Retrying metadata for %s (attempt %d/%d) because cached image is still the placeholder",
            url,
            retries + 1,
            METADATA_MAX_RETRIES,
        )
        metadata_cache.pop(url, None)

    if not is_public_http_url(url):
        logger.warning("Skipping metadata fetch for unsafe URL %s", url)
        fallback_description = build_fallback_description(url, text_snippet)
        metadata = {
            "image_url": PLACEHOLDER_IMAGE,
            "og_image_url": None,
            "description": truncate_description(fallback_description),
        }
        cache_metadata(url, metadata)
        return metadata

    logger.info("Fetching metadata for %s", url)
    if deadline is None:
        deadline = time.monotonic() + METADATA_DEADLINE_SECONDS
    fallback_description = build_fallback_description(url, text_snippet)
    description = fallback_description
    image_filename = None
    og_image_url = None

    # PDF URLs
    if url.lower().endswith(".pdf") and time.monotonic() < deadline:
        logger.info("PDF URL detected, rendering first page for %s", url)
        image_filename = await _render_pdf_first_page(url)
        if image_filename:
            og_image_url = None
            metadata = {
                "image_url": f"/static/images/{image_filename}",
                "og_image_url": None,
                "description": truncate_description(
                    text_snippet or fallback_description
                ),
                "retries": retries + 1,
            }
            cache_metadata(url, metadata)
            return metadata
        # PDF render failed — fall through to favicon composite at the end

    html = None
    final_url = url
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

    owns_session = session is None
    if owns_session:
        # Layer 1: curl_cffi (Chrome TLS) + Layer 2 residential fetcher fallback.
        html, cffi_final_url = await _curl_cffi_fetch_html(
            url, headers, deadline=deadline
        )
        if cffi_final_url:
            final_url = cffi_final_url
    else:
        # Test path or caller-provided aiohttp session.
        for ssl_value in (None, False):
            try:
                request_kwargs: dict[str, Any] = {
                    "timeout": aiohttp.ClientTimeout(total=15),
                    "allow_redirects": True,
                    "headers": headers,
                }
                if ssl_value is False:
                    request_kwargs["ssl"] = False

                async with session.get(
                    aiohttp_request_url(url), **request_kwargs
                ) as response:
                    response.raise_for_status()
                    final_url = str(response.url)
                    if not is_public_http_url(final_url):
                        logger.warning(
                            "Metadata request for %s redirected to unsafe URL %s",
                            url,
                            final_url,
                        )
                        break
                    content_type = response.headers.get("Content-Type", "").lower()
                    if (
                        "text/html" not in content_type
                        and "application/xhtml" not in content_type
                    ):
                        logger.warning(
                            "URL %s did not return HTML (Content-Type: %s).",
                            url,
                            content_type,
                        )
                        break
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > MAX_HTML_BYTES:
                        logger.warning(
                            "Skipping oversized HTML response for %s (%s bytes).",
                            url,
                            content_length,
                        )
                        break
                    html_bytes = await read_response_capped(
                        response.content, MAX_HTML_BYTES
                    )
                    if html_bytes is None:
                        logger.warning(
                            "Skipping oversized HTML response for %s (> %d bytes).",
                            url,
                            MAX_HTML_BYTES,
                        )
                        break
                    charset = response.charset or "utf-8"
                    html = html_bytes.decode(charset, errors="ignore")
                    break
            except SSLError as exc:
                if ssl_value is None:
                    logger.warning(
                        "SSL error fetching metadata for %s: %s. Retrying with ssl=False.",
                        url,
                        exc,
                    )
                    continue
                logger.warning("SSL-disabled retry failed for %s: %s", url, exc)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "aiohttp fetch failed for %s: %s - %s",
                    url,
                    type(exc).__name__,
                    exc,
                )
                break
            except Exception as exc:
                logger.error(
                    "Unexpected metadata fetch error for %s: %s",
                    url,
                    exc,
                    exc_info=True,
                )
                break
    if html:
        description = extract_description_from_html(html, fallback_description)
        # Budget policy: do NOT download og:images to the box. Hand the remote
        # og:image URL to clients so their browsers load it straight from the
        # source CDN. Only when a page exposes no usable og:image do we fall back
        # to a locally captured screenshot (served sparingly from our box).
        og_image_url = extract_og_image_url(html, final_url)
        if og_image_url:
            logger.info("Using remote og:image for %s", url)

    # Layer 2.5: Wayback Machine archive fallback.
    # When the original page is anti-bot blocked (no HTML, no og:image), try
    # the Wayback Machine archive. Cached pages still contain the original
    # og:image meta tags and are not behind the same bot protection.
    if not og_image_url and not html and time.monotonic() < deadline:
        wb_html, wb_url = await _wayback_fetch_html(url)
        if wb_html:
            description = extract_description_from_html(wb_html, fallback_description)
            og_image_url = extract_og_image_url(wb_html, url)
            if og_image_url:
                # Strip Wayback URL prefix to get the original CDN URL
                # Wayback wraps images as /web/TIMESTAMPim_/ORIGINAL_URL
                m = re.match(
                    r"https?://web\.archive\.org/web/\d+[a-z_]*/(.+)",
                    og_image_url,
                    re.IGNORECASE,
                )
                if m:
                    og_image_url = m.group(1)
                    if not og_image_url.startswith(("http://", "https://")):
                        og_image_url = "https://" + og_image_url
                logger.info("Using wayback og:image for %s", url)

    if (
        not og_image_url
        and ENABLE_SCREENSHOT_FALLBACK
        and enable_screenshot
        and time.monotonic() < deadline
    ):
        if is_public_http_url(final_url):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "Metadata deadline exhausted before screenshot for %s", url
                )
            else:
                screenshot_timeout = min(SCREENSHOT_TIMEOUT_SECONDS, remaining)
                logger.info("Attempting screenshot fallback for %s", final_url)
                image_filename = await capture_screenshot_with_timeout(
                    final_url, timeout_override=screenshot_timeout
                )
            if image_filename:
                logger.info(
                    "Successfully captured screenshot fallback for %s", final_url
                )
            else:
                logger.warning("Screenshot capture failed for %s", final_url)
        else:
            logger.warning("Skipping screenshot fallback for unsafe URL %s", final_url)

    if not image_filename and not og_image_url and time.monotonic() < deadline:
        image_filename = await generate_favicon_composite(url)

    metadata = {
        "image_url": (
            f"/static/images/{image_filename}" if image_filename else PLACEHOLDER_IMAGE
        ),
        "og_image_url": og_image_url,
        "description": truncate_description(description or fallback_description),
        "retries": retries + 1,
    }
    cache_metadata(url, metadata)
    logger.debug(
        "Finished metadata for %s. Image: %s, Description: %r",
        url,
        metadata["image_url"],
        metadata["description"][:80],
    )
    return metadata
