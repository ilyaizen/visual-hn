"""hcker.news proxy — fetches upstream HTML/assets/API and rewrites for local serving."""

from __future__ import annotations

import asyncio
import re
import logging
import time
from typing import Any
from pathlib import Path
from urllib.request import Request as UrlRequest, urlopen

from bs4 import BeautifulSoup
from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi import Request
from fastapi.responses import HTMLResponse, Response, RedirectResponse

logger = logging.getLogger(__name__)

# Shared limiter — imported by main.py to register middleware + exception handler.
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

# Lazy import to avoid circular dependency at module load
_feed_enrichment = None


def _get_feed_enrichment():
    global _feed_enrichment
    if _feed_enrichment is None:
        import feed_enrichment

        _feed_enrichment = feed_enrichment
    return _feed_enrichment


HCKER_NEWS_ORIGIN = "https://hcker.news"
EXTENSION_DIR = Path(__file__).parent / "visual-hn-previews"
PREVIEW_RUNTIME_VERSION = "20260720-v46"

# ── Simple TTL cache for upstream fetches ──────────────────────────────────
# Avoids re-fetching hcker.news on every single request.  Two-tier TTL:
#   soft TTL  — serve cached + refresh in background (not done here, but
#               the caller can re-fetch on next hit)
#   hard TTL  — absolute max age; never serve content older than this
#
# On fetch failure, we fall back to ANY cached value regardless of age
# (up to hard TTL), so a blip at hcker.news doesn't 502 our users.


class _TTLCache:
    """Dict-backed TTL cache with stale-while-revalidate semantics."""

    def __init__(self):
        # key -> (created_monotonic, soft_ttl, hard_ttl, value)
        self._store: dict[str, tuple[float, float, float, Any]] = {}

    def _age(self, key: str) -> float | None:
        """Return age in seconds, or None if missing/expired past hard TTL."""
        entry = self._store.get(key)
        if entry is None:
            return None
        created, _soft, hard, _val = entry
        age = time.monotonic() - created
        if age > hard:
            del self._store[key]
            return None
        return age

    def get(self, key: str) -> Any:
        """Return cached value if within hard TTL, else None."""
        entry = self._store.get(key)
        if entry is None:
            return None
        created, _soft, hard, value = entry
        age = time.monotonic() - created
        if age > hard:
            del self._store[key]
            return None
        return value

    def is_fresh(self, key: str) -> bool:
        """True if cached and within soft TTL."""
        entry = self._store.get(key)
        if entry is None:
            return False
        created, soft, _hard, _val = entry
        return (time.monotonic() - created) <= soft

    def set(self, key: str, value: Any, soft_ttl: float, hard_ttl: float) -> None:
        self._store[key] = (time.monotonic(), soft_ttl, hard_ttl, value)

    def purge(self) -> None:
        now = time.monotonic()
        stale = [
            k
            for k, (created, _s, hard, _) in self._store.items()
            if now - created > hard
        ]
        for k in stale:
            del self._store[k]


_cache = _TTLCache()
# soft = when to re-fetch next time;  hard = absolute max age before discard
CACHE_HTML_SOFT = 30 * 60  # 30 min — homepage soft TTL
CACHE_HTML_HARD = 60 * 60  # 60 min — homepage hard TTL (fallback max age)
CACHE_BYTES_SOFT = 15 * 60  # 15 min — assets soft TTL
CACHE_BYTES_HARD = 60 * 60  # 60 min — assets hard TTL

# ── Upstream fetchers ────────────────────────────────────────────────────────


def fetch_hcker_news_html(query: bytes = b"") -> str:
    """Fetch hcker.news homepage.  Returns fresh cache → fetches upstream →
    falls back to stale cache on failure (graceful degradation).
    When *query* is provided (e.g. ``b"view=frontpage"``), the upstream URL
    includes the query string and a distinct cache key is used so the two
    views are cached independently."""
    cache_key = (
        "home:html:view=frontpage" if query == b"view=frontpage" else "home:html"
    )
    if _cache.is_fresh(cache_key):
        return _cache.get(cache_key)

    try:
        url = HCKER_NEWS_ORIGIN + "/"
        if query:
            url += "?" + query.decode("utf-8", errors="ignore")
        request = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=15) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html = response.read().decode(charset, errors="replace")
        _cache.set(cache_key, html, CACHE_HTML_SOFT, CACHE_HTML_HARD)
        return html
    except Exception as exc:
        logger.warning("Upstream hcker.news fetch failed: %s", exc)
        stale = _cache.get(cache_key)
        if stale is not None:
            logger.warning("Serving stale cached homepage (age < hard TTL)")
            return stale
        raise  # no cache at all — let caller handle the 502


