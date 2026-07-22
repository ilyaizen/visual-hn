"""Visual-HN metadata package — backward-compatible re-exports.

All public names from the old monolithic metadata.py are available here.
"""

from __future__ import annotations

# ── safety ──
from .safety import (
    normalize_whitespace,
    resolve_metadata_url,
    aiohttp_request_url,
    is_public_http_url,
    source_domain,
    favicon_url,
    _is_hn_internal_url,
)

# ── cache ──
from .cache import (
    metadata_cache,
    PLACEHOLDER_IMAGE,
    METADATA_CACHE_MAX_ITEMS,
    METADATA_MAX_RETRIES,
    cache_metadata,
    get_cached_metadata,
    should_use_cached_metadata,
)

# ── parser ──
from .parser import (
    clean_html_text,
    truncate_description,
    DESCRIPTION_LIMIT,
    _meta_content,
    _meta_contents,
    _json_ld_description,
    _first_substantial_paragraph,
    extract_description_from_html,
    extract_image_urls_from_html,
    extract_og_image_url,
    build_fallback_description,
)

# ── fetcher ──
from .fetcher import (
    USER_AGENT,
    ENABLE_SCREENSHOT_FALLBACK,
    RESIDENTIAL_FETCHER_URL,
    RESIDENTIAL_FETCHER_SECRET,
    RESIDENTIAL_FETCHER_TIMEOUT,
    SCREENSHOT_TIMEOUT_SECONDS,
    METADATA_DEADLINE_SECONDS,
    CFFI_TIMEOUT,
    MAX_HTML_BYTES,
    MAX_IMAGE_BYTES,
    read_response_capped,
    _curl_cffi_fetch_html,
    _residential_fetch_html,
    _wayback_fetch_html,
    capture_screenshot_with_timeout,
    capture_screenshot,
    FetchFailure,
    HtmlFetchResult,
)

# ── images ──
from .images import (
    IMAGE_DIR,
    MAX_STORED_IMAGE_WIDTH,
    JPEG_QUALITY,
    MIN_IMAGE_WIDTH,
    MIN_IMAGE_HEIGHT,
    SCREENSHOT_MIN_BYTES,
    download_and_resize_image,
    is_image_too_small,
    _generate_hn_card,
    _render_pdf_first_page,
    generate_favicon_composite,
)

# ── orchestrator ──
from .orchestrator import fetch_metadata

__all__ = [
    # safety
    "normalize_whitespace",
    "resolve_metadata_url",
    "aiohttp_request_url",
    "is_public_http_url",
    "source_domain",
    "favicon_url",
    "_is_hn_internal_url",
    # cache
    "metadata_cache",
    "PLACEHOLDER_IMAGE",
    "METADATA_CACHE_MAX_ITEMS",
    "METADATA_MAX_RETRIES",
    "cache_metadata",
    "get_cached_metadata",
    "should_use_cached_metadata",
    # parser
    "clean_html_text",
    "truncate_description",
    "DESCRIPTION_LIMIT",
    "extract_description_from_html",
    "extract_image_urls_from_html",
    "extract_og_image_url",
    "build_fallback_description",
    # fetcher
    "USER_AGENT",
    "ENABLE_SCREENSHOT_FALLBACK",
    "SCREENSHOT_TIMEOUT_SECONDS",
    "METADATA_DEADLINE_SECONDS",
    "CFFI_TIMEOUT",
    "MAX_HTML_BYTES",
    "MAX_IMAGE_BYTES",
    "read_response_capped",
    "capture_screenshot_with_timeout",
    "capture_screenshot",
    # images
    "download_and_resize_image",
    "is_image_too_small",
    "generate_favicon_composite",
    "_render_pdf_first_page",
    # orchestrator
    "fetch_metadata",
]
