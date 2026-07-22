# --- START OF FILE metadata.py ---
from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import ipaddress
import json
import logging
import os
import re
import socket
from collections import OrderedDict
from contextlib import suppress
from io import BytesIO
from ssl import SSLError
from typing import Any, Dict
from urllib.parse import urljoin, urlparse

import aiohttp
from curl_cffi.requests import AsyncSession as CurlCffiSession
from bs4 import BeautifulSoup
from PIL import Image, ImageFile
from yarl import URL
import screenshot as screenshot_module

logger = logging.getLogger(__name__)
# Many source/CDN images are otherwise valid but have tiny trailing-byte
# inconsistencies. Accept them so cards do not unnecessarily fall back to the
# placeholder; size/content checks below still reject unusable files.
ImageFile.LOAD_TRUNCATED_IMAGES = True

metadata_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
IMAGE_DIR = "static/images"
PLACEHOLDER_IMAGE = "/static/images/placeholder.jpg"
# Must match curl_cffi's impersonate='chrome' default UA (Chrome 146 macOS).
# Overriding to a different OS/version creates contradictions with the TLS
# fingerprint and Sec-Ch-Ua-Platform headers that curl_cffi sends automatically.
# See https://scrapfly.dev/posts/browser-math-os-fingerprint/ for why
# UA/OS consistency matters down to the Math.tanh bits.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
ENABLE_SCREENSHOT_FALLBACK = os.environ.get("VHN_SCREENSHOTS", "1").lower() not in {
    "0",
    "false",
    "no",
}
# Layer 2: residential headful browser on the residential node (optional fallback).
# When curl_cffi gets blocked by Cloudflare JS challenges, the VPS calls a
# headful Playwright fetcher running on the residential node's IP via Tailscale.
RESIDENTIAL_FETCHER_URL = os.environ.get("VHN_RESIDENTIAL_FETCHER_URL", "")
RESIDENTIAL_FETCHER_SECRET = os.environ.get("VHN_RESIDENTIAL_FETCHER_SECRET", "")
RESIDENTIAL_FETCHER_TIMEOUT = float(
    os.environ.get("VHN_RESIDENTIAL_FETCHER_TIMEOUT", "120")
)
SCREENSHOT_MIN_BYTES = 4 * 1024
SCREENSHOT_TIMEOUT_SECONDS = float(os.environ.get("VHN_SCREENSHOT_TIMEOUT", "20"))
METADATA_CACHE_MAX_ITEMS = int(os.environ.get("VHN_METADATA_CACHE_MAX_ITEMS", "300"))
METADATA_MAX_RETRIES = int(os.environ.get("VHN_METADATA_MAX_RETRIES", "3"))
CFFI_TIMEOUT = int(os.environ.get("VHN_CFFI_TIMEOUT", "25"))
MIN_IMAGE_WIDTH = 400
MIN_IMAGE_HEIGHT = 100
# Budget guard: stored images are capped to this width and heavily JPEG-compressed
# so the Hetzner box never serves full-resolution OG originals (disk/bandwidth).
MAX_STORED_IMAGE_WIDTH = int(os.environ.get("VHN_MAX_IMAGE_WIDTH", "1024"))
JPEG_QUALITY = int(os.environ.get("VHN_JPEG_QUALITY", "72"))
DESCRIPTION_LIMIT = 280
MAX_HTML_BYTES = int(os.environ.get("VHN_MAX_HTML_BYTES", str(2 * 1024 * 1024)))
MAX_IMAGE_BYTES = int(os.environ.get("VHN_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))

os.makedirs(IMAGE_DIR, exist_ok=True)


def cache_metadata(url: str, metadata: Dict[str, Any]) -> None:
    """Store metadata in a bounded LRU cache so long-running servers cannot grow forever."""
    if METADATA_CACHE_MAX_ITEMS <= 0:
        return
    metadata_cache[url] = metadata
    metadata_cache.move_to_end(url)
    while len(metadata_cache) > METADATA_CACHE_MAX_ITEMS:
        evicted_url, _ = metadata_cache.popitem(last=False)
        logger.debug("Evicted metadata cache entry for %s", evicted_url)


def get_cached_metadata(url: str) -> Dict[str, Any] | None:
    cached = metadata_cache.get(url)
    if cached is not None:
        metadata_cache.move_to_end(url)
    return cached


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


def normalize_whitespace(value: str | None) -> str:
    """Collapse noisy whitespace and decode HTML entities."""
    if not value:
        return ""
    decoded = html_lib.unescape(value)
    return re.sub(r"\s+", " ", decoded).strip()


async def _curl_cffi_fetch_html(
    url: str, headers: dict[str, str]
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
            response = await cffi_session.get(
                url,
                headers=headers,
                allow_redirects=True,
            )
            response.raise_for_status()
            final_url = response.url or url

            if not is_public_http_url(final_url):
                logger.warning(
                    "curl_cffi redirected %s to unsafe URL %s", url, final_url
                )
                return None, final_url

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
            logger.info(
                "curl_cffi got %s for %s, deferring to residential fetcher",
                status_code,
                url,
            )
            return await _residential_fetch_html(url)

        logger.warning(
            "curl_cffi fetch failed for %s: %s - %s",
            url,
            type(exc).__name__,
            exc,
        )

    return None, None


async def _residential_fetch_html(url: str) -> tuple[str | None, str | None]:
    """Call the residential node's headful browser fetcher via Tailscale.

    Returns (html_text, final_url). Both None on failure or timeout.
    Never blocks — falls through to screenshot layer if node is off.
    """
    if not RESIDENTIAL_FETCHER_URL:
        return None, None

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
                timeout=aiohttp.ClientTimeout(total=RESIDENTIAL_FETCHER_TIMEOUT),
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


def clean_html_text(value: str | None) -> str:
    """Convert an HTML snippet into readable plain text."""
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return normalize_whitespace(soup.get_text(" "))


def truncate_description(value: str, limit: int = DESCRIPTION_LIMIT) -> str:
    """Truncate descriptions at a word boundary when possible."""
    value = normalize_whitespace(value)
    if len(value) <= limit:
        return value
    truncated = value[:limit].rstrip()
    last_space = truncated.rfind(" ")
    if last_space > 120:
        truncated = truncated[:last_space]
    return f"{truncated}..."


def resolve_metadata_url(candidate: str | None, base_url: str | None) -> str | None:
    """Resolve absolute, protocol-relative, root-relative, and page-relative metadata URLs."""
    candidate = normalize_whitespace(candidate)
    if not candidate:
        return None
    if candidate.startswith("//"):
        scheme = urlparse(base_url or "https://").scheme or "https"
        return f"{scheme}:{candidate}"
    if base_url:
        return urljoin(base_url, candidate)
    return candidate if candidate.startswith(("http://", "https://")) else None


def aiohttp_request_url(url: str) -> URL:
    """Build an aiohttp URL without canonicalizing signed query strings.

    Some image CDNs sign the exact query string. aiohttp/yarl normally rewrites
    harmless-looking encodings like `%2C` to `,`, which is enough to invalidate
    Guardian-style image signatures and turn a good OG image into a 401.
    """
    return URL(url, encoded=True)


def is_public_http_url(url: str | None) -> bool:
    """Return True for public http(s) URLs and False for localhost/private targets."""
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if (
        not host
        or host in {"localhost", "localhost.localdomain"}
        or host.endswith(".local")
    ):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"(?:0x[0-9a-f]+|0[0-7]+|[0-9.]+)", host):
            return False
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return True
        for info in infos:
            sockaddr = info[4]
            address = sockaddr[0]
            try:
                resolved = ipaddress.ip_address(address)
            except ValueError:
                continue
            if resolved.is_multicast or not resolved.is_global:
                return False
        return True
    return not (ip.is_multicast or not ip.is_global)


def _meta_content(soup: BeautifulSoup, *selectors: tuple[str, str]) -> str:
    for content in _meta_contents(soup, *selectors):
        return content
    return ""


def _meta_contents(soup: BeautifulSoup, *selectors: tuple[str, str]) -> list[str]:
    contents: list[str] = []
    for attr, value in selectors:
        for tag in soup.find_all("meta", attrs={attr: value}):
            if tag and tag.get("content"):
                content = normalize_whitespace(tag.get("content"))
                if content:
                    contents.append(content)
    return contents


def _json_ld_description(soup: BeautifulSoup) -> str:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop(0)
            if isinstance(item, dict):
                description = normalize_whitespace(item.get("description"))
                if description:
                    return description
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
            elif isinstance(item, list):
                stack.extend(item)
    return ""


def _first_substantial_paragraph(soup: BeautifulSoup) -> str:
    for unwanted in soup(["script", "style", "noscript", "svg"]):
        unwanted.decompose()
    for tag in soup.find_all(["p", "article"]):
        text = normalize_whitespace(tag.get_text(" "))
        if len(text) >= 60:
            return text
    return ""


def extract_description_from_html(html: str, fallback_description: str = "") -> str:
    """Extract the best available human-readable description from a page."""
    soup = BeautifulSoup(html or "", "html.parser")
    candidates = [
        _meta_content(
            soup,
            ("property", "og:description"),
            ("name", "og:description"),
        ),
        _meta_content(
            soup,
            ("name", "twitter:description"),
            ("property", "twitter:description"),
        ),
        _meta_content(
            soup,
            ("name", "description"),
            ("itemprop", "description"),
        ),
        _json_ld_description(soup),
        _first_substantial_paragraph(soup),
        fallback_description,
    ]
    for candidate in candidates:
        candidate = truncate_description(candidate)
        if candidate:
            return candidate
    return ""


def extract_image_urls_from_html(html: str, base_url: str) -> list[str]:
    """Extract and resolve social/card image candidates from page markup."""
    soup = BeautifulSoup(html or "", "html.parser")
    raw_candidates: list[str] = []
    raw_candidates.extend(
        _meta_contents(
            soup,
            ("property", "og:image"),
            ("name", "og:image"),
            ("property", "og:image:secure_url"),
            ("name", "twitter:image"),
            ("property", "twitter:image"),
            ("name", "twitter:image:src"),
        )
    )

    for link in soup.find_all("link"):
        rel_values = [str(rel).lower() for rel in (link.get("rel") or [])]
        if "image_src" in rel_values or "preload" in rel_values:
            href = normalize_whitespace(link.get("href"))
            as_value = normalize_whitespace(link.get("as")).lower()
            if href and ("image_src" in rel_values or as_value == "image"):
                raw_candidates.append(href)

    for img in soup.find_all("img"):
        src = normalize_whitespace(img.get("src") or img.get("data-src"))
        if src:
            raw_candidates.append(src)

    resolved_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        resolved = resolve_metadata_url(candidate, base_url)
        if resolved and resolved not in seen:
            seen.add(resolved)
            resolved_candidates.append(resolved)
    return resolved_candidates


def extract_og_image_url(html: str, base_url: str) -> str | None:
    """Return the first public og:image/twitter:image URL, to be loaded client-side.

    Unlike extract_image_urls_from_html this deliberately ignores generic <img>
    body tags: only declared social-card images are handed to the browser, and the
    URL is validated as public http(s) so we never expose private/SSRF targets.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    candidates = _meta_contents(
        soup,
        ("property", "og:image"),
        ("name", "og:image"),
        ("property", "og:image:secure_url"),
        ("name", "twitter:image"),
        ("property", "twitter:image"),
        ("name", "twitter:image:src"),
    )
    for link in soup.find_all("link"):
        rel_values = [str(rel).lower() for rel in (link.get("rel") or [])]
        if "image_src" in rel_values:
            href = normalize_whitespace(link.get("href"))
            if href:
                candidates.append(href)

    for candidate in candidates:
        resolved = resolve_metadata_url(candidate, base_url)
        if resolved and is_public_http_url(resolved):
            return resolved
    return None


def build_fallback_description(url: str, text_snippet: str = "") -> str:
    """Build friendly copy when metadata descriptions are unavailable."""
    snippet = clean_html_text(text_snippet)
    if snippet:
        return truncate_description(snippet)

    hostname = source_domain(url) or "the source site"
    return f"Read the full story on {hostname}."


def source_domain(url: str | None) -> str:
    """Return a compact source domain for display beside a story title."""
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return ""
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def favicon_url(url: str | None) -> str:
    """Return a small favicon URL for the story source, or empty string when unavailable."""
    domain = source_domain(url)
    if not domain:
        return ""
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64"


def should_use_cached_metadata(cached: Dict[str, Any] | None) -> bool:
    """Return True when cached metadata is good enough to skip refetching."""
    if not cached:
        return False
    # A remote og:image counts as a good result even though nothing is stored
    # locally; otherwise only a real local fallback (non-placeholder) qualifies.
    if cached.get("og_image_url"):
        return True
    if cached.get("image_url") != PLACEHOLDER_IMAGE:
        return True
    # Placeholder stuck after N attempts — stop retrying to avoid endless re-fetch.
    if cached.get("retries", 0) >= METADATA_MAX_RETRIES:
        return True
    return False


async def fetch_metadata(
    url: str,
    text_snippet: str = "",
    session: aiohttp.ClientSession | None = None,
    enable_screenshot: bool = True,
) -> Dict[str, Any]:
    """
    Fetch metadata from a URL, prefer social tags, and fall back to screenshots/images.

    Description priority: OG → Twitter → standard meta description → JSON-LD → first
    substantial paragraph → HN/story text snippet → friendly domain copy.
    Image priority: OG/Twitter image download → browser screenshot → placeholder.
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
    fallback_description = build_fallback_description(url, text_snippet)
    description = fallback_description
    image_filename = None
    og_image_url = None

    # PDF URLs
    if url.lower().endswith(".pdf"):
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
        html, cffi_final_url = await _curl_cffi_fetch_html(url, headers)
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
                    "aiohttp fetch failed for %s: %s - %s", url, type(exc).__name__, exc
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
    if not og_image_url and not html:
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

    if not og_image_url and ENABLE_SCREENSHOT_FALLBACK and enable_screenshot:
        if is_public_http_url(final_url):
            logger.info("Attempting screenshot fallback for %s", final_url)
            image_filename = await capture_screenshot_with_timeout(final_url)
            if image_filename:
                logger.info(
                    "Successfully captured screenshot fallback for %s", final_url
                )
            else:
                logger.warning("Screenshot capture failed for %s", final_url)
        else:
            logger.warning("Skipping screenshot fallback for unsafe URL %s", final_url)

    if not image_filename and not og_image_url:
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


async def download_and_resize_image(
    image_url: str,
    base_url: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Download, resize, normalize, and save an image to static/images."""
    resolved_image_url = resolve_metadata_url(image_url, base_url)
    if not resolved_image_url:
        logger.warning("Could not resolve image URL %s against %s", image_url, base_url)
        return None

    if not is_public_http_url(resolved_image_url):
        logger.warning("Skipping image download for unsafe URL %s", resolved_image_url)
        return None

    try:
        logger.debug("Attempting to download image: %s", resolved_image_url)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        }
        owns_session = session is None
        image_data: bytes | None = None
        if owns_session:
            # curl_cffi with Chrome TLS fingerprint for image downloads.
            try:
                async with CurlCffiSession(
                    impersonate="chrome",
                    timeout=CFFI_TIMEOUT,
                    verify=True,
                ) as cffi_session:
                    response = await cffi_session.get(
                        resolved_image_url,
                        headers=headers,
                        allow_redirects=True,
                    )
                    response.raise_for_status()
                    content_type = (response.headers.get("content-type") or "").lower()
                    if not content_type.startswith("image/"):
                        logger.warning(
                            "URL %s did not return an image (Content-Type: %s).",
                            resolved_image_url,
                            content_type,
                        )
                        return None
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_IMAGE_BYTES:
                        logger.warning(
                            "Skipping oversized image %s (%s bytes).",
                            resolved_image_url,
                            content_length,
                        )
                        return None
                    raw = response.content
                    if raw and len(raw) > MAX_IMAGE_BYTES:
                        logger.warning(
                            "Skipping oversized image %s (> %d bytes).",
                            resolved_image_url,
                            MAX_IMAGE_BYTES,
                        )
                        return None
                    image_data = raw
            except Exception as exc:
                logger.warning(
                    "curl_cffi image download failed for %s: %s - %s",
                    resolved_image_url,
                    type(exc).__name__,
                    exc,
                )
                return None
        else:
            # Test path or caller-provided aiohttp session.
            try:
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
                            aiohttp_request_url(resolved_image_url), **request_kwargs
                        ) as response:
                            response.raise_for_status()
                            content_type = response.headers.get(
                                "Content-Type", ""
                            ).lower()
                            if not content_type.startswith("image/"):
                                logger.warning(
                                    "URL %s did not return an image (Content-Type: %s).",
                                    resolved_image_url,
                                    content_type,
                                )
                                return None
                            content_length = response.headers.get("Content-Length")
                            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                                logger.warning(
                                    "Skipping oversized image %s (%s bytes).",
                                    resolved_image_url,
                                    content_length,
                                )
                                return None
                            image_data = await read_response_capped(
                                response.content, MAX_IMAGE_BYTES
                            )
                            if image_data is None:
                                logger.warning(
                                    "Skipping oversized image %s (> %d bytes).",
                                    resolved_image_url,
                                    MAX_IMAGE_BYTES,
                                )
                                return None
                        break
                    except SSLError as exc:
                        if ssl_value is None:
                            logger.warning(
                                "SSL error downloading image %s: %s. Retrying with ssl=False.",
                                resolved_image_url,
                                exc,
                            )
                            continue
                        raise
            finally:
                if owns_session and session is not None:
                    await session.close()

        if not image_data:
            return None

        image_hash = hashlib.md5(resolved_image_url.encode("utf-8")).hexdigest()
        image_filename = f"{image_hash}.jpg"
        image_path = os.path.join(IMAGE_DIR, image_filename)
        os.makedirs(IMAGE_DIR, exist_ok=True)
        with BytesIO(image_data) as image_buffer:
            with Image.open(image_buffer) as opened_image:
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
        return image_filename
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        SSLError,
        Image.UnidentifiedImageError,
        OSError,
    ) as exc:
        logger.warning(
            "Error downloading or processing image %s: %s - %s",
            resolved_image_url,
            type(exc).__name__,
            exc,
        )
        return None
    except Exception as exc:
        logger.error(
            "Unexpected error in download_and_resize_image for %s: %s",
            resolved_image_url,
            exc,
            exc_info=True,
        )
        return None


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


async def capture_screenshot_with_timeout(url: str) -> str | None:
    """Run screenshot fallback without letting a wedged browser stall a scrape cycle."""
    task = asyncio.create_task(capture_screenshot(url))
    try:
        return await asyncio.wait_for(task, timeout=SCREENSHOT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error(
            "Screenshot fallback timed out after %.1fs for %s",
            SCREENSHOT_TIMEOUT_SECONDS,
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


def _is_hn_internal_url(url: str) -> bool:
    """True for HN post pages (Ask HN, text posts with no external URL)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        host in {"news.ycombinator.com", "www.news.ycombinator.com"}
        and parsed.path == "/item"
    )


async def _generate_hn_card(text_snippet: str) -> str | None:
    """Generate a branded HN card for Ask HN / text posts.

    Orange HN logo + truncated post text on a slate card.
    """
    try:
        import textwrap

        card_w, card_h = MAX_STORED_IMAGE_WIDTH, int(MAX_STORED_IMAGE_WIDTH * 0.75)
        card = Image.new("RGB", (card_w, card_h), (15, 23, 42))

        from PIL import ImageDraw, ImageFont

        draw = ImageDraw.Draw(card)
        font_bold = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42
        )
        font_text = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22
        )

        # Orange "Y" logo block (mimicking HN's favicon)
        logo_size = 72
        logo_x = (card_w - logo_size) // 2
        logo_y = 50
        draw.rounded_rectangle(
            [logo_x, logo_y, logo_x + logo_size, logo_y + logo_size],
            radius=8,
            fill=(255, 102, 0),
        )
        # Draw "Y" in white on the orange block
        y_bbox = draw.textbbox((0, 0), "Y", font=font_bold)
        y_w = y_bbox[2] - y_bbox[0]
        y_h = y_bbox[3] - y_bbox[1]
        draw.text(
            (logo_x + (logo_size - y_w) // 2, logo_y + (logo_size - y_h) // 2 - 5),
            "Y",
            fill=(255, 255, 255),
            font=font_bold,
        )

        # Truncate and wrap the text snippet below the logo.
        # clean_html_text handles tag stripping + HTML entity decoding (&#x2F; etc.)
        clean_text = clean_html_text(text_snippet)
        if not clean_text:
            clean_text = "Hacker News"
        # Truncate to ~120 chars, wrapped
        if len(clean_text) > 120:
            clean_text = clean_text[:117] + "..."
        lines = textwrap.wrap(clean_text, width=38)
        text_y = logo_y + logo_size + 30
        for line in lines[:4]:
            text_bbox = draw.textbbox((0, 0), line, font=font_text)
            line_w = text_bbox[2] - text_bbox[0]
            draw.text(
                ((card_w - line_w) // 2, text_y),
                line,
                fill=(148, 163, 184),
                font=font_text,
            )
            text_y += 30

        composite_hash = hashlib.md5(b"hn-card-" + clean_text[:50].encode()).hexdigest()
        composite_filename = f"hn-{composite_hash}.jpg"
        composite_path = os.path.join(IMAGE_DIR, composite_filename)
        card.save(composite_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        logger.info("Generated HN branded card: %s", composite_filename)
        return composite_filename
    except Exception as exc:
        logger.warning("HN card generation failed: %s - %s", type(exc).__name__, exc)
        return None


async def _render_pdf_first_page(url: str) -> str | None:
    """Download a PDF and render its first page as a JPEG preview.

    Uses curl_cffi for download (Chrome TLS) and pdftoppm (Poppler) for rendering.
    """
    if not is_public_http_url(url):
        return None

    try:
        async with CurlCffiSession(
            impersonate="chrome",
            timeout=CFFI_TIMEOUT,
            verify=True,
        ) as cffi_session:
            response = await cffi_session.get(url, allow_redirects=True)
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").lower()
            if "pdf" not in content_type and not url.lower().endswith(".pdf"):
                logger.warning(
                    "PDF URL returned non-PDF content-type: %s", content_type
                )
                return None
            pdf_data = response.content
            if not pdf_data or len(pdf_data) < 1000:
                return None
            if len(pdf_data) > MAX_IMAGE_BYTES * 2:
                logger.warning(
                    "PDF too large for preview render: %d bytes", len(pdf_data)
                )
                return None
    except Exception as exc:
        logger.warning(
            "PDF download failed for %s: %s - %s", url, type(exc).__name__, exc
        )
        return None

    try:
        pdf_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        pdf_filename = f"pdf-{pdf_hash}.jpg"
        pdf_path = os.path.join(IMAGE_DIR, pdf_filename)

        # Write PDF to temp file, render first page with pdftoppm
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_data)
            tmp_path = tmp.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "pdftoppm",
                "-jpeg",
                "-r",
                "150",
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                tmp_path,
                pdf_path.replace(".jpg", ""),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("pdftoppm failed for %s: %s", url, stderr.decode()[:200])
                return None
            # pdftoppm with -singlefile outputs directly to the specified path
            if not os.path.exists(pdf_path):
                # try alternate naming (pdftoppm may add .jpg itself)
                alt = pdf_path.replace(".jpg", ".jpg")
                if os.path.exists(alt):
                    os.rename(alt, pdf_path)
                else:
                    logger.warning("pdftoppm output not found at %s", pdf_path)
                    return None
        finally:
            with suppress(OSError):
                os.unlink(tmp_path)

        # Resize to max stored width
        with Image.open(pdf_path) as img:
            image = img.convert("RGB")
            if image.width > MAX_STORED_IMAGE_WIDTH:
                ratio = MAX_STORED_IMAGE_WIDTH / float(image.width)
                height = int(float(image.height) * ratio)
                resized = image.resize(
                    (MAX_STORED_IMAGE_WIDTH, height), Image.Resampling.LANCZOS
                )
                resized.save(pdf_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
            else:
                image.save(pdf_path, "JPEG", quality=JPEG_QUALITY, optimize=True)

        logger.info("Rendered PDF first page for %s → %s", url, pdf_filename)
        return pdf_filename
    except Exception as exc:
        logger.warning(
            "PDF render failed for %s: %s - %s", url, type(exc).__name__, exc
        )
        return None


async def generate_favicon_composite(url: str) -> str | None:
    """Generate a branded card with the site's favicon + domain name.

    Replaces the blank placeholder when all other image paths fail.
    Returns a local image filename, or None on failure.
    """
    domain = source_domain(url)
    if not domain:
        return None

    # Try Google S2 first, then DuckDuckGo as fallback (different index,
    # catches newer/smaller domains Google hasn't crawled yet).
    fav_data = None
    for fav_url in (
        f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
        f"https://icons.duckduckgo.com/ip3/{domain}.ico",
    ):
        try:
            async with CurlCffiSession(
                impersonate="chrome",
                timeout=CFFI_TIMEOUT,
                verify=True,
            ) as cffi_session:
                response = await cffi_session.get(fav_url, allow_redirects=True)
                response.raise_for_status()
                data = response.content
                if data and len(data) > 100:
                    fav_data = data
                    break
        except Exception:
            continue

    if not fav_data:
        logger.warning("Favicon download failed for %s (all sources)", domain)
        return None

    try:
        composite_hash = hashlib.md5(f"favicon-{domain}".encode()).hexdigest()
        composite_filename = f"fav-{composite_hash}.jpg"
        composite_path = os.path.join(IMAGE_DIR, composite_filename)

        card_w, card_h = MAX_STORED_IMAGE_WIDTH, int(MAX_STORED_IMAGE_WIDTH * 0.75)
        card = Image.new("RGB", (card_w, card_h), (15, 23, 42))

        with BytesIO(fav_data) as fav_buffer:
            with Image.open(fav_buffer) as fav_img:
                fav_img = fav_img.convert("RGBA")
                icon_size = min(96, card_h // 3)
                fav_img = fav_img.resize(
                    (icon_size, icon_size), Image.Resampling.LANCZOS
                )
                icon_x = (card_w - icon_size) // 2
                icon_y = (card_h - icon_size) // 2 - 20
                card.paste(fav_img, (icon_x, icon_y), fav_img)

        try:
            from PIL import ImageDraw, ImageFont

            draw = ImageDraw.Draw(card)
            font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            try:
                font = ImageFont.truetype(font_path, 28)
            except OSError:
                font = ImageFont.load_default()
            text = domain
            text_bbox = draw.textbbox((0, 0), text, font=font)
            text_w = text_bbox[2] - text_bbox[0]
            text_x = (card_w - text_w) // 2
            text_y = icon_y + icon_size + 15
            draw.text((text_x, text_y), text, fill=(100, 116, 139), font=font)
        except ImportError:
            pass

        card.save(composite_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        logger.info("Generated favicon composite card for %s", domain)
        return composite_filename
    except Exception as exc:
        logger.warning(
            "Favicon composite generation failed for %s: %s - %s",
            domain,
            type(exc).__name__,
            exc,
        )
        return None


# --- END OF FILE metadata.py ---