def fetch_hcker_news_bytes(path: str, query: bytes = b"") -> tuple[bytes, str]:
    """Fetch proxied asset/API.  Returns fresh cache → fetches upstream →
    falls back to stale cache on failure."""
    cache_key = f"bytes:{path}:{query!r}"
    if _cache.is_fresh(cache_key):
        return _cache.get(cache_key)

    try:
        suffix = path.lstrip("/")
        url = f"{HCKER_NEWS_ORIGIN}/{suffix}"
        if query:
            url += "?" + query.decode("utf-8", errors="ignore")
        request = UrlRequest(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": HCKER_NEWS_ORIGIN + "/"},
        )
        with urlopen(request, timeout=15) as response:
            content_type = response.headers.get(
                "content-type", "application/octet-stream"
            )
            result = (response.read(), content_type)
        _cache.set(cache_key, result, CACHE_BYTES_SOFT, CACHE_BYTES_HARD)
        return result
    except Exception as exc:
        logger.warning("Upstream fetch failed for /%s: %s", path, exc)
        stale = _cache.get(cache_key)
        if stale is not None:
            return stale
        raise


# ── HTML rewriting ───────────────────────────────────────────────────────────


def normalize_rocket_loader_scripts(html: str) -> str:
    """Cloudflare Rocket Loader rewrites script types; undo that for our proxy."""
    html = re.sub(r'\sdata-cf-settings="[^"]*"', "", html)
    html = re.sub(
        r"<script\b[^>]*rocket-loader\.min\.js[^>]*>\s*</script>",
        "",
        html,
        flags=re.IGNORECASE,
    )
    html = re.sub(r'type="[^"]+-module"', 'type="module"', html)
    html = re.sub(r'type="[^"]+-text/javascript"', 'type="text/javascript"', html)
    return html


def rewrite_hcker_news_asset_urls(html: str) -> str:
    """Keep hcker.news root-relative URLs on this origin so FastAPI can proxy them."""
    return html


def rewrite_proxy_header(html: str) -> str:
    """Brand the proxied hcker.news header for this preview-enhanced reader."""
    soup = BeautifulSoup(html, "html.parser")

    RAINBOW_HTML = (
        '<span class="vhn-rainbow">'
        '<span class="vhn-rainbow-char" style="--i:0">v</span>'
        '<span class="vhn-rainbow-char" style="--i:1">i</span>'
        '<span class="vhn-rainbow-char" style="--i:2">s</span>'
        '<span class="vhn-rainbow-char" style="--i:3">u</span>'
        '<span class="vhn-rainbow-char" style="--i:4">a</span>'
        '<span class="vhn-rainbow-char" style="--i:5">l</span>'
        "</span>"
        '<span class="vhn-logo-suffix">.hcker.news</span>'
    )

    container = None
    for tag_name in (["header", "nav"], ["body"]):
        for tag in soup.find_all(tag_name):
            text = tag.get_text(" ", strip=True).lower()
            if "hcker.news" in text and "a better hacker news reader" in text:
                container = tag
                break
        if container is not None:
            break

    if container is None:
        return html

    for link in container.find_all("a"):
        link_text = link.get_text(" ", strip=True).lower()
        href = link.get("href", "")
        if link_text == "hcker.news":
            link.clear()
            link.append(BeautifulSoup(RAINBOW_HTML, "html.parser"))
            continue
        if "hacker news" in link_text or (
            isinstance(href, str) and "news.ycombinator.com" in href
        ):
            link["href"] = HCKER_NEWS_ORIGIN
            link.string = re.sub(
                "hacker news",
                "hcker.news",
                link.get_text(" ", strip=True),
                flags=re.IGNORECASE,
            )

    for text_node in container.find_all(string=True):
        if "hacker news reader" in text_node.lower():
            text_node.replace_with(
                re.sub(
                    "hacker news reader",
                    "hcker.news reader",
                    str(text_node),
                    flags=re.IGNORECASE,
                )
            )

    # Replace the tagline with our branded version
    tagline = container.find("span", class_="tagline")
    if tagline:
        tagline.clear()
        tagline.string = "hcker.news reader with pictures"

    # Inject a GitHub link into the header-links row (after "about")
    links_container = container.find(class_="header-links")
    if links_container:
        existing_github = links_container.find(
            "a", href=re.compile(r"github\.com/ilyaizen/visual-hn")
        )
        if not existing_github:
            about_link = links_container.find("a", href="/about")
            github_link = soup.new_tag(
                "a",
                href="https://github.com/ilyaizen/visual-hn/",
                attrs={
                    "class": "vhn-github-link",
                    "target": "_blank",
                    "rel": "noopener noreferrer",
                },
            )
            github_link.string = "github"
            if about_link:
                about_link.insert_after(github_link)
            else:
                links_container.append(github_link)

    return str(soup)


