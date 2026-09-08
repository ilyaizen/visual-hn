import json
from pathlib import Path

from playwright.sync_api import sync_playwright


CONTENT_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "visual-hn-previews"
    / "src"
    / "content.js"
).read_text()
OVERLAY_STYLE = (
    Path(__file__).resolve().parents[1]
    / "visual-hn-previews"
    / "styles"
    / "overlay.css"
).read_text()

UPSTREAM_STYLE = """
#header { position: sticky; top: 0; height: 40px; background: #eee; }
.settings-panel { position: sticky; top: 45px; height: 30px; background: #ddd; }
.feed-header { position: sticky; top: 75px; height: 24px; background: #ccc; }
"""

TEST_PAGE = """
<header id="header"></header>
<main>
  <div class="settings-panel"><div id="settings-content"></div></div>
  <div id="settings-sticky-trigger"></div>
  <div class="feed-header"></div>
  <article id="story"><a href="https://news.ycombinator.com/item?id=1">Story</a></article>
</main>
"""

STUB = """
const story = document.querySelector('#story');
const anchor = story.querySelector('a');
window.VHN = {
  findRows: () => [{ row: story, anchor, id: '1' }],
  titleAnchor: () => anchor,
  titleHost: () => story,
  fetchImages: async () => new Map([['1', {
    image_url: 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==',
    title: 'Story', position: 1
  }]]),
  apiOk: true,
};
"""


def open_page(playwright):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content(TEST_PAGE)
    page.add_style_tag(content=UPSTREAM_STYLE)
    page.add_style_tag(content=OVERLAY_STYLE)
    page.add_script_tag(content=STUB)
    page.add_script_tag(content=CONTENT_SCRIPT)
    page.wait_for_timeout(350)
    return browser, page


def test_sticky_header_toggle_unpins_and_restores():
    with sync_playwright() as playwright:
        browser, page = open_page(playwright)

        # Default ON: hcker.news's own sticky positioning is untouched.
        assert page.locator("#vhn-sticky-header").is_checked()
        assert (
            page.evaluate(
                "getComputedStyle(document.querySelector('#header')).position"
            )
            == "sticky"
        )

        # Toggle OFF: the whole sticky stack (header, settings bar, feed
        # headers) unpins and scrolls away with the page.
        page.locator("#vhn-sticky-header").uncheck()
        page.wait_for_timeout(150)
        for selector in ("#header", ".settings-panel", ".feed-header"):
            assert (
                page.evaluate(
                    "getComputedStyle(document.querySelector(%s)).position" % json.dumps(selector)
                )
                == "static"
            ), selector
        assert page.evaluate("document.documentElement.classList.contains('vhn-header-static')")

        # Toggle back ON: everything pinned again, override class removed.
        page.locator("#vhn-sticky-header").check()
        page.wait_for_timeout(150)
        for selector in ("#header", ".settings-panel", ".feed-header"):
            assert (
                page.evaluate(
                    "getComputedStyle(document.querySelector(%s)).position" % json.dumps(selector)
                )
                == "sticky"
            ), selector
        assert not page.evaluate("document.documentElement.classList.contains('vhn-header-static')")

        browser.close()


def test_static_header_default_when_setting_disabled():
    """Extension mode starts from stored prefs; static comes from storage."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        # set_content() pages are opaque about:blank origins where localStorage
        # throws; route a real origin so the localStorage settings path runs.
        page.route(
            "http://vhn.test/",
            lambda route: route.fulfill(body=TEST_PAGE, content_type="text/html"),
        )
        page.goto("http://vhn.test/")
        page.add_style_tag(content=UPSTREAM_STYLE)
        page.add_style_tag(content=OVERLAY_STYLE)
        page.evaluate(
            "window.localStorage.setItem('vhn-preview-settings', JSON.stringify({ stickyHeader: false }))"
        )
        page.add_script_tag(content=STUB)
        page.add_script_tag(content=CONTENT_SCRIPT)
        page.wait_for_timeout(350)

        assert not page.locator("#vhn-sticky-header").is_checked()
        assert (
            page.evaluate(
                "getComputedStyle(document.querySelector('#header')).position"
            )
            == "static"
        )
        browser.close()
