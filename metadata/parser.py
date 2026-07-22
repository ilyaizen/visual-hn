"""HTML parsing — description extraction, image URL discovery, text cleanup.

Imports from safety: normalize_whitespace, resolve_metadata_url, is_public_http_url,
source_domain.
"""

from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup

from .safety import (
    is_public_http_url,
    normalize_whitespace,
    resolve_metadata_url,
    source_domain,
)

DESCRIPTION_LIMIT = 280


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
