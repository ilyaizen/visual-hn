from pathlib import Path

from playwright.sync_api import sync_playwright


CONTENT_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "visual-hn-previews"
    / "src"
    / "content.js"
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
