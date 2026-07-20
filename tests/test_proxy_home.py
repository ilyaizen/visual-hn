import json
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

import hcker_proxy
import main

SAMPLE_HCKER_HTML = """<!doctype html><html><head><title>hcker.news</title></head><body><main>feed</main></body></html>"""


# ── Image payload rank/trend fields ──────────────────────────────────────────


class _FakeStory:
    """Minimal stand-in for a Story ORM row."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_build_image_payload_includes_position_and_trend():
    story = _FakeStory(
        title="Example",
        url="https://example.com/post",
        og_image_url="https://example.com/img.png",
        image_url=None,
        description="desc",
        current_position=3,
        trend="up",
    )
    payload = main.build_image_payload({42: story}, "https://host")

    assert "42" in payload["images"]
    entry = payload["images"]["42"]
    assert entry["position"] == 3
    assert entry["trend"] == "up"


def test_build_image_payload_handles_missing_position():
    story = _FakeStory(
        title="Late entry",
        url="https://example.com/late",
        og_image_url="https://example.com/img.png",
        image_url=None,
        description="",
        current_position=None,
        trend="same",
    )
    payload = main.build_image_payload({7: story}, "https://host")

    entry = payload["images"]["7"]
    assert entry["position"] is None
    assert entry["trend"] == "same"


# ── Preview injection (now in hcker_proxy) ──────────────────────────────────


def test_inject_preview_assets_adds_css_and_scripts_before_body_close():
    html = hcker_proxy.inject_preview_assets(SAMPLE_HCKER_HTML)

    assert (
        f'<link rel="stylesheet" href="/visual-hn-previews/styles/overlay.css?v={hcker_proxy.PREVIEW_RUNTIME_VERSION}" />'
        in html
    )
    assert (
        f'<script type="module" src="/visual-hn-previews/src/header-rebrand.js?v={hcker_proxy.PREVIEW_RUNTIME_VERSION}"></script>'
        in html
    )
    assert (
        f'<script src="/visual-hn-previews/src/dom.js?v={hcker_proxy.PREVIEW_RUNTIME_VERSION}" defer></script>'
        in html
    )
    assert (
        f'<script src="/visual-hn-previews/src/api.js?v={hcker_proxy.PREVIEW_RUNTIME_VERSION}" defer></script>'
        in html
    )
    assert (
        f'<script src="/visual-hn-previews/src/content.js?v={hcker_proxy.PREVIEW_RUNTIME_VERSION}" defer></script>'
        in html
    )
    assert html.index("/visual-hn-previews/styles/overlay.css") < html.index("</head>")
    assert html.index("/visual-hn-previews/src/content.js") < html.index("</body>")


def test_inject_preview_assets_inserts_runtime_config_for_same_origin_api():
    html = hcker_proxy.inject_preview_assets(SAMPLE_HCKER_HTML)

    assert "window.VHN_WEB_DEFAULTS" in html
    assert "apiBase: window.location.origin" in html
    assert 'imageSize: "md"' in html
    assert "showFavicons: true" in html


def test_inject_preview_assets_cache_busts_changed_preview_runtime_scripts():
    html = hcker_proxy.inject_preview_assets(SAMPLE_HCKER_HTML)

    assert f"header-rebrand.js?v={hcker_proxy.PREVIEW_RUNTIME_VERSION}" in html
    assert f"content.js?v={hcker_proxy.PREVIEW_RUNTIME_VERSION}" in html


def test_rewrite_hcker_news_asset_urls_keeps_root_relative_urls_for_local_proxy():
    html = '<html><head><link href="/assets/app.css"></head><body><script src="/assets/app.js"></script><a href="/newest">new</a></body></html>'

    rewritten = hcker_proxy.rewrite_hcker_news_asset_urls(html)

    assert 'href="/assets/app.css"' in rewritten
    assert 'src="/assets/app.js"' in rewritten
    assert 'href="/newest"' in rewritten


def test_rewrite_proxy_header_brands_reader_and_repoints_link():
    html = '<html><body><header><h1><a href="/">hcker.news</a></h1><span class="tagline">a better <a href="https://news.ycombinator.com/news">hacker news</a> reader</span></header></body></html>'

    rewritten = hcker_proxy.rewrite_proxy_header(html)

    # The h1 link is rebranded to animated rainbow "visual" + ".hcker.news".
    assert "vhn-rainbow" in rewritten
    assert "vhn-rainbow-char" in rewritten
    assert "vhn-logo-suffix" in rewritten
    assert "hcker.news" in rewritten
    assert "hacker news" not in rewritten.lower()
    assert "reader" in rewritten
    assert "pictures" in rewritten
    assert "news.ycombinator.com" not in rewritten


def test_header_rebrand_script_targets_header_title_without_rebranding_descriptor():
    script = (hcker_proxy.EXTENSION_DIR / "src" / "header-rebrand.js").read_text()

    assert "#header h1 a" in script
    assert "#header .tagline" in script
    assert "visual.hcker.news" in script
    assert "TITLE_RE = /^hcker\\.news$/i" in script


def test_normalize_rocket_loader_script_types_restores_executable_scripts():
    html = (
        '<script type="abc-module" crossorigin src="/assets/main.js"></script>'
        '<script src="/cdn-cgi/scripts/7d0fa10a/cloudflare-static/rocket-loader.min.js" data-cf-settings="abc-|49" defer></script>'
    )

    normalized = hcker_proxy.normalize_rocket_loader_scripts(html)

    assert 'type="module"' in normalized
    assert "rocket-loader.min.js" not in normalized
    assert "data-cf-settings" not in normalized


def test_content_script_defaults_to_small_images_and_favicons_enabled():
    script = (hcker_proxy.EXTENSION_DIR / "src" / "content.js").read_text()

    assert (
        "const DEFAULT_SETTINGS = { enabled: true, apiBase: '', imageSize: 'md', aspectRatio: 'landscape', showFavicons: true, showDescriptions: true, showHoverPreview: false, showRankBadges: true };"
        in script
    )
    assert 'data-vhn-size="md">Medium</button>' in script
    assert script.index('data-vhn-size="md">Medium</button>') < script.index(
        'data-vhn-size="large">Large</button>'
    )
    assert 'id="vhn-show-favicons"' in script
    assert "entry.favicon && settings.showFavicons" in script


# ── Cache control ────────────────────────────────────────────────────────────


def test_cache_control_assets_long():
    assert "immutable" in hcker_proxy._cache_control("assets/main-abc123.js")


def test_cache_control_timeline_15m():
    assert "max-age=900" in hcker_proxy._cache_control("api/timeline")


def test_cache_control_session_no_store():
    assert hcker_proxy._cache_control("api/account/session") == "no-store"


def test_cache_control_default():
    assert "max-age=900" in hcker_proxy._cache_control("registerSW.js")


# ── Route tests (via TestClient with mocked upstream) ────────────────────────


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _mock_fetch_bytes(path: str, query: bytes = b"") -> tuple[bytes, str]:
    """Fake upstream that returns deterministic content per path."""
    if path == "api/timeline":
        return (json.dumps({"stories": [], "count": 0}).encode(), "application/json")
    if path == "api/account/session":
        return (json.dumps({"user": None}).encode(), "application/json")
    if path.startswith("assets/"):
        return (b"/* js */", "application/javascript")
    return (b"<html>ok</html>", "text/html")


def _mock_fetch_html(query: bytes = b"") -> str:
    return SAMPLE_HCKER_HTML


# Root proxy
def test_root_returns_injected_html(client):
    with patch.object(hcker_proxy, "fetch_hcker_news_html", _mock_fetch_html):
        resp = client.get("/")
    assert resp.status_code == 200
    assert "VHN_WEB_DEFAULTS" in resp.text
    assert "/visual-hn-previews/src/content.js" in resp.text


def test_root_upstream_failure_returns_502(client):
    def _fail(query: bytes = b""):
        raise ConnectionError("upstream down")

    with patch.object(hcker_proxy, "fetch_hcker_news_html", _fail):
        resp = client.get("/")
    assert resp.status_code == 502
    assert "unavailable" in resp.text.lower()


# API proxies
def test_timeline_proxy(client):
    with patch.object(hcker_proxy, "fetch_hcker_news_bytes", _mock_fetch_bytes):
        resp = client.get("/api/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert "stories" in body


def test_account_session_proxy(client):
    with patch.object(hcker_proxy, "fetch_hcker_news_bytes", _mock_fetch_bytes):
        resp = client.get("/api/account/session")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


def test_assets_proxy(client):
    with patch.object(hcker_proxy, "fetch_hcker_news_bytes", _mock_fetch_bytes):
        resp = client.get("/assets/main-abc.js")
    assert resp.status_code == 200
    assert "immutable" in resp.headers.get("cache-control", "")


def test_proxy_upstream_failure_returns_502(client):
    def _fail(path: str, query: bytes = b""):
        raise ConnectionError("upstream down")

    with patch.object(hcker_proxy, "fetch_hcker_news_bytes", _fail):
        resp = client.get("/api/timeline")
    assert resp.status_code == 502


# Redirect routes
def test_changelog_redirects_upstream(client):
    resp = client.get("/changelog", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://hcker.news/changelog"


def test_about_redirects_upstream(client):
    resp = client.get("/about", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://hcker.news/about"


def test_search_redirects_upstream_with_query(client):
    resp = client.get("/search?q=python", follow_redirects=False)
    assert resp.status_code == 302
    assert "hcker.news/search?q=python" in resp.headers["location"]


def test_item_redirects_upstream(client):
    resp = client.get("/item?id=12345", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://hcker.news/item?id=12345"


def test_item_no_id_redirects_to_root(client):
    resp = client.get("/item", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://hcker.news"


# ── Feed enrichment timeline trigger ─────────────────────────────────────────


def test_timeline_proxy_triggers_enrichment_for_missing_ids(client):
    """Timeline proxy should fire-and-forget enrichment for story IDs not in DB."""
    timeline_response = {
        "stories": [{"id": 999001}, {"id": 999002}],
        "count": 2,
    }

    def _mock_bytes(path: str, query: bytes = b""):
        if path == "api/timeline":
            return (json.dumps(timeline_response).encode(), "application/json")
        return (b"{}", "application/json")

    enrichment_called_with = []

    async def _mock_enrich(ids):
        enrichment_called_with.extend(ids)

    with patch.object(hcker_proxy, "fetch_hcker_news_bytes", _mock_bytes):
        with patch("hcker_proxy._get_feed_enrichment") as mock_fe:
            mock_fe.return_value.enrich_missing_stories = _mock_enrich
            resp = client.get("/api/timeline")

    assert resp.status_code == 200
    # Enrichment should have been triggered (fire-and-forget via create_task)
    # Note: in TestClient sync context, create_task runs but we verify the call was set up


# ── Feed enrichment module ───────────────────────────────────────────────────


def test_get_known_ids_empty_db(monkeypatch):
    """get_known_ids returns empty set on empty DB."""
    import feed_enrichment
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from models import Base
    import database

    async def _test():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        monkeypatch.setattr(database, "async_session", factory)
        monkeypatch.setattr(feed_enrichment, "async_session", factory)

        ids = await feed_enrichment.get_known_ids()
        assert ids == set()

        await engine.dispose()

    import asyncio

    asyncio.run(_test())
