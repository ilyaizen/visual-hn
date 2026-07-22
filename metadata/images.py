"""Image processing — download, resize, card generation, favicon composites.

Imports from safety: is_public_http_url, resolve_metadata_url, source_domain,
aiohttp_request_url.
Imports from parser: clean_html_text.
Imports from fetcher: CFFI_TIMEOUT, USER_AGENT, MAX_IMAGE_BYTES, read_response_capped.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from io import BytesIO
from ssl import SSLError
from typing import Any
from urllib.parse import urljoin

import aiohttp
from curl_cffi.requests import AsyncSession as CurlCffiSession
from PIL import Image, ImageFile

from .safety import (
    aiohttp_request_url,
    is_public_http_url,
    resolve_metadata_url,
    source_domain,
)
from .parser import clean_html_text

ImageFile.LOAD_TRUNCATED_IMAGES = True

logger = logging.getLogger(__name__)

IMAGE_DIR = "static/images"
MAX_STORED_IMAGE_WIDTH = int(os.environ.get("VHN_MAX_IMAGE_WIDTH", "1024"))
JPEG_QUALITY = int(os.environ.get("VHN_JPEG_QUALITY", "72"))
MIN_IMAGE_WIDTH = 400
MIN_IMAGE_HEIGHT = 100
SCREENSHOT_MIN_BYTES = 4 * 1024


async def download_and_resize_image(
    image_url: str,
    base_url: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Download, resize, normalize, and save an image to static/images."""
    import metadata

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
            "User-Agent": metadata.USER_AGENT,
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        }
        owns_session = session is None
        image_data: bytes | None = None
        if owns_session:
            # curl_cffi with Chrome TLS fingerprint for image downloads.
            try:
                async with CurlCffiSession(
                    impersonate="chrome",
                    timeout=metadata.CFFI_TIMEOUT,
                    verify=True,
                ) as cffi_session:
                    current_img_url = resolved_image_url
                    response = None
                    for _ in range(6):
                        if not is_public_http_url(current_img_url):
                            logger.warning(
                                "Image redirect chain hit unsafe URL %s",
                                current_img_url,
                            )
                            return None
                        response = await cffi_session.get(
                            current_img_url,
                            headers=headers,
                            allow_redirects=False,
                        )
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                break
                            current_img_url = urljoin(current_img_url, location)
                            continue
                        break
                    else:
                        logger.warning(
                            "Too many redirects for image %s", resolved_image_url
                        )
                        return None

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
                    if (
                        content_length
                        and int(content_length) > metadata.MAX_IMAGE_BYTES
                    ):
                        logger.warning(
                            "Skipping oversized image %s (%s bytes).",
                            resolved_image_url,
                            content_length,
                        )
                        return None
                    raw = response.content
                    if raw and len(raw) > metadata.MAX_IMAGE_BYTES:
                        logger.warning(
                            "Skipping oversized image %s (> %d bytes).",
                            resolved_image_url,
                            metadata.MAX_IMAGE_BYTES,
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
                            if (
                                content_length
                                and int(content_length) > metadata.MAX_IMAGE_BYTES
                            ):
                                logger.warning(
                                    "Skipping oversized image %s (%s bytes).",
                                    resolved_image_url,
                                    content_length,
                                )
                                return None
                            image_data = await metadata.read_response_capped(
                                response.content, metadata.MAX_IMAGE_BYTES
                            )
                            if image_data is None:
                                logger.warning(
                                    "Skipping oversized image %s (> %d bytes).",
                                    resolved_image_url,
                                    metadata.MAX_IMAGE_BYTES,
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
        image_path = os.path.join(metadata.IMAGE_DIR, image_filename)
        os.makedirs(metadata.IMAGE_DIR, exist_ok=True)
        with BytesIO(image_data) as image_buffer:
            with Image.open(image_buffer) as opened_image:
                image = opened_image.convert("RGB")
                if image.width > metadata.MAX_STORED_IMAGE_WIDTH:
                    ratio = metadata.MAX_STORED_IMAGE_WIDTH / float(image.width)
                    height = int(float(image.height) * ratio)
                    resized = image.resize(
                        (metadata.MAX_STORED_IMAGE_WIDTH, height),
                        Image.Resampling.LANCZOS,
                    )
                    image.close()
                    image = resized
                try:
                    image.save(
                        image_path, "JPEG", quality=metadata.JPEG_QUALITY, optimize=True
                    )
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
    import metadata

    image_path = os.path.join(metadata.IMAGE_DIR, image_filename)
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


async def _generate_hn_card(text_snippet: str) -> str | None:
    """Generate a branded HN card for Ask HN / text posts.

    Orange HN logo + truncated post text on a slate card.
    """
    import metadata

    try:
        import textwrap

        card_w, card_h = metadata.MAX_STORED_IMAGE_WIDTH, int(
            metadata.MAX_STORED_IMAGE_WIDTH * 0.75
        )
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
        composite_path = os.path.join(metadata.IMAGE_DIR, composite_filename)
        card.save(composite_path, "JPEG", quality=metadata.JPEG_QUALITY, optimize=True)
        logger.info("Generated HN branded card: %s", composite_filename)
        return composite_filename
    except Exception as exc:
        logger.warning("HN card generation failed: %s - %s", type(exc).__name__, exc)
        return None


async def _render_pdf_first_page(url: str) -> str | None:
    """Download a PDF and render its first page as a JPEG preview.

    Uses curl_cffi for download (Chrome TLS) and pdftoppm (Poppler) for rendering.
    """
    import metadata

    if not is_public_http_url(url):
        return None

    try:
        async with CurlCffiSession(
            impersonate="chrome",
            timeout=metadata.CFFI_TIMEOUT,
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
            if len(pdf_data) > metadata.MAX_IMAGE_BYTES * 2:
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
        pdf_path = os.path.join(metadata.IMAGE_DIR, pdf_filename)

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
            from contextlib import suppress

            with suppress(OSError):
                os.unlink(tmp_path)

        # Resize to max stored width
        with Image.open(pdf_path) as img:
            image = img.convert("RGB")
            if image.width > metadata.MAX_STORED_IMAGE_WIDTH:
                ratio = metadata.MAX_STORED_IMAGE_WIDTH / float(image.width)
                height = int(float(image.height) * ratio)
                resized = image.resize(
                    (metadata.MAX_STORED_IMAGE_WIDTH, height), Image.Resampling.LANCZOS
                )
                resized.save(
                    pdf_path, "JPEG", quality=metadata.JPEG_QUALITY, optimize=True
                )
            else:
                image.save(
                    pdf_path, "JPEG", quality=metadata.JPEG_QUALITY, optimize=True
                )

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
    import metadata

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
                timeout=metadata.CFFI_TIMEOUT,
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
        composite_path = os.path.join(metadata.IMAGE_DIR, composite_filename)

        card_w, card_h = metadata.MAX_STORED_IMAGE_WIDTH, int(
            metadata.MAX_STORED_IMAGE_WIDTH * 0.75
        )
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

        card.save(composite_path, "JPEG", quality=metadata.JPEG_QUALITY, optimize=True)
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
