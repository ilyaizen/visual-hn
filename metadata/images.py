"""Image processing — card generation, favicon composites.

Imports from safety: is_public_http_url, resolve_metadata_url, source_domain.
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

from curl_cffi.requests import AsyncSession as CurlCffiSession
from PIL import Image, ImageFile

from .safety import (
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
