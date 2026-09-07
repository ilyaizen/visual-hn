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


def test_vhn_settings_section_stays_inside_closed_upstream_settings_content():
    """hcker.news now collapses #settings-content, not #settings-panel itself."""
    html = """
    <main>
      <div id="settings-panel" data-settings-state="closed">
        <button id="settings-toggle" class="settings-toggle" aria-expanded="false">Show</button>
        <div id="settings-sheet">
          <div id="settings-content" aria-hidden="true" inert>
            <div id="settings-tab-panels"></div>
          </div>
        </div>
      </div>
    </main>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        page.add_script_tag(
            content="""
            window.VHN = {
              findRows: () => [],
              titleAnchor: () => null,
              titleHost: () => null,
              fetchImages: async () => new Map(),
              apiOk: true,
            };
            """
        )
        page.add_script_tag(content=CONTENT_SCRIPT)
        page.wait_for_timeout(350)

        assert page.locator(
            "#settings-content > #vhn-previews-settings-section"
        ).count() == 1
        assert page.locator(
            "#settings-panel > #vhn-previews-settings-section"
        ).count() == 0
        assert page.locator("#settings-toggle").get_attribute("aria-expanded") == "false"

        browser.close()


def test_image_position_reinjects_and_repositions_hover_preview():
    html = """
    <main>
      <div id="settings-panel"><div id="settings-content"></div></div>
      <article id="story"><a href="https://news.ycombinator.com/item?id=1">Story</a></article>
    </main>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        page.add_style_tag(content=OVERLAY_STYLE)
        page.add_script_tag(
            content="""
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
        )
        page.add_script_tag(content=CONTENT_SCRIPT)
        page.wait_for_timeout(350)

        assert page.locator("#story > .vhn-thumb-wrap").evaluate(
            "(el) => el === el.parentElement.firstElementChild"
        )
        assert page.locator("#story > .vhn-thumb-wrap").evaluate(
            "(el) => el.classList.contains('vhn-pos-left')"
        )
        assert page.locator(".vhn-preview").evaluate(
            "(el) => el.getBoundingClientRect().left > el.parentElement.getBoundingClientRect().right"
        )

        page.locator('[data-vhn-position="right"]').click()
        page.wait_for_timeout(200)

        assert page.locator("#story > .vhn-thumb-wrap").evaluate(
            "(el) => el === el.parentElement.lastElementChild"
        )
        assert page.locator("#story > .vhn-thumb-wrap").evaluate(
            "(el) => el.classList.contains('vhn-pos-right')"
        )
        assert page.locator(".vhn-preview").evaluate(
            "(el) => el.getBoundingClientRect().right < el.parentElement.getBoundingClientRect().left"
        )
        assert page.locator('[data-vhn-position="right"]').get_attribute("aria-checked") == "true"
        assert page.locator('[data-vhn-position="left"]').get_attribute("aria-checked") == "false"

        browser.close()


def test_hover_preview_flips_above_viewport_bottom_and_badge_follows_right_image():
    html = """
    <main>
      <div id="settings-panel"><div id="settings-content"></div></div>
      <div style="height: 640px"></div>
      <article id="story"><a href="https://news.ycombinator.com/item?id=1">Story</a></article>
    </main>
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 900, "height": 800})
        page.set_content(html)
        page.add_style_tag(content=OVERLAY_STYLE)
        page.add_script_tag(
            content="""
            const story = document.querySelector('#story');
            const anchor = story.querySelector('a');
            window.VHN = {
              findRows: () => [{ row: story, anchor, id: '1' }],
              titleAnchor: () => anchor,
              titleHost: () => story,
              fetchImages: async () => new Map([['1', {
                image_url: 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==',
                title: 'Story', position: 4
              }]]),
              apiOk: true,
            };
            """
        )
        page.add_script_tag(content=CONTENT_SCRIPT)
        page.wait_for_timeout(350)
        page.locator('#vhn-show-hover-preview').check()
        page.mouse.move(10, 10)
        page.locator('.vhn-thumb-wrap').hover()
        page.wait_for_timeout(200)

        assert page.locator('.vhn-thumb-wrap').evaluate(
            "(el) => el.classList.contains('vhn-preview-flip-y')"
        )
        preview_rect = page.locator('.vhn-preview').evaluate(
            "(el) => ({ bottom: el.getBoundingClientRect().bottom, height: el.getBoundingClientRect().height, transform: getComputedStyle(el).transform })"
        )
        assert preview_rect['bottom'] <= 800, preview_rect

        page.locator('[data-vhn-position="right"]').click()
        page.wait_for_timeout(200)
        assert page.locator('.vhn-rank-badge').evaluate(
            "(badge) => badge.getBoundingClientRect().right <= badge.parentElement.getBoundingClientRect().right"
        )
        assert page.locator('.vhn-rank-badge').evaluate(
            "(badge) => badge.getBoundingClientRect().left > badge.parentElement.getBoundingClientRect().left"
        )

        browser.close()


def test_xs_size_and_left_hckr_metrics_alignment():
    html = """
    <main>
      <div id="settings-panel"><div id="settings-content"></div></div>
      <article id="story" class="story">
        <a class="story-metrics-link">12 pts · 4 comments</a>
        <div class="story-details">
          <div class="story-title"><a href="https://example.com">Story</a></div>
          <div class="story-meta">by test · now</div>
        </div>
      </article>
    </main>
    """
    upstream_style = """
      .story { display: flex; align-items: baseline; gap: 10px; }
      .story-details { flex: 1; min-width: 0; }
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        page.add_style_tag(content=upstream_style)
        page.add_style_tag(content=OVERLAY_STYLE)
        page.add_script_tag(
            content="""
            const story = document.querySelector('#story');
            const title = story.querySelector('.story-title a');
            window.VHN = {
              findRows: () => [{ row: story, anchor: title, id: '1' }],
              titleAnchor: () => title,
              titleHost: () => story.querySelector('.story-details'),
              fetchImages: async () => new Map([['1', {
                image_url: 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==',
                title: 'Story', position: 1
              }]]),
              apiOk: true,
            };
            """
        )
        page.add_script_tag(content=CONTENT_SCRIPT)
        page.wait_for_timeout(350)

        page.locator('#vhn-size-trigger').click()
        assert page.locator('[data-vhn-size="xxs"]').count() == 1
        page.locator('[data-vhn-size="xxs"]').click()
        page.wait_for_timeout(200)

        assert page.locator('#story .vhn-thumb-wrap').evaluate(
            "(el) => el.classList.contains('vhn-xxs')"
        )
        assert page.locator('#story .vhn-thumb-wrap').evaluate(
            "(el) => getComputedStyle(el).width"
        ) == '80px'
        assert page.locator('#story').evaluate(
            "(el) => el.classList.contains('vhn-hckr-left-preview')"
        )
        assert page.locator('.story-metrics-link').evaluate(
            "(el) => getComputedStyle(el).alignSelf"
        ) == 'flex-start'

        page.locator('[data-vhn-position="right"]').click()
        page.wait_for_timeout(200)
        assert not page.locator('#story').evaluate(
            "(el) => el.classList.contains('vhn-hckr-left-preview')"
        )
        assert page.locator('.story-metrics-link').evaluate(
            "(el) => getComputedStyle(el).alignSelf"
        ) == 'auto'

        browser.close()
