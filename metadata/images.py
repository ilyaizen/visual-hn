"""Image processing — card generation, favicon composites.

Imports from safety: is_public_http_url, resolve_metadata_url, source_domain.
Imports from parser: clean_html_text.
PDF fetching: every redirect hop is re-validated against is_public_http_url
(allow_redirects=False + manual loop), the body is streamed with a hard byte
cap, and pdftoppm runs under an asyncio timeout with kill on expiry.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from contextlib import suppress
from io import BytesIO
from ssl import SSLError
from typing import Any
from urllib.parse import urljoin

from asyncio import create_subprocess_exec

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


async def _download_pdf_capped(cffi_session, url: str, max_bytes: int) -> bytes | None:
    """Download a PDF body, validating every redirect hop and streaming with a hard byte cap.

    Returns the PDF bytes, or None on any policy violation (unsafe redirect hop,
    non-PDF content, oversized body, network error). No automatic redirect
    following: each hop is checked with is_public_http_url before being followed.
    """
    current_url = url
    response = None
    for _ in range(6):
        if not is_public_http_url(current_url, strict=True):
            logger.warning(
                "PDF fetch redirect chain hit unsafe URL %s (origin %s)",
                current_url,
                url,
            )
            return None
        response = await cffi_session.get(
            current_url, allow_redirects=False, stream=True
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                break
            current_url = urljoin(current_url, location)
            continue
        try:
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").lower()
            if "pdf" not in content_type and not url.lower().endswith(".pdf"):
                logger.warning(
                    "PDF URL returned non-PDF content-type: %s", content_type
                )
                return None
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_content(64 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(
                        "PDF too large for preview render: >%d bytes", max_bytes
                    )
                    return None
            pdf_data = b"".join(chunks)
        finally:
            await response.aclose()
        if not pdf_data or len(pdf_data) < 1000:
            return None
        return pdf_data
    return None


PDF_POPPLER_TIMEOUT_SECONDS = float(os.environ.get("VHN_PDF_POPPLER_TIMEOUT", "20"))


async def _run_pdftoppm(tmp_path: str, output_base: str) -> tuple[int, bytes]:
    """Run pdftoppm with a hard timeout; kill the process tree on expiry.

    Returns (returncode, stderr_bytes). Raises TimeoutError if killed.
    """
    proc = await create_subprocess_exec(
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
        output_base,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=PDF_POPPLER_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await proc.wait()
        raise TimeoutError(
            f"pdftoppm exceeded {PDF_POPPLER_TIMEOUT_SECONDS}s and was killed"
        ) from None
    return proc.returncode, stderr


async def _render_pdf_first_page(url: str) -> str | None:
    """Download a PDF and render its first page as a JPEG preview.

    Uses curl_cffi for download (Chrome TLS) and pdftoppm (Poppler) for rendering.
    Download is throttled: manual redirect validation + streamed byte cap; the
    renderer runs under a hard timeout and is killed if hung.
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
            pdf_data = await _download_pdf_capped(
                cffi_session, url, metadata.MAX_IMAGE_BYTES * 2
            )
        if pdf_data is None:
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
            returncode, stderr = await _run_pdftoppm(
                tmp_path, pdf_path.replace(".jpg", "")
            )
            if returncode != 0:
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
