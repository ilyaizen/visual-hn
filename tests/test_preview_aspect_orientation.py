"""Aspect Ratio / Orientation split (20260908-v58).

The old single `aspectRatio` dropdown (square | portrait | landscape) became
two settings: `aspectRatio` (square | 4:3 | 16:9) and `orientation`
(square | portrait | landscape, which transposes the ratio). Defaults moved
to small (xs) / 4:3 / landscape / right / hover preview on.
"""

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

TEST_PAGE = """
<main>
  <div id="settings-panel"><div id="settings-content"></div></div>
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


def open_page(playwright, stored=None):
    """Load the test page on a real origin so the localStorage settings path
    runs (set_content() pages are opaque about:blank where storage throws)."""
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.route(
        "http://vhn.test/",
        lambda route: route.fulfill(body=TEST_PAGE, content_type="text/html"),
    )
    page.goto("http://vhn.test/")
    page.add_style_tag(content=OVERLAY_STYLE)
    if stored is not None:
        page.evaluate(
            "v => window.localStorage.setItem('vhn-preview-settings', JSON.stringify(v))",
            stored,
        )
    page.add_script_tag(content=STUB)
    page.add_script_tag(content=CONTENT_SCRIPT)
    page.wait_for_timeout(350)
    return browser, page


def thumb_aspect(page):
    return page.locator("#story .vhn-thumb").evaluate(
        "(el) => getComputedStyle(el).aspectRatio"
    )


def test_new_defaults_small_43_landscape_right_hover():
    with sync_playwright() as playwright:
        browser, page = open_page(playwright)
        wrap = page.locator("#story .vhn-thumb-wrap")
        classes = wrap.evaluate("(el) => el.className")
        assert "vhn-xs" in classes
        assert "vhn-ar-4x3" in classes
        assert "vhn-or-landscape" in classes
        assert "vhn-pos-right" in classes
        assert thumb_aspect(page) == "4 / 3"
        assert page.locator("#vhn-show-hover-preview").is_checked()
        assert page.locator('[data-vhn-ar="4:3"]').get_attribute("aria-selected") == "true"
        assert (
            page.locator('[data-vhn-or="landscape"]').get_attribute("aria-selected")
            == "true"
        )
        browser.close()


def test_orientation_transposes_chosen_ratio():
    with sync_playwright() as playwright:
        browser, page = open_page(playwright)

        # Default: 4:3 landscape
        assert thumb_aspect(page) == "4 / 3"

        # 16:9 landscape
        page.locator("#vhn-ar-trigger").click()
        page.locator('[data-vhn-ar="16:9"]').click()
        page.wait_for_timeout(200)
        assert thumb_aspect(page) == "16 / 9"

        # 16:9 portrait -> 9 / 16
        page.locator("#vhn-or-trigger").click()
        page.locator('[data-vhn-or="portrait"]').click()
        page.wait_for_timeout(200)
        assert thumb_aspect(page) == "9 / 16"

        # Square orientation wins over any ratio
        page.locator("#vhn-or-trigger").click()
        page.locator('[data-vhn-or="square"]').click()
        page.wait_for_timeout(200)
        assert thumb_aspect(page) == "1 / 1"

        # Square ratio + landscape orientation -> 1 / 1
        page.locator("#vhn-or-trigger").click()
        page.locator('[data-vhn-or="landscape"]').click()
        page.wait_for_timeout(200)
        page.locator("#vhn-ar-trigger").click()
        page.locator('[data-vhn-ar="square"]').click()
        page.wait_for_timeout(200)
        assert thumb_aspect(page) == "1 / 1"

        browser.close()


def test_dropdowns_show_all_options():
    with sync_playwright() as playwright:
        browser, page = open_page(playwright)

        page.locator("#vhn-ar-trigger").click()
        ar_options = page.locator("#vhn-ar-menu .vhn-dropdown-option").all_inner_texts()
        assert ar_options == ["Square", "4:3", "16:9"]

        # Close the first menu — it floats over the Orientation row and would
        # swallow the click on that row's trigger.
        page.locator("#vhn-ar-trigger").click()
        page.locator("#vhn-or-trigger").click()
        or_options = page.locator("#vhn-or-menu .vhn-dropdown-option").all_inner_texts()
        assert or_options == ["Square", "Portrait", "Landscape"]

        browser.close()


def test_legacy_aspect_ratio_value_migrates_to_orientation():
    """Pre-v58 stored `aspectRatio: portrait` must become orientation=portrait
    with the new default ratio, not break the thumb."""
    with sync_playwright() as playwright:
        browser, page = open_page(playwright, stored={"aspectRatio": "portrait"})
        wrap = page.locator("#story .vhn-thumb-wrap")
        classes = wrap.evaluate("(el) => el.className")
        assert "vhn-or-portrait" in classes
        assert "vhn-ar-4x3" in classes
        assert thumb_aspect(page) == "3 / 4"

        # Orientation dropdown reflects the migrated value.
        assert (
            page.locator("#vhn-or-trigger .vhn-dropdown-selected-text").inner_text()
            == "Portrait"
        )
        browser.close()


def test_legacy_square_value_migrates_to_square_orientation():
    with sync_playwright() as playwright:
        browser, page = open_page(playwright, stored={"aspectRatio": "square"})
        classes = page.locator("#story .vhn-thumb-wrap").evaluate(
            "(el) => el.className"
        )
        assert "vhn-or-square" in classes
        assert thumb_aspect(page) == "1 / 1"
        browser.close()


def test_new_format_storage_is_not_remangled():
    """A stored v58 pair (4:3 + landscape) must survive reload untouched."""
    with sync_playwright() as playwright:
        browser, page = open_page(
            playwright, stored={"aspectRatio": "4:3", "orientation": "portrait"}
        )
        classes = page.locator("#story .vhn-thumb-wrap").evaluate(
            "(el) => el.className"
        )
        assert "vhn-or-portrait" in classes
        assert "vhn-ar-4x3" in classes
        assert thumb_aspect(page) == "3 / 4"
        saved = page.evaluate(
            "JSON.parse(window.localStorage.getItem('vhn-preview-settings'))"
        )
        assert saved["aspectRatio"] == "4:3"
        assert saved["orientation"] == "portrait"
        browser.close()


def test_settings_persist_after_reload():
    with sync_playwright() as playwright:
        browser, page = open_page(playwright)
        page.locator("#vhn-ar-trigger").click()
        page.locator('[data-vhn-ar="16:9"]').click()
        page.wait_for_timeout(200)
        page.locator("#vhn-or-trigger").click()
        page.locator('[data-vhn-or="portrait"]').click()
        page.wait_for_timeout(200)

        saved = json.loads(
            page.evaluate(
                "window.localStorage.getItem('vhn-preview-settings')"
            )
        )
        assert saved["aspectRatio"] == "16:9"
        assert saved["orientation"] == "portrait"
        browser.close()