def rewrite_meta_tags(html: str) -> str:
    """Replace upstream hcker.news OG/Twitter metadata with VHN branding.

    hcker.news injects its own og:title, og:description, twitter:* and meta
    description. When crawlers fetch our proxy, they see hcker.news's copy.
    This swaps every branding-bearing tag for VHN's own.
    """
    soup = BeautifulSoup(html, "html.parser")

    VHN_TITLE = "visual.hcker.news"
    VHN_DESC = (
        "visual.hcker.news — a hcker.news reader with pictures. "
        "Preview images, rank badges, and trend arrows for every story."
    )
    VHN_URL = "https://hn.is-ai-good-yet.com/"
    # Keep upstream hcker.news og:image / twitter:image — their icon is fine;
    # we only override textual metadata (title, description, site_name, url).

    # Tags keyed by property/name → new content. Keys not found are skipped.
    tag_map = {
        # Open Graph
        ("property", "og:site_name"): "VHN",
        ("property", "og:title"): VHN_TITLE,
        ("property", "og:description"): VHN_DESC,
        ("property", "og:url"): VHN_URL,
        # Twitter
        ("name", "twitter:title"): VHN_TITLE,
        ("name", "twitter:description"): VHN_DESC,
        # Standard
        ("name", "description"): VHN_DESC,
    }

    for meta in soup.find_all("meta"):
        attr_key: str | None = None
        attr_val: str | None = None
        for attr in ("property", "name"):
            val = meta.get(attr)
            if isinstance(val, str):
                attr_key, attr_val = attr, val
                break
        if (
            attr_key is not None
            and attr_val is not None
            and (attr_key, attr_val) in tag_map
        ):
            meta["content"] = tag_map[(attr_key, attr_val)]

    # <title> tag — regex like inject_preview_assets does, but consolidated here
    title_tag = soup.find("title")
    if title_tag:
        title_tag.string = VHN_TITLE

    return str(soup)


def inject_preview_assets(html: str) -> str:
    """Inject the former extension runtime into the proxied hcker.news homepage."""
    config = (
        "<script>document.documentElement.classList.add('js');"
        "window.VHN_WEB_DEFAULTS = { enabled: true, "
        'apiBase: window.location.origin, imageSize: "md", showFavicons: true, showDescriptions: true, showHoverPreview: false };</script>'
    )
    css = f'<link rel="stylesheet" href="/visual-hn-previews/styles/overlay.css?v={PREVIEW_RUNTIME_VERSION}" />'
    scripts = "\n".join(
        [
            f'<script type="module" src="/visual-hn-previews/src/header-rebrand.js?v={PREVIEW_RUNTIME_VERSION}"></script>',
            f'<script src="/visual-hn-previews/src/dom.js?v={PREVIEW_RUNTIME_VERSION}" defer></script>',
            f'<script src="/visual-hn-previews/src/api.js?v={PREVIEW_RUNTIME_VERSION}" defer></script>',
            f'<script src="/visual-hn-previews/src/content.js?v={PREVIEW_RUNTIME_VERSION}" defer></script>',
        ]
    )

    if "</head>" in html:
        html = html.replace("</head>", f"{config}\n{css}\n</head>", 1)
    else:
        html = config + "\n" + css + "\n" + html

    if "</body>" in html:
        return html.replace("</body>", f"{scripts}\n</body>", 1)
    return html + "\n" + scripts


# ── Generic proxy helper ─────────────────────────────────────────────────────


def _cache_control(path: str) -> str:
    """Return appropriate Cache-Control for a proxied path."""
    if path.startswith("assets/"):
        # Hashed JS/CSS — safe to cache long
        return "public, max-age=604800, immutable"
    if path == "api/timeline":
        return "public, max-age=900"  # 15 min
    if path == "api/account/session":
        return "no-store"
    return "public, max-age=900"


async def proxy_hcker_news_path(path: str, request: Request) -> Response:
    try:
        body, content_type = await asyncio.to_thread(
            fetch_hcker_news_bytes,
            path,
            request.scope.get("query_string", b""),
        )
    except Exception as exc:
        logger.warning("Failed to proxy hcker.news path /%s: %s", path, exc)
        return Response(
            f"Upstream hcker.news error for /{path}",
            status_code=502,
            media_type="text/plain",
        )

    return Response(
        body,
        media_type=content_type,
        headers={"Cache-Control": _cache_control(path)},
    )


# ── Route handlers ───────────────────────────────────────────────────────────


