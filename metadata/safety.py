"""Safety utilities — URL validation, resolution, and domain helpers.

No dependencies on other metadata sub-modules.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

from yarl import URL


def normalize_whitespace(value: str | None) -> str:
    """Collapse noisy whitespace and decode HTML entities."""
    if not value:
        return ""
    decoded = html_lib.unescape(value)
    return re.sub(r"\s+", " ", decoded).strip()


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
    harmless-looking encodings like ``%2C`` to ``,```, which is enough to invalidate
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


def _is_hn_internal_url(url: str) -> bool:
    """True for HN post pages (Ask HN, text posts with no external URL)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        host in {"news.ycombinator.com", "www.news.ycombinator.com"}
        and parsed.path == "/item"
    )
