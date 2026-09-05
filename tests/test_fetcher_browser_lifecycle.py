"""Regression tests: residential fetcher browser lifecycle.

Covers the driver-node leak fixed on 2026-09-05: a failed browser launch
leaves _playwright set while _browser stays None, and the old teardown gate
(`if _browser:`) skipped cleanup — leaking one ~30 MB playwright driver
node per failed relaunch.
"""

from types import SimpleNamespace

import pytest

import residential_fetcher as rf


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Never leak fake browser state into other tests."""
    yield
    rf._browser = None
    rf._playwright = None


class FakeContext:
    """Minimal stand-in for a playwright BrowserContext."""

    def __init__(self, connected: bool = True):
        self.closed = False
        self.browser = SimpleNamespace(is_connected=lambda: connected)

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext):
        self._context = context
        self.launch_calls = 0

    async def launch_persistent_context(self, **kwargs):
        self.launch_calls += 1
        return self._context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium):
        self.stopped = False
        self.chromium = chromium

    async def stop(self):
        self.stopped = True


class FakeAsyncPlaywright:
    """Stand-in for async_playwright(): .start() returns the instance."""

    def __init__(self, playwright: FakePlaywright):
        self._playwright = playwright

    async def start(self):
        return self._playwright


async def test_ensure_browser_tears_down_stale_playwright_without_browser(
    monkeypatch,
):
    """The leak: _browser=None + _playwright=set (failed launch) must trigger
    teardown before relaunch, else the old driver node is orphaned."""
    stale_playwright = FakePlaywright(FakeChromium(FakeContext()))
    teardown_calls = []

    async def fake_teardown():
        teardown_calls.append(True)

    fresh_context = FakeContext(connected=True)
    fresh_chromium = FakeChromium(fresh_context)
    fresh_playwright = FakePlaywright(fresh_chromium)

    monkeypatch.setattr(rf, "_browser", None)
    monkeypatch.setattr(rf, "_playwright", stale_playwright)
    monkeypatch.setattr(rf, "_teardown_browser", fake_teardown)
    monkeypatch.setattr(
        rf, "async_playwright", lambda: FakeAsyncPlaywright(fresh_playwright)
    )

    context = await rf._ensure_browser()

    assert teardown_calls == [True], "stale _playwright alone must trigger teardown"
    assert context is fresh_context
    assert fresh_chromium.launch_calls == 1


async def test_teardown_browser_stops_playwright_even_without_browser():
    """Teardown must stop playwright and reset globals even when the browser
    context is already gone."""
    context = FakeContext()
    playwright = FakePlaywright(FakeChromium(context))

    rf._browser = context
    rf._playwright = playwright
    await rf._teardown_browser()
    assert context.closed
    assert playwright.stopped
    assert rf._browser is None
    assert rf._playwright is None

    # Browser already None (failed launch) — playwright must still be stopped.
    leftover = FakePlaywright(FakeChromium(FakeContext()))
    rf._browser = None
    rf._playwright = leftover
    await rf._teardown_browser()
    assert leftover.stopped
    assert rf._playwright is None
