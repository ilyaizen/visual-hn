"""HTML parsing — description extraction, image URL discovery, text cleanup.

Imports from safety: normalize_whitespace, resolve_metadata_url, is_public_http_url,
source_domain.
"""

from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup
import re

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


def extract_og_image_url(html: str, base_url: str) -> str | None:
    """Return the first public og:image/twitter:image URL, to be loaded client-side.

    Only declared social-card images are handed to the browser, and the
    URL is validated as public http(s) so we never expose private/SSRF targets.

    GitHub repo og:images use S3 pre-signed URLs (repository-images.githubusercontent.com)
    that expire in ~5 minutes.  Rewrite them to the stable opengraph.githubassets.com
    form so the image stays valid long after the scrape.
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
            resolved = _stabilize_github_repo_image(resolved, base_url)
            return resolved
    return None


_GITHUB_REPO_IMAGE_RE = re.compile(
    r"^https://repository-images\.githubusercontent\.com/"
)
_GITHUB_REPO_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+?)(?:/|$)"
)


def _stabilize_github_repo_image(og_url: str, page_url: str) -> str:
    """Convert an expiring GitHub repo-image URL to a stable opengraph URL.

    GitHub sets og:image to repository-images.githubusercontent.com/... with S3
    signed params (X-Amz-Expires=300).  These expire in 5 minutes.  The stable
    equivalent is opengraph.githubassets.com/1/{owner}/{repo}.

    Only applies when the page URL is a github.com/{owner}/{repo} link.
    """
    if not _GITHUB_REPO_IMAGE_RE.match(og_url):
        return og_url
    m = _GITHUB_REPO_URL_RE.match(page_url)
    if not m:
        return og_url
    owner, repo = m.group(1), m.group(2)
    return f"https://opengraph.githubassets.com/1/{owner}/{repo}"


def build_fallback_description(url: str, text_snippet: str = "") -> str:
    """Build friendly copy when metadata descriptions are unavailable."""
    snippet = clean_html_text(text_snippet)
    if snippet:
        return truncate_description(snippet)

    hostname = source_domain(url) or "the source site"
    return f"Read the full story on {hostname}."