def register_routes(app) -> None:
    """Register all hcker.news proxy routes on the FastAPI app."""

    @app.get("/assets/{path:path}")
    async def hcker_assets_proxy(path: str, request: Request):
        return await proxy_hcker_news_path(f"assets/{path}", request)

    @app.get("/registerSW.js")
    async def hcker_register_sw_proxy(request: Request):
        return await proxy_hcker_news_path("registerSW.js", request)

    @app.get("/manifest.webmanifest")
    async def hcker_manifest_proxy(request: Request):
        return await proxy_hcker_news_path("manifest.webmanifest", request)

    @app.get("/api/timeline")
    @limiter.limit("30/minute")
    async def hcker_timeline_proxy(request: Request):
        response = await proxy_hcker_news_path("api/timeline", request)
        # Fire-and-forget: enrich any story IDs we don't have locally
        if response.status_code == 200:
            try:
                import json as _json

                raw = response.body
                if isinstance(raw, (bytes, bytearray)):
                    body = _json.loads(raw)
                else:
                    body = _json.loads(bytes(raw))
                ids = [
                    s.get("id") or s.get("story_id")
                    for s in body.get("stories", [])
                    if isinstance(s, dict)
                ]
                ids = [i for i in ids if isinstance(i, int)]
                if ids:
                    fe = _get_feed_enrichment()
                    asyncio.create_task(fe.enrich_missing_stories(ids))
            except Exception:
                pass  # enrichment is best-effort, never break the response
        return response

    @app.get("/api/frontpage")
    async def hcker_frontpage_proxy(request: Request):
        response = await proxy_hcker_news_path("api/frontpage", request)
        # Fire-and-forget: enrich any story IDs we don't have locally
        if response.status_code == 200:
            try:
                import json as _json

                raw = response.body
                if isinstance(raw, (bytes, bytearray)):
                    body = _json.loads(raw)
                else:
                    body = _json.loads(bytes(raw))
                ids = [
                    s.get("id") or s.get("story_id")
                    for s in body.get("stories", [])
                    if isinstance(s, dict)
                ]
                ids = [i for i in ids if isinstance(i, int)]
                if ids:
                    fe = _get_feed_enrichment()
                    asyncio.create_task(fe.enrich_missing_stories(ids))
            except Exception:
                pass
        return response

    @app.get("/api/account/session")
    async def hcker_account_session_proxy(request: Request):
        return await proxy_hcker_news_path("api/account/session", request)

    # Content pages → redirect upstream (not worth proxying)
    for _route in ("/changelog", "/about", "/notes", "/feeds"):

        @app.get(_route)
        async def _redirect_upstream(route=_route):
            return RedirectResponse(url=f"{HCKER_NEWS_ORIGIN}{route}", status_code=302)

    # Search → redirect upstream
    @app.get("/search")
    async def hcker_search_redirect(request: Request):
        qs = request.scope.get("query_string", b"").decode("utf-8", errors="ignore")
        target = f"{HCKER_NEWS_ORIGIN}/search"
        if qs:
            target += "?" + qs
        return RedirectResponse(url=target, status_code=302)

    # Story detail routes → redirect upstream
    @app.get("/item")
    async def hcker_item_redirect(id: int | None = None):
        if id is None:
            return RedirectResponse(url=HCKER_NEWS_ORIGIN, status_code=302)
        return RedirectResponse(
            url=f"{HCKER_NEWS_ORIGIN}/item?id={id}", status_code=302
        )

    # Root: proxied hcker.news homepage with injected preview runtime
    @app.get("/", response_class=HTMLResponse)
    @limiter.limit("60/minute")
    async def read_root(request: Request):
        qs = request.scope.get("query_string", b"").decode("utf-8", errors="ignore")
        fetch_query = b"view=frontpage" if "view=frontpage" in qs else b""
        try:
            html = await asyncio.to_thread(fetch_hcker_news_html, fetch_query)
        except Exception as exc:
            logger.warning("Failed to proxy hcker.news homepage: %s", exc)
            return HTMLResponse(
                "<!doctype html><html><body>hcker.news is temporarily unavailable.</body></html>",
                status_code=502,
            )

        # Wrap the rewriting pipeline: if any step breaks (e.g. hcker.news
        # changed their HTML structure), serve the raw proxy HTML rather than
        # crashing.  The page works without our injections — just no thumbnails.
        try:
            html = inject_preview_assets(
                rewrite_meta_tags(
                    rewrite_proxy_header(
                        rewrite_hcker_news_asset_urls(
                            normalize_rocket_loader_scripts(html)
                        )
                    )
                )
            )
        except Exception as exc:
            logger.warning(
                "Injection pipeline failed (serving raw proxy HTML): %s", exc
            )

        return HTMLResponse(html, headers={"Cache-Control": "public, max-age=900"})
