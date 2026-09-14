"""Admin page: title/branding, favicon links, and thumbnail lightbox clicks.

Regression coverage for the "some thumbnails won't click" bug: the grid used
inline onclick="openLightbox('...')" built with esc(), which escapes double
quotes but not single quotes — any story title containing an apostrophe
produced invalid JS and a dead click handler. Thumbnails now carry data-*
attributes (escaped with escAttr) and one delegated click listener opens the
lightbox.
"""

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

import main

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "admin.html").read_text()
SCRIPT = TEMPLATE.split("<script>")[1].split("</script>")[0]

# 1x1 transparent PNG
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VHN_ADMIN_PASSWORD", "test-password")
    main._admin_sessions.clear()
    with TestClient(main.app) as c:
        yield c
    main._admin_sessions.clear()


def _login(client):
    resp = client.post("/admin/login", data={"password": "test-password"})
    assert resp.status_code == 200


# ── Title & favicon ──────────────────────────────────────────────────────────


def test_admin_title_is_visual_hn_admin():
    assert "<title>visual-hn admin</title>" in TEMPLATE
    assert "visual.hcker.news" not in TEMPLATE


def test_admin_page_has_favicon_links():
    assert 'href="/static/favicon.ico"' in TEMPLATE
    assert 'href="/static/favicon-32x32.png"' in TEMPLATE
    assert 'href="/static/favicon-16x16.png"' in TEMPLATE
    assert 'href="/static/apple-touch-icon.png"' in TEMPLATE


def test_admin_page_serves_favicon_and_title(client):
    _login(client)
    resp = client.get("/admin")
    assert resp.status_code == 200
    body = resp.text
    assert "<title>visual-hn admin</title>" in body
    assert "/static/favicon.ico" in body
    assert client.get("/static/favicon.ico").status_code == 200
    assert client.get("/static/favicon-32x32.png").status_code == 200


# ── Thumbnail click → lightbox ───────────────────────────────────────────────


def test_thumbnails_use_data_attributes_not_inline_onclick():
    assert 'onclick="openLightbox' not in TEMPLATE
    assert "data-image" in TEMPLATE
    assert "escAttr" in TEMPLATE


def test_screenshot_api_shape_unauthorized(client):
    assert client.get("/admin/api/recent-screenshots").status_code in (401, 404)


STORY = {
    "image": "/static/screenshot.png",
    "title": "It's a test with 'single quotes'",
    "url": "https://example.com/a'b?x=1",
    "domain": "example.com",
    "missing": False,
    "bytes": 999999,
    "has_og": True,
    "position": 2,
    "score": 100,
    "comments": 50,
    "hn_id": 123,
    "captured_at": 1757700000,
    "posted_at": 1757600000,
}


def test_thumb_with_apostrophe_title_opens_lightbox():
    """The actual bug: an apostrophe in the title used to kill the onclick."""

    def handle_api(route):
        path = route.request.url
        if "recent-screenshots" in path:
            route.fulfill(json={"screenshots": [STORY]})
        else:
            route.fulfill(json={})

    def handle_img(route):
        route.fulfill(body=PNG_1PX, content_type="image/png")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "http://vhn.test/admin",
            lambda route: route.fulfill(body=TEMPLATE, content_type="text/html"),
        )
        page.route("**/admin/api/**", handle_api)
        page.route("**/static/**", handle_img)
        page.goto("http://vhn.test/admin")
        page.add_script_tag(content=SCRIPT)
        page.wait_for_selector(".ss-thumb")

        thumb = page.locator(".ss-thumb").first
        # attribute round-trips the apostrophe intact
        assert thumb.get_attribute("data-title") == STORY["title"]

        thumb.click()
        page.wait_for_selector(".vhn-modal.vhn-open")

        assert page.locator(".vhn-modal-title").text_content() == STORY["title"]
        assert page.locator(".vhn-modal-title").get_attribute("href") == STORY["url"]
        browser.close()
